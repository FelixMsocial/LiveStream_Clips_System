"""Cloudflare Queues HTTP pull client.

Docs: https://developers.cloudflare.com/queues/configuration/pull-consumers/

Only pull + ack + retry are used here.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

log = logging.getLogger(__name__)


@dataclass
class PulledMessage:
    id: str
    timestamp: str
    attempts: int
    body: dict[str, Any]
    lease_id: str


class QueuePullClient:
    def __init__(
        self,
        account_id: str,
        queue_id: str,
        token: str,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        self._base = (
            f"https://api.cloudflare.com/client/v4/accounts/{account_id}"
            f"/queues/{queue_id}"
        )
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        self._c = client or httpx.Client(timeout=30.0)

    def pull(self, *, batch_size: int = 1, visibility_ms: int = 300_000) -> list[PulledMessage]:
        body = {"batch_size": batch_size, "visibility_timeout_ms": visibility_ms}
        r = self._c.post(f"{self._base}/messages/pull", headers=self._headers, json=body)
        r.raise_for_status()
        data = r.json().get("result", {}).get("messages", []) or []
        msgs: list[PulledMessage] = []
        for m in data:
            raw_body = m.get("body")
            if isinstance(raw_body, str):
                import json

                try:
                    parsed = json.loads(raw_body)
                except Exception:
                    parsed = {"_raw": raw_body}
            else:
                parsed = raw_body or {}
            msgs.append(
                PulledMessage(
                    id=m["id"],
                    timestamp=m.get("timestamp_ms", ""),
                    attempts=int(m.get("attempts", 1)),
                    body=parsed,
                    lease_id=m["lease_id"],
                )
            )
        return msgs

    def ack(self, lease_ids: list[str]) -> None:
        if not lease_ids:
            return
        r = self._c.post(
            f"{self._base}/messages/ack",
            headers=self._headers,
            json={"acks": [{"lease_id": lid} for lid in lease_ids]},
        )
        r.raise_for_status()

    def retry(self, lease_id: str, *, delay_seconds: int = 30) -> None:
        r = self._c.post(
            f"{self._base}/messages/ack",
            headers=self._headers,
            json={
                "retries": [
                    {"lease_id": lease_id, "delay_seconds": delay_seconds}
                ]
            },
        )
        r.raise_for_status()
