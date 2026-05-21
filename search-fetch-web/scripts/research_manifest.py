#!/usr/bin/env python3
"""Persistent manifest for profile-driven research coverage and execution records."""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from _config import RESEARCH_RUNS_DIR as BASE_DIR

import _locking

PROFILE_PATH = Path(__file__).resolve().parent.parent / "assets" / "research-profiles.json"


def load_profiles() -> dict[str, Any]:
    return json.loads(PROFILE_PATH.read_text(encoding="utf-8"))


def load_profile(profile: str) -> dict[str, Any]:
    profiles = load_profiles()
    if profile not in profiles:
        raise ValueError(f"unknown research profile: {profile}")
    return profiles[profile]


def run_path(run_id: str) -> Path:
    return BASE_DIR / f"{run_id}.json"


def create_run(profile: str, query: str) -> dict[str, Any]:
    config = load_profile(profile)
    run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    manifest = {
        "run_id": run_id,
        "profile": profile,
        "query": query,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "required_platforms": config.get("required_platforms", []),
        "optional_platforms": config.get("optional_platforms", []),
        "search_order": config.get("search_order", []),
        "min_successful_required_platforms": config.get("min_successful_required_platforms", 0),
        "min_samples_per_required_platform": config.get("min_samples_per_required_platform", 1),
        "platforms": {},
        "events": [],
        "coverage_ok": False,
        "missing_required": [],
        "research_brief": {},
        "search_strategy": {},
        "execution": {
            "mode": "single_thread_platform_serial",
            "active_platform": None,
            "active_stage": None,
            "platform_sequence": [],
            "platform_stage_history": [],
            "parallel_violation_detected": False,
            "stage_gate_blocked": False,
            "notes": []
        }
    }
    save_run(manifest)
    return manifest


def load_run(run_id: str) -> dict[str, Any]:
    path = run_path(run_id)
    if not path.exists():
        raise FileNotFoundError(f"research run manifest not found: {run_id}")
    return json.loads(path.read_text(encoding="utf-8"))


