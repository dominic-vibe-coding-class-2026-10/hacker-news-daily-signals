"""English headlines for stories whose scraped title is not already English."""

from __future__ import annotations

import html
import re
import unicodedata
from collections.abc import Callable
from typing import Any

import httpx

# Ideographs only. Fullwidth punctuation is normalized after translation.
_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_PIPE = re.compile(r"\s*\|\s*")
_TRANSLATE_URL = "https://translate.googleapis.com/translate_a/single"
_MYMEMORY_URL = "https://api.mymemory.translated.net/get"
_BATCH = 20


def contains_cjk(text: str) -> bool:
    return bool(_CJK.search(text or ""))


def tidy_english_headline(text: str) -> str:
    """Collapse whitespace and give the HKET fullwidth bar a normal space."""
    cleaned = html.unescape(unicodedata.normalize("NFKC", text or ""))
    cleaned = _PIPE.sub(" | ", cleaned)
    cleaned = " ".join(cleaned.split()).strip()
    return cleaned.strip("|").strip()


def parse_translate_payload(payload: Any) -> str:
    if not isinstance(payload, list) or not payload or not isinstance(payload[0], list):
        raise ValueError("unexpected translation payload")
    parts: list[str] = []
    for chunk in payload[0]:
        if isinstance(chunk, list) and chunk and isinstance(chunk[0], str):
            parts.append(chunk[0])
    text = "".join(parts).strip()
    if not text:
        raise ValueError("empty translation")
    return text


def _google_raw(text: str, client: httpx.Client) -> str:
    headers = {"User-Agent": "Mozilla/5.0"}
    last_error: Exception | None = None
    for name in ("dict-chrome-ex", "gtx"):
        response = client.get(
            _TRANSLATE_URL,
            params={"client": name, "sl": "auto", "tl": "en", "dt": "t", "q": text},
            headers=headers,
        )
        if response.status_code in {429, 500, 502, 503, 504}:
            last_error = RuntimeError(f"HTTP {response.status_code}")
            continue
        try:
            response.raise_for_status()
            return parse_translate_payload(response.json())
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            last_error = exc
    raise RuntimeError(f"English title translation failed: {last_error}")


def _mymemory(text: str, client: httpx.Client) -> str:
    response = client.get(
        _MYMEMORY_URL,
        params={"q": text[:500], "langpair": "zh-TW|en"},
        headers={"User-Agent": "Mozilla/5.0"},
    )
    response.raise_for_status()
    data = response.json()
    translated = html.unescape(
        str((data.get("responseData") or {}).get("translatedText") or "")
    ).strip()
    if (
        not translated
        or "MYMEMORY WARNING" in translated.upper()
        or data.get("responseStatus") not in (200, "200")
    ):
        raise ValueError("MyMemory translation unavailable")
    return translated


def translate_zh_to_en(text: str, client: httpx.Client | None = None) -> str:
    """Translate one Traditional Chinese headline to English."""
    owns_client = client is None
    client = client or httpx.Client(timeout=30)
    try:
        try:
            return _google_raw(text, client)
        except (httpx.HTTPError, ValueError, TypeError, RuntimeError):
            return _mymemory(text, client)
    finally:
        if owns_client:
            client.close()


def _google_lines(texts: list[str], client: httpx.Client) -> list[str]:
    """Translate headlines together. A mismatched line count is split and retried."""
    if not texts:
        return []
    if len(texts) == 1:
        return [translate_zh_to_en(texts[0], client=client)]
    try:
        raw = _google_raw("\n".join(texts), client)
    except (httpx.HTTPError, ValueError, TypeError, RuntimeError):
        mid = len(texts) // 2
        return _google_lines(texts[:mid], client) + _google_lines(texts[mid:], client)
    lines = raw.split("\n")
    if len(lines) != len(texts):
        mid = len(texts) // 2
        return _google_lines(texts[:mid], client) + _google_lines(texts[mid:], client)
    return lines


