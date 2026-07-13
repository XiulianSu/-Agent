"""Persistent cross-session memory stored as Markdown files (WIP — not wired into main yet)."""

from __future__ import annotations

from pathlib import Path

MEMORY_DIR = Path.cwd() / ".memory"
MEMORY_INDEX = MEMORY_DIR / "MEMORY.md"
MEMORY_TYPES = frozenset({"user", "feedback", "project", "reference"})


def configure_long_term_memory(memory_dir: Path) -> None:
    global MEMORY_DIR, MEMORY_INDEX
    MEMORY_DIR = memory_dir
    MEMORY_INDEX = MEMORY_DIR / "MEMORY.md"
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---"):
        return {}, text

    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text

    meta: dict[str, str] = {}
    for line in parts[1].strip().split("\n"):
        if ":" in line:
            key, value = line.split(":", 1)
            meta[key.strip()] = value.strip()
    return meta, parts[2].strip()
