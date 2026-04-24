"""Thin HTTP client to the approval-worker's internal API."""
from __future__ import annotations

import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)


class D1Api:
    def __init__(self, base_url: str, secret: str, *, worker_id: str) -> None:
        self._base = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {secret}"}
        self._worker_id = worker_id
        self._c = httpx.Client(timeout=30.0)

    def heartbeat(self, heartbeat_url: str) -> None:
        try:
            self._c.post(
                heartbeat_url,
                headers=self._headers,
                json={"worker_id": self._worker_id},
                timeout=10.0,
            )
        except Exception as e:
            log.warning("heartbeat failed: %s", e)

    def patch_clip(self, clip_id: str, fields: dict[str, Any]) -> None:
        r = self._c.patch(
            f"{self._base}/clips/{clip_id}",
            headers={**self._headers, "Content-Type": "application/json"},
            json=fields,
        )
        r.raise_for_status()

    def trigger_approval_send(self, clip_id: str) -> None:
        r = self._c.post(
            f"{self._base}/approval-send",
            headers={**self._headers, "Content-Type": "application/json"},
            json={"clip_id": clip_id},
        )
        r.raise_for_status()

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
