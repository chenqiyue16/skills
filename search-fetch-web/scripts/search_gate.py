#!/usr/bin/env python3
"""Search-layer gating helper for SearXNG/Baidu/Playwright escalation with profile awareness."""

from __future__ import annotations

import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import research_manifest  # type: ignore
import scheduler  # type: ignore

ENGINE_DOMAIN = {
    "searxng": "searxng",
    "baidu": "baidu.com",
    "playwright": "searxng"  # Playwright escalation reuses searxng slot (no independent quota)
}

CHINESE_COMMUNITY_HINTS = [
    "taptap",
    "贴吧",
    "tieba",
    "nga",
    "微博",
    "weibo",
    "小红书",
    "xiaohongshu",
    "知乎",
    "zhihu",
    "b站",
    "bilibili",
    "抖音",
    "douyin",
]

BROWSER_CONTENT_ONLY_HINTS = [
    "贴吧",
    "tieba",
    "知乎",
    "zhihu",
    "b站",
    "bilibili",
    "抖音",
    "douyin",
    "小红书",
    "xiaohongshu",
    "taptap",
]

SEARCH_ONLY_HINTS = [
    "官网",
    "文档",
    "教程",
    "新闻",
    "资料",
    "介绍",
    "是什么",
    "how to",
    "docs",
    "documentation",
    "official site",
]


def infer_risk_level(query: str, risk_level: str, need_interaction: bool) -> tuple[str, bool]:
    if risk_level != "medium" or need_interaction:
        return risk_level, need_interaction

    lower = query.lower()
    if any(hint in lower for hint in CHINESE_COMMUNITY_HINTS):
        return "high", True

    return risk_level, need_interaction


def _profile_required_search(profile: str | None) -> list[str]:
    if not profile:
        return []
    config = research_manifest.load_profile(profile)
    return config.get("search_order", [])


def decide(engine: str, query: str, risk_level: str = "medium", need_interaction: bool = False, profile: str | None = None) -> dict:
    engine = engine.lower()
    if engine not in ENGINE_DOMAIN:
        raise ValueError(f"unsupported engine: {engine}")

    risk_level, need_interaction = infer_risk_level(query, risk_level, need_interaction)
    lower = query.lower()
    browser_content_only = any(hint in lower for hint in BROWSER_CONTENT_ONLY_HINTS)
    search_only_intent = any(hint in lower for hint in SEARCH_ONLY_HINTS)
    domain = ENGINE_DOMAIN[engine]
    decision = scheduler.schedule(domain, f"search:{engine}")
    profile_order = _profile_required_search(profile)

    recommended_next = None
    if engine == "searxng":
        if not decision["allowed"]:
            recommended_next = "baidu"
        elif profile_order and "baidu" in profile_order and browser_content_only and not search_only_intent:
            recommended_next = "baidu"
    elif engine == "baidu":
        if not decision["allowed"]:
            recommended_next = "playwright"
        elif profile_order and "playwright" in profile_order and browser_content_only and not search_only_intent:
            recommended_next = "playwright"
        elif risk_level == "high" and need_interaction and browser_content_only and not search_only_intent:
            recommended_next = "playwright"

    return {
        "engine": engine,
        "query": query,
        "risk_level": risk_level,
        "need_interaction": need_interaction,
        "profile": profile,
        "decision": decision,
        "recommended_next": recommended_next,
        "browser_content_only": browser_content_only,
        "search_only_intent": search_only_intent,
    }


if __name__ == "__main__":
    if len(sys.argv) < 3:
        raise SystemExit("usage: search_gate.py <engine> <query> [risk_level] [need_interaction] [profile]")

    engine = sys.argv[1]
    query = sys.argv[2]
    risk_level = sys.argv[3] if len(sys.argv) > 3 else "medium"
    need_interaction = len(sys.argv) > 4 and sys.argv[4].lower() in {"1", "true", "yes"}
    profile = sys.argv[5] if len(sys.argv) > 5 else None
    print(json.dumps(decide(engine, query, risk_level, need_interaction, profile=profile), ensure_ascii=False, indent=2))
