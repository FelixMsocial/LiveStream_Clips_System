"""
Generate a D1 SQL migration from the .md prompt files.

Usage:
  python scripts/gen_prompt_migration.py

Writes: migrations/0016_update_prompts_v2.sql

Versions inserted:
  gameplay tag: version 3  (previous: 2)
  vlog tag:     version 2  (previous: 1)
"""
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

GAMEPLAY_VERSION = 3
VLOG_VERSION = 2

PROMPTS = [
    # (key, tag, version, md_path)
    ("clip_substance_scorer",  "gameplay", GAMEPLAY_VERSION, REPO / "prompts/gameplay/Clip_Substance_Scorer_Prompt_v1.2.md"),
    ("hook_overlay_generator", "gameplay", GAMEPLAY_VERSION, REPO / "prompts/gameplay/Hook_Overlay_Generator_Prompt_v1.1.md"),
    ("hook_overlay_scorer",    "gameplay", GAMEPLAY_VERSION, REPO / "prompts/gameplay/Hook_Overlay_Scorer_Prompt_v1.0.md"),
    ("per_platform_post_text", "gameplay", GAMEPLAY_VERSION, REPO / "prompts/gameplay/Per_Platform_Post_Text_Generator_Prompt_v1.1.md"),
    ("clip_substance_scorer",  "vlog",     VLOG_VERSION,     REPO / "prompts/vlog/Clip_Substance_Scorer_Prompt_v1.0.md"),
    ("hook_overlay_generator", "vlog",     VLOG_VERSION,     REPO / "prompts/vlog/Hook_Overlay_Generator_Prompt_v1.0.md"),
    ("hook_overlay_scorer",    "vlog",     VLOG_VERSION,     REPO / "prompts/vlog/Hook_Overlay_Scorer_Prompt_v1.0.md"),
    ("per_platform_post_text", "vlog",     VLOG_VERSION,     REPO / "prompts/vlog/Per_Platform_Post_Text_Generator_Prompt_v1.0.md"),
]

OUT = REPO / "migrations" / "0016_update_prompts_v2.sql"


def escape(body: str) -> str:
    """Escape single quotes for SQLite (double them)."""
    return body.replace("'", "''")


def main() -> None:
    lines = [
        "-- Seed updated prompts: gameplay v3 (CS2 identity guard + creator context),",
        "--   vlog v2 (Amazon+CS2 hybrid framing, CS2 guard, remove no-gameplay restriction).",
        "-- Single quotes escaped by doubling per SQLite convention.",
        "",
        "INSERT OR IGNORE INTO prompts (key, tag, version, body) VALUES",
    ]

    entries = []
    for key, tag, version, md_path in PROMPTS:
        body = md_path.read_text(encoding="utf-8").rstrip()
        escaped = escape(body)
        entries.append(f"('{key}', '{tag}', {version},\n'{escaped}')")

    lines.append(",\n".join(entries) + ";")
    lines.append("")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"Written: {OUT}")
    print(f"  gameplay prompts: version {GAMEPLAY_VERSION}")
    print(f"  vlog prompts:     version {VLOG_VERSION}")


if __name__ == "__main__":
    main()
