"""SQLite storage for daily Hacker News signals."""

from __future__ import annotations

import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from app.sources import SOURCE_IDS

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "hn.db"

_lock = threading.Lock()


def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _lock:
        conn = connect()
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS stories (
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
                    title_en TEXT NOT NULL DEFAULT '',
                    UNIQUE(date, source, title, external_url)
                );

                CREATE TABLE IF NOT EXISTS days (
                    date TEXT PRIMARY KEY,
                    note TEXT NOT NULL DEFAULT '',
                    story_count INTEGER NOT NULL DEFAULT 0,
                    scraped_at TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            _migrate_source_column(conn)
            _migrate_title_en(conn)
            conn.commit()
        finally:
            conn.close()


def _migrate_source_column(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(stories)")}
    if "source" not in columns:
        conn.execute("ALTER TABLE stories ADD COLUMN source TEXT NOT NULL DEFAULT 'hn'")
    create_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'stories'"
    ).fetchone()[0]
    unique_clause = create_sql.split("UNIQUE", 1)[-1] if "UNIQUE" in create_sql else ""
    if "source" in unique_clause:
        return
    conn.executescript(
        """
        CREATE TABLE stories_new (
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
        );
        INSERT INTO stories_new (
            id, date, source, title, external_url, hn_url, points, comments,
            summary_en, summary_zh, raw_content
        )
        SELECT
            id, date, COALESCE(source, 'hn'), title, external_url, hn_url,
            points, comments, summary_en, summary_zh, raw_content
        FROM stories;
        DROP TABLE stories;
        ALTER TABLE stories_new RENAME TO stories;
        """
    )


def _migrate_title_en(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(stories)")}
    if "title_en" not in columns:
        conn.execute("ALTER TABLE stories ADD COLUMN title_en TEXT NOT NULL DEFAULT ''")


def _refresh_english_titles() -> None:
    """Fill title_en for rows just saved. Failures leave the scraped title in place."""
    try:
        from app.titles import backfill_english_titles

        backfill_english_titles()
    except Exception as exc:
        print(f"English headline backfill failed: {exc}", flush=True)


def meta_get(key: str, default: str = "") -> str:
    conn = connect()
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default
    finally:
        conn.close()


def meta_set(key: str, value: str) -> None:
    with _lock:
        conn = connect()
        try:
            conn.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            conn.commit()
        finally:
            conn.close()


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def acquire_run_lock() -> bool:
    """Single-process scrape lock. A dead owner is replaced."""
    init_db()
    with _lock:
        conn = connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT value FROM meta WHERE key = 'scrape_pid'"
            ).fetchone()
            if row:
                try:
                    owner = int(row["value"])
                except ValueError:
                    owner = 0
                if owner and _pid_alive(owner) and owner != os.getpid():
                    conn.rollback()
                    return False
            conn.execute(
                "INSERT INTO meta(key, value) VALUES('scrape_pid', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(os.getpid()),),
            )
            conn.commit()
            return True
        finally:
            conn.close()


def release_run_lock() -> None:
    if meta_get("scrape_pid") == str(os.getpid()):
        meta_set("scrape_pid", "")


def _story_row(day: str, source: str, story: dict) -> tuple:
    return (
        day,
        source,
        story["title"],
        story.get("external_url") or "",
        story.get("hn_url") or "",
        int(story.get("points") or 0),
        int(story.get("comments") or 0),
        story.get("summary_en") or "",
        story.get("summary_zh") or "",
        story.get("raw_content") or "",
        story.get("title_en") or "",
    )


def _recount_day(conn: sqlite3.Connection, day: str, scraped_at: str) -> None:
    total = conn.execute(
        "SELECT COUNT(*) AS n FROM stories WHERE date = ?",
        (day,),
    ).fetchone()["n"]
    conn.execute(
        """
        INSERT INTO days(date, note, story_count, scraped_at)
        VALUES(?, ?, ?, ?)
        ON CONFLICT(date) DO UPDATE SET
            note = excluded.note,
            story_count = excluded.story_count,
            scraped_at = excluded.scraped_at
        """,
        (day, "" if total else "empty", total, scraped_at),
    )


def replace_day(day: str, stories: list[dict], source: str = "hn") -> None:
    """Replace one source's rows for a day and keep the other sources."""
    scraped_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _lock:
        conn = connect()
        try:
            conn.execute("BEGIN")
            conn.execute(
                "DELETE FROM stories WHERE date = ? AND source = ?",
                (day, source),
            )
            conn.executemany(
                """
                INSERT INTO stories (
                    date, source, title, external_url, hn_url, points, comments,
                    summary_en, summary_zh, raw_content, title_en
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [_story_row(day, source, story) for story in stories],
            )
            _recount_day(conn, day, scraped_at)
            conn.commit()
        finally:
            conn.close()
    _refresh_english_titles()


def save_on_publication_dates(source: str, stories: list[dict]) -> list[str]:
    """Store each story on story['date'] and drop this source's older copy of the same URL."""
    scraped_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    by_day: dict[str, list[dict]] = {}
    urls: list[str] = []
    for story in stories:
        day = story["date"]
        by_day.setdefault(day, []).append(story)
        for key in (story.get("external_url") or "", story.get("hn_url") or ""):
            if key:
                urls.append(key)
    with _lock:
        conn = connect()
        try:
            conn.execute("BEGIN")
            old_dates: set[str] = set()
            if urls:
                placeholders = ", ".join("?" for _ in urls)
                rows = conn.execute(
                    f"""
                    SELECT DISTINCT date FROM stories
                    WHERE source = ?
                      AND (external_url IN ({placeholders}) OR hn_url IN ({placeholders}))
                    """,
                    [source, *urls, *urls],
                ).fetchall()
                old_dates = {row["date"] for row in rows}
                conn.execute(
                    f"""
                    DELETE FROM stories
                    WHERE source = ?
                      AND (external_url IN ({placeholders}) OR hn_url IN ({placeholders}))
                    """,
                    [source, *urls, *urls],
                )
            for day in by_day:
                conn.execute(
                    "DELETE FROM stories WHERE date = ? AND source = ?",
                    (day, source),
                )
            conn.executemany(
                """
                INSERT INTO stories (
                    date, source, title, external_url, hn_url, points, comments,
                    summary_en, summary_zh, raw_content, title_en
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    _story_row(day, source, story)
                    for day, group in by_day.items()
                    for story in group
                ],
            )
            for day in sorted(old_dates | set(by_day)):
                _recount_day(conn, day, scraped_at)
            conn.commit()
        finally:
            conn.close()
    _refresh_english_titles()
    return sorted(by_day)


def move_story_dates(updates: list[tuple[int, str]]) -> list[str]:
    """Point existing rows at a new day. Each item is (story id, YYYY-MM-DD)."""
    if not updates:
        return []
    scraped_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _lock:
        conn = connect()
        try:
            conn.execute("BEGIN")
            ids = [story_id for story_id, _day in updates]
            placeholders = ", ".join("?" for _ in ids)
            old_dates = {
                row["date"]
                for row in conn.execute(
                    f"SELECT DISTINCT date FROM stories WHERE id IN ({placeholders})",
                    ids,
                ).fetchall()
            }
            conn.executemany(
                "UPDATE stories SET date = ? WHERE id = ?",
                [(day, story_id) for story_id, day in updates],
            )
            touched = old_dates | {day for _story_id, day in updates}
            for day in sorted(touched):
                _recount_day(conn, day, scraped_at)
            conn.commit()
        finally:
            conn.close()
    return sorted(touched)


def list_days() -> list[dict]:
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT date, note, story_count, scraped_at FROM days ORDER BY date DESC"
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def stories_for(day: str, sources: list[str] | None = None) -> list[dict]:
    if sources is not None and not sources:
        return []
    sql = """
        SELECT date, source, title, title_en, external_url, hn_url, points, comments,
               summary_en, summary_zh, raw_content
        FROM stories
        WHERE date = ?
    """
    params: list[str] = [day]
    if sources is not None:
        placeholders = ", ".join("?" for _ in sources)
        sql += f" AND source IN ({placeholders})"
        params.extend(sources)
    rank = " ".join(
        f"WHEN '{source_id}' THEN {index}" for index, source_id in enumerate(SOURCE_IDS)
    )
    sql += f" ORDER BY CASE source {rank} ELSE 99 END, points DESC, comments DESC, title COLLATE NOCASE"
    conn = connect()
    try:
        rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def day_row(day: str) -> dict | None:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT date, note, story_count, scraped_at FROM days WHERE date = ?",
            (day,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def count_english_headlines() -> int:
    """Rows that can show an English headline: stored title_en, or an English title."""
    from app.titles import contains_cjk

    conn = connect()
    try:
        rows = conn.execute("SELECT title, title_en FROM stories").fetchall()
    finally:
        conn.close()
    ready = 0
    for row in rows:
        english = (row["title_en"] or "").strip()
        title = row["title"] or ""
        if (english and not contains_cjk(english)) or not contains_cjk(title):
            ready += 1
    return ready


def has_completed_preload() -> bool:
    return meta_get("preload_complete") == "1"


def completed_dates() -> set[str]:
    return {row["date"] for row in list_days()}
