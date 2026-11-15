"""Ollama Cloud filter: last 24 hours, plus bilingual summaries."""

from __future__ import annotations

import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

SYSTEM_PROMPT = (
    "You are a strict filter. Given a list of Hacker News stories with their "
    "age text, return only those that are 24 hours old or newer. Output valid "
    "JSON array of objects with keys: title, external_url, points, "
    "comment_count, summary_en (max 2 sentences in English), summary_zh "
    "(max 2 sentences in Traditional Chinese). If a story is older than 24 "
    "hours, exclude it completely. Do not add any story that was not in the "
    "input."
)

OLLAMA_CHAT_URL = "https://ollama.com/api/chat"
BATCH_SIZE = 4


def cloud_api_key() -> str:
    return (
        os.environ.get("OLLAMA_CLOUD_API_KEY", "").strip()
        or os.environ.get("OLLAMA_API_KEY", "").strip()
    )


def cloud_model() -> str:
    return os.environ.get("OLLAMA_MODEL", "").strip() or "llama3.1:8b"


def parse_model_array(text: str) -> list[dict]:
    cleaned = (text or "").strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    start_candidates = [index for index in (cleaned.find("["), cleaned.find("{")) if index >= 0]
    if not start_candidates:
        raise ValueError("model response did not contain JSON")
    start = min(start_candidates)
    end = cleaned.rfind("]" if cleaned[start] == "[" else "}")
    if end < start:
        raise ValueError("model response JSON was truncated")
    data = json.loads(cleaned[start : end + 1])
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        for key in ("stories", "items", "results", "data"):
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    raise ValueError("model response was not a JSON array")


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip()).casefold()


def _post_with_backoff(client: httpx.Client, payload: dict, headers: dict) -> httpx.Response:
    response: httpx.Response | None = None
    for attempt in range(4):
        response = client.post(OLLAMA_CHAT_URL, json=payload, headers=headers)
        if response.status_code not in {429, 500, 502, 503, 504}:
            return response
        time.sleep(2 ** attempt)
    assert response is not None
    return response


def _user_message(stories: list[dict]) -> str:
    payload = [
        {
            "title": story["title"],
            "external_url": story.get("external_url") or "",
            "points": int(story.get("points") or 0),
            "comment_count": int(story.get("comments") or 0),
            "age_text": story.get("age_text") or "",
            "content": (story.get("raw_content") or "")[:800],
        }
        for story in stories
    ]
    return (
        "Filter the stories below. age_text is measured from each story's "
        "submission time to the reference time for this digest. "
        "Copy title, external_url, points, and comment_count exactly from the "
        "input. summary_en and summary_zh must come only from that story's "
        "title and content. Return a JSON array and nothing else.\n\n"
        + json.dumps(payload, ensure_ascii=False)
    )


def _chat(stories: list[dict]) -> list[dict]:
    key = cloud_api_key()
    if not key:
        raise RuntimeError("Set OLLAMA_CLOUD_API_KEY or OLLAMA_API_KEY in .env")
    payload = {
        "model": cloud_model(),
        "stream": False,
        "think": False,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": _user_message(stories)},
        ],
        "options": {"temperature": 0, "num_predict": 1400},
    }
    headers = {"Authorization": f"Bearer {key}"}
    with httpx.Client(timeout=180) as client:
        response = _post_with_backoff(client, payload, headers)
        if response.status_code == 400 and "think" in response.text.lower():
            payload.pop("think", None)
            response = _post_with_backoff(client, payload, headers)
        if response.status_code == 401:
            raise RuntimeError("Ollama Cloud rejected the API key")
        if response.status_code >= 400:
            detail = response.text[:300].replace("\n", " ")
            raise RuntimeError(f"Ollama Cloud HTTP {response.status_code}: {detail}")
        body = response.json()
    content = ((body.get("message") or {}).get("content")) or ""
    return parse_model_array(content)


def _match_input(item: dict, stories: list[dict]) -> dict | None:
    title = _norm(str(item.get("title") or ""))
    url = (str(item.get("external_url") or "")).strip()
    for story in stories:
        if _norm(story["title"]) != title:
            continue
        if url and url != (story.get("external_url") or ""):
            continue
        return story
    for story in stories:
        if _norm(story["title"]) == title:
            return story
    return None


def _apply(stories: list[dict], items: list[dict]) -> list[dict]:
    kept: list[dict] = []
    seen: set[int] = set()
    for item in items:
        source = _match_input(item, stories)
        if source is None:
            continue
        marker = id(source)
        if marker in seen:
            continue
        seen.add(marker)
        summary_en = str(item.get("summary_en") or "").strip()
        summary_zh = str(item.get("summary_zh") or "").strip()
        if not summary_en or not summary_zh:
            continue
        kept.append(
            {
                **source,
                "points": int(source.get("points") or 0),
                "comments": int(source.get("comments") or 0),
                "external_url": source.get("external_url") or "",
                "summary_en": summary_en,
                "summary_zh": summary_zh,
            }
        )
    return kept


def _filter_batch(batch: list[dict]) -> list[dict]:
    try:
        items = _chat(batch)
    except (httpx.HTTPError, ValueError, json.JSONDecodeError, RuntimeError):
        if len(batch) == 1:
            raise
        midpoint = len(batch) // 2
        return _filter_batch(batch[:midpoint]) + _filter_batch(batch[midpoint:])
    matched = _apply(batch, items)
    if not matched and len(batch) > 1:
        midpoint = len(batch) // 2
        return _filter_batch(batch[:midpoint]) + _filter_batch(batch[midpoint:])
    matched_ids = {id(story) for story in matched}
    missing = [story for story in batch if id(story) not in matched_ids]
    if missing and len(missing) < len(batch):
        matched.extend(_filter_batch(missing))
    return matched


def filter_stories(stories: list[dict]) -> list[dict]:
    """Return model-kept stories, using scraped URLs and points only."""
    if not stories:
        return []
    batches = [stories[index : index + BATCH_SIZE] for index in range(0, len(stories), BATCH_SIZE)]
    kept: list[dict] = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(_filter_batch, batch) for batch in batches]
        for number, future in enumerate(futures, start=1):
            kept.extend(future.result())
            print(f"  summarized {number}/{len(futures)}", flush=True)
    return _dedupe(kept)


def _dedupe(stories: list[dict]) -> list[dict]:
    seen: set[tuple[str, str]] = set()
    unique: list[dict] = []
    for story in stories:
        key = (_norm(story["title"]), story.get("external_url") or "")
        if key in seen:
            continue
        seen.add(key)
        unique.append(story)
    return unique
