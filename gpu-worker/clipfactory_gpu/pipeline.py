"""End-to-end per-clip pipeline.

Each stage writes its timing + outputs to D1 via the approval-worker API so a
crash mid-pipe leaves a resumable checkpoint.
"""
from __future__ import annotations

import json
import logging
import shutil
import time
from pathlib import Path
from typing import Any

from .claude_copy import run_copy
from .config import Config
from .d1_api import D1Api
from .deepgram import transcribe_safe
from .ffmpeg_recipes import SponsorConfig, build_cmd, run
from .gemini import analyze_with_fallback
from .r2_client import R2Client

log = logging.getLogger(__name__)


def _probe_duration(ffmpeg_bin: str, path: Path) -> float:
    import subprocess

    # ffprobe is bundled with ffmpeg; swap the binary name if needed.
    probe_bin = ffmpeg_bin.replace("ffmpeg", "ffprobe")
    cmd = [
        probe_bin, "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=nokey=1:noprint_wrappers=1",
        str(path),
    ]
    out = subprocess.run(cmd, capture_output=True, text=True)
    try:
        return float(out.stdout.strip())
    except Exception:
        return 30.0  # safe default


def run_pipeline(
    *,
    cfg: Config,
    r2: R2Client,
    d1: D1Api,
    prompts: dict[str, str],
    clip_id: str,
    raw_clip_r2_key: str,
    stream_session_id: str | None,
) -> None:
    """Run the whole GPU pipeline for a single clip."""
    timings: dict[str, int] = {}
    work = Path(cfg.work_dir) / clip_id
    work.mkdir(parents=True, exist_ok=True)

    raw_path = work / "raw.mp4"
    final_path = work / "final.mp4"
    srt_path = work / "captions.srt"

    try:
        # 1. Download raw.
        t0 = time.monotonic()
        try:
            r2.download(raw_clip_r2_key, raw_path)
        except Exception as e:
            # If object missing (common after manual bucket wipes), skip job cleanly
            if "404" in str(e) or "Not Found" in str(e):
                log.warning("raw clip missing in R2, skipping: %s (%s)", raw_clip_r2_key, clip_id)
                _alert_issue(
                    d1,
                    clip_id=clip_id,
                    alert_type="raw_clip_missing",
                    message=(
                        f"Raw clip object was missing in R2. "
                        f"raw_key={raw_clip_r2_key}"
                    ),
                )
                d1.patch_clip(
                    clip_id,
                    {"status": "missing_raw", "gpu_timings_ms": json.dumps(timings)},
                )
                return
            raise
        timings["download"] = _ms_since(t0)
        duration_sec = _probe_duration(cfg.ffmpeg_bin, raw_path)
        d1.patch_clip(clip_id, {"status": "analyzing", "duration_sec": duration_sec})

        # 2. Gemini analysis.
        t0 = time.monotonic()
        vision, gemini_err = analyze_with_fallback(
            cfg.gemini_api_key, raw_path, prompts["gemini_analysis"], duration_sec
        )
        timings["vision"] = _ms_since(t0)
        if gemini_err:
            _alert_issue(
                d1,
                clip_id=clip_id,
                alert_type="gemini_api_failure",
                message=(
                    "Gemini analysis failed after 3 attempts; "
                    f"using degraded midpoint trim fallback. Error: {gemini_err}"
                ),
            )
        d1.patch_clip(
            clip_id,
            {"vision_analysis": json.dumps(vision, ensure_ascii=False)},
        )

        # 3. Deepgram transcription.
        t0 = time.monotonic()
        srt_text, _words, deepgram_err = transcribe_safe(cfg.deepgram_api_key, raw_path)
        timings["transcribe"] = _ms_since(t0)
        if deepgram_err:
            _alert_issue(
                d1,
                clip_id=clip_id,
                alert_type="deepgram_api_failure",
                message=(
                    "Deepgram transcription failed after 3 attempts; "
                    f"continuing without captions. Error: {deepgram_err}"
                ),
            )
        if srt_text:
            srt_path.write_text(srt_text, encoding="utf-8")
            d1.patch_clip(clip_id, {"transcript_srt": srt_text})

        # 4. FFmpeg edit.
        d1.patch_clip(clip_id, {"status": "editing"})
        trim = vision["recommended_trim"]
        sponsor = _load_sponsor(stream_session_id, work, r2) if stream_session_id else None
        cmd = build_cmd(
            ffmpeg_bin=cfg.ffmpeg_bin,
            input_path=raw_path,
            output_path=final_path,
            trim_start=float(trim["start_sec"]),
            trim_end=float(trim["end_sec"]),
            subtitles_path=srt_path if srt_text else None,
            sponsor=sponsor,
        )
        t0 = time.monotonic()
        try:
            _run_with_retries("ffmpeg", 3, lambda: run(cmd))
        except Exception as e:  # noqa: BLE001
            _alert_issue(
                d1,
                clip_id=clip_id,
                alert_type="ffmpeg_failure",
                message=f"FFmpeg composition failed after 3 attempts. Error: {e}",
            )
            raise
        timings["ffmpeg"] = _ms_since(t0)

        # 5. Upload final.
        final_key = f"final/{clip_id}.mp4"
        t0 = time.monotonic()
        try:
            _run_with_retries("r2_upload", 3, lambda: r2.upload(final_path, final_key))
        except Exception as e:  # noqa: BLE001
            _alert_issue(
                d1,
                clip_id=clip_id,
                alert_type="r2_upload_failure",
                message=f"Final video upload to R2 failed after 3 attempts. Error: {e}",
            )
            raise
        timings["upload"] = _ms_since(t0)
        d1.patch_clip(clip_id, {"final_clip_r2_key": final_key})

        # 6. Claude copy × 3.
        t0 = time.monotonic()
        copy_out, claude_failures = run_copy(
            cfg.anthropic_api_key,
            prompts,
            vision,
            _first_quotes(vision),
            str(vision.get("vibe", "unknown")),
        )
        timings["copy"] = _ms_since(t0)
        if claude_failures:
            detail = "; ".join(
                f"{platform}={err}" for platform, err in claude_failures.items()
            )
            _alert_issue(
                d1,
                clip_id=clip_id,
                alert_type="claude_copy_failure",
                message=(
                    "Claude copy generation failed after 3 attempts for one or more "
                    f"platforms; fallback text used. Details: {detail}"
                ),
            )

        d1.patch_clip(
            clip_id,
            {
                "instagram_post_text": copy_out.get("instagram", ""),
                "youtube_post_text": copy_out.get("youtube", ""),
                "tiktok_post_text": copy_out.get("tiktok", ""),
                "gpu_timings_ms": json.dumps(timings),
                "status": "pending_approval",
            },
        )

        # 7. Trigger approval send.
        try:
            d1.trigger_approval_send(clip_id)
        except Exception as e:  # noqa: BLE001
            _alert_issue(
                d1,
                clip_id=clip_id,
                alert_type="approval_send_failure",
                message=(
                    "Final clip rendered and uploaded, but approval notification "
                    f"failed after 3 attempts. Error: {e}"
                ),
            )
            log.exception("approval-send failed for %s", clip_id)

    except Exception as e:
        log.exception("pipeline failed for %s: %s", clip_id, e)
        _alert_issue(
            d1,
            clip_id=clip_id,
            alert_type="gpu_pipeline_failure",
            message=f"GPU pipeline failed during editing flow. Error: {e}",
        )
        try:
            d1.patch_clip(
                clip_id,
                {
                    "status": "failed_edit",
                    "gpu_timings_ms": json.dumps(timings),
                },
            )
        except Exception:
            log.exception("failed to mark failed_edit")
        raise
    finally:
        # Cleanup workdir on success only — leave it for debugging on error.
        if final_path.exists():
            shutil.rmtree(work, ignore_errors=True)