def _atomic_write_unlocked(path: Path, manifest: dict[str, Any]) -> None:
    """Atomically replace a manifest while the caller holds the manifest lock."""
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    payload = json.dumps(manifest, ensure_ascii=False, indent=2)
    try:
        with tmp_path.open("w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def _read_modify_write(run_id: str, modify_fn) -> dict[str, Any]:
    """Protect manifest read-modify-write with an exclusive flock and atomic replace."""
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    path = run_path(run_id)
    if not path.exists():
        raise FileNotFoundError(f"research run manifest not found: {run_id}")

    # Keep the lock on a stable companion file. Locking the JSON inode itself is
    # unsafe with os.replace(): after replacement, a later opener can lock the
    # new inode while the first writer still holds the old one.
    lock_path = path.with_name(f"{path.name}.lock")
    with lock_path.open("a+b") as lock_handle:
        _locking.flock_ex(lock_handle.fileno())
        try:
            if not path.exists():
                raise FileNotFoundError(f"research run manifest not found: {run_id}")
            raw = path.read_text(encoding="utf-8")
            manifest = json.loads(raw) if raw.strip() else {}
            result = modify_fn(manifest)
            if result is not None:
                manifest = result
            if manifest.get("run_id") != run_id:
                raise ValueError(f"manifest run_id mismatch: expected {run_id}, got {manifest.get('run_id')}")
            _atomic_write_unlocked(path, manifest)
            return manifest
        finally:
            _locking.flock_un(lock_handle.fileno())


def save_run(manifest: dict[str, Any]) -> None:
    """Persist a full manifest using the same locked, atomic write path."""
    run_id = manifest["run_id"]
    path = run_path(run_id)
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        # Create the file before flocking it; the final payload is still written atomically.
        path.touch()

    def replace_manifest(_current: dict[str, Any]) -> dict[str, Any]:
        return manifest

    _read_modify_write(run_id, replace_manifest)


def _platform_entry(manifest: dict[str, Any], domain: str) -> dict[str, Any]:
    return manifest.setdefault("platforms", {}).setdefault(domain, {
        "searched": False,
        "search_path": [],
        "content_fetched": False,
        "fetch_backend": None,
        "samples": 0,
        "blocked_reason": None,
        "notes": [],
        "content_text_length": 0,
        "content_preview": "",
        "quality_text": "",
        "quality_text_length": 0,
        "quality_passed": None,
        "quality_notes": [],
        "sample_details": [],
        "checklist_gate": None,
        "flow_evidence": {},
        "stages": {
            "search_started": False,
            "search_completed": False,
            "fetch_started": False,
            "fetch_completed": False,
            "platform_finalized": False
        }
    })


def begin_platform_stage(run_id: str, domain: str, stage: str) -> dict[str, Any]:
    blocked_error: list[str] = []

    def modify(manifest: dict[str, Any]) -> dict[str, Any]:
        execution = manifest.setdefault("execution", {})
        active_platform = execution.get("active_platform")
        active_stage = execution.get("active_stage")
        if active_platform and active_platform != domain:
            execution["parallel_violation_detected"] = True
            execution["stage_gate_blocked"] = True
            execution.setdefault("notes", []).append(
                f"blocked cross-platform transition: active={active_platform}/{active_stage} requested={domain}/{stage}"
            )
            blocked_error.append(f"platform stage blocked: {active_platform}/{active_stage} still active, cannot enter {domain}/{stage}")
            return manifest
        execution["active_platform"] = domain
        execution["active_stage"] = stage
        if domain not in execution.setdefault("platform_sequence", []):
            execution["platform_sequence"].append(domain)
        execution.setdefault("platform_stage_history", []).append({
            "domain": domain,
            "stage": stage,
            "event": "begin",
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        })
        entry = _platform_entry(manifest, domain)
        if stage == "search":
            entry.setdefault("stages", {})["search_started"] = True
        elif stage == "fetch":
            entry.setdefault("stages", {})["fetch_started"] = True
        return manifest

    manifest = _read_modify_write(run_id, modify)
    if blocked_error:
        raise RuntimeError(blocked_error[0])
    return manifest


def complete_platform_stage(run_id: str, domain: str, stage: str, *, success: bool, blocked_reason: str | None = None, keep_active_on_failure: bool = False) -> dict[str, Any]:
    def modify(manifest: dict[str, Any]) -> dict[str, Any]:
        execution = manifest.setdefault("execution", {})
        entry = _platform_entry(manifest, domain)
        if stage == "search":
            entry.setdefault("stages", {})["search_completed"] = success
        elif stage == "fetch":
            entry.setdefault("stages", {})["fetch_completed"] = success
            entry.setdefault("stages", {})["platform_finalized"] = success
        execution.setdefault("platform_stage_history", []).append({
            "domain": domain,
            "stage": stage,
            "event": "complete",
            "success": success,
            "blocked_reason": blocked_reason,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        })
        if execution.get("active_platform") == domain and execution.get("active_stage") == stage:
            if not (keep_active_on_failure and not success):
                execution["active_platform"] = None
                execution["active_stage"] = None
        if blocked_reason:
            execution["stage_gate_blocked"] = True
        return manifest

    return _read_modify_write(run_id, modify)


def record_search(run_id: str, domain: str, engine: str, query: str, success: bool = True, note: str | None = None) -> dict[str, Any]:
    def modify(manifest: dict[str, Any]) -> dict[str, Any]:
        entry = _platform_entry(manifest, domain)
        entry["searched"] = entry["searched"] or success
        if engine not in entry["search_path"]:
            entry["search_path"].append(engine)
        manifest.setdefault("events", []).append({
            "type": "search",
            "domain": domain,
            "engine": engine,
            "query": query,
            "success": success,
            "note": note,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        })
        return manifest

    return _read_modify_write(run_id, modify)


def record_search_plan(run_id: str, domain: str, query: str, planned_engines: list[str], recommended_engine: str) -> dict[str, Any]:
    def modify(manifest: dict[str, Any]) -> dict[str, Any]:
        platform = manifest.setdefault("platforms", {}).setdefault(domain, {})
        platform["planned_search_path"] = planned_engines[:]
        manifest.setdefault("events", []).append({
            "type": "search_plan",
            "domain": domain,
            "query": query,
            "planned_engines": planned_engines[:],
            "recommended_engine": recommended_engine,
        })
        return manifest

    try:
        return _read_modify_write(run_id, modify)
    except FileNotFoundError:
        raise



def record_fetch(run_id: str, domain: str, backend: str, success: bool, samples: int = 1, blocked_reason: str | None = None, note: str | None = None, content_text: str | None = None, quality_text: str | None = None, quality_passed: bool | None = None, quality_notes: list[str] | None = None, sample_details: list[dict[str, Any]] | None = None, checklist_gate: dict[str, Any] | None = None, flow_evidence: dict[str, Any] | None = None) -> dict[str, Any]:
    def modify(manifest: dict[str, Any]) -> dict[str, Any]:
        entry = _platform_entry(manifest, domain)
        if success:
            entry["content_fetched"] = True
            entry["fetch_backend"] = backend
            entry["samples"] = max(entry.get("samples", 0), samples)
        if content_text is not None:
            entry["content_text_length"] = len(content_text)
            entry["content_preview"] = content_text[:500]
        if quality_text is not None:
            entry["quality_text"] = quality_text
            entry["quality_text_length"] = len(quality_text)
        if quality_passed is not None:
            entry["quality_passed"] = quality_passed
        if quality_notes:
            entry.setdefault("quality_notes", []).extend(quality_notes)
        if blocked_reason:
            entry["blocked_reason"] = blocked_reason
        if note:
            entry.setdefault("notes", []).append(note)
        if sample_details:
            entry["sample_details"] = sample_details
        if checklist_gate is not None:
            entry["checklist_gate"] = checklist_gate
        if flow_evidence is not None:
            entry["flow_evidence"] = flow_evidence
        manifest.setdefault("events", []).append({
            "type": "fetch",
            "domain": domain,
            "backend": backend,
            "success": success,
            "samples": samples,
            "blocked_reason": blocked_reason,
            "note": note,
            "content_text_length": len(content_text) if content_text is not None else None,
            "quality_text_length": len(quality_text) if quality_text is not None else None,
            "quality_passed": quality_passed,
            "quality_notes": quality_notes or [],
            "sample_details_count": len(sample_details or []),
            "checklist_gate": checklist_gate,
            "flow_evidence": flow_evidence or {},
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        })
        return manifest

    return _read_modify_write(run_id, modify)


def sample_evidence_summary(manifest: dict[str, Any], domain: str, limit: int = 3) -> list[dict[str, Any]]:
    entry = manifest.get("platforms", {}).get(domain, {})
    details = entry.get("sample_details", []) or []
    summary = []
    for item in details[:limit]:
        summary.append({
            "title": item.get("title"),
            "url": item.get("url"),
            "main_text_preview": (item.get("main_text") or "")[:200],
            "comments_preview": (item.get("comments") or [])[:3],
            "comment_text_length": item.get("comment_text_length", 0),
        })
    return summary


def finalize_run(run_id: str, coverage_ok: bool, missing_required: list[str]) -> dict[str, Any]:
    def modify(manifest: dict[str, Any]) -> dict[str, Any]:
        execution = manifest.setdefault("execution", {})
        execution["active_platform"] = None
        execution["active_stage"] = None
        manifest["coverage_ok"] = coverage_ok
        manifest["missing_required"] = missing_required
        manifest["sample_evidence"] = {
            domain: sample_evidence_summary(manifest, domain)
            for domain in manifest.get("platforms", {})
            if manifest.get("platforms", {}).get(domain, {}).get("sample_details")
        }
        manifest["finalized_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        return manifest

    return _read_modify_write(run_id, modify)



if __name__ == "__main__":
    raise SystemExit("use this module via guarded_search_fetch.py or validator")
