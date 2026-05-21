#!/usr/bin/env python3
"""Unified guarded entrypoint for profile-driven search/fetch decisions and manifest recording."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib.parse import unquote_plus, urlparse

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import bilibili_comment_fetch  # type: ignore
import checklist_gate  # type: ignore
import content_quality  # type: ignore
import research_manifest  # type: ignore
import playwright_fetch  # type: ignore
import scheduler  # type: ignore
import search_gate  # type: ignore
import xhs_sampler  # type: ignore
import tieba_playwright_fetch  # type: ignore
import douyin_sampler  # type: ignore
import taptap_playwright_fetch  # type: ignore

SEARCH_BACKENDS = {"searxng", "baidu", "playwright"}
FETCH_BACKENDS = {"auto", "playwright", "web_fetch", "http", "api"}
STRICT_PLAYWRIGHT_ONLY_DOMAINS = {"douyin.com"}


def _build_quality_text(*parts: str) -> str:
    chunks = [part.strip() for part in parts if part and part.strip()]
    return "\n\n".join(chunks)


def _quality_text_from_samples(sample_details: list[dict]) -> str:
    blocks: list[str] = []
    for item in sample_details or []:
        title = item.get("title") or ""
        main_text = item.get("main_text") or item.get("main_text_preview") or ""
        comments = item.get("comments") or item.get("comments_preview") or []
        comment_snippet = item.get("comment_snippet") or ""
        block_parts: list[str] = []
        if title:
            block_parts.append(title)
        if main_text:
            block_parts.append(main_text)
        if comment_snippet:
            block_parts.append(comment_snippet)
        if comments:
            block_parts.extend([c for c in comments if c])
        block = _build_quality_text(*block_parts)
        if block:
            blocks.append(block)
    return "\n\n".join(blocks)


def _parse_flag(args: list[str], name: str) -> tuple[str | None, list[str]]:
    if name not in args:
        return None, args
    idx = args.index(name)
    if idx + 1 >= len(args):
        raise SystemExit(f"missing value for {name}")
    value = args[idx + 1]
    new_args = args[:idx] + args[idx + 2:]
    return value, new_args


def _resolve_profile(profile: str | None) -> dict | None:
    if not profile:
        return None
    return research_manifest.load_profile(profile)


def _resolve_fetch_backend(domain: str, requested_backend: str) -> tuple[str, dict]:
    policy = scheduler.resolve_policy(domain)
    allowed = policy.get("allowed_fetch_backends")
    disallowed = set(policy.get("disallowed_fetch_backends", []))
    fetch_mode = policy.get("content_fetch_mode", "default")
    comment_fetch_mode = policy.get("comment_fetch_mode", "default")

    backend = requested_backend
    if backend == "auto":
        if domain == "bilibili.com" and comment_fetch_mode == "api_preferred":
            backend = "api"
        else:
            backend = "playwright" if fetch_mode in {"playwright_only", "playwright_preferred"} else "web_fetch"

    if domain in STRICT_PLAYWRIGHT_ONLY_DOMAINS and backend != "playwright":
        raise ValueError(f"{domain} must use guarded Playwright sampler flow, got backend: {backend}")
    if backend in disallowed:
        raise ValueError(f"{domain} content fetch forbids backend: {backend}")
    if allowed and backend not in allowed:
        raise ValueError(f"{domain} content fetch must use one of {allowed}, got {backend}")

    return backend, policy


def plan_search(query: str, risk_level: str = "medium", need_interaction: bool = False, profile: str | None = None, run_id: str | None = None, domain: str | None = None) -> dict:
    if profile:
        os.environ["SEARCH_FETCH_PROFILE"] = profile
    os.environ.setdefault("SEARCH_FETCH_SESSION_KIND", "main")
    searxng = search_gate.decide("searxng", query, risk_level, need_interaction, profile=profile)
    fallback = None
    recommended_engine = None
    attempted_engines: list[str] = ["searxng"]

    if searxng["decision"]["allowed"] and not searxng.get("recommended_next"):
        recommended_engine = "searxng"
    else:
        if searxng.get("recommended_next") == "baidu":
            fallback = search_gate.decide("baidu", query, risk_level, need_interaction, profile=profile)
            attempted_engines.append("baidu")
            if fallback["decision"]["allowed"] and not fallback.get("recommended_next"):
                recommended_engine = "baidu"
            elif fallback.get("recommended_next") == "playwright":
                recommended_engine = "playwright"
                attempted_engines.append("playwright")
            elif fallback["decision"]["allowed"]:
                recommended_engine = "baidu"
        elif searxng.get("recommended_next") == "playwright":
            recommended_engine = "playwright"
            attempted_engines.append("playwright")
        elif searxng["decision"]["allowed"]:
            recommended_engine = "searxng"

    if run_id and domain and recommended_engine in SEARCH_BACKENDS:
        search_ok = bool(recommended_engine)
        research_manifest.begin_platform_stage(run_id, domain, "search")
        research_manifest.record_search_plan(run_id, domain, query, attempted_engines, recommended_engine)
        research_manifest.record_search(run_id, domain, recommended_engine or "searxng", query, success=search_ok)
        research_manifest.complete_platform_stage(run_id, domain, "search", success=search_ok)

    return {
        "mode": "search",
        "query": query,
        "profile": profile,
        "run_id": run_id,
        "domain": domain,
        "recommended_engine": recommended_engine,
        "primary": searxng,
        "fallback": fallback,
    }


def _scheduler_result_for_fetch(domain: str, outcome: str, blocked: bool, pages_increment: int = 1) -> dict:
    try:
        return scheduler.record_result(domain, outcome, blocked=blocked, pages_increment=pages_increment)
    except Exception as exc:
        return {"ok": False, "error": f"scheduler_record_failed:{type(exc).__name__}", "message": str(exc)}


def _schedule_specialized_fetch(domain: str, url: str, profile: str | None, run_id: str | None, backend: str, policy: dict) -> dict | None:
    previous_lock_id = os.environ.get("SEARCH_FETCH_LOCK_ID")
    temp_lock_id = f"specialized-{domain}"
    os.environ["SEARCH_FETCH_LOCK_ID"] = temp_lock_id
    try:
        decision = scheduler.schedule(domain, "fetch")
    finally:
        if previous_lock_id is None:
            os.environ.pop("SEARCH_FETCH_LOCK_ID", None)
        else:
            os.environ["SEARCH_FETCH_LOCK_ID"] = previous_lock_id
    if decision.get("allowed"):
        return None
    return {
        "mode": "fetch",
        "url": url,
        "domain": domain,
        "profile": profile,
        "run_id": run_id,
        "backend": backend,
        "policy": policy,
        "allowed": False,
        "reason": decision.get("reason"),
        "wait_seconds": decision.get("wait_seconds"),
        "scheduler": decision,
    }


def execute_fetch(url: str, profile: str | None = None, run_id: str | None = None, backend: str = "auto", domain: str | None = None) -> dict:
    resolved_domain = scheduler.normalize_domain(domain or url)
    if profile:
        os.environ["SEARCH_FETCH_PROFILE"] = profile
    os.environ.setdefault("SEARCH_FETCH_SESSION_KIND", "main")
    selected_backend, policy = _resolve_fetch_backend(resolved_domain, backend)

    if policy.get("isolated_run_only"):
        os.environ["SEARCH_FETCH_RUN_SCOPE"] = resolved_domain

    # Only enforce Playwright for high-risk community domains; allow web_fetch for normal sites
    _is_high_risk_community = (
        resolved_domain in scheduler.HIGH_RISK_DOMAINS
        or policy.get("content_fetch_mode") in {"playwright_only", "playwright_preferred"}
        or policy.get("allowed_fetch_backends") == ["playwright"]
    )
    if selected_backend != "playwright" and _is_high_risk_community:
        raise ValueError(
            f"{resolved_domain} fetch backend resolved to {selected_backend}; high-risk community content must not bypass Playwright"
        )

    # Non-high-risk domains with web_fetch/http backend: pass through as a policy-ok signal.
    # The caller (safe_fetch_router or agent) is responsible for the actual HTTP fetch.
    if selected_backend != "playwright" and not _is_high_risk_community:
        return {
            "mode": "fetch",
            "url": url,
            "domain": resolved_domain,
            "profile": profile,
            "run_id": run_id,
            "backend": selected_backend,
            "policy": policy,
            "allowed": True,
            "reason": "normal_domain_web_fetch_allowed",
            "note": f"{resolved_domain} is not a high-risk community domain; caller should use web_fetch directly.",
        }

    if resolved_domain == "xiaohongshu.com" and ("search_result" in url or "/search_result?" in url):
        blocked = _schedule_specialized_fetch(resolved_domain, url, profile, run_id, selected_backend, policy)
        if blocked:
            return blocked
        query = url.split("keyword=", 1)[1].split("&", 1)[0] if "keyword=" in url else url
        query = unquote_plus(query)
        result = xhs_sampler.sample(query)
        valid_items = [
            item for item in result.get("items", [])
            if item.get("parse_ok") and item.get("is_expected_domain") and not item.get("looks_like_search_page")
        ]
        text_parts = []
        for item in valid_items:
            if item.get("main_text"):
                text_parts.append(item["main_text"])
            if item.get("comment_snippet"):
                text_parts.append(item["comment_snippet"])
        text = "\n\n".join(text_parts)
        quality = content_quality.evaluate(profile, resolved_domain, text) if profile else {"passed": True, "text_length": len(text), "notes": []}
        flow_evidence = dict(result.get("flow_evidence") or {})
        flow_evidence.update({
            "opened_note_count": len(valid_items),
            "opened_note_urls": [item.get("url") for item in valid_items if item.get("url")],
        })
        checklist = {
            "search_or_entry_opened": bool(flow_evidence.get("search_engine_discovery_complete", True)),
            "real_note_detail_opened": len(valid_items) > 0,
            "note_text_or_comment_captured": bool(text.strip()),
            "quality_gate_passed": bool(quality.get("passed")),
        }
        gate = checklist_gate.evaluate(resolved_domain, checklist, flow_evidence, fetch_backend=selected_backend)
        success = bool(result.get("ok") and len(valid_items) >= 5 and quality.get("passed") and gate.get("passed"))
        blocked_reason = None if result.get("ok") else result.get("reason", "sampler_failed")
        if len(valid_items) < 5:
            blocked_reason = "insufficient_valid_note_samples"
        if not gate.get("passed"):
            blocked_reason = "checklist_gate_failed"

        if run_id:
            sample_details = []
            for item in valid_items:
                sample_details.append({
                    "title": item.get("title"),
                    "url": item.get("url"),
                    "main_text": item.get("main_text", "")[:800],
                    "comment_snippet": item.get("comment_snippet", "")[:800],
                    "comments": item.get("comments", [])[:10],
                    "comment_text_length": item.get("comment_text_length", 0),
                    "coord": item.get("coord"),
                })
            research_manifest.begin_platform_stage(run_id, resolved_domain, "fetch")
            quality_text = _build_quality_text(text, _quality_text_from_samples(sample_details))
            research_manifest.record_fetch(
                run_id,
                resolved_domain,
                "playwright",
                success=success,
                samples=len(valid_items),
                blocked_reason=blocked_reason,
                note=f"policy={policy.get('content_fetch_mode', 'default')}; sampler=search_result; checklist_gate={json.dumps(gate, ensure_ascii=False)}",
                content_text=text,
                quality_text=quality_text,
                quality_passed=quality.get("passed"),
                quality_notes=quality.get("notes", []),
                sample_details=sample_details,
                checklist_gate=gate,
                flow_evidence=flow_evidence,
            )
            research_manifest.complete_platform_stage(run_id, resolved_domain, "fetch", success=success, blocked_reason=blocked_reason)

        record = result.get("record") or _scheduler_result_for_fetch(
            resolved_domain,
            "ok" if success else (blocked_reason or "sampler_failed"),
            blocked=not success,
            pages_increment=1,
        )
        return {
            "mode": "fetch",
            "url": url,
            "domain": resolved_domain,
            "profile": profile,
            "run_id": run_id,
            "backend": "playwright",
            "policy": policy,
            "result": result,
            "quality": quality,
            "record": record,
        }

    if resolved_domain == "tieba.baidu.com":
        blocked = _schedule_specialized_fetch(resolved_domain, url, profile, run_id, selected_backend, policy)
        if blocked:
            return blocked
        if "/f/search/res" in url:
            query = url.split("qw=", 1)[1].split("&", 1)[0] if "qw=" in url else url
            query = unquote_plus(query)
            result = tieba_playwright_fetch.sample(query)
            note_suffix = "bar_direct_entry"
        elif "/f?kw=" in url:
            query = url.split("kw=", 1)[1].split("&", 1)[0] if "kw=" in url else url
            query = unquote_plus(query)
            result = tieba_playwright_fetch.sample(query)
            note_suffix = "bar_direct_entry"
        else:
            query = url
            result = tieba_playwright_fetch.sample(query)
            note_suffix = "thread_search_legacy"
        valid_items = [item for item in result.get("items", []) if item.get("parse_ok") and "/p/" in ((item.get("detail_url") or item.get("url") or ""))]
        flow_evidence = {
            "thread_detail_count": len(valid_items),
            "thread_detail_urls": [item.get("detail_url") or item.get("url") for item in valid_items if (item.get("detail_url") or item.get("url"))],
        }
        text = "\n\n".join((item.get("main_text") or "") for item in valid_items)
        quality = content_quality.evaluate(profile, resolved_domain, text) if profile else {"passed": True, "text_length": len(text), "notes": []}
        checklist = result.get("checklist") or {}
        checklist["quality_gate_passed"] = bool(quality.get("passed"))
        gate = checklist_gate.evaluate(resolved_domain, checklist, flow_evidence, fetch_backend=selected_backend)
        required_count = 5
        success = bool(result.get("ok") and len(valid_items) >= required_count and quality.get("passed") and gate.get("passed"))
        blocked_reason = None if result.get("ok") else result.get("reason", "sampler_failed")
        if len(valid_items) < required_count:
            blocked_reason = "insufficient_valid_thread_samples"
        if not gate.get("passed"):
            blocked_reason = "checklist_gate_failed"
        sample_details = []
        for item in valid_items:
            sample_details.append({
                "title": item.get("title") or item.get("candidate", {}).get("title") or item.get("candidate", {}).get("text"),
                "url": item.get("detail_url") or item.get("url"),
                "main_text": (item.get("main_text") or "")[:800],
                "comments": item.get("comments", [])[:10],
                "comment_text_length": item.get("comment_text_length", 0),
                "candidate": item.get("candidate", {}),
            })
        if run_id:
            research_manifest.begin_platform_stage(run_id, resolved_domain, "fetch")
            quality_text = _build_quality_text(text, _quality_text_from_samples(sample_details))
            research_manifest.record_fetch(
                run_id,
                resolved_domain,
                "playwright",
                success=success,
                samples=len(sample_details) if success else 0,
                blocked_reason=blocked_reason,
                note=f"policy={policy.get('content_fetch_mode', 'default')}; sampler={note_suffix}; checklist_gate={json.dumps(gate, ensure_ascii=False)}",
                content_text=text,
                quality_text=quality_text,
                quality_passed=quality.get("passed"),
                quality_notes=quality.get("notes", []),
                sample_details=sample_details,
                checklist_gate=gate,
                flow_evidence=flow_evidence,
            )
            research_manifest.complete_platform_stage(run_id, resolved_domain, "fetch", success=success, blocked_reason=blocked_reason)
        record = _scheduler_result_for_fetch(
            resolved_domain,
            "ok" if success else (blocked_reason or "sampler_failed"),
            blocked=not success,
            pages_increment=1,
        )
        return {
            "mode": "fetch",
            "url": url,
            "domain": resolved_domain,
            "profile": profile,
            "run_id": run_id,
            "backend": "playwright",
            "policy": policy,
            "result": result,
            "quality": quality,
            "sample_details": sample_details,
            "record": record,
        }

    if resolved_domain == "douyin.com" and "/search/" in url:
        blocked = _schedule_specialized_fetch(resolved_domain, url, profile, run_id, selected_backend, policy)
        if blocked:
            return blocked
        query = url.split('/search/', 1)[1].split('?', 1)[0] if '/search/' in url else url
        query = unquote_plus(query)
        result = douyin_sampler.sample(query)
        valid_items = [
            item for item in result.get("items", [])
            if item.get("parse_ok")
            and "modal_id=" in (item.get("url") or "")
            and item.get("comment_opened")
            and item.get("comment_text_length", 0) >= 16
            and len(item.get("comments", []) or []) >= 2
        ]
        flow_evidence = {
            "opened_modal_count": len(valid_items),
            "opened_modal_urls": [item.get("url") for item in valid_items if item.get("url")],
            "comment_open_success_count": sum(1 for item in valid_items if item.get("comment_opened")),
            "comment_sample_count": sum(len(item.get("comments", []) or []) for item in valid_items),
        }
        text = "\n\n".join((item.get("main_text") or "") for item in valid_items)
        quality = content_quality.evaluate(profile, resolved_domain, text) if profile else {"passed": True, "text_length": len(text), "notes": []}
        checklist = result.get("checklist") or {}
        checklist["quality_gate_passed"] = bool(quality.get("passed"))
        gate = checklist_gate.evaluate(resolved_domain, checklist, flow_evidence, fetch_backend=selected_backend)
        success = bool(result.get("ok") and len(valid_items) >= 2 and quality.get("passed") and gate.get("passed"))
        blocked_reason = None if success else result.get("reason", "sampler_failed_or_no_modal_comments")
        if not gate.get("passed"):
            blocked_reason = "checklist_gate_failed"
        sample_details = []
        for item in valid_items:
            sample_details.append({
                "title": item.get("title") or item.get("candidate", {}).get("title"),
                "url": item.get("url"),
                "main_text": (item.get("main_text") or "")[:800],
                "comments": item.get("comments", [])[:10],
                "comment_text_length": item.get("comment_text_length", 0),
                "comment_opened": item.get("comment_opened", False),
                "parse_ok": item.get("parse_ok", False),
            })
        if run_id:
            research_manifest.begin_platform_stage(run_id, resolved_domain, "fetch")
            quality_text = _build_quality_text(text, _quality_text_from_samples(sample_details))
            research_manifest.record_fetch(
                run_id,
                resolved_domain,
                "playwright",
                success=success,
                samples=len(sample_details) if success else 0,
                blocked_reason=blocked_reason,
                note=f"policy={policy.get('content_fetch_mode', 'default')}; sampler=modal_search; checklist_gate={json.dumps(gate, ensure_ascii=False)}",
                content_text=text,
                quality_text=quality_text,
                quality_passed=quality.get("passed"),
                quality_notes=quality.get("notes", []),
                sample_details=sample_details,
                checklist_gate=gate,
                flow_evidence=flow_evidence,
            )
            research_manifest.complete_platform_stage(run_id, resolved_domain, "fetch", success=success, blocked_reason=blocked_reason)
        record = _scheduler_result_for_fetch(
            resolved_domain,
            "ok" if success else (blocked_reason or "sampler_failed_or_no_modal_comments"),
            blocked=not success,
            pages_increment=1,
        )
        return {
            "mode": "fetch",
            "url": url,
            "domain": resolved_domain,
            "profile": profile,
            "run_id": run_id,
            "backend": "playwright",
            "policy": policy,
            "result": result,
            "quality": quality,
            "sample_details": sample_details,
            "record": record,
        }

    result = playwright_fetch.fetch(url)
    text = result.get("text", "") or ""
    quality = content_quality.evaluate(profile, resolved_domain, text) if profile else {"passed": True, "text_length": len(text), "notes": []}
    success = bool(result.get("allowed") and not result.get("blocked") and text and quality.get("passed"))
    blocked_reason = None
    if not result.get("allowed"):
        blocked_reason = result.get("reason")
    elif result.get("blocked"):
        blocked_reason = result.get("reason") or "block_signal"

    sample_details = []
    flow_evidence = {}
    if resolved_domain == "bilibili.com" and success:
        search_url = result.get("final_url") or url
        import _playwright_base as pw
        detail_page = pw.new_page()
        try:
            playwright_fetch.open_url(detail_page, search_url)
            playwright_fetch.human_pause(2, 6, precision=1)
            playwright_fetch.wait_ready(detail_page)
            playwright_fetch.gentle_scroll(detail_page, search_url)
            playwright_fetch.human_pause(1, 3, precision=1)
            search_samples = playwright_fetch.extract_bilibili_search_samples(detail_page, limit=8)
            detail_clicks = []
            opened_bvids = []
            visible_comment_bvids = []
            overlay_handled = True
            detail_note = "search_only"

            for index, item in enumerate(search_samples[:5]):
                if index > 0:
                    playwright_fetch.open_url(detail_page, search_url)
                    playwright_fetch.human_pause(2, 6, precision=1)
                    playwright_fetch.wait_ready(detail_page)
                    playwright_fetch.gentle_scroll(detail_page, search_url)
                    playwright_fetch.human_pause(1, 3, precision=1)
                href = item.get("href") or ""
                bvid = playwright_fetch.extract_bilibili_bvid(href)
                if bvid:
                    pw.strip_target_blank(detail_page, f'a[href*="/video/{bvid}"]')
                clicked = playwright_fetch.click_bilibili_video_result(detail_page, index=index)
                actual_bvid = clicked.get("bvid") or bvid or playwright_fetch.extract_bilibili_bvid(clicked.get("href") or href)
                if actual_bvid and not clicked.get("bvid"):
                    clicked["bvid"] = actual_bvid
                detail_clicks.append(clicked)
                if not clicked.get("ok") or not href or not actual_bvid:
                    continue

                # click_bilibili_video_result may close detail_page when target="_blank" wasn't stripped
                if clicked.get("_switched_page") and clicked.get("_new_page"):
                    scan_page = clicked["_new_page"]
                    detail_page = scan_page
                else:
                    scan_page = detail_page

                playwright_fetch.human_pause(2, 5, precision=1)
                playwright_fetch.wait_ready(scan_page)
                overlay_result = playwright_fetch.dismiss_bilibili_overlay_and_scroll(scan_page)
                overlay_handled = overlay_handled and bool(overlay_result.get('clicked', 0) >= 0)
                playwright_fetch.human_pause(2, 4, precision=1)
                playwright_fetch.gentle_scroll(scan_page, href)
                playwright_fetch.human_pause(1, 3, precision=1)
                meta = playwright_fetch.extract_bilibili_video_meta(scan_page)
                final_url = playwright_fetch.current_url(scan_page) or href
                final_bvid = playwright_fetch.extract_bilibili_bvid(final_url) or actual_bvid
                comments = playwright_fetch.extract_bilibili_video_comments(scan_page, limit=8)
                debug_snapshot = {}
                comment_scroll = {}
                if not comments:
                    comment_scroll = playwright_fetch.scroll_to_bilibili_comment_region(scan_page)
                    playwright_fetch.human_pause(5, 10, precision=1)
                    comments = playwright_fetch.extract_bilibili_video_comments(scan_page, limit=8)
                if not comments:
                    playwright_fetch.gentle_scroll(scan_page, final_url)
                    playwright_fetch.human_pause(2, 4, precision=1)
                    comments = playwright_fetch.extract_bilibili_video_comments(scan_page, limit=8)
                if not comments:
                    debug_snapshot = playwright_fetch.bilibili_comment_debug_snapshot(scan_page)
                    debug_snapshot["comment_scroll"] = comment_scroll
                if final_bvid:
                    opened_bvids.append(final_bvid)
                    if comments:
                        visible_comment_bvids.append(final_bvid)
                sample_details.append({
                    "bvid": final_bvid,
                    "title": meta.get("title") or clicked.get("title"),
                    "url": final_url,
                    "main_text": (meta.get("bodyPreview") or clicked.get("parent") or clicked.get("title") or "")[:800],
                    "comments": comments,
                    "comment_text_length": sum(len(c) for c in comments),
                    "debug_snapshot": debug_snapshot,
                    "overlay_result": overlay_result,
                })
                playwright_fetch.human_pause(1, 2, precision=1)

            detail_note = "detail_with_comments" if visible_comment_bvids else "detail_without_comments"
            flow_evidence = {
                "opened_video_count": len(opened_bvids),
                "opened_bvids": opened_bvids,
                "comment_videos": len(visible_comment_bvids),
                "visible_comment_videos": visible_comment_bvids,
            }
            aggregate_checklist = {
                "search_opened": bool(search_samples),
                "real_video_opened": len(opened_bvids) > 0,
                "ad_or_overlay_handled": overlay_handled,
                "real_bvid_confirmed": len(opened_bvids) > 0,
                "cookie_checked": False,
                "comment_json_or_visible_comment_captured": len(visible_comment_bvids) > 0,
                "quality_gate_passed": True,
            }
            if sample_details:
                sample_details[0]["checklist"] = aggregate_checklist
            result["bilibili_detail_clicks"] = detail_clicks
            result["bilibili_detail_note"] = detail_note
            result["bilibili_checklist"] = aggregate_checklist
            result["bilibili_flow_evidence"] = flow_evidence
        finally:
            pw.close_page(detail_page)
    elif resolved_domain == "taptap.cn" and success:
        taptap_query = result.get("final_title") or result.get("final_url") or url
        taptap_result = taptap_playwright_fetch.fetch(taptap_query)
        review_blocks = [
            taptap_result.get("comprehensive", {}),
            taptap_result.get("comprehensive_sorted", {}),
            taptap_result.get("latest", {}),
        ]
        for block in review_blocks:
            reviews = block.get("reviews") or []
            if not reviews:
                continue
            sample_details.append({
                "title": block.get("title") or taptap_result.get("query"),
                "url": block.get("url"),
                "main_text": (block.get("bodyPreview") or "")[:800],
                "comments": reviews[:10],
                "comment_text_length": sum(len(x) for x in reviews[:10]),
                "sort_tab": "comprehensive" if block in (review_blocks[0], review_blocks[1]) else "latest",
            })
        flow_evidence = {
            "opened_review_block_count": len(sample_details),
            "opened_review_urls": [item.get("url") for item in sample_details if item.get("url")],
            "sort_tabs": [item.get("sort_tab") for item in sample_details if item.get("sort_tab")],
        }
        result["taptap_review_flow"] = taptap_result

    gate = {"passed": True, "required": [], "checklist": {}, "missing": []}
    if resolved_domain == "bilibili.com":
        checklist = result.get("bilibili_checklist") or (sample_details[0].get("checklist") if sample_details else {}) or {}
        checklist["quality_gate_passed"] = bool(quality.get("passed"))
        gate = checklist_gate.evaluate(resolved_domain, checklist, flow_evidence, fetch_backend=selected_backend)
        success = bool(success and sample_details and gate.get("passed"))
        if not gate.get("passed"):
            blocked_reason = "checklist_gate_failed"
    elif resolved_domain == "taptap.cn":
        review_text = _quality_text_from_samples(sample_details)
        if review_text and len(review_text) > len(text):
            quality = content_quality.evaluate(profile, resolved_domain, review_text) if profile else {"passed": True, "text_length": len(review_text), "notes": []}
        checklist = (result.get("taptap_review_flow") or {}).get("checklist") or {}
        checklist["quality_gate_passed"] = bool(quality.get("passed"))
        gate = checklist_gate.evaluate(resolved_domain, checklist, flow_evidence, fetch_backend=selected_backend)
        success = bool(success and sample_details and gate.get("passed"))
        if not gate.get("passed"):
            blocked_reason = "checklist_gate_failed"
    if run_id:
        research_manifest.begin_platform_stage(run_id, resolved_domain, "fetch")
        quality_text = _build_quality_text(text, _quality_text_from_samples(sample_details))
        research_manifest.record_fetch(
            run_id,
            resolved_domain,
            "playwright",
            success=success,
            samples=len(sample_details) if success else 0,
            blocked_reason=blocked_reason,
            note=f"policy={policy.get('content_fetch_mode', 'default')}{'; sampler=review_fetch' if resolved_domain == 'taptap.cn' else ''}; checklist_gate={json.dumps(gate, ensure_ascii=False)}",
            content_text=text,
            quality_text=quality_text,
            quality_passed=quality.get("passed"),
            quality_notes=quality.get("notes", []),
            sample_details=sample_details,
            checklist_gate=gate,
            flow_evidence=flow_evidence,
        )
        research_manifest.complete_platform_stage(run_id, resolved_domain, "fetch", success=success, blocked_reason=blocked_reason)

    record = result.get("record") or _scheduler_result_for_fetch(
        resolved_domain,
        "ok" if success else (blocked_reason or result.get("reason") or "fetch_failed"),
        blocked=not success,
        pages_increment=1 if success else 0,
    )
    return {
        "mode": "fetch",
        "url": url,
        "domain": resolved_domain,
        "profile": profile,
        "run_id": run_id,
        "backend": "playwright",
        "policy": policy,
        "result": result,
        "quality": quality,
        "sample_details": sample_details,
        "record": record,
    }


def deferred_required_domains(profile: str | None) -> list[str]:
    if not profile:
        return []
    cfg = research_manifest.load_profile(profile)
    domains = []
    for domain in cfg.get("required_platforms", []) + cfg.get("optional_platforms", []):
        if scheduler.resolve_policy(domain).get("defer_in_profile"):
            domains.append(domain)
    return domains


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit(
            "usage: guarded_search_fetch.py <manifest-init|manifest-finalize|search|fetch> ..."
        )

    mode = sys.argv[1]
    args = sys.argv[2:]

    profile, args = _parse_flag(args, "--profile")
    run_id, args = _parse_flag(args, "--run-id")
    domain, args = _parse_flag(args, "--domain")
    backend, args = _parse_flag(args, "--backend")
    risk_level, args = _parse_flag(args, "--risk-level")
    need_interaction_raw, args = _parse_flag(args, "--need-interaction")
    need_interaction = bool(need_interaction_raw and need_interaction_raw.lower() in {"1", "true", "yes"})

    if mode == "manifest-init":
        if not profile or not args:
            raise SystemExit("usage: guarded_search_fetch.py manifest-init <query> --profile <profile>")
        scheduler.reset_run("all")
        manifest = research_manifest.create_run(profile, args[0])
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
        return

    if mode == "manifest-finalize":
        if not run_id:
            raise SystemExit("usage: guarded_search_fetch.py manifest-finalize --run-id <run_id>")
        manifest = research_manifest.load_run(run_id)
        missing_required = [
            domain for domain in manifest.get("required_platforms", [])
            if not manifest.get("platforms", {}).get(domain, {}).get("content_fetched")
        ]
        validator_result = None
        validator_ok = False
        try:
            import validate_research_run  # type: ignore
            validator_result = validate_research_run.validate(str(research_manifest.run_path(run_id)), manifest.get("profile"))
            validator_ok = bool(validator_result.get("ok"))
        except Exception as exc:
            validator_result = {"ok": False, "errors": [f"validator_failed:{type(exc).__name__}:{exc}"]}
        finalized = research_manifest.finalize_run(run_id, coverage_ok=(not missing_required and validator_ok), missing_required=missing_required)
        finalized["validator"] = validator_result
        finalized["deferred_domains"] = deferred_required_domains(finalized.get("profile"))
        print(json.dumps(finalized, ensure_ascii=False, indent=2))
        raise SystemExit(0 if finalized.get("coverage_ok") else 1)

    if mode == "search":
        if not args:
            raise SystemExit("usage: guarded_search_fetch.py search <query> [--profile <profile>] [--run-id <run_id>] [--domain <domain>]")
        result = plan_search(
            args[0],
            risk_level=risk_level or "medium",
            need_interaction=need_interaction,
            profile=profile,
            run_id=run_id,
            domain=domain,
        )
        if profile and domain and scheduler.resolve_policy(domain).get("defer_in_profile"):
            result["deferred_in_profile"] = True
            result["defer_reason"] = "high_risk_domain_should_run_late_and_in_isolation"
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    if mode == "fetch":
        if not args:
            raise SystemExit("usage: guarded_search_fetch.py fetch <url> [--profile <profile>] [--run-id <run_id>] [--backend <backend>] [--domain <domain>]")
        result = execute_fetch(
            args[0],
            profile=profile,
            run_id=run_id,
            backend=backend or "auto",
            domain=domain,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    raise SystemExit(f"unknown mode: {mode}")


if __name__ == "__main__":
    main()
