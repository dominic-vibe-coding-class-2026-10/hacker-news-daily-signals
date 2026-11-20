from datetime import date, datetime, timezone
from pathlib import Path

from app.other_sources import (
    keep_hket_page,
    parse_hket_parts,
    parse_hket_time,
    publication_day,
)

from app import db
from app.filter import parse_model_array
from app.main import stories as api_stories
from app.titles import (
    backfill_english_titles,
    contains_cjk,
    english_title,
    localized_headline,
    parse_translate_payload,
    tidy_english_headline,
)
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


def _use_tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "hn.db")
    db.init_db()


def _story(**overrides):
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
    story.update(overrides)
    return story


def test_english_title_leaves_english_text_and_tidies_translation():
    assert contains_cjk("蘋果發表新機")
    assert not contains_cjk("Apple announces a phone")
    assert english_title("Already English", translate=lambda text: "nope") == "Already English"
    translated = english_title("蘋果發表新機", translate=lambda text: "Apple|announces a phone")
    assert translated == "Apple | announces a phone"
    assert tidy_english_headline("  Meta｜rumor  ") == "Meta | rumor"


def test_parse_translate_payload_joins_chunks():
    payload = [
        [["AI agent|63% deployed", "src", None], [" today", "src"]],
        None,
        "zh-TW",
    ]
    assert parse_translate_payload(payload) == "AI agent|63% deployed today"
    try:
        parse_translate_payload({"stories": []})
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError")


def test_localized_headline_follows_display_language():
    story = {"title": "蘋果發表新機", "title_en": "Apple announces a phone"}
    assert localized_headline(story, "en") == "Apple announces a phone"
    assert localized_headline(story, "zh") == "蘋果發表新機"
    english = {"title": "SpaceX catches the Starship", "title_en": ""}
    assert localized_headline(english, "en") == "SpaceX catches the Starship"
    assert localized_headline(english, "zh") == "SpaceX catches the Starship"


def test_backfill_adds_english_headlines_without_replacing_rows(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path, monkeypatch)
    conn = db.connect()
    try:
        conn.execute(
            """
            INSERT INTO stories (
                date, source, title, external_url, hn_url, points, comments,
                summary_en, summary_zh, raw_content
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "2026-11-15",
                "hket",
                "蘋果發表新機",
                "https://inews.hket.com/article/9",
                "https://inews.hket.com/article/9",
                0,
                0,
                "Apple announced a new phone.",
                "蘋果發表了新機。",
                "body",
            ),
        )
        conn.execute(
            """
            INSERT INTO stories (
                date, source, title, external_url, hn_url, points, comments,
                summary_en, summary_zh, raw_content
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "2026-11-15",
                "hn",
                "SpaceX catches the Starship",
                "https://example.com/hn",
                "https://news.ycombinator.com/item?id=2",
                40,
                8,
                "SpaceX caught the rocket.",
                "SpaceX 接住了火箭。",
                "hn body",
            ),
        )
        conn.commit()
        before = [dict(row) for row in conn.execute("SELECT * FROM stories ORDER BY source")]
    finally:
        conn.close()

    calls = []

    def fake_translate(text):
        calls.append(text)
        return "Apple announces a phone"

    updated = backfill_english_titles(translate=fake_translate)
    assert updated == 1
    assert calls == ["蘋果發表新機"]
    assert backfill_english_titles(translate=fake_translate) == 0
    assert calls == ["蘋果發表新機"]

    conn = db.connect()
    try:
        after = [dict(row) for row in conn.execute("SELECT * FROM stories ORDER BY id")]
    finally:
        conn.close()
    assert len(after) == len(before) == 2
    hket = next(row for row in after if row["source"] == "hket")
    hn = next(row for row in after if row["source"] == "hn")
    original_hket = next(row for row in before if row["source"] == "hket")
    original_hn = next(row for row in before if row["source"] == "hn")
    assert hket["id"] == original_hket["id"]
    assert hket["title"] == "蘋果發表新機"
    assert hket["title_en"] == "Apple announces a phone"
    assert hket["summary_en"] == original_hket["summary_en"]
    assert hket["summary_zh"] == original_hket["summary_zh"]
    assert hket["points"] == 0
    assert hn == original_hn
    assert hn["title_en"] == ""
    assert db.count_english_headlines() == 2

    english = api_stories("2026-11-15", "hket,hn", "en")
    chinese = api_stories("2026-11-15", "hket,hn", "zh")
    en_hket = next(row for row in english["stories"] if row["source"] == "hket")
    zh_hket = next(row for row in chinese["stories"] if row["source"] == "hket")
    assert en_hket["headline"] == "Apple announces a phone"
    assert en_hket["title"] == "蘋果發表新機"
    assert zh_hket["headline"] == "蘋果發表新機"
    en_hn = next(row for row in english["stories"] if row["source"] == "hn")
    assert en_hn["headline"] == "SpaceX catches the Starship"


