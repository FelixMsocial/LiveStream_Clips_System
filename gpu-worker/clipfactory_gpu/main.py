"""Main loop — pulls from CLIP_EDIT queue and runs the pipeline serially.

Concurrency = 1 (can lift later). Crash-safe: unacked messages reappear when
the visibility window expires, and D1 holds last-known status.
"""
from __future__ import annotations

import logging
import signal
import time
from pathlib import Path
from typing import Any

from .config import Config, load_config
from .d1_api import D1Api
from .pipeline import run_pipeline
from .queue_pull import PulledMessage, QueuePullClient
from .r2_client import R2Client

log = logging.getLogger(__name__)

IDLE_SLEEP_SEC = 5.0
HEARTBEAT_INTERVAL_SEC = 60.0
PROMPT_REFRESH_SEC = 300.0  # Re-fetch prompts from D1 every 5 minutes
ALERT_COOLDOWN_SEC = 900.0


class Daemon:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.q = QueuePullClient(
            account_id=cfg.cf_account_id,
            queue_id=cfg.cf_clip_edit_queue_id,
            token=cfg.cf_queues_pull_token,
        )
        self.r2 = R2Client(
            endpoint=cfg.r2_endpoint,
            access_key_id=cfg.r2_access_key_id,
            secret_access_key=cfg.r2_secret_access_key,
            bucket=cfg.r2_bucket,
        )
        self.d1 = D1Api(
            cfg.gpu_d1_api_url,
            cfg.gpu_internal_secret,
            worker_id=cfg.gpu_worker_id,
        )
        self._prompts_cache: dict[str, dict[str, str]] = {}  # tag → {key: body}
        self._running = True
        self._last_heartbeat = 0.0
        self._last_prompt_fetch: dict[str, float] = {}  # tag → monotonic timestamp
        self._heartbeat_failures = 0
        self._pull_failures = 0
        self._last_worker_alert: dict[str, float] = {}
        Path(cfg.work_dir).mkdir(parents=True, exist_ok=True)

    def stop(self, *_a: Any) -> None:
        log.info("shutdown requested")
        self._running = False

    def _beat(self) -> None:
        if time.monotonic() - self._last_heartbeat < HEARTBEAT_INTERVAL_SEC:
            return
        ok = self.d1.heartbeat(self.cfg.gpu_heartbeat_url)
        if ok:
            self._heartbeat_failures = 0
        else:
            self._heartbeat_failures += 1
            if self._heartbeat_failures >= 3:
                self._notify_worker_issue(
                    "gpu_heartbeat_failure",
                    (
                        f"GPU heartbeat failed {self._heartbeat_failures} consecutive times. "
                        f"worker_id={self.cfg.gpu_worker_id}"
                    ),
                )
        self._last_heartbeat = time.monotonic()

    def _notify_worker_issue(self, alert_type: str, message: str) -> None:
        now = time.monotonic()
        last = self._last_worker_alert.get(alert_type, 0.0)
        if now - last < ALERT_COOLDOWN_SEC:
            return
        self.d1.send_alert(alert_type=alert_type, message=message)
        self._last_worker_alert[alert_type] = now

    def _get_prompts(self, tag: str) -> dict[str, str]:
        """Return prompts for `tag`, fetching from D1 if stale or missing.

        Falls back to prompts_fallback.PROMPTS[tag] (then 'gameplay') on API failure.
        """
        from . import prompts_fallback

        now = time.monotonic()
        last = self._last_prompt_fetch.get(tag, 0.0)
        cached = self._prompts_cache.get(tag)
        if cached and now - last < PROMPT_REFRESH_SEC:
            return cached

        fetched = self.d1.fetch_prompts(tag=tag)
        if fetched:
            log.info("loaded %d prompts from D1 API (tag=%s)", len(fetched), tag)
            self._prompts_cache[tag] = fetched
            self._last_prompt_fetch[tag] = now
            return fetched

        # API unavailable — use fallback module, then 'gameplay' if tag missing.
        if cached:
            log.warning(
                "prompt fetch failed (tag=%s), retaining stale cache (retry in %ds)",
                tag, int(PROMPT_REFRESH_SEC),
            )
            self._last_prompt_fetch[tag] = now
            return cached

        fallback = (
            prompts_fallback.PROMPTS.get(tag)
            or prompts_fallback.PROMPTS["gameplay"]
        )
        log.info("using fallback prompts (tag=%s, D1 API unavailable)", tag)
        self._prompts_cache[tag] = fallback
        self._last_prompt_fetch[tag] = now
        return fallback

    def _preload_prompts(self) -> None:
        """Pre-warm the gameplay prompt cache at startup."""
        self._get_prompts("gameplay")

    def run(self) -> None:
        self._preload_prompts()
        self._beat()
        while self._running:
            try:
                msgs = self.q.pull(batch_size=1, visibility_ms=15 * 60 * 1000)
            except Exception as e:  # noqa: BLE001
                log.warning("pull failed: %s", e)
                self._pull_failures += 1
                if self._pull_failures >= 3:
                    self._notify_worker_issue(
                        "queue_pull_failure",
                        (
                            "Cloudflare queue pull failed 3+ consecutive times. "
                            f"worker_id={self.cfg.gpu_worker_id}. Last error: {e}"
                        ),
                    )
                time.sleep(IDLE_SLEEP_SEC)
                self._beat()
                continue
            self._pull_failures = 0

            if not msgs:
                self._beat()
                self._preload_prompts()  # keep gameplay cache warm during idle
                time.sleep(IDLE_SLEEP_SEC)
                continue

            for m in msgs:
                self._handle(m)
            self._beat()

    def _handle(self, m: PulledMessage) -> None:
        body = m.body
        clip_id = body.get("clip_id")
        raw_key = body.get("raw_clip_r2_key")
        if not clip_id or not raw_key:
            log.error("bad payload, acking to skip: %s", body)
            self.d1.send_alert(
                alert_type="bad_queue_payload",
                message=f"GPU worker received malformed CLIP_EDIT message: {body}",
            )
            self.q.ack([m.lease_id])
            return
        try:
            content_tag = body.get("content_tag") or "gameplay"
            prompts = self._get_prompts(content_tag)
            run_pipeline(
                cfg=self.cfg,
                r2=self.r2,
                d1=self.d1,
                prompts=prompts,
                clip_id=clip_id,
                raw_clip_r2_key=raw_key,
                stream_session_id=body.get("stream_session_id"),
            )
            self.q.ack([m.lease_id])
        except Exception as e:  # noqa: BLE001
            log.exception("pipeline failed, retrying queue msg: %s", e)
            if m.attempts >= 3:
                self.d1.send_alert(
                    clip_id=clip_id,
                    alert_type="gpu_queue_retry_exhausted",
                    message=(
                        f"Clip edit job failed after {m.attempts} queue attempts. "
                        f"clip_id={clip_id}. Last error: {e}"
                    ),
                )
            # Queues will redeliver after visibility_ms; we optionally nudge it sooner.
            try:
                self.q.retry(m.lease_id, delay_seconds=60)
            except Exception:
                log.exception("retry call failed")
                self.d1.send_alert(
                    clip_id=clip_id,
                    alert_type="queue_retry_call_failed",
                    message=f"Failed to request queue retry for clip_id={clip_id}",
                )


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    cfg = load_config()
    daemon = Daemon(cfg)
    signal.signal(signal.SIGINT, daemon.stop)
    signal.signal(signal.SIGTERM, daemon.stop)
    daemon.run()


if __name__ == "__main__":
    main()
