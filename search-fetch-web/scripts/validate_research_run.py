#!/usr/bin/env python3
"""Validate profile-driven research manifest before final deep-research output."""

from __future__ import annotations

import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import content_quality  # type: ignore
import research_manifest  # type: ignore
import scheduler  # type: ignore


def _sample_matches_domain(sample: dict, expected_domain: str) -> bool:
    url = sample.get("url", "")
    if not url:
        return False
    return scheduler.normalize_domain(url) == expected_domain


def _validate_event_order(manifest: dict, required_platforms: list[str], errors: list[str]) -> None:
    events = manifest.get("events", []) or []
    for domain in required_platforms:
        domain_events = [event for event in events if event.get("domain") == domain]
        if not domain_events:
            continue
        first_search = next((i for i, event in enumerate(domain_events) if event.get("type") == "search"), None)
        first_fetch = next((i for i, event in enumerate(domain_events) if event.get("type") == "fetch"), None)
        if first_fetch is not None and first_search is not None and first_fetch < first_search:
            errors.append(f"required platform event order invalid (fetch before search): {domain}")


def validate(manifest_path: str, profile: str | None = None) -> dict:
    path = Path(manifest_path)
    if not path.exists():
        raise FileNotFoundError(f"manifest not found: {manifest_path}")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    profile_name = profile or manifest["profile"]
    config = research_manifest.load_profile(profile_name)
    errors: list[str] = []
    required_platforms = config.get("required_platforms", [])
    min_samples = config.get("min_samples_per_required_platform", 1)
    required_search_backends = config.get("required_search_backends", {}).get("default", [])
    backend_policy = config.get("content_backend_policy", {})
    min_lengths = config.get("min_content_text_length", {})

    successful_required = 0

    execution = manifest.get("execution") or {}
    if execution.get("parallel_violation_detected"):
        errors.append("execution parallel violation detected")
    if execution.get("stage_gate_blocked"):
        errors.append("execution stage_gate_blocked")
    if execution.get("active_platform") or execution.get("active_stage"):
        errors.append("execution still has active platform/stage")
    _validate_event_order(manifest, required_platforms, errors)

    for domain in required_platforms:
        entry = manifest.get("platforms", {}).get(domain)
        if not entry:
            errors.append(f"missing required platform: {domain}")
            continue

        if not entry.get("searched"):
            errors.append(f"required platform not searched: {domain}")

        search_path = entry.get("search_path", [])
        for backend in required_search_backends:
            if backend not in search_path:
                errors.append(f"required platform missing search backend {backend}: {domain}")

        if not entry.get("content_fetched"):
            errors.append(f"required platform missing content fetch: {domain}")
            continue

        successful_required += 1
        samples = entry.get("samples", 0)
        if samples < min_samples:
            errors.append(f"required platform samples below minimum ({samples} < {min_samples}): {domain}")

        expected_backend = backend_policy.get(domain)
        actual_backend = entry.get("fetch_backend")
        if expected_backend == "playwright" and actual_backend != "playwright":
            errors.append(f"required platform fetched with forbidden backend ({actual_backend}): {domain}")

        min_length = min_lengths.get(domain, min_lengths.get("default", 0))
        text_length = entry.get("content_text_length", 0)
        if text_length < min_length:
            errors.append(f"required platform content too short ({text_length} < {min_length}): {domain}")

        quality_source_text = entry.get("quality_text") or entry.get("content_preview") or ""
        quality_recomputed = content_quality.evaluate(profile_name, domain, quality_source_text)
        quality_passed = entry.get("quality_passed")
        if quality_passed is False:
            notes = entry.get("quality_notes", [])
            errors.append(f"required platform content quality failed: {domain} ({'; '.join(notes)})")
        elif quality_source_text and not quality_recomputed.get("passed"):
            errors.append(f"required platform content quality failed: {domain} ({'; '.join(quality_recomputed.get('notes', []))})")

        sample_details = entry.get("sample_details", []) or []
        checklist_gate = entry.get("checklist_gate") or {}
        flow_evidence = entry.get("flow_evidence") or {}
        stages = entry.get("stages") or {}
        if actual_backend in {"playwright", "api"} and not sample_details:
            errors.append(f"required platform missing sample details: {domain}")
        if not checklist_gate.get("passed"):
            errors.append(f"required platform checklist gate failed: {domain}")
        if not stages.get("search_completed"):
            errors.append(f"required platform search stage incomplete: {domain}")
        if not stages.get("fetch_completed"):
            errors.append(f"required platform fetch stage incomplete: {domain}")
        if domain == "douyin.com" and actual_backend == "playwright":
            if int(flow_evidence.get("opened_modal_count") or 0) < 2:
                errors.append("required platform douyin.com opened fewer than 2 modal details")
            notes = " ".join(entry.get("notes", []) or [])
            if "sampler=modal_search" not in notes:
                errors.append("required platform douyin.com missing modal_search sampler note")
            comment_samples = [s for s in sample_details if s.get("comment_opened") and len(s.get("comments", []) or []) >= 2]
            if len(comment_samples) < 2:
                errors.append(f"required platform douyin.com has fewer than 2 modal comment samples (got {len(comment_samples)})")
        if domain == "tieba.baidu.com" and actual_backend == "playwright":
            if int(flow_evidence.get("thread_detail_count") or 0) < 5:
                errors.append("required platform tieba.baidu.com opened fewer than 5 thread details")
            notes = " ".join(entry.get("notes", []) or [])
            if "sampler=bar_direct_entry" not in notes:
                errors.append("required platform tieba.baidu.com must use bar_direct_entry sampler")
            if len(sample_details) < 5:
                errors.append("required platform tieba.baidu.com has fewer than 5 thread detail samples")
            if not all(
                "/p/" in (sample.get("url") or "")
                and len((sample.get("main_text") or "").strip()) >= 80
                for sample in sample_details
            ):
                errors.append("required platform tieba.baidu.com contains invalid thread detail samples")
        for sample in sample_details:
            if not _sample_matches_domain(sample, domain):
                errors.append(f"required platform sample detail domain mismatch: {domain} -> {sample.get('url')}")
        if domain == "bilibili.com" and actual_backend == "playwright":
            if int(flow_evidence.get("opened_video_count") or 0) < 5:
                errors.append("required platform bilibili.com opened fewer than 5 videos")
            if len(flow_evidence.get("opened_bvids") or []) < 5:
                errors.append("required platform bilibili.com missing 5 distinct bvid records")
            notes = " ".join(entry.get("notes", []) or [])
            if "policy=playwright_only" not in notes:
                errors.append("required platform bilibili.com missing playwright_only policy note")
            if len(sample_details) < 5:
                errors.append("required platform bilibili.com has fewer than 5 video samples")
            if not any(len(sample.get("comments", []) or []) > 0 for sample in sample_details):
                errors.append("required platform bilibili.com missing visible comment samples")
        if domain == "xiaohongshu.com" and actual_backend == "playwright":
            notes = " ".join(entry.get("notes", []) or [])
            if "sampler=search_result" not in notes:
                errors.append("required platform xiaohongshu.com missing search_result sampler note")
            if len(sample_details) < 5:
                errors.append("required platform xiaohongshu.com has fewer than 5 detail samples")
            if not all(len((sample.get("main_text") or "").strip()) > 0 for sample in sample_details):
                errors.append("required platform xiaohongshu.com contains empty detail text")
            if not all(len(sample.get("comments", []) or []) >= 1 and sample.get("comment_text_length", 0) >= 1 for sample in sample_details[:5]):
                errors.append("required platform xiaohongshu.com missing visible comment samples")
        if domain == "taptap.cn" and actual_backend == "playwright":
            notes = " ".join(entry.get("notes", []) or [])
            if len(sample_details) < 1:
                errors.append("required platform taptap.cn has no review blocks")
            if not sample_details:
                errors.append("required platform taptap.cn missing review samples")
            # TapTap 评论内容可在搜索页/详情页直接抓取，无需强制点开评论 tab
            # 也不再强制要求 comprehensive/latest sort tab 覆盖

    for domain, entry in manifest.get("platforms", {}).items():
        expected_backend = backend_policy.get(domain)
        actual_backend = entry.get("fetch_backend")
        if expected_backend == "playwright" and actual_backend and actual_backend != "playwright":
            errors.append(f"platform fetched with forbidden backend ({actual_backend}): {domain}")
        policy = scheduler.resolve_policy(domain)
        if entry.get("content_fetched") and actual_backend:
            disallowed = policy.get("disallowed_fetch_backends", [])
            if actual_backend in disallowed:
                errors.append(f"platform fetched with domain-disallowed backend ({actual_backend}): {domain}")
        if entry.get("quality_passed") is False:
            notes = entry.get("quality_notes", [])
            errors.append(f"platform content quality failed: {domain} ({'; '.join(notes)})")
        for sample in entry.get("sample_details", []) or []:
            if not _sample_matches_domain(sample, domain):
                errors.append(f"platform sample detail domain mismatch: {domain} -> {sample.get('url')}")

    if successful_required < config.get("min_successful_required_platforms", 0):
        errors.append(
            f"successful required platforms below minimum ({successful_required} < {config.get('min_successful_required_platforms', 0)})"
        )

    missing_required = [
        domain for domain in required_platforms
        if not manifest.get("platforms", {}).get(domain, {}).get("content_fetched")
    ]

    sample_evidence = {
        domain: research_manifest.sample_evidence_summary(manifest, domain)
        for domain in manifest.get("platforms", {})
        if manifest.get("platforms", {}).get(domain, {}).get("sample_details")
    }

    return {
        "ok": not errors,
        "profile": profile_name,
        "run_id": manifest.get("run_id"),
        "errors": errors,
        "missing_required": missing_required,
        "successful_required": successful_required,
        "sample_evidence": sample_evidence,
    }


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("usage: validate_research_run.py <manifest-path> [profile]")
    try:
        result = validate(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)
    except FileNotFoundError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        raise SystemExit(1)
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["ok"] else 1)
