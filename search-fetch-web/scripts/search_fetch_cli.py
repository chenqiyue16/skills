#!/usr/bin/env python3
"""Unified JSON-first CLI for search-fetch workflows."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.parse
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import bilibili_comment_fetch  # type: ignore
import bilibili_cookie_refresh  # type: ignore
import douyin_sampler  # type: ignore
import guarded_search_fetch  # type: ignore
import research_manifest  # type: ignore
import playwright_fetch  # type: ignore
import taobao_sampler  # type: ignore
import tmall_sampler  # type: ignore
import jd_sampler  # type: ignore
import taptap_playwright_fetch  # type: ignore
import tieba_playwright_fetch  # type: ignore
import xhs_sampler  # type: ignore
import login_saver  # type: ignore
import _html_render  # type: ignore
from _config import BILIBILI_COOKIE_PATH  # type: ignore
from _time_filter import parse_within_arg, filter_items_by_time, bili_video_within_pubdate  # type: ignore
import checklist_gate  # type: ignore
import content_quality  # type: ignore

# ---------------------------------------------------------------------------
# Sample result cache — prevents duplicate sample() calls within TTL
# ---------------------------------------------------------------------------
SAMPLE_CACHE_DIR = SCRIPT_DIR.parent / '.data' / 'sample-cache'
SAMPLE_CACHE_TTL = 300  # 5 minutes


def _cache_key(platform: str, query: str, count: int) -> str:
    raw = f"{platform}:{query}:{count}"
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]


def _save_sample_cache(platform: str, query: str, count: int, payload: dict) -> None:
    SAMPLE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    key = _cache_key(platform, query, count)
    cache_file = SAMPLE_CACHE_DIR / f"{key}.json"
    cache_file.write_text(json.dumps({
        'platform': platform,
        'query': query,
        'count': count,
        'saved_at': time.time(),
        'payload': payload,
    }, ensure_ascii=False), encoding='utf-8')


def _load_sample_cache(platform: str, query: str, count: int) -> dict | None:
    key = _cache_key(platform, query, count)
    cache_file = SAMPLE_CACHE_DIR / f"{key}.json"
    if not cache_file.exists():
        return None
    try:
        data = json.loads(cache_file.read_text(encoding='utf-8'))
        if time.time() - data.get('saved_at', 0) > SAMPLE_CACHE_TTL:
            return None
        if data.get('platform') == platform and data.get('query') == query and data.get('count') == count:
            return data.get('payload')
    except Exception:
        pass
    return None


def _record_cli_run_to_manifest(run_id: str, domain: str, ok: bool, sample_details: list[dict], notes: list[str], query: str = "", flow_evidence: dict | None = None, profile: str | None = None) -> dict | None:
    if not run_id:
        return None
    # Set profile env var only if explicitly provided
    if profile:
        os.environ["SEARCH_FETCH_PROFILE"] = profile
    try:
        text_parts = []
        for item in sample_details:
            if item.get("main_text"):
                text_parts.append(item["main_text"][:800])
        text = "\n\n".join(text_parts)
        research_manifest.begin_platform_stage(run_id, domain, "search")
        research_manifest.record_search(run_id, domain, "playwright", query or (notes[0] if notes else ""), success=True)
        research_manifest.complete_platform_stage(run_id, domain, "search", success=True)
        research_manifest.begin_platform_stage(run_id, domain, "fetch")
        # Real quality evaluation when profile is available
        if profile:
            quality = content_quality.evaluate(profile, domain, text)
            quality_passed = quality.get("passed", ok)
        else:
            quality_passed = ok
        # Real checklist gate evaluation when profile is available
        if profile:
            checklist = {
                "discovery_layer_complete": bool(sample_details),
                "detail_layer_entered": bool(sample_details),
                "not_stuck_in_list_or_search": bool(sample_details),
                "usable_content_captured": bool(text.strip()),
                "sample_evidence_valid": bool(sample_details),
                "quality_gate_passed": quality_passed,
            }
            gate = checklist_gate.evaluate(domain, checklist, flow_evidence, fetch_backend="playwright")
            checklist_gate_result = gate
        else:
            checklist_gate_result = {"passed": ok}
        research_manifest.record_fetch(
            run_id, domain, "playwright",
            success=ok,
            samples=len(sample_details) if ok else 0,
            note="; ".join(notes),
            content_text=text,
            quality_passed=quality_passed,
            sample_details=sample_details,
            flow_evidence=flow_evidence or {},
            checklist_gate=checklist_gate_result,
        )
        research_manifest.complete_platform_stage(run_id, domain, "fetch", success=ok)
        return {"recorded": True, "run_id": run_id}
    except Exception as exc:
        return {"recorded": False, "error": str(exc)}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="search-fetch")
    parser.add_argument("--html", action="store_true", default=True, help="Render complete results as HTML file (default)")
    parser.add_argument("--no-html", action="store_false", dest="html", help="Disable HTML output")
    root_subparsers = parser.add_subparsers(dest="platform", required=True)

    bili = root_subparsers.add_parser("bili")
    bili_subparsers = bili.add_subparsers(dest="command", required=True)
    bili_subparsers.add_parser("search").add_argument("--query", required=True)
    open_cmd = bili_subparsers.add_parser("open")
    open_cmd.add_argument("--query", required=True)
    open_cmd.add_argument("--target-count", type=int, default=5)
    open_cmd.add_argument("--comment-wait-sec", type=int, default=8)
    open_cmd.add_argument("--within", default=None)
    comments = bili_subparsers.add_parser("comments")
    comments.add_argument("--bvid", required=True)
    comments.add_argument("--pages", type=int, default=10)
    comments.add_argument("--include-sub", action="store_true")
    comments.add_argument("--sub-pages", type=int, default=5)
    comments.add_argument("--within", default=None)
    refresh_cookie = bili_subparsers.add_parser("refresh-cookie")
    refresh_cookie.add_argument("--check-only", action="store_true")
    run_cmd = bili_subparsers.add_parser("run")
    run_cmd.add_argument("--query", required=True)
    run_cmd.add_argument("--target-count", type=int, default=5)
    run_cmd.add_argument("--comment-pages", type=int, default=3)
    run_cmd.add_argument("--include-sub", action="store_true")
    run_cmd.add_argument("--sub-pages", type=int, default=5)
    run_cmd.add_argument("--comment-wait-sec", type=int, default=8)
    run_cmd.add_argument("--run-id", default=None)
    run_cmd.add_argument("--profile", default=None)
    run_cmd.add_argument("--within", default=None)

    taptap = root_subparsers.add_parser("taptap")
    taptap_subparsers = taptap.add_subparsers(dest="command", required=True)
    taptap_subparsers.add_parser("search").add_argument("--query", required=True)
    taptap_open = taptap_subparsers.add_parser("open")
    taptap_open.add_argument("--query", required=True)
    taptap_open.add_argument("--within", default=None)
    taptap_reviews = taptap_subparsers.add_parser("reviews")
    taptap_reviews.add_argument("--query", required=True)
    taptap_reviews.add_argument("--target-count", type=int, default=10)
    taptap_reviews.add_argument("--within", default=None)
    taptap_run = taptap_subparsers.add_parser("run")
    taptap_run.add_argument("--query", required=True)
    taptap_run.add_argument("--target-count", type=int, default=10)
    taptap_run.add_argument("--run-id", default=None)
    taptap_run.add_argument("--profile", default=None)
    taptap_run.add_argument("--within", default=None)

    tieba = root_subparsers.add_parser("tieba")
    tieba_subparsers = tieba.add_subparsers(dest="command", required=True)
    tieba_subparsers.add_parser("search").add_argument("--query", required=True)
    tieba_open = tieba_subparsers.add_parser("open")
    tieba_open.add_argument("--query", required=True)
    tieba_open.add_argument("--target-count", type=int, default=5)
    tieba_open.add_argument("--max-attempts", type=int, default=10)
    tieba_open.add_argument("--within", default=None)
    tieba_threads = tieba_subparsers.add_parser("threads")
    tieba_threads.add_argument("--query", required=True)
    tieba_threads.add_argument("--target-count", type=int, default=5)
    tieba_threads.add_argument("--max-attempts", type=int, default=10)
    tieba_threads.add_argument("--within", default=None)
    tieba_run = tieba_subparsers.add_parser("run")
    tieba_run.add_argument("--query", required=True)
    tieba_run.add_argument("--target-count", type=int, default=5)
    tieba_run.add_argument("--max-attempts", type=int, default=10)
    tieba_run.add_argument("--run-id", default=None)
    tieba_run.add_argument("--profile", default=None)
    tieba_run.add_argument("--within", default=None)

    xhs = root_subparsers.add_parser("xhs")
    xhs_subparsers = xhs.add_subparsers(dest="command", required=True)
    xhs_subparsers.add_parser("search").add_argument("--query", required=True)
    xhs_open = xhs_subparsers.add_parser("open")
    xhs_open.add_argument("--query", required=True)
    xhs_open.add_argument("--target-count", type=int, default=5)
    xhs_open.add_argument("--within", default=None)
    xhs_notes = xhs_subparsers.add_parser("notes")
    xhs_notes.add_argument("--query", required=True)
    xhs_notes.add_argument("--target-count", type=int, default=5)
    xhs_notes.add_argument("--within", default=None)
    xhs_run = xhs_subparsers.add_parser("run")
    xhs_run.add_argument("--query", required=True)
    xhs_run.add_argument("--target-count", type=int, default=5)
    xhs_run.add_argument("--run-id", default=None)
    xhs_run.add_argument("--profile", default=None)
    xhs_run.add_argument("--within", default=None)

    douyin = root_subparsers.add_parser("douyin")
    douyin_subparsers = douyin.add_subparsers(dest="command", required=True)
    douyin_subparsers.add_parser("search").add_argument("--query", required=True)
    douyin_cards = douyin_subparsers.add_parser("cards")
    douyin_cards.add_argument("--query", required=True)
    douyin_cards.add_argument("--target-count", type=int, default=10)
    douyin_cards.add_argument("--max-scrolls", type=int, default=None)
    douyin_cards.add_argument("--resolve-links", action=argparse.BooleanOptionalAction, default=True)
    douyin_cards.add_argument("--within", default=None)
    douyin_open = douyin_subparsers.add_parser("open")
    douyin_open.add_argument("--query", required=True)
    douyin_open.add_argument("--target-count", type=int, default=5)
    douyin_open.add_argument("--max-scrolls", type=int, default=None)
    douyin_open.add_argument("--resolve-links", action=argparse.BooleanOptionalAction, default=True)
    douyin_open.add_argument("--within", default=None)
    douyin_videos = douyin_subparsers.add_parser("videos")
    douyin_videos.add_argument("--query", required=True)
    douyin_videos.add_argument("--target-count", type=int, default=5)
    douyin_videos.add_argument("--max-scrolls", type=int, default=None)
    douyin_videos.add_argument("--resolve-links", action=argparse.BooleanOptionalAction, default=True)
    douyin_videos.add_argument("--within", default=None)
    douyin_run = douyin_subparsers.add_parser("run")
    douyin_run.add_argument("--query", required=True)
    douyin_run.add_argument("--target-count", type=int, default=5)
    douyin_run.add_argument("--max-scrolls", type=int, default=None)
    douyin_run.add_argument("--resolve-links", action=argparse.BooleanOptionalAction, default=True)
    douyin_run.add_argument("--run-id", default=None)
    douyin_run.add_argument("--profile", default=None)
    douyin_run.add_argument("--within", default=None)
    douyin_deep = douyin_subparsers.add_parser("deep")
    douyin_deep.add_argument("--query", required=True)
    douyin_deep.add_argument("--target-count", type=int, default=5)
    douyin_deep.add_argument("--run-id", default=None)
    douyin_deep.add_argument("--profile", default=None)
    douyin_deep.add_argument("--within", default=None)

    taobao = root_subparsers.add_parser("taobao")
    taobao_subparsers = taobao.add_subparsers(dest="command", required=True)
    taobao_subparsers.add_parser("search").add_argument("--query", required=True)
    taobao_cards = taobao_subparsers.add_parser("cards")
    taobao_cards.add_argument("--query", required=True)
    taobao_cards.add_argument("--target-count", type=int, default=20)
    taobao_cards.add_argument("--max-scrolls", type=int, default=None)
    taobao_cards.add_argument("--max-pages", type=int, default=None)
    taobao_open = taobao_subparsers.add_parser("open")
    taobao_open.add_argument("--query", required=True)
    taobao_open.add_argument("--target-count", type=int, default=20)
    taobao_open.add_argument("--max-scrolls", type=int, default=None)
    taobao_open.add_argument("--max-pages", type=int, default=None)
    taobao_run = taobao_subparsers.add_parser("run")
    taobao_run.add_argument("--query", required=True)
    taobao_run.add_argument("--target-count", type=int, default=20)
    taobao_run.add_argument("--max-scrolls", type=int, default=None)
    taobao_run.add_argument("--max-pages", type=int, default=None)
    taobao_run.add_argument("--run-id", default=None)
    taobao_run.add_argument("--profile", default=None)
    taobao_login = taobao_subparsers.add_parser("login")
    taobao_login.add_argument("--timeout", type=int, default=180)

    tmall = root_subparsers.add_parser("tmall")
    tmall_subparsers = tmall.add_subparsers(dest="command", required=True)
    tmall_subparsers.add_parser("search").add_argument("--query", required=True)
    tmall_cards = tmall_subparsers.add_parser("cards")
    tmall_cards.add_argument("--query", required=True)
    tmall_cards.add_argument("--target-count", type=int, default=20)
    tmall_cards.add_argument("--max-scrolls", type=int, default=None)
    tmall_cards.add_argument("--max-pages", type=int, default=None)
    tmall_open = tmall_subparsers.add_parser("open")
    tmall_open.add_argument("--query", required=True)
    tmall_open.add_argument("--target-count", type=int, default=20)
    tmall_open.add_argument("--max-scrolls", type=int, default=None)
    tmall_open.add_argument("--max-pages", type=int, default=None)
    tmall_run = tmall_subparsers.add_parser("run")
    tmall_run.add_argument("--query", required=True)
    tmall_run.add_argument("--target-count", type=int, default=20)
    tmall_run.add_argument("--max-scrolls", type=int, default=None)
    tmall_run.add_argument("--max-pages", type=int, default=None)
    tmall_run.add_argument("--run-id", default=None)
    tmall_run.add_argument("--profile", default=None)
    tmall_login = tmall_subparsers.add_parser("login")
    tmall_login.add_argument("--timeout", type=int, default=180)

    jd = root_subparsers.add_parser("jd")
    jd_subparsers = jd.add_subparsers(dest="command", required=True)
    jd_subparsers.add_parser("search").add_argument("--query", required=True)
    jd_cards = jd_subparsers.add_parser("cards")
    jd_cards.add_argument("--query", required=True)
    jd_cards.add_argument("--target-count", type=int, default=20)
    jd_cards.add_argument("--max-scrolls", type=int, default=None)
    jd_cards.add_argument("--max-pages", type=int, default=None)
    jd_open = jd_subparsers.add_parser("open")
    jd_open.add_argument("--query", required=True)
    jd_open.add_argument("--target-count", type=int, default=20)
    jd_open.add_argument("--max-scrolls", type=int, default=None)
    jd_open.add_argument("--max-pages", type=int, default=None)
    jd_run = jd_subparsers.add_parser("run")
    jd_run.add_argument("--query", required=True)
    jd_run.add_argument("--target-count", type=int, default=20)
    jd_run.add_argument("--max-scrolls", type=int, default=None)
    jd_run.add_argument("--max-pages", type=int, default=None)
    jd_run.add_argument("--run-id", default=None)
    jd_run.add_argument("--profile", default=None)
    jd_login = jd_subparsers.add_parser("login")
    jd_login.add_argument("--timeout", type=int, default=180)

    login_parser = root_subparsers.add_parser("login")
    login_parser.add_argument("--url", required=True, help="目标站点 URL")
    login_parser.add_argument("--wait-text", default="", help="登录成功后页面应包含的文本")
    login_parser.add_argument("--timeout", type=int, default=120, help="最长等待秒数（默认 120）")
    login_parser.add_argument("--isolated", action="store_true", help="使用独立 Profile（抖音等高风险站点）")

    return parser


def bilibili_search_url(query: str) -> str:
    return f"https://search.bilibili.com/all?keyword={urllib.parse.quote(query)}"


def taptap_search_url(query: str) -> str:
    return f"https://www.taptap.cn/search/{urllib.parse.quote(query)}"


def cookie_status() -> dict:
    exists = BILIBILI_COOKIE_PATH.exists()
    text = BILIBILI_COOKIE_PATH.read_text().strip() if exists else ""
    return {"path": str(BILIBILI_COOKIE_PATH), "exists": exists, "non_empty": bool(text), "length": len(text)}


def handle_bili_search(args: argparse.Namespace) -> dict:
    return guarded_search_fetch.plan_search(args.query, risk_level="high", need_interaction=True, domain="bilibili.com")


def _enrich_bili_contexts_with_pubdate(contexts: list[dict]) -> None:
    """Enrich video contexts with pubdate from B站 API for time filtering."""
    for ctx in contexts:
        bvid = ctx.get("bvid") or ""
        if not bvid or not ctx.get("ok"):
            continue
        meta = ctx.get("meta") or {}
        if meta.get("pubdate"):
            continue
        try:
            info = bilibili_comment_fetch.get_video_info(bvid)
            meta["pubdate"] = info.get("pubdate")
        except Exception:
            pass


def handle_bili_open(args: argparse.Namespace) -> dict:
    within = parse_within_arg(getattr(args, "within", None))
    search_url = bilibili_search_url(args.query)
    opened = playwright_fetch.ensure_bilibili_multiple_video_contexts(search_url, min_results=max(args.target_count, 1), comment_wait_sec=max(args.comment_wait_sec, 1))
    if within is not None:
        contexts = opened.get("contexts") or []
        _enrich_bili_contexts_with_pubdate(contexts)
        kept = []
        for ctx in contexts:
            if not ctx.get("ok"):
                kept.append(ctx)
                continue
            meta = ctx.get("meta") or {}
            if bili_video_within_pubdate(meta, within):
                kept.append(ctx)
            else:
                ctx["_time_within"] = False
        opened["contexts"] = kept
    return {"mode": "bili.open", "query": args.query, "search_url": search_url, "target_count": max(args.target_count, 1), "opened": opened, "within": getattr(args, "within", None)}


def _require_cookie() -> dict | None:
    status = cookie_status()
    if status["non_empty"]:
        return None
    return {"ok": False, "reason": "missing_cookie", "cookie": status, "hint": "run `search-fetch bili refresh-cookie` after logging into bilibili.com in Edge"}


def handle_bili_comments(args: argparse.Namespace) -> dict:
    missing = _require_cookie()
    if missing:
        return {"mode": "bili.comments", **missing}
    within = parse_within_arg(getattr(args, "within", None))
    comments = bilibili_comment_fetch.collect_comments(args.bvid, max_root_pages=max(args.pages, 1), include_sub=bool(args.include_sub), max_sub_pages=max(args.sub_pages, 1), within=within)
    return {"mode": "bili.comments", "ok": True, "bvid": args.bvid, "cookie": cookie_status(), "comments": comments, "within": getattr(args, "within", None)}


def handle_bili_refresh_cookie(args: argparse.Namespace) -> dict:
    status = cookie_status()
    if args.check_only:
        return {"mode": "bili.refresh-cookie", "ok": status["non_empty"], "cookie": status}
    refreshed = bilibili_cookie_refresh.refresh()
    return {"mode": "bili.refresh-cookie", **refreshed, "cookie": cookie_status()}


def handle_bili_run(args: argparse.Namespace) -> dict:
    within = parse_within_arg(getattr(args, "within", None))
    search_url = bilibili_search_url(args.query)
    opened = playwright_fetch.ensure_bilibili_multiple_video_contexts(search_url, min_results=max(args.target_count, 1), comment_wait_sec=max(args.comment_wait_sec, 1))
    cookie = cookie_status()
    # Enrich with pubdate from API and filter video contexts if within is specified
    contexts = opened.get("contexts") or []
    if within is not None:
        _enrich_bili_contexts_with_pubdate(contexts)
        kept = []
        for ctx in contexts:
            if not ctx.get("ok"):
                kept.append(ctx)
                continue
            meta = ctx.get("meta") or {}
            if bili_video_within_pubdate(meta, within):
                kept.append(ctx)
            else:
                ctx["_time_within"] = False
        opened["contexts"] = kept
    comment_results = []
    for ctx in opened.get("contexts", []) or []:
        bvid = ctx.get("bvid") or ""
        if not bvid or not ctx.get("ok"):
            continue
        if not cookie["non_empty"]:
            comment_results.append({"bvid": bvid, "ok": False, "reason": "missing_cookie", "cookie": cookie})
            continue
        comments = bilibili_comment_fetch.collect_comments(bvid, max_root_pages=max(args.comment_pages, 1), include_sub=bool(args.include_sub), max_sub_pages=max(args.sub_pages, 1), within=within)
        comment_ok = bool(comments.get("root_comments"))
        comment_results.append({"bvid": bvid, "ok": comment_ok, "comments": comments})
    opened_ok = bool(opened.get("ok"))
    any_comment_ok = any(r.get("ok") for r in comment_results)
    query_keywords = [w for w in args.query.split() if len(w) >= 2]
    sample_details = []
    for ctx in opened.get("contexts", []) or []:
        bvid = ctx.get("bvid") or ""
        if not bvid or not ctx.get("ok"):
            continue
        title = ctx.get("final_title") or bvid
        if query_keywords and not any(kw in title for kw in query_keywords):
            continue
        comments_for_detail = []
        for cr in comment_results:
            if cr.get("bvid") == bvid and cr.get("ok"):
                comments_for_detail = (cr.get("comments") or {}).get("root_comments", [])[:5]
                break
        comment_texts = [c.get("message", "") if isinstance(c, dict) else str(c) for c in comments_for_detail]
        combined_text = f"{title}\n" + "\n".join(comment_texts)
        sample_details.append({
            "url": ctx.get("final_url") or f"https://www.bilibili.com/video/{bvid}",
            "title": title,
            "main_text": combined_text[:800],
            "comments": comment_texts,
            "comment_text_length": sum(len(x) for x in comment_texts),
        })
    ok = opened_ok and any_comment_ok and len(sample_details) > 0
    manifest_result = _record_cli_run_to_manifest(
        getattr(args, "run_id", None), "bilibili.com", ok, sample_details,
        ["policy=playwright_only"],
        query=args.query,
        flow_evidence={
            "opened_video_count": len(sample_details),
            "opened_bvids": [s.get("url", "").split("/video/")[-1].split("?")[0] if "/video/" in (s.get("url") or "") else s.get("url", "") for s in sample_details if s.get("url")],
            "visible_comment_videos": [s.get("url", "") for s in sample_details if s.get("comments")],
        },
        profile=getattr(args, "profile", None),
    )
    return {"mode": "bili.run", "ok": ok, "query": args.query, "search_url": search_url, "target_count": max(args.target_count, 1), "cookie": cookie, "videos": opened.get("contexts", []) or [], "opened": opened, "comment_results": comment_results, "manifest": manifest_result, "within": getattr(args, "within", None)}


def handle_taptap_search(args: argparse.Namespace) -> dict:
    return guarded_search_fetch.plan_search(args.query, risk_level="high", need_interaction=True, domain="taptap.cn")


def _taptap_sample_cached(query: str, review_limit: int) -> dict:
    cached = _load_sample_cache('taptap', query, review_limit)
    if cached is not None:
        return {**cached, '_from_cache': True}
    result = taptap_playwright_fetch.fetch(query, review_limit=review_limit)
    _save_sample_cache('taptap', query, review_limit, result)
    return result


def handle_taptap_open(args: argparse.Namespace) -> dict:
    within = parse_within_arg(getattr(args, "within", None))
    reviews = _taptap_sample_cached(args.query, 10)
    if within is not None:
        for block_key in ("detail_comments", "comprehensive", "comprehensive_sorted", "latest"):
            block = reviews.get(block_key)
            if not block or not isinstance(block, dict):
                continue
            revs = block.get("reviews") or []
            if revs:
                block["reviews"] = filter_items_by_time(
                    [{"main_text": r} for r in revs], within, text_keys=["main_text"]
                )
                block["reviews"] = [r["main_text"] for r in block["reviews"]]
    return {"mode": "taptap.open", "query": args.query, "search_url": taptap_search_url(args.query), "opened": {"query": args.query, "step1": reviews.get("step1", {}), "detail_comments": reviews.get("detail_comments", {}), "checklist": reviews.get("checklist", {})}, "within": getattr(args, "within", None)}


def handle_taptap_reviews(args: argparse.Namespace) -> dict:
    within = parse_within_arg(getattr(args, "within", None))
    reviews = _taptap_sample_cached(args.query, max(args.target_count, 1))
    checklist = reviews.get("checklist") or {}
    has_reviews = bool(checklist.get("review_content_captured"))
    # Filter review blocks by time if within is specified
    if within is not None and has_reviews:
        for block_key in ("detail_comments", "comprehensive", "comprehensive_sorted", "latest"):
            block = reviews.get(block_key)
            if not block or not isinstance(block, dict):
                continue
            revs = block.get("reviews") or []
            if revs:
                block["reviews"] = filter_items_by_time(
                    [{"main_text": r} for r in revs], within, text_keys=["main_text"]
                )
                block["reviews"] = [r["main_text"] for r in block["reviews"]]
    return {"mode": "taptap.reviews", "ok": has_reviews, "query": args.query, "target_count": max(args.target_count, 1), "reviews": reviews, "within": getattr(args, "within", None)}


def handle_taptap_run(args: argparse.Namespace) -> dict:
    within = parse_within_arg(getattr(args, "within", None))
    reviews = _taptap_sample_cached(args.query, max(args.target_count, 1))
    checklist = reviews.get("checklist") or {}
    step1_ok = bool((reviews.get("step1") or {}).get("ok"))
    has_reviews = bool(checklist.get("review_content_captured"))
    # Filter review blocks by time if within is specified
    if within is not None and has_reviews:
        for block_key in ("detail_comments", "comprehensive", "comprehensive_sorted", "latest"):
            block = reviews.get(block_key)
            if not block or not isinstance(block, dict):
                continue
            revs = block.get("reviews") or []
            if revs:
                block["reviews"] = filter_items_by_time(
                    [{"main_text": r} for r in revs], within, text_keys=["main_text"]
                )
                block["reviews"] = [r["main_text"] for r in block["reviews"]]
    sample_details = []
    detail_comments = reviews.get("detail_comments") or {}
    if detail_comments.get("reviews"):
        sample_details.append({
            "url": detail_comments.get("url") or f"https://www.taptap.cn/app/209601/review",
            "title": detail_comments.get("title") or args.query,
            "main_text": (detail_comments.get("bodyPreview") or "")[:800],
            "comments": (detail_comments.get("reviews") or [])[:10],
            "comment_text_length": sum(len(x) for x in (detail_comments.get("reviews") or [])[:10]),
            "sort_tab": "detail_fallback",
        })
    for block_key in ("comprehensive", "comprehensive_sorted", "latest"):
        block = reviews.get(block_key) or {}
        revs = block.get("reviews") or []
        if not revs:
            continue
        sample_details.append({
            "url": block.get("url") or f"https://www.taptap.cn/search/{args.query}",
            "title": block.get("title") or args.query,
            "main_text": (block.get("bodyPreview") or "")[:800],
            "comments": revs[:10],
            "comment_text_length": sum(len(x) for x in revs[:10]),
            "sort_tab": block_key if block_key != "comprehensive_sorted" else "comprehensive",
        })
    ok = step1_ok and has_reviews
    manifest_result = _record_cli_run_to_manifest(
        getattr(args, "run_id", None), "taptap.cn", ok, sample_details,
        ["policy=playwright_only; sampler=review_fetch"],
        query=args.query,
        flow_evidence={"opened_review_block_count": len(sample_details)},
        profile=getattr(args, "profile", None),
    )
    return {"mode": "taptap.run", "ok": ok, "query": args.query, "search_url": taptap_search_url(args.query), "target_count": max(args.target_count, 1), "reviews": reviews, "manifest": manifest_result, "within": getattr(args, "within", None)}


def handle_tieba_search(args: argparse.Namespace) -> dict:
    return guarded_search_fetch.plan_search(args.query, risk_level="high", need_interaction=True, domain="tieba.baidu.com")


def _valid_tieba_thread_items(payload: dict) -> list[dict]:
    items = [item for item in (payload.get("items") or []) if item.get("parse_ok") and "/p/" in (item.get("detail_url") or item.get("url") or "")]
    for item in items:
        if not item.get("url"):
            item["url"] = item.get("detail_url", "")
    return items


def _tieba_sample_cached(query: str, count: int, max_attempts: int) -> dict:
    # Tieba cache key includes max_attempts since it affects results
    cache_key_extra = f"ma{max_attempts}"
    key_str = f"tieba:{query}:{count}:{cache_key_extra}"
    cache_hash = hashlib.sha256(key_str.encode('utf-8')).hexdigest()[:16]
    cached = _load_sample_cache('tieba', f"{query}:{cache_key_extra}", count)
    if cached is not None:
        return {**cached, '_from_cache': True}
    result = tieba_playwright_fetch.sample(query, count=count, max_attempts=max_attempts)
    _save_sample_cache('tieba', f"{query}:{cache_key_extra}", count, result)
    return result


def handle_tieba_open(args: argparse.Namespace) -> dict:
    within = parse_within_arg(getattr(args, "within", None))
    threads = _tieba_sample_cached(args.query, max(args.target_count, 1), max(args.max_attempts, 1))
    valid_items = _valid_tieba_thread_items(threads)
    if within is not None:
        valid_items = filter_items_by_time(valid_items, within)
    return {"mode": "tieba.open", "query": args.query, "opened": {"bar_url": threads.get("bar_url"), "candidate_pool_size": threads.get("candidate_pool_size"), "checklist": threads.get("checklist", {}), "target_count": max(args.target_count, 1), "max_attempts": max(args.max_attempts, 1), "valid_thread_count": len(valid_items)}, "within": getattr(args, "within", None)}


def handle_tieba_threads(args: argparse.Namespace) -> dict:
    within = parse_within_arg(getattr(args, "within", None))
    threads = _tieba_sample_cached(args.query, max(args.target_count, 1), max(args.max_attempts, 1))
    valid_items = _valid_tieba_thread_items(threads)
    if within is not None:
        valid_items = filter_items_by_time(valid_items, within)
    return {"mode": "tieba.threads", "ok": len(valid_items) >= max(args.target_count, 1), "query": args.query, "target_count": max(args.target_count, 1), "max_attempts": max(args.max_attempts, 1), "thread_details": valid_items, "threads": threads, "within": getattr(args, "within", None)}


def handle_tieba_run(args: argparse.Namespace) -> dict:
    within = parse_within_arg(getattr(args, "within", None))
    threads = _tieba_sample_cached(args.query, max(args.target_count, 1), max(args.max_attempts, 1))
    valid_items = _valid_tieba_thread_items(threads)
    if within is not None:
        valid_items = filter_items_by_time(valid_items, within)
    ok = len(valid_items) >= max(args.target_count, 1)
    manifest_result = _record_cli_run_to_manifest(
        getattr(args, "run_id", None), "tieba.baidu.com", ok, valid_items,
        ["policy=playwright_only; sampler=bar_direct_entry"],
        query=args.query,
        flow_evidence={"thread_detail_count": len(valid_items)},
        profile=getattr(args, "profile", None),
    )
    return {"mode": "tieba.run", "ok": ok, "query": args.query, "bar_url": threads.get("bar_url"), "target_count": max(args.target_count, 1), "max_attempts": max(args.max_attempts, 1), "valid_thread_count": len(valid_items), "thread_details": valid_items, "threads": threads, "manifest": manifest_result, "within": getattr(args, "within", None)}


def handle_xhs_search(args: argparse.Namespace) -> dict:
    return guarded_search_fetch.plan_search(args.query, risk_level="high", need_interaction=True, domain="xiaohongshu.com")


def _valid_xhs_items(payload: dict) -> list[dict]:
    return [
        item for item in (payload.get("items") or [])
        if item.get("parse_ok") and item.get("main_text") and item.get("comments_requirement_met")
    ]


def _ensure_xhs_run_scope() -> None:
    os.environ.setdefault("SEARCH_FETCH_RUN_SCOPE", "xiaohongshu.com")


def _ensure_douyin_run_scope() -> None:
    os.environ.setdefault("SEARCH_FETCH_RUN_SCOPE", "douyin.com")


def _ensure_taobao_run_scope() -> None:
    os.environ.setdefault("SEARCH_FETCH_RUN_SCOPE", "taobao.com")


def _ensure_marketplace_run_scope(platform: str) -> None:
    domain = {
        "taobao": "taobao.com",
        "tmall": "tmall.com",
        "jd": "jd.com",
    }.get(platform, "taobao.com")
    os.environ.setdefault("SEARCH_FETCH_RUN_SCOPE", domain)


def _valid_douyin_items(payload: dict) -> list[dict]:
    return [
        item for item in (payload.get("items") or [])
        if item.get("parse_ok") and item.get("main_text")
    ]


def _xhs_sample_cached(query: str, count: int) -> dict:
    cached = _load_sample_cache('xhs', query, count)
    if cached is not None:
        return {**cached, '_from_cache': True}
    result = xhs_sampler.sample(query, count=count)
    _save_sample_cache('xhs', query, count, result)
    return result


def handle_xhs_open(args: argparse.Namespace) -> dict:
    _ensure_xhs_run_scope()
    within = parse_within_arg(getattr(args, "within", None))
    notes = _xhs_sample_cached(args.query, max(args.target_count, 1))
    valid_items = _valid_xhs_items(notes)
    if within is not None:
        valid_items = filter_items_by_time(valid_items, within)
    return {"mode": "xhs.open", "query": args.query, "opened": {"browser_search_discovery": notes.get("browser_search_discovery", {}), "flow_evidence": notes.get("flow_evidence", {}), "target_count": max(args.target_count, 1), "valid_note_count": len(valid_items)}, "within": getattr(args, "within", None)}


def handle_xhs_notes(args: argparse.Namespace) -> dict:
    _ensure_xhs_run_scope()
    within = parse_within_arg(getattr(args, "within", None))
    notes = _xhs_sample_cached(args.query, max(args.target_count, 1))
    valid_items = _valid_xhs_items(notes)
    if within is not None:
        valid_items = filter_items_by_time(valid_items, within)
    return {"mode": "xhs.notes", "ok": len(valid_items) >= max(args.target_count, 1), "query": args.query, "target_count": max(args.target_count, 1), "note_details": valid_items, "notes": notes, "within": getattr(args, "within", None)}


def handle_xhs_run(args: argparse.Namespace) -> dict:
    _ensure_xhs_run_scope()
    within = parse_within_arg(getattr(args, "within", None))
    notes = _xhs_sample_cached(args.query, max(args.target_count, 1))
    valid_items = _valid_xhs_items(notes)
    if within is not None:
        valid_items = filter_items_by_time(valid_items, within)
    ok = len(valid_items) >= max(args.target_count, 1)
    manifest_result = _record_cli_run_to_manifest(
        getattr(args, "run_id", None), "xiaohongshu.com", ok, valid_items,
        ["policy=playwright_only; sampler=search_result"],
        query=args.query,
        flow_evidence={"opened_note_count": len(valid_items)},
        profile=getattr(args, "profile", None),
    )
    return {"mode": "xhs.run", "ok": ok, "query": args.query, "target_count": max(args.target_count, 1), "valid_note_count": len(valid_items), "note_details": valid_items, "notes": notes, "manifest": manifest_result, "within": getattr(args, "within", None)}


def handle_douyin_search(args: argparse.Namespace) -> dict:
    return guarded_search_fetch.plan_search(args.query, risk_level="high", need_interaction=True, domain="douyin.com")


def handle_douyin_cards(args: argparse.Namespace) -> dict:
    _ensure_douyin_run_scope()
    result = douyin_sampler.cards(args.query, count=max(args.target_count, 1), max_scrolls=getattr(args, "max_scrolls", None), resolve_links=bool(getattr(args, "resolve_links", True)))
    return {"mode": "douyin.cards", **result, "within": getattr(args, "within", None)}


def _douyin_light_cards(args: argparse.Namespace) -> dict:
    _ensure_douyin_run_scope()
    result = douyin_sampler.cards(args.query, count=max(args.target_count, 1), max_scrolls=getattr(args, "max_scrolls", None), resolve_links=bool(getattr(args, "resolve_links", True)))
    within = parse_within_arg(getattr(args, "within", None))
    items = result.get("items") or []
    if within is not None:
        items = filter_items_by_time(items, within)
    return {**result, "items": items, "card_count": len(items), "within": getattr(args, "within", None)}


def _douyin_sample_cached(query: str, count: int) -> dict:
    cached = _load_sample_cache('douyin', query, count)
    if cached is not None:
        return {**cached, '_from_cache': True}
    result = douyin_sampler.sample(query, count=count)
    _save_sample_cache('douyin', query, count, result)
    return result


def handle_douyin_open(args: argparse.Namespace) -> dict:
    result = _douyin_light_cards(args)
    return {"mode": "douyin.open", "default_mode": "cards", "query": args.query, "opened": {"flow_evidence": result.get("flow_evidence", {}), "target_count": max(args.target_count, 1), "card_count": len(result.get("items") or [])}, "items": result.get("items") or [], "within": getattr(args, "within", None)}


def handle_douyin_videos(args: argparse.Namespace) -> dict:
    result = _douyin_light_cards(args)
    items = result.get("items") or []
    ok = len(items) >= max(args.target_count, 1)
    return {"mode": "douyin.videos", "default_mode": "cards", "ok": ok, "query": args.query, "target_count": max(args.target_count, 1), "card_count": len(items), "video_cards": items, "cards": result, "within": getattr(args, "within", None)}


def handle_douyin_run(args: argparse.Namespace) -> dict:
    result = _douyin_light_cards(args)
    items = result.get("items") or []
    manifest_items = [
        {**item, "main_text": item.get("main_text") or item.get("text", ""), "url": item.get("url") or item.get("href", "")}
        for item in items
    ]
    ok = len(items) >= max(args.target_count, 1)
    manifest_result = _record_cli_run_to_manifest(
        getattr(args, "run_id", None), "douyin.com", ok, manifest_items,
        ["policy=playwright_only; sampler=search_cards; detail=off"],
        query=args.query,
        flow_evidence={"card_count": len(items)},
        profile=getattr(args, "profile", None),
    )
    return {"mode": "douyin.run", "default_mode": "cards", "ok": ok, "query": args.query, "target_count": max(args.target_count, 1), "card_count": len(items), "video_cards": items, "cards": result, "manifest": manifest_result, "within": getattr(args, "within", None)}


def handle_douyin_deep(args: argparse.Namespace) -> dict:
    _ensure_douyin_run_scope()
    within = parse_within_arg(getattr(args, "within", None))
    videos = _douyin_sample_cached(args.query, max(args.target_count, 1))
    valid_items = _valid_douyin_items(videos)
    if within is not None:
        valid_items = filter_items_by_time(valid_items, within)
    ok = len(valid_items) >= max(args.target_count, 1)
    manifest_result = _record_cli_run_to_manifest(
        getattr(args, "run_id", None), "douyin.com", ok, valid_items,
        ["policy=playwright_only; sampler=modal_search"],
        query=args.query,
        flow_evidence={"opened_modal_count": len(valid_items)},
        profile=getattr(args, "profile", None),
    )
    return {"mode": "douyin.deep", "ok": ok, "query": args.query, "target_count": max(args.target_count, 1), "valid_video_count": len(valid_items), "video_details": valid_items, "videos": videos, "manifest": manifest_result, "within": getattr(args, "within", None)}


def handle_taobao_search(args: argparse.Namespace) -> dict:
    return guarded_search_fetch.plan_search(args.query, risk_level="high", need_interaction=True, domain="taobao.com")


def handle_marketplace_search(args: argparse.Namespace, platform: str) -> dict:
    domain = {"taobao": "taobao.com", "tmall": "tmall.com", "jd": "jd.com"}[platform]
    return guarded_search_fetch.plan_search(args.query, risk_level="high", need_interaction=True, domain=domain)


def _marketplace_light_cards(args: argparse.Namespace, platform: str) -> dict:
    _ensure_marketplace_run_scope(platform)
    sampler = {"taobao": taobao_sampler, "tmall": tmall_sampler, "jd": jd_sampler}[platform]
    return sampler.cards(
        args.query,
        count=max(args.target_count, 1),
        max_scrolls=getattr(args, "max_scrolls", None),
        max_pages=getattr(args, "max_pages", None),
        platform=platform,
    )


def _taobao_light_cards(args: argparse.Namespace) -> dict:
    return _marketplace_light_cards(args, "taobao")


def handle_taobao_cards(args: argparse.Namespace) -> dict:
    result = _taobao_light_cards(args)
    return {"mode": "taobao.cards", **result}


def handle_taobao_open(args: argparse.Namespace) -> dict:
    result = _taobao_light_cards(args)
    return {
        "mode": "taobao.open",
        "default_mode": "cards",
        "query": args.query,
        "opened": {
            "flow_evidence": result.get("flow_evidence", {}),
            "target_count": max(args.target_count, 1),
            "card_count": len(result.get("items") or []),
        },
        "items": result.get("items") or [],
    }


def handle_taobao_run(args: argparse.Namespace) -> dict:
    return handle_marketplace_run(args, "taobao")


def handle_marketplace_cards(args: argparse.Namespace, platform: str) -> dict:
    result = _marketplace_light_cards(args, platform)
    return {"mode": f"{platform}.cards", **result}


def handle_marketplace_open(args: argparse.Namespace, platform: str) -> dict:
    result = _marketplace_light_cards(args, platform)
    return {
        "mode": f"{platform}.open",
        "default_mode": "cards",
        "query": args.query,
        "opened": {
            "flow_evidence": result.get("flow_evidence", {}),
            "target_count": max(args.target_count, 1),
            "card_count": len(result.get("items") or []),
        },
        "items": result.get("items") or [],
    }


def handle_marketplace_run(args: argparse.Namespace, platform: str) -> dict:
    result = _marketplace_light_cards(args, platform)
    items = result.get("items") or []
    manifest_items = [
        {**item, "main_text": item.get("main_text") or item.get("text", ""), "url": item.get("url") or item.get("href", "")}
        for item in items
    ]
    ok = len(items) >= max(args.target_count, 1)
    domain = {"taobao": "taobao.com", "tmall": "tmall.com", "jd": "jd.com"}[platform]
    manifest_result = _record_cli_run_to_manifest(
        getattr(args, "run_id", None), domain, ok, manifest_items,
        ["policy=playwright_only; sampler=search_cards; detail=off"],
        query=args.query,
        flow_evidence={"card_count": len(items)},
        profile=getattr(args, "profile", None),
    )
    return {
        "mode": f"{platform}.run",
        "default_mode": "cards",
        "ok": ok,
        "query": args.query,
        "target_count": max(args.target_count, 1),
        "card_count": len(items),
        "product_cards": items,
        "cards": result,
        "manifest": manifest_result,
    }


def handle_marketplace_login(args: argparse.Namespace, platform: str) -> dict:
    sampler = {"taobao": taobao_sampler, "tmall": tmall_sampler, "jd": jd_sampler}[platform]
    url = sampler.login_url(platform)
    try:
        login_saver.run(url=url, wait_text="", timeout=args.timeout, isolated=False)
        _ensure_marketplace_run_scope(platform)
        verify = sampler.cards(sampler.verify_query(platform), count=1, max_scrolls=1, max_pages=1, platform=platform)
        ok = bool(verify.get("ok")) or verify.get("reason") != "login_required"
        return {"mode": f"{platform}.login", "ok": ok, "url": url, "isolated": False, "verify": {"ok": verify.get("ok"), "reason": verify.get("reason"), "card_count": verify.get("card_count", 0)}}
    except SystemExit as e:
        if e.code != 0:
            return {"mode": f"{platform}.login", "ok": False, "url": url, "isolated": False, "error": str(e)}
        return {"mode": f"{platform}.login", "ok": True, "url": url, "isolated": False}


def handle_login(args: argparse.Namespace) -> dict:
    isolated = bool(args.isolated)
    if "douyin.com" in args.url.lower():
        isolated = True
    try:
        login_saver.run(url=args.url, wait_text=args.wait_text, timeout=args.timeout, isolated=isolated)
        return {"mode": "login", "ok": True, "url": args.url, "isolated": isolated}
    except SystemExit as e:
        if e.code != 0:
            return {"mode": "login", "ok": False, "url": args.url, "isolated": isolated, "error": str(e)}
        return {"mode": "login", "ok": True, "url": args.url, "isolated": isolated}


def dispatch(args: argparse.Namespace) -> dict:
    if args.platform == "login":
        return handle_login(args)
    if args.platform == "bili":
        if args.command == "search": return handle_bili_search(args)
        if args.command == "open": return handle_bili_open(args)
        if args.command == "comments": return handle_bili_comments(args)
        if args.command == "refresh-cookie": return handle_bili_refresh_cookie(args)
        if args.command == "run": return handle_bili_run(args)
        raise SystemExit(f"unsupported bili command: {args.command}")
    if args.platform == "taptap":
        if args.command == "search": return handle_taptap_search(args)
        if args.command == "open": return handle_taptap_open(args)
        if args.command == "reviews": return handle_taptap_reviews(args)
        if args.command == "run": return handle_taptap_run(args)
        raise SystemExit(f"unsupported taptap command: {args.command}")
    if args.platform == "tieba":
        if args.command == "search": return handle_tieba_search(args)
        if args.command == "open": return handle_tieba_open(args)
        if args.command == "threads": return handle_tieba_threads(args)
        if args.command == "run": return handle_tieba_run(args)
        raise SystemExit(f"unsupported tieba command: {args.command}")
    if args.platform == "xhs":
        if args.command == "search": return handle_xhs_search(args)
        if args.command == "open": return handle_xhs_open(args)
        if args.command == "notes": return handle_xhs_notes(args)
        if args.command == "run": return handle_xhs_run(args)
        raise SystemExit(f"unsupported xhs command: {args.command}")
    if args.platform == "douyin":
        if args.command == "search": return handle_douyin_search(args)
        if args.command == "cards": return handle_douyin_cards(args)
        if args.command == "open": return handle_douyin_open(args)
        if args.command == "videos": return handle_douyin_videos(args)
        if args.command == "run": return handle_douyin_run(args)
        if args.command == "deep": return handle_douyin_deep(args)
        raise SystemExit(f"unsupported douyin command: {args.command}")
    if args.platform == "taobao":
        if args.command == "search": return handle_taobao_search(args)
        if args.command == "cards": return handle_taobao_cards(args)
        if args.command == "open": return handle_taobao_open(args)
        if args.command == "run": return handle_taobao_run(args)
        if args.command == "login": return handle_marketplace_login(args, "taobao")
        raise SystemExit(f"unsupported taobao command: {args.command}")
    if args.platform == "tmall":
        if args.command == "search": return handle_marketplace_search(args, "tmall")
        if args.command == "cards": return handle_marketplace_cards(args, "tmall")
        if args.command == "open": return handle_marketplace_open(args, "tmall")
        if args.command == "run": return handle_marketplace_run(args, "tmall")
        if args.command == "login": return handle_marketplace_login(args, "tmall")
        raise SystemExit(f"unsupported tmall command: {args.command}")
    if args.platform == "jd":
        if args.command == "search": return handle_marketplace_search(args, "jd")
        if args.command == "cards": return handle_marketplace_cards(args, "jd")
        if args.command == "open": return handle_marketplace_open(args, "jd")
        if args.command == "run": return handle_marketplace_run(args, "jd")
        if args.command == "login": return handle_marketplace_login(args, "jd")
        raise SystemExit(f"unsupported jd command: {args.command}")
    raise SystemExit(f"unsupported platform: {args.platform}")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = build_parser()
    args = parser.parse_args()
    result = dispatch(args)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.html:
        html_path = _html_render.render(result)
        print(f"\n[HTML] {html_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