def _load_sponsor(
    session_id: str, work: Path, r2: R2Client
) -> SponsorConfig | None:
    # V1: if sponsor asset exists at sponsors/{session_id}.*, pull it.
    # For video formats we assume a green-screen logo and pre-process with
    # chroma-key before placing in the output.
    for ext in ("mp4", "mov", "png", "webp"):
        key = f"sponsors/{session_id}.{ext}"
        head = r2.head(key)
        if head:
            dest = work / f"sponsor.{ext}"
            r2.download(key, dest)
            is_video = ext in {"mp4", "mov"}
            return SponsorConfig(
                path=dest,
                is_video=is_video,
                remove_green=is_video,
                chroma_similarity=0.28 if is_video else 0.22,
                chroma_blend=0.00 if is_video else 0.08,
                opacity=0.95 if is_video else 0.85,
            )
    return None


def _first_quotes(vision: dict[str, Any]) -> str:
    quotes = vision.get("quotes") or []
    joined = " ".join(str(q.get("text", "")) for q in quotes[:4])
    return joined.strip()


def _ms_since(t0: float) -> int:
    return int((time.monotonic() - t0) * 1000)


def _run_with_retries(stage: str, attempts: int, fn: Any) -> Any:
    last: Exception | None = None
    for i in range(1, attempts + 1):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            last = e
            if i == attempts:
                raise
            backoff = min(2 ** i, 8)
            log.warning("%s failed (%d/%d): %s; retrying in %ss", stage, i, attempts, e, backoff)
            time.sleep(backoff)
    raise RuntimeError(f"{stage} failed unexpectedly: {last}")


def _alert_issue(
    d1: D1Api,
    *,
    clip_id: str,
    alert_type: str,
    message: str,
) -> None:
    d1.send_alert(
        clip_id=clip_id,
        alert_type=alert_type,
        message=message,
    )
