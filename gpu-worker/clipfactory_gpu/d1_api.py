"""Thin HTTP client to the approval-worker's internal API."""
from __future__ import annotations

import logging
import time
from typing import Any

import httpx

log = logging.getLogger(__name__)


class D1Api:
    def __init__(self, base_url: str, secret: str, *, worker_id: str) -> None:
        self._base = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {secret}"}
        self._worker_id = worker_id
        self._c = httpx.Client(timeout=30.0)

    def _request_with_retry(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
        timeout: float | None = None,
        attempts: int = 3,
    ) -> httpx.Response:
        last: Exception | None = None
        for i in range(attempts):
            try:
                r = self._c.request(
                    method,
                    url,
                    headers=headers,
                    json=json,
                    timeout=timeout,
                )
                r.raise_for_status()
                return r
            except Exception as e:  # noqa: BLE001
                last = e
                if i == attempts - 1:
                    raise
                time.sleep(min(2 ** i, 5))
        raise RuntimeError(f"request failed unexpectedly: {last}")

    def heartbeat(self, heartbeat_url: str) -> bool:
        try:
            self._request_with_retry(
                "POST",
                heartbeat_url,
                headers=self._headers,
                json={"worker_id": self._worker_id},
                timeout=10.0,
                attempts=3,
            )
            return True
        except Exception as e:
            log.warning("heartbeat failed: %s", e)
            return False

    def patch_clip(self, clip_id: str, fields: dict[str, Any]) -> None:
        self._request_with_retry(
            "PATCH",
            f"{self._base}/clips/{clip_id}",
            headers={**self._headers, "Content-Type": "application/json"},
            json=fields,
            attempts=3,
        )

    def trigger_approval_send(self, clip_id: str) -> None:
        self._request_with_retry(
            "POST",
            f"{self._base}/approval-send",
            headers={**self._headers, "Content-Type": "application/json"},
            json={"clip_id": clip_id},
            attempts=3,
        )

    def send_alert(
        self,
        *,
        alert_type: str,
        message: str,
        clip_id: str | None = None,
    ) -> bool:
        payload: dict[str, Any] = {
            "alert_type": alert_type,
            "message": message,
        }
        if clip_id:
            payload["clip_id"] = clip_id
        try:
            self._request_with_retry(
                "POST",
                f"{self._base}/alert",
                headers={**self._headers, "Content-Type": "application/json"},
                json=payload,
                attempts=3,
            )
            return True
        except Exception as e:  # noqa: BLE001
            log.warning("alert send failed (%s): %s", alert_type, e)
            return False

    def fetch_prompts(self) -> dict[str, str] | None:
        """Fetch latest prompt bodies from D1 via approval-worker.

        Returns a dict mapping prompt key → body, or None on failure.
        """
        try:
            r = self._c.get(
                f"{self._base}/prompts",
                headers=self._headers,
                timeout=10.0,
            )
            r.raise_for_status()
            data = r.json()
            prompts = data.get("prompts")
            if isinstance(prompts, dict) and prompts:
                return prompts
            return None
        except Exception as e:
            log.warning("fetch_prompts failed: %s", e)
            return None
