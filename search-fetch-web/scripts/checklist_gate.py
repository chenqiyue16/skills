#!/usr/bin/env python3
"""Fail-closed checklist gate for search-fetch platform runs."""

from __future__ import annotations

from typing import Any

import scheduler  # type: ignore


DEFAULT_CHECKLIST = [
    "discovery_layer_complete",
    "detail_layer_entered",
    "not_stuck_in_list_or_search",
    "usable_content_captured",
    "sample_evidence_valid",
    "quality_gate_passed",
]

BROWSER_REQUIRED_DOMAINS = {
    "bilibili.com",
    "taptap.cn",
    "tieba.baidu.com",
    "xiaohongshu.com",
    "douyin.com",
    "zhihu.com",
}

# Unified thresholds — must match validate_research_run.py expectations
MIN_BILIBILI_VIDEOS = 5
MIN_DOUYIN_MODALS = 2
MIN_TIEBA_THREADS = 5


def required_items_for_domain(domain: str) -> list[str]:
    policy = scheduler.resolve_policy(domain)
    return list(policy.get("required_checklist") or DEFAULT_CHECKLIST)


def evaluate(
    domain: str,
    checklist: dict[str, Any] | None,
    flow_evidence: dict[str, Any] | None = None,
    fetch_backend: str | None = None,
) -> dict[str, Any]:
    required = required_items_for_domain(domain)
    given = checklist or {}
    flow = flow_evidence or {}
    if domain == "bilibili.com" and fetch_backend == "playwright":
        required = [item for item in required if item != "cookie_checked"]
    missing = [item for item in required if not bool(given.get(item))]
    evidence_errors: list[str] = []

    if domain in BROWSER_REQUIRED_DOMAINS and fetch_backend != "playwright":
        evidence_errors.append(f"fetch_backend_not_browser:{fetch_backend or 'missing'}")

    if domain == "bilibili.com":
        opened_video_count = int(flow.get("opened_video_count") or 0)
        opened_bvids = flow.get("opened_bvids") or []
        comment_videos = flow.get("api_comment_videos") or flow.get("visible_comment_videos") or []
        if opened_video_count < MIN_BILIBILI_VIDEOS:
            evidence_errors.append(f"opened_video_count<{MIN_BILIBILI_VIDEOS}")
        if len(opened_bvids) < MIN_BILIBILI_VIDEOS:
            evidence_errors.append(f"opened_bvids<{MIN_BILIBILI_VIDEOS}")
        if len(comment_videos) < 1:
            evidence_errors.append(f"comment_videos<1")

    if domain == "douyin.com":
        opened_modal_count = int(flow.get("opened_modal_count") or 0)
        opened_modal_urls = flow.get("opened_modal_urls") or []
        if opened_modal_count < MIN_DOUYIN_MODALS:
            evidence_errors.append(f"opened_modal_count<{MIN_DOUYIN_MODALS}")
        if len(opened_modal_urls) < MIN_DOUYIN_MODALS:
            evidence_errors.append(f"opened_modal_urls<{MIN_DOUYIN_MODALS}")

    if domain == "tieba.baidu.com":
        thread_detail_count = int(flow.get("thread_detail_count") or 0)
        if thread_detail_count < MIN_TIEBA_THREADS:
            evidence_errors.append(f"thread_detail_count<{MIN_TIEBA_THREADS}")

    return {
        "required": required,
        "checklist": given,
        "flow_evidence": flow,
        "fetch_backend": fetch_backend,
        "passed": (not missing and not evidence_errors),
        "missing": missing,
        "evidence_errors": evidence_errors,
    }
