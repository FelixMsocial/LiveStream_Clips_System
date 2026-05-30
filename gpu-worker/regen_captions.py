"""Standalone caption regenerator for a specific clip.

Uses the substance data already stored in D1 and re-runs the hook + per-platform
caption generation steps (Steps 2 & 4 of the pipeline) using the same prompts.
"""
import json
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

from clipfactory_gpu.config import load_config
from clipfactory_gpu.hook_generator import generate as generate_hook
from clipfactory_gpu.claude_copy import run_copy
from clipfactory_gpu.prompts_fallback import PROMPTS

# Substance data for clip 019e6f0d-4e03-7bdd-9cb9-a3ccf023e1fe (from D1)
SUBSTANCE_JSON = """{"rule_scores": {"1_peak_moment_clarity": {"score": 9, "reasoning": "The clip has an exceptionally clear peak moment. The setup is at 00:13 ($500, and we throw the cameraman overboard), and the punchline lands perfectly at 00:16 with his emphatic No, no, no and hand gesture.", "peak_timestamp_seconds": 16}, "2_emotional_arousal": {"score": 7, "reasoning": "The moment generates strong positive affect through humor. The host's playful tone, smile, and emphatic rejection of the idea create a genuinely funny and charming interaction, well above a baseline level of amusement.", "peak_emotion": "humor"}, "3_self_contained_context": {"score": 9, "reasoning": "The context is immediately understandable. The visuals show they're on a boat, and the dialogue makes it clear he's responding to a viewer's dare involving his cameraman. No prior knowledge of the streamer or his crew is required.", "extractable_element": null}, "4_quotable_memorable_beat": {"score": 8, "reasoning": "The entire sequence is highly quotable, particularly the setup and reversal.", "extractable_element": "The line \\\"$500, and we throw the cameraman overboard,\\\" followed immediately by his emphatic \\\"No no no no no\\\" and hand wave."}, "5_narrative_arc": {"score": 9, "reasoning": "The clip contains a perfect, compact narrative arc.", "extractable_element": null}, "6_share_trigger": {"score": 7, "reasoning": "The clip's share trigger is its wholesome and relatable 'good boss/team leader' moment.", "trigger_type": "identity"}, "7_visual_clarity": {"score": 8, "reasoning": "The visual quality is high, shot in bright daylight on a boat.", "extractable_element": null}, "8_originality": {"score": 6, "reasoning": "While the 'viewer dare' format is common in IRL streaming, the unique Amazon river boat setting provides a fresh backdrop.", "extractable_element": null}}, "coherence_bonus": {"score": 9, "reasoning": "Every strength amplifies the others."}, "weighted_total": 81.7, "interpretation": "viral_candidate", "context_summary": "While on a boat in the Amazon, the host Jordy reads a viewer's request to see his cameraman get thrown in the water. After a price of $500 is suggested, Jordy playfully agrees to throw the cameraman overboard before immediately shutting the idea down with a smile, declaring 'We are one team here,' showcasing their positive relationship.", "recommended_trim_window": {"start_seconds": 10.0, "end_seconds": 23.0, "rationale": "Captures the full joke arc."}, "recommended_crop": {"horizontal_focus": 0.5, "rationale": "Host remains centered."}, "rulebook_version": "1.0-vlog", "_extracted": {"peak_timestamp_seconds": 16.0, "peak_emotion": "humor", "extractable_element": "The line \\\"$500, and we throw the cameraman overboard,\\\" followed immediately by his emphatic \\\"No no no no no\\\" and hand wave.", "trigger_type": "identity", "horizontal_focus": 0.5}}"""

CONTENT_TAG = "vlog"

def main():
    cfg = load_config()
    prompts = PROMPTS.get(CONTENT_TAG, PROMPTS["gameplay"])

    substance = json.loads(SUBSTANCE_JSON)

    print("=== Step 2: Hook Generator ===")
    hook_out = generate_hook(
        cfg.anthropic_api_key,
        prompts["hook_overlay_generator"],
        substance,
        iteration=1,
        model=cfg.claude_model,
    )
    print(f"Hook text: {hook_out.get('hook_text')}")
    print(f"Archetype: {hook_out.get('primary_archetype')}")
    print()

    print("=== Step 4: Per-Platform Captions ===")
    captions, caption_full, err = run_copy(
        cfg.anthropic_api_key,
        prompts["per_platform_post_text"],
        substance,
        hook_out,
        model=cfg.claude_model,
    )

    if err:
        print(f"Error: {err}")
    else:
        print(f"\nYouTube Shorts:\n{captions['youtube']}")
        print(f"\nTikTok:\n{captions['tiktok']}")
        print(f"\nInstagram Reels:\n{captions['instagram']}")
        print()
        print("=== Full Caption Scores JSON ===")
        print(json.dumps(caption_full, indent=2, ensure_ascii=False))

if __name__ == "__main__":
    main()
