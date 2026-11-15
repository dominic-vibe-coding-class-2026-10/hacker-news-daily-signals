from datetime import date, datetime, timezone

from app.other_sources import (
    keep_hket_page,
    parse_hket_parts,
    parse_hket_time,
    publication_day,
)

from app import db
from app.filter import parse_model_array
from app.timeutil import (
    age_text_as_of,
    parse_comments,
    parse_points,
    reference_for,
    within_window,
)


def test_window_includes_same_day_and_excludes_older():
    reference = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)
    fresh = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)
    stale = datetime(2026, 11, 14, 11, 59, tzinfo=timezone.utc)
    assert within_window(fresh, reference)
    assert not within_window(stale, reference)


def test_archive_reference_is_end_of_day():
    now = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)
    assert reference_for(date(2026, 11, 14), now) == datetime(
        2026, 11, 14, 23, 59, 59, tzinfo=timezone.utc
    )
    assert reference_for(date(2026, 11, 15), now) == now


def test_hket_time_is_hong_kong():
    parsed = parse_hket_time("2026-11-15", "19:55")
    assert parsed == datetime(2026, 11, 15, 11, 55, tzinfo=timezone.utc)
    now = datetime(2026, 11, 15, 12, 42, tzinfo=timezone.utc)
    assert parse_hket_parts(["今日", "19:00"], now) == datetime(
        2026, 11, 15, 11, 0, tzinfo=timezone.utc
    )
    assert parse_hket_parts(["14:35", "2026/11/13"], now) == datetime(
        2026, 11, 13, 6, 35, tzinfo=timezone.utc
    )


def test_hket_page_keeps_dated_cards_older_than_a_day():
    reference = datetime(2026, 11, 15, 12, 42, tzinfo=timezone.utc)
    stories = [
        {
            "title": "Today",
            "external_url": "https://inews.hket.com/article/1",
            "posted": datetime(2026, 11, 15, 11, 0, tzinfo=timezone.utc),
        },
        {
            "title": "Three days ago",
            "external_url": "https://inews.hket.com/article/2",
            "posted": datetime(2026, 11, 12, 8, 0, tzinfo=timezone.utc),
        },
        {
            "title": "No time",
            "external_url": "https://inews.hket.com/article/3",
        },
        {
            "title": "Today",
            "external_url": "https://inews.hket.com/article/1",
            "posted": datetime(2026, 11, 15, 11, 0, tzinfo=timezone.utc),
        },
    ]
    kept = keep_hket_page(stories, reference, limit=30)
    assert [story["title"] for story in kept] == ["Today", "Three days ago"]
    assert kept[1]["age_text"].endswith("ago")


def test_publication_day_follows_the_date_on_the_article():
    early_hong_kong = parse_hket_time("2026-11-14", "01:30")
    assert early_hong_kong == datetime(2026, 11, 13, 17, 30, tzinfo=timezone.utc)
    assert publication_day("hket", early_hong_kong) == "2026-11-14"


def test_save_on_publication_dates_leaves_other_sources(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "hn.db")
    db.init_db()
    hn = {
        "title": "Front page",
        "external_url": "https://example.com/hn",
        "hn_url": "https://news.ycombinator.com/item?id=1",
        "points": 10,
        "comments": 2,
        "summary_en": "English.",
        "summary_zh": "中文。",
        "raw_content": "body",
    }
    db.replace_day("2026-11-15", [hn], source="hn")
    misplaced = {
        **hn,
        "title": "Printed on the 14th",
        "external_url": "https://inews.hket.com/article/1",
        "hn_url": "https://inews.hket.com/article/1",
        "points": 0,
        "comments": 0,
    }
    db.replace_day("2026-11-15", [misplaced], source="hket")
    moved = {**misplaced, "date": "2026-11-14"}
    dates = db.save_on_publication_dates("hket", [moved])
    assert dates == ["2026-11-14"]
    assert [story["title"] for story in db.stories_for("2026-11-14", ["hket"])] == [
        "Printed on the 14th"
    ]
    assert db.stories_for("2026-11-15", ["hket"]) == []
    assert db.stories_for("2026-11-15", ["hn"])[0]["title"] == "Front page"


def test_age_text():
    posted = datetime(2026, 11, 15, 9, 0, tzinfo=timezone.utc)
    reference = datetime(2026, 11, 15, 12, 30, tzinfo=timezone.utc)
    assert age_text_as_of(posted, reference) == "3 hours ago"


def test_points_and_comments():
    assert parse_points("1276 points") == 1276
    assert parse_points("") == 0
    assert parse_comments("862 comments") == 862
    assert parse_comments("discuss") == 0


def test_parse_model_array_from_fenced_object():
    raw = """```json
    {"stories": [{"title": "One", "summary_en": "A.", "summary_zh": "甲。"}]}
    ```"""
    rows = parse_model_array(raw)
    assert rows[0]["title"] == "One"


def test_replace_day_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "hn.db")
    db.init_db()
    story = {
        "title": "Signal",
        "external_url": "https://example.com/a",
        "hn_url": "https://news.ycombinator.com/item?id=1",
        "points": 10,
        "comments": 2,
        "summary_en": "English.",
        "summary_zh": "中文。",
        "raw_content": "body",
    }
    db.replace_day("2026-11-15", [story], source="hn")
    db.replace_day(
        "2026-11-15",
        [{**story, "title": "HKET story", "external_url": "https://example.com/a"}],
        source="hket",
    )
    assert len(db.stories_for("2026-11-15")) == 2
    only_hn = db.stories_for("2026-11-15", ["hn"])
    assert only_hn[0]["summary_zh"] == "中文。"
    assert only_hn[0]["source"] == "hn"
    assert db.list_days()[0]["story_count"] == 2
