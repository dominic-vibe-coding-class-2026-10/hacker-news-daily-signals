"""The four front pages this tracker reads. HKET is first."""

from __future__ import annotations

SOURCES = (
    {
        "id": "hket",
        "label_en": "HKET",
        "label_zh": "香港經濟日報",
        "url": "https://inews.hket.com/sran010/%E7%A7%91%E6%8A%80",
    },
    {
        "id": "hn",
        "label_en": "Hacker News",
        "label_zh": "Hacker News",
        "url": "https://news.ycombinator.com/",
    },
    {
        "id": "lobsters",
        "label_en": "Lobsters",
        "label_zh": "Lobsters",
        "url": "https://lobste.rs/",
    },
    {
        "id": "slashdot",
        "label_en": "Slashdot",
        "label_zh": "Slashdot",
        "url": "https://slashdot.org/",
    },
)

SOURCE_IDS = tuple(source["id"] for source in SOURCES)
SOURCE_LABELS = {source["id"]: source["label_en"] for source in SOURCES}
