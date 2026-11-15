"""Age windows for a daily Hacker News digest."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone

WINDOW = timedelta(hours=24)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_posted_at(raw: str | None) -> datetime | None:
    if not raw:
        return None
    text = raw.strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def reference_for(day: date, now: datetime | None = None) -> datetime:
    """End of an archive day, or the current time when the day is today."""
    now = now or utcnow()
    if day == now.astimezone(timezone.utc).date():
        return now
    end = datetime(day.year, day.month, day.day, 23, 59, 59, tzinfo=timezone.utc)
    return min(end, now)


def within_window(posted: datetime, reference: datetime) -> bool:
    age = reference - posted
    return timedelta(0) <= age <= WINDOW


def age_text_as_of(posted: datetime, reference: datetime) -> str:
    seconds = int((reference - posted).total_seconds())
    if seconds < 0:
        return "in the future"
    if seconds < 3600:
        minutes = max(1, seconds // 60)
        unit = "minute" if minutes == 1 else "minutes"
        return f"{minutes} {unit} ago"
    if seconds < 86400:
        hours = seconds // 3600
        unit = "hour" if hours == 1 else "hours"
        return f"{hours} {unit} ago"
    days = seconds // 86400
    unit = "day" if days == 1 else "days"
    return f"{days} {unit} ago"


def parse_points(score_text: str) -> int:
    match = re.search(r"\d+", score_text or "")
    return int(match.group()) if match else 0


def parse_comments(comment_text: str) -> int:
    text = (comment_text or "").replace("\xa0", " ").strip().lower()
    if not text or "discuss" in text:
        return 0
    match = re.search(r"\d+", text)
    return int(match.group()) if match else 0


def recent_dates(n: int, today: date | None = None) -> list[date]:
    today = today or utcnow().date()
    return [today - timedelta(days=offset) for offset in range(n)]
