"""Daily markdown report."""

from __future__ import annotations

from pathlib import Path

from app.db import DATA_DIR, stories_for
from app.sources import SOURCE_IDS, SOURCE_LABELS


def write_report(day: str, stories: list[dict] | None = None) -> Path:
    if stories is None:
        stories = stories_for(day)
    lines = [
        f"# Hacker News Daily Signals — {day}",
        "",
        "## Stories from last 24 hours",
        "",
    ]
    if not stories:
        lines.append("No stories from the last 24 hours were found.")
        lines.append("")
    grouped: dict[str, list[dict]] = {source_id: [] for source_id in SOURCE_IDS}
    for story in stories:
        grouped.setdefault(story.get("source") or "hn", []).append(story)
    for source_id, group in grouped.items():
        if not group:
            continue
        lines.append(f"## {SOURCE_LABELS.get(source_id, source_id)}")
        lines.append("")
        for story in group:
            url = story.get("external_url") or story.get("hn_url") or ""
            title = story["title"].replace("[", "\\[").replace("]", "\\]")
            if url:
                lines.append(f"### [{title}]({url})")
            else:
                lines.append(f"### {story['title']}")
            lines.append(f"- Source: {SOURCE_LABELS.get(source_id, source_id)}")
            lines.append(
                f"- Points: {int(story.get('points') or 0)} | "
                f"Comments: {int(story.get('comments') or 0)}"
            )
            lines.append(f"- Summary (EN): {story.get('summary_en') or ''}")
            lines.append(f"- Summary (ZH): {story.get('summary_zh') or ''}")
            lines.append("")
    directory = DATA_DIR / "reports"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{day}.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
