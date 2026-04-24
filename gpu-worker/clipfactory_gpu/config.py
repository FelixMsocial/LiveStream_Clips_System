"""Config loading — reads from .env (and the process environment)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Config:
    # Cloudflare Queues pull
    cf_account_id: str
    cf_queues_pull_token: str
    cf_clip_edit_queue_id: str

    # R2
    r2_account_id: str
    r2_access_key_id: str
    r2_secret_access_key: str
    r2_bucket: str
    r2_endpoint: str

    # APIs
    gemini_api_key: str
    anthropic_api_key: str
    deepgram_api_key: str

    # Coordination with approval-worker
    gpu_worker_id: str
    gpu_heartbeat_url: str  # e.g. https://clip-approval.../api/gpu/heartbeat
    gpu_d1_api_url: str     # base, e.g. https://clip-approval.../api/internal
    gpu_internal_secret: str

    # FFmpeg
    ffmpeg_bin: str
    work_dir: str


def load_config() -> Config:
    load_dotenv(dotenv_path=_find_env(), override=False)

    def req(name: str) -> str:
        v = os.environ.get(name, "")
        if not v:
            raise RuntimeError(f"missing required env: {name}")
        return v

    def req_any(*names: str) -> str:
        for name in names:
            v = os.environ.get(name, "")
            if v:
                return v
        joined = " or ".join(names)
        raise RuntimeError(f"missing required env: one of {joined}")

    return Config(
        # Support both names; CLOUDFLARE_ACCOUNT_ID is used by current .env template.
        cf_account_id=req_any("CF_ACCOUNT_ID", "CLOUDFLARE_ACCOUNT_ID"),
        cf_queues_pull_token=req("CF_QUEUES_PULL_TOKEN"),
        cf_clip_edit_queue_id=req("CF_CLIP_EDIT_QUEUE_ID"),
        r2_account_id=req("R2_ACCOUNT_ID"),
        r2_access_key_id=req("R2_ACCESS_KEY_ID"),
        r2_secret_access_key=req("R2_SECRET_ACCESS_KEY"),
        r2_bucket=os.environ.get("R2_BUCKET", "clip-bucket"),
        r2_endpoint=req("R2_ENDPOINT"),
        gemini_api_key=req("GEMINI_API_KEY"),
        anthropic_api_key=req("ANTHROPIC_API_KEY"),
        deepgram_api_key=req("DEEPGRAM_API_KEY"),
        gpu_worker_id=os.environ.get("GPU_WORKER_ID", "gpu-01"),
        gpu_heartbeat_url=req("GPU_HEARTBEAT_URL"),
        gpu_d1_api_url=req("GPU_D1_API_URL"),
        gpu_internal_secret=req("GPU_INTERNAL_SECRET"),
        ffmpeg_bin=os.environ.get("FFMPEG_BIN", "ffmpeg"),
        work_dir=os.environ.get("GPU_WORK_DIR", str(Path.cwd() / "tmp")),
    )


def _find_env() -> str | None:
    here = Path(__file__).resolve()
    for p in [here.parent.parent / ".env", here.parent.parent.parent / ".env"]:
        if p.exists():
            return str(p)
    return None
