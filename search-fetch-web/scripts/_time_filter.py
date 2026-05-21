#!/usr/bin/env python3
"""Time filtering utilities for platform samplers.

Parses Chinese relative time expressions from scraped text and provides
within-range checking. Used by the CLI layer to post-filter results when
--within is specified (e.g. --within 24h, --within 7d).
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

TZ_CN = timezone(timedelta(hours=8))


def parse_within_arg(within_str: str | None) -> timedelta | None:
    """Parse --within argument like '24h', '7d', '30m', '1w'.

    Returns None if within_str is None or empty (no filtering).
    Raises ValueError if the format is invalid.
    """
    if not within_str:
        return None
    m = re.fullmatch(r'(\d+)([mhdw])', within_str.strip().lower())
    if not m:
        raise ValueError(f"Invalid --within format: '{within_str}'. Use e.g. 30m, 24h, 7d, 1w")
    value = int(m.group(1))
    unit = m.group(2)
    if unit == 'm':
        return timedelta(minutes=value)
    if unit == 'h':
        return timedelta(hours=value)
    if unit == 'd':
        return timedelta(days=value)
    if unit == 'w':
        return timedelta(weeks=value)
    return None


def parse_chinese_relative_time(text: str, now: datetime | None = None) -> datetime | None:
    """Parse a Chinese relative time expression to a datetime.

    Handles: 刚刚, 半小时前, X分钟前, X小时前, X天前, X周前, X个月前, X年前,
             昨天, 今天, 前天, dates like 2024-01-15, X月X日/号.
    Returns None if no recognizable pattern is found.
    """
    if not text:
        return None
    if now is None:
        now = datetime.now(TZ_CN)

    s = text.strip()

    # Exact / fixed matches
    if s in ("刚刚", "刚刚发布"):
        return now
    if "半小时前" in s:
        return now - timedelta(minutes=30)
    if "昨天" in s:
        return now - timedelta(days=1)
    if "前天" in s:
        return now - timedelta(days=2)
    if "今天" in s:
        return now.replace(hour=0, minute=0, second=0, microsecond=0)

    # Relative: X单位前
    for pattern, factory in [
        (r'(\d+)\s*分钟前', lambda v: timedelta(minutes=v)),
        (r'(\d+)\s*小时前', lambda v: timedelta(hours=v)),
        (r'(\d+)\s*天前', lambda v: timedelta(days=v)),
        (r'(\d+)\s*周前', lambda v: timedelta(weeks=v)),
        (r'(\d+)\s*个?月前', lambda v: timedelta(days=v * 30)),
        (r'(\d+)\s*年前', lambda v: timedelta(days=v * 365)),
    ]:
        m = re.search(pattern, s)
        if m:
            return now - factory(int(m.group(1)))

    # Absolute date: 2024-01-15 or 2024/01/15
    m = re.search(r'(\d{4})[/-](\d{1,2})[/-](\d{1,2})', s)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=TZ_CN)
        except ValueError:
            return None

    # X月X日 / X月X号 (assume current year, previous if future)
    m = re.search(r'(\d{1,2})月(\d{1,2})[日号]', s)
    if m:
        try:
            dt = datetime(now.year, int(m.group(1)), int(m.group(2)), tzinfo=TZ_CN)
            if dt > now:
                dt = datetime(now.year - 1, int(m.group(1)), int(m.group(2)), tzinfo=TZ_CN)
            return dt
        except ValueError:
            return None

    return None


def find_timestamp_in_text(text: str, now: datetime | None = None) -> tuple[datetime | None, str | None]:
    """Find the first parseable timestamp in a block of text.

    Returns (datetime, matched_text) or (None, None).
    """
    if not text:
        return None, None
    if now is None:
        now = datetime.now(TZ_CN)

    # Try line-by-line first (more precise when each line is a standalone field)
    for line in text.split('\n'):
        line = line.strip()
        if not line:
            continue
        dt = parse_chinese_relative_time(line, now=now)
        if dt is not None:
            return dt, line

    # Try inline patterns in longer text
    patterns = [
        r'刚刚(?:发布)?',
        r'半小时前',
        r'\d+\s*分钟前',
        r'\d+\s*小时前',
        r'\d+\s*天前',
        r'\d+\s*周前',
        r'\d+\s*个?月前',
        r'\d+\s*年前',
        r'昨天',
        r'前天',
        r'\d{4}[/-]\d{1,2}[/-]\d{1,2}',
        r'\d{1,2}月\d{1,2}[日号]',
    ]
    for pattern in patterns:
        m = re.search(pattern, text)
        if m:
            dt = parse_chinese_relative_time(m.group(0), now=now)
            if dt is not None:
                return dt, m.group(0)

    return None, None


def is_within_time(text: str, within: timedelta, now: datetime | None = None) -> tuple[bool, str | None]:
    """Check if any timestamp in text is within the given time range.

    Returns (is_within, matched_time_text).
    If no timestamp found, returns (True, None) — conservative: keep the item.
    """
    if not text or within is None:
        return True, None
    if now is None:
        now = datetime.now(TZ_CN)

    dt, matched = find_timestamp_in_text(text, now=now)
    if dt is None:
        return True, None
    cutoff = now - within
    return dt >= cutoff, matched


def filter_items_by_time(
    items: list[dict],
    within: timedelta,
    text_keys: list[str] | None = None,
    now: datetime | None = None,
) -> list[dict]:
    """Filter items by time range based on timestamps found in their text fields.

    text_keys: which dict keys to scan for timestamps (default: ["main_text"]).
    Items with no detectable timestamp are kept (conservative).
    Adds _time_within (bool) and _time_matched (str|None) to each kept item.
    """
    if within is None or not items:
        return items
    if text_keys is None:
        text_keys = ["main_text"]
    if now is None:
        now = datetime.now(TZ_CN)

    filtered: list[dict] = []
    for item in items:
        texts: list[str] = []
        for key in text_keys:
            val = item.get(key, "")
            if val:
                texts.append(str(val))
        # Also check comments list
        comments = item.get("comments") or []
        if isinstance(comments, list):
            for c in comments:
                if isinstance(c, str):
                    texts.append(c)
                elif isinstance(c, dict):
                    msg = c.get("message", "")
                    if msg:
                        texts.append(msg)
        # Also check candidate parent text (for tieba etc.)
        candidate = item.get("candidate")
        if isinstance(candidate, dict):
            for ck in ("parent", "text"):
                cv = candidate.get(ck, "")
                if cv:
                    texts.append(str(cv))

        combined = "\n".join(texts)
        ok, matched = is_within_time(combined, within, now=now)
        item["_time_within"] = ok
        item["_time_matched"] = matched
        if ok:
            filtered.append(item)
    return filtered


def filter_bili_comments_by_ctime(
    comments: list[dict],
    within: timedelta,
    now: datetime | None = None,
) -> list[dict]:
    """Filter B站 comments by their ctime (Unix timestamp) field.

    Each comment dict must have a 'ctime' key with a Unix timestamp (seconds).
    """
    if within is None or not comments:
        return comments
    if now is None:
        now = datetime.now(TZ_CN)
    cutoff = now - within
    cutoff_ts = cutoff.timestamp()
    filtered: list[dict] = []
    for c in comments:
        ctime = c.get("ctime")
        if ctime is None:
            c["_time_within"] = True  # keep if no timestamp
            filtered.append(c)
            continue
        try:
            ts = float(ctime)
        except (TypeError, ValueError):
            c["_time_within"] = True
            filtered.append(c)
            continue
        ok = ts >= cutoff_ts
        c["_time_within"] = ok
        if ok:
            filtered.append(c)
    return filtered


def bili_video_within_pubdate(
    video_info: dict,
    within: timedelta,
    now: datetime | None = None,
) -> bool:
    """Check if a B站 video's pubdate is within the time range.

    Returns True if within range or if pubdate is not available (conservative).
    """
    pubdate = video_info.get("pubdate")
    if pubdate is None:
        return True
    if now is None:
        now = datetime.now(TZ_CN)
    cutoff = now - within
    try:
        return float(pubdate) >= cutoff.timestamp()
    except (TypeError, ValueError):
        return True
