"""End-to-end per-clip pipeline (v1.1).

Stage order:
  download → transcribe (full 90s) → Step 1 substance score (Gemini) →
  Step 2/3 hook generate-and-score iteration loop (Claude) →
  ffmpeg edit (trim + reframe + word captions + hook overlay + sponsor) →
  Step 4 per-platform captions (Claude single call) →
  upload final → trigger Telegram approval

Each stage writes its outputs back to D1 via the approval-worker API so a
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
from .deepgram import transcribe_safe, words_to_ass
from .ffmpeg_recipes import SponsorConfig, build_cmd, run
from .hook_generator import generate as hook_generate
from .hook_scorer import score as hook_score
from .r2_client import R2Client
from .substance_scorer import score_with_fallback as substance_score

log = logging.getLogger(__name__)


def _extract_audio_for_asr(
    ffmpeg_bin: str, src: Path, dst: Path, start: float, end: float
) -> None:
    """Extract audio from `src[start:end]` re-encoded to AAC for Deepgram."""
    import subprocess

    duration = max(1.0, end - start)
    cmd = [
        ffmpeg_bin, "-y",
        "-ss", f"{start:.3f}",
        "-i", str(src),
        "-t", f"{duration:.3f}",
        "-vn",
        "-c:a", "aac",
        "-b:a", "128k",
        "-movflags", "+faststart",
        str(dst),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"asr audio extract failed (rc={proc.returncode}): {proc.stderr[-500:]}"
        )


def _probe_duration(ffmpeg_bin: str, path: Path) -> float:
    import subprocess

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
        return 90.0


def _full_transcript_text(words: list[dict[str, Any]]) -> str:
    """Flatten full-window word list into plain text for Step 1's input."""
    return " ".join(
        (w.get("punctuated_word") or w.get("word") or "").strip()
        for w in words
        if (w.get("punctuated_word") or w.get("word"))
    ).strip()


def _rebase_words_to_trim(
    words: list[dict[str, Any]], trim_start: float, trim_end: float
) -> list[dict[str, Any]]:
    """Filter words inside [trim_start, trim_end] and rebase timestamps to 0-based."""
    out: list[dict[str, Any]] = []
    for w in words:
        ws = float(w.get("start", 0.0))
        we = float(w.get("end", ws))
        if we <= trim_start or ws >= trim_end:
            continue
        rebased = dict(w)
        rebased["start"] = max(0.0, ws - trim_start)
        rebased["end"] = max(rebased["start"] + 0.05, we - trim_start)
        out.append(rebased)
    return out