def _titles_needing_english(rows: list[dict]) -> list[str]:
    needed: list[str] = []
    seen: set[str] = set()
    for row in rows:
        raw_title = row.get("title") or ""
        current = " ".join(str(row.get("title_en") or "").split())
        if current and not contains_cjk(current):
            continue
        if not contains_cjk(raw_title):
            continue
        key = " ".join(raw_title.split())
        if key in seen:
            continue
        seen.add(key)
        needed.append(key)
    return needed


def _translate_unique_titles(titles: list[str]) -> dict[str, str]:
    if not titles:
        return {}
    found: dict[str, str] = {}
    with httpx.Client(timeout=30) as client:
        for start in range(0, len(titles), _BATCH):
            chunk = titles[start : start + _BATCH]
            try:
                lines = _google_lines(chunk, client)
            except Exception:
                lines = []
            if len(lines) != len(chunk):
                lines = [""] * len(chunk)
            for title, line in zip(chunk, lines):
                try:
                    found[title] = english_title(title, translate=lambda _text, line=line: line)
                except Exception:
                    try:
                        found[title] = english_title(
                            title,
                            translate=lambda text, client=client: translate_zh_to_en(
                                text, client=client
                            ),
                        )
                    except Exception:
                        continue
    return found


def english_title(title: str, translate: Callable[[str], str] | None = None) -> str:
    """Return an English headline. English titles pass through unchanged."""
    cleaned = " ".join((title or "").split())
    if not cleaned:
        return ""
    if not contains_cjk(cleaned):
        return cleaned
    translator = translate or translate_zh_to_en
    translated = tidy_english_headline(translator(cleaned))
    if not translated or contains_cjk(translated) or translated.casefold() == cleaned.casefold():
        raise ValueError("translation was not an English headline")
    return translated


def localized_headline(story: dict, lang: str) -> str:
    """Headline for the dashboard. Chinese keeps the scraped title."""
    original = str(story.get("title") or "")
    if str(lang or "").lower().startswith("zh"):
        return original
    english = tidy_english_headline(str(story.get("title_en") or ""))
    if english and not contains_cjk(english):
        return english
    if not contains_cjk(original):
        return original
    return original


def backfill_english_titles(translate: Callable[[str], str] | None = None) -> int:
    """Set title_en on stored rows whose title is Chinese. Other columns stay put."""
    from app.db import _lock, connect, init_db

    init_db()
    conn = connect()
    try:
        rows = [
            dict(row)
            for row in conn.execute("SELECT id, title, title_en FROM stories")
        ]
    finally:
        conn.close()

    if translate is not None:
        return _write_english_titles(rows, translate)
    prepared: dict[str, str] = {}
    # Tests replace translate_zh_to_en. Skip the network batch in that case.
    if getattr(translate_zh_to_en, "__module__", "") == __name__:
        prepared = _translate_unique_titles(_titles_needing_english(rows))

    def translator(text: str) -> str:
        cached = prepared.get(text)
        if cached:
            return cached
        return translate_zh_to_en(text)

    return _write_english_titles(rows, translator)


def _write_english_titles(
    rows: list[dict], translate: Callable[[str], str]
) -> int:
    from app.db import _lock, connect

    updates: list[tuple[str, int, str]] = []
    cache: dict[str, str] = {}
    missed = 0
    for row in rows:
        raw_title = row["title"] or ""
        current = " ".join(str(row["title_en"] or "").split())
        if current and not contains_cjk(current):
            continue
        if not contains_cjk(raw_title):
            continue
        key = " ".join(raw_title.split())
        if key not in cache:
            try:
                cache[key] = english_title(key, translate=translate)
            except Exception:
                cache[key] = ""
        desired = cache[key]
        if not desired:
            missed += 1
            continue
        if desired != current:
            updates.append((desired, int(row["id"]), raw_title))

    if updates:
        with _lock:
            conn = connect()
            try:
                conn.execute("BEGIN")
                conn.executemany(
                    "UPDATE stories SET title_en = ? WHERE id = ? AND title = ?",
                    updates,
                )
                conn.commit()
            finally:
                conn.close()
    if missed:
        print(f"English headlines missing for {missed} stories", flush=True)
    return len(updates)


if __name__ == "__main__":
    updated = backfill_english_titles()
    print(f"Updated {updated} English headlines", flush=True)
