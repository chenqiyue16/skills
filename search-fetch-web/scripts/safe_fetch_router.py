#!/usr/bin/env python3
"""Policy-enforced fetch router that blocks forbidden backends for high-risk domains."""

from __future__ import annotations

import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import guarded_search_fetch  # type: ignore
import research_manifest  # type: ignore
import scheduler  # type: ignore


def route(url: str, profile: str | None = None, run_id: str | None = None, requested_backend: str = "auto", domain: str | None = None) -> dict:
    resolved_domain = scheduler.normalize_domain(domain or url)
    policy = scheduler.resolve_policy(resolved_domain)
    config = research_manifest.load_profile(profile) if profile else None
    required_platforms = set(config.get("required_platforms", [])) if config else set()

    if resolved_domain in required_platforms:
        if requested_backend in {"web_fetch", "http"}:
            return {
                "ok": False,
                "url": url,
                "domain": resolved_domain,
                "profile": profile,
                "requested_backend": requested_backend,
                "reason": "forbidden_backend_for_required_platform",
                "policy": policy,
            }

    allowed = policy.get("allowed_fetch_backends")
    disallowed = set(policy.get("disallowed_fetch_backends", []))
    if requested_backend in disallowed:
        return {
            "ok": False,
            "url": url,
            "domain": resolved_domain,
            "profile": profile,
            "requested_backend": requested_backend,
            "reason": "domain_policy_disallowed_backend",
            "policy": policy,
        }

    if allowed and requested_backend not in {"auto"} and requested_backend not in allowed:
        return {
            "ok": False,
            "url": url,
            "domain": resolved_domain,
            "profile": profile,
            "requested_backend": requested_backend,
            "reason": "backend_not_in_allowed_list",
            "policy": policy,
        }

    try:
        result = guarded_search_fetch.execute_fetch(
            url,
            profile=profile,
            run_id=run_id,
            backend=requested_backend,
            domain=resolved_domain,
        )
        if isinstance(result, dict) and not result.get("allowed", True):
            return {
                "ok": False,
                "url": url,
                "domain": resolved_domain,
                "profile": profile,
                "requested_backend": requested_backend,
                "policy": policy,
                "result": result,
                "reason": result.get("reason", "scheduler_blocked"),
            }
        return {
            "ok": True,
            "url": url,
            "domain": resolved_domain,
            "profile": profile,
            "requested_backend": requested_backend,
            "policy": policy,
            "result": result,
        }
    except Exception as exc:
        return {
            "ok": False,
            "url": url,
            "domain": resolved_domain,
            "profile": profile,
            "requested_backend": requested_backend,
            "reason": str(exc),
            "policy": policy,
        }


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("usage: safe_fetch_router.py <url> [profile] [run_id] [backend] [domain]")
    url = sys.argv[1]
    profile = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] != "-" else None
    run_id = sys.argv[3] if len(sys.argv) > 3 and sys.argv[3] != "-" else None
    backend = sys.argv[4] if len(sys.argv) > 4 else "auto"
    domain = sys.argv[5] if len(sys.argv) > 5 and sys.argv[5] != "-" else None
    result = route(url, profile=profile, run_id=run_id, requested_backend=backend, domain=domain)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["ok"] else 1)