def _run_hook_loop(
    cfg: Config,
    prompts: dict[str, str],
    substance: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], int]:
    """Iterate generate→score until PASS or HOOK_MAX_ITERATIONS hit.

    Returns (final hook_output, final scorer_output, iteration_count).
    On iteration cap, ships best-effort regardless of score.
    """
    last_gen: dict[str, Any] = {}
    last_score: dict[str, Any] = {}
    feedback: list[dict[str, Any]] | None = None

    for iteration in range(1, cfg.hook_max_iterations + 1):
        try:
            last_gen = hook_generate(
                cfg.gemini_api_key,
                prompts["hook_overlay_generator"],
                substance,
                iteration=iteration,
                previous_feedback=feedback,
                model=cfg.gemini_flash_model,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("hook generator failed on iter %d: %s", iteration, e)
            if not last_gen:
                last_gen = {
                    "hook_text": substance.get("_extracted", {}).get("extractable_element") or "",
                    "primary_archetype": "curiosity_gap",
                    "_error": str(e),
                }
            break

        try:
            last_score = hook_score(
                cfg.gemini_api_key,
                prompts["hook_overlay_scorer"],
                last_gen,
                substance,
                iteration=iteration,
                previous_feedback=feedback,
                model=cfg.gemini_flash_model,
                pass_threshold=cfg.hook_pass_threshold,
                alignment_floor=cfg.hook_alignment_floor,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("hook scorer failed on iter %d: %s", iteration, e)
            last_score = {"verdict": "FAIL", "_error": str(e), "weighted_total": 0}
            break

        if last_score.get("verdict") == "PASS":
            return last_gen, last_score, iteration

        feedback = last_score.get("improvement_feedback") or None

    return last_gen, last_score, cfg.hook_max_iterations


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
    timings: dict[str, int] = {}
    work = Path(cfg.work_dir) / clip_id
    work.mkdir(parents=True, exist_ok=True)

    raw_path = work / "raw.mp4"
    final_path = work / "final.mp4"
    srt_path = work / "captions.srt"
    ass_path = work / "captions.ass"
    full_audio_path = work / "full_audio.m4a"

    try:
        # 1. Download raw.
        t0 = time.monotonic()
        try:
            r2.download(raw_clip_r2_key, raw_path)
        except Exception as e:
            if "404" in str(e) or "Not Found" in str(e):
                log.warning("raw clip missing in R2, skipping: %s (%s)", raw_clip_r2_key, clip_id)
                _alert_issue(
                    d1, clip_id=clip_id, alert_type="raw_clip_missing",
                    message=f"Raw clip object was missing in R2. raw_key={raw_clip_r2_key}",
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

        # 2. Transcribe FULL window — Step 1 needs full transcript context;
        #    word-level timestamps will be filtered + rebased for ASS later.
        t0 = time.monotonic()
        _extract_audio_for_asr(cfg.ffmpeg_bin, raw_path, full_audio_path, 0.0, duration_sec)
        full_srt, full_words, deepgram_err = transcribe_safe(
            cfg.deepgram_api_key, full_audio_path, content_type="audio/mp4"
        )
        timings["transcribe"] = _ms_since(t0)
        if deepgram_err:
            _alert_issue(
                d1, clip_id=clip_id, alert_type="deepgram_api_failure",
                message=(
                    "Deepgram transcription failed after 3 attempts; "
                    f"continuing without captions. Error: {deepgram_err}"
                ),
            )
        full_text = _full_transcript_text(full_words)

        # 3. Step 1 — Substance Scorer (Gemini, native video).
        t0 = time.monotonic()
        substance, gemini_err = substance_score(
            cfg.gemini_api_key,
            raw_path,
            prompts["clip_substance_scorer"],
            duration_sec,
            model=cfg.gemini_model,
            transcript_excerpt=full_text,
        )
        timings["substance_score"] = _ms_since(t0)
        if gemini_err:
            _alert_issue(
                d1, clip_id=clip_id, alert_type="substance_scorer_failure",
                message=(
                    "Substance scorer failed after 3 attempts; using degraded fallback. "
                    f"Error: {gemini_err}"
                ),
            )

        weighted_total = int(substance.get("weighted_total", 0) or 0)
        low_flag = 1 if weighted_total < cfg.substance_low_threshold else 0
        trim = substance["recommended_trim_window"]
        trim_start = float(trim["start_seconds"])
        trim_end = float(trim["end_seconds"])
        peak_ts = float(
            substance.get("_extracted", {}).get("peak_timestamp_seconds")
            or (trim_start + trim_end) / 2.0
        )
        try:
            crop_focus_x = float(
                substance.get("_extracted", {}).get("horizontal_focus", 0.5)
            )
        except (TypeError, ValueError):
            log.warning("substance _extracted.horizontal_focus malformed; defaulting to 0.5")
            crop_focus_x = 0.5
        crop_focus_x = max(0.0, min(1.0, crop_focus_x))

        d1.patch_clip(
            clip_id,
            {
                "vision_analysis": json.dumps(substance, ensure_ascii=False),
                "substance_score": weighted_total,
                "substance_score_json": json.dumps(substance, ensure_ascii=False),
                "low_potential_flag": low_flag,
                "peak_timestamp_sec": peak_ts,
                "trim_start_sec": trim_start,
                "trim_end_sec": trim_end,
            },
        )

        # 4. Persist trim-relative SRT + word-level ASS for the burn-in pass.
        trim_words = _rebase_words_to_trim(full_words, trim_start, trim_end)
        if trim_words:
            from .deepgram import _words_to_srt  # internal helper, fine here
            trim_srt = _words_to_srt(trim_words)
            if trim_srt:
                srt_path.write_text(trim_srt, encoding="utf-8")
                d1.patch_clip(clip_id, {"transcript_srt": trim_srt})
            ass_text = words_to_ass(trim_words)
            if ass_text:
                ass_path.write_text(ass_text, encoding="utf-8")

        # 5. Steps 2 & 3 — Hook generate ↔ score iteration loop.
        t0 = time.monotonic()
        hook_out, hook_eval, iter_count = _run_hook_loop(cfg, prompts, substance)
        timings["hook_loop"] = _ms_since(t0)
        hook_text = (hook_out.get("hook_text") or "").strip()
        # Drop generator flag-back strings and error messages — these must not be burned in.
        _HOOK_NON_CONTENT = (
            "cannot generate hook",
            "system error",
            "no resolvable uncertainty",
        )
        if any(s in hook_text.lower() for s in _HOOK_NON_CONTENT):
            log.warning("hook_text looks like a generator flag-back, suppressing burn-in: %r", hook_text)
            hook_text = ""
        d1.patch_clip(
            clip_id,
            {
                "hook_overlay_text": hook_text,
                "hook_score": int(hook_eval.get("weighted_total") or 0),
                "hook_score_json": json.dumps(hook_eval, ensure_ascii=False),
                "hook_iterations": iter_count,
            },
        )

        # 6. FFmpeg edit (trim + reframe + word captions + hook overlay + sponsor).
        d1.patch_clip(clip_id, {"status": "editing"})
        sponsor = _load_sponsor(work, r2, session_id=stream_session_id)
        cmd = build_cmd(
            ffmpeg_bin=cfg.ffmpeg_bin,
            input_path=raw_path,
            output_path=final_path,
            trim_start=trim_start,
            trim_end=trim_end,
            subtitles_path=ass_path if ass_path.exists() else None,
            sponsor=sponsor,
            hook_text=hook_text or None,
            hook_font_path=cfg.hook_font_path if hook_text else None,
            hook_emoji_font_path=cfg.hook_emoji_font_path if hook_text else None,
            brand_logo_path=Path(cfg.brand_logo_path) if cfg.brand_logo_path else None,
            brand_video_path=Path(cfg.brand_video_path) if cfg.brand_video_path else None,
            crop_focus_x=crop_focus_x,
        )
        t0 = time.monotonic()
        try:
            _run_with_retries("ffmpeg", 3, lambda: run(cmd))
        except Exception as e:  # noqa: BLE001
            _alert_issue(
                d1, clip_id=clip_id, alert_type="ffmpeg_failure",
                message=f"FFmpeg composition failed after 3 attempts. Error: {e}",
            )
            raise
        timings["ffmpeg"] = _ms_since(t0)

        # 7. Upload final.
        final_key = f"final/{clip_id}.mp4"
        t0 = time.monotonic()
        try:
            _run_with_retries("r2_upload", 3, lambda: r2.upload(final_path, final_key))
        except Exception as e:  # noqa: BLE001
            _alert_issue(
                d1, clip_id=clip_id, alert_type="r2_upload_failure",
                message=f"Final video upload to R2 failed after 3 attempts. Error: {e}",
            )
            raise
        timings["upload"] = _ms_since(t0)
        d1.patch_clip(clip_id, {"final_clip_r2_key": final_key})

        # 8. Step 4 — Per-platform captions (single Gemini Flash call).
        t0 = time.monotonic()
        captions, caption_full, copy_err = run_copy(
            cfg.gemini_api_key,
            prompts["per_platform_post_text"],
            substance,
            hook_out,
            model=cfg.gemini_flash_model,
        )
        timings["copy"] = _ms_since(t0)
        if copy_err:
            _alert_issue(
                d1, clip_id=clip_id, alert_type="claude_copy_failure",
                message=(
                    "Per-platform caption generation failed; fallback text used. "
                    f"Error: {copy_err}"
                ),
            )

        d1.patch_clip(
            clip_id,
            {
                "instagram_post_text": captions.get("instagram", ""),
                "youtube_post_text": captions.get("youtube", ""),
                "tiktok_post_text": captions.get("tiktok", ""),
                "caption_scores_json": json.dumps(caption_full, ensure_ascii=False),
                "gpu_timings_ms": json.dumps(timings),
                "status": "pending_approval",
            },
        )

        # 9. Trigger approval send.
        try:
            d1.trigger_approval_send(clip_id)
        except Exception as e:  # noqa: BLE001
            _alert_issue(
                d1, clip_id=clip_id, alert_type="approval_send_failure",
                message=(
                    "Final clip rendered and uploaded, but approval notification "
                    f"failed after 3 attempts. Error: {e}"
                ),
            )
            log.exception("approval-send failed for %s", clip_id)

    except Exception as e:
        log.exception("pipeline failed for %s: %s", clip_id, e)
        _alert_issue(
            d1, clip_id=clip_id, alert_type="gpu_pipeline_failure",
            message=f"GPU pipeline failed during editing flow. Error: {e}",
        )
        try:
            d1.patch_clip(
                clip_id,
                {"status": "failed_edit", "gpu_timings_ms": json.dumps(timings)},
            )
        except Exception:
            log.exception("failed to mark failed_edit")
        raise
    finally:
        if final_path.exists():
            shutil.rmtree(work, ignore_errors=True)


def _load_sponsor(
    work: Path, r2: R2Client, *, session_id: str | None = None
) -> SponsorConfig | None:
    # Candidate keys: global persistent slot first, then legacy session-scoped key.
    candidates: list[str] = []
    for ext in ("mp4", "mov", "png", "webp"):
        candidates.append(f"sponsors/active.{ext}")
    if session_id:
        for ext in ("mp4", "mov", "png", "webp"):
            candidates.append(f"sponsors/{session_id}.{ext}")

    for key in candidates:
        if not r2.head(key):
            continue
        ext = key.rsplit(".", 1)[-1]
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
    d1.send_alert(clip_id=clip_id, alert_type=alert_type, message=message)