def test_title_en_migration_keeps_existing_rows(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "hn.db")
    conn = __import__("sqlite3").connect(tmp_path / "hn.db")
    conn.execute(
        """
        CREATE TABLE stories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'hn',
            title TEXT NOT NULL,
            external_url TEXT NOT NULL DEFAULT '',
            hn_url TEXT NOT NULL DEFAULT '',
            points INTEGER NOT NULL DEFAULT 0,
            comments INTEGER NOT NULL DEFAULT 0,
            summary_en TEXT NOT NULL DEFAULT '',
            summary_zh TEXT NOT NULL DEFAULT '',
            raw_content TEXT NOT NULL DEFAULT '',
            UNIQUE(date, source, title, external_url)
        )
        """
    )
    conn.execute(
        "INSERT INTO stories (date, source, title, summary_en, summary_zh) VALUES (?, ?, ?, ?, ?)",
        ("2026-11-14", "hket", "一週科技回顧", "A weekly review.", "一週回顧。"),
    )
    conn.commit()
    conn.close()
    db.init_db()
    rows = db.stories_for("2026-11-14", ["hket"])
    assert rows[0]["title"] == "一週科技回顧"
    assert rows[0]["title_en"] == ""
    assert rows[0]["summary_en"] == "A weekly review."


def test_new_chinese_story_gets_english_headline_and_failed_translation_keeps_row(
    tmp_path, monkeypatch
):
    _use_tmp_db(tmp_path, monkeypatch)

    def fake_translate(text, client=None):
        return "Tesla accessory opens the trunk"

    monkeypatch.setattr("app.titles.translate_zh_to_en", fake_translate)
    db.replace_day(
        "2026-11-20",
        [
            _story(
                title="特斯拉配件一踢開車尾箱",
                external_url="https://inews.hket.com/article/3",
                hn_url="https://inews.hket.com/article/3",
                points=0,
                comments=0,
            )
        ],
        source="hket",
    )
    stored = db.stories_for("2026-11-20", ["hket"])
    assert stored[0]["title"] == "特斯拉配件一踢開車尾箱"
    assert stored[0]["title_en"] == "Tesla accessory opens the trunk"
    assert api_stories("2026-11-20", "hket", "en")["stories"][0]["headline"] == (
        "Tesla accessory opens the trunk"
    )
    assert api_stories("2026-11-20", "hket", "zh")["stories"][0]["headline"] == (
        "特斯拉配件一踢開車尾箱"
    )

    def fail(text, client=None):
        raise RuntimeError("offline")

    monkeypatch.setattr("app.titles.translate_zh_to_en", fail)
    db.replace_day(
        "2026-11-20",
        [
            _story(
                title="網絡安全警示",
                external_url="https://inews.hket.com/article/4",
                hn_url="https://inews.hket.com/article/4",
                points=0,
                comments=0,
            )
        ],
        source="hket",
    )
    kept = db.stories_for("2026-11-20", ["hket"])
    assert len(kept) == 1
    assert kept[0]["title"] == "網絡安全警示"
    assert kept[0]["title_en"] == ""
    assert kept[0]["summary_zh"] == "中文。"


def test_headline_batches_stay_aligned_and_split_when_lines_collapse(monkeypatch):
    from app.titles import _google_lines

    calls = []

    def fake_raw(text, client):
        calls.append(text)
        if text == "蘋果\n特斯拉":
            return "Apple\nTesla"
        if "\n" in text:
            return "collapsed into one line"
        return "English headline"

    monkeypatch.setattr("app.titles._google_raw", fake_raw)
    aligned = _google_lines(["蘋果", "特斯拉"], client=object())
    assert aligned == ["Apple", "Tesla"]
    assert calls == ["蘋果\n特斯拉"]

    calls.clear()
    split = _google_lines(["甲", "乙"], client=object())
    assert split == ["English headline", "English headline"]
    assert calls[0] == "甲\n乙"
    assert "甲" in calls and "乙" in calls


def test_dashboard_requests_headline_for_the_active_language():
    html = Path(__file__).resolve().parent.parent.joinpath("templates", "dashboard.html").read_text(
        encoding="utf-8"
    )
    assert "lang: state.lang" in html
    assert "story.headline || story.title" in html
    assert "english_titles" in html
    assert "Dominic Yu" in html
    assert "dominic@data-science-hk.org" in html
