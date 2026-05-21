#!/usr/bin/env python3
"""Domain-aware scheduler for throttling, cooldowns, and block handling."""

from __future__ import annotations

from dataclasses import dataclass, asdict
from pathlib import Path
from urllib.parse import urlparse
import json
import os
import random
import sys
import time

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from _config import STATE_PATH

import _locking

ISOLATED_RUN_DOMAINS = {"xiaohongshu.com", "douyin.com"}
GAME_SENTIMENT_CN_REQUIRED_DOMAINS = {
    "bilibili.com",
    "taptap.cn",
    "tieba.baidu.com",
    "xiaohongshu.com",
    "douyin.com",
}

POLICY_PATH = Path(__file__).resolve().parent.parent / "assets" / "domain-policies.json"
HIGH_RISK_DOMAINS = {
    "weibo.com",
    "s.weibo.com",
    "xiaohongshu.com",
    "taptap.cn",
    "taptap.com",
    "tieba.baidu.com",
    "waptieba.baidu.com",
    "nga.178.com",
    "bbs.nga.cn",
    "bilibili.com",
    "reddit.com",
    "douyin.com",
    "taobao.com",
    "s.taobao.com",
    "tmall.com",
    "detail.tmall.com",
    "jd.com",
    "search.jd.com",
    "passport.jd.com",
}
LOCK_STALE_SEC = 180
RUN_STALE_SEC = 1800


@dataclass
class DomainState:
    last_access: float = 0.0
    cooldown_until: float = 0.0
    pages_fetched_in_run: int = 0
    block_signals: int = 0
    last_outcome: str = ""
    run_started_at: float = 0.0
    hard_stopped: bool = False
    active_lock_id: str = ""
    active_lock_expires_at: float = 0.0


def load_policies() -> dict:
    return json.loads(POLICY_PATH.read_text())


def normalize_domain(domain_or_url: str) -> str:
    parsed = urlparse(domain_or_url)
    domain = parsed.netloc or domain_or_url
    domain = domain.lower().strip()
    if domain.startswith("www."):
        domain = domain[4:]
    return domain


def resolve_policy(domain: str) -> dict:
    policies = load_policies()
    domain = normalize_domain(domain)
    if domain in policies:
        return policies[domain]
    parts = domain.split(".")
    for i in range(1, len(parts) - 1):
        suffix = ".".join(parts[i:])
        if suffix in policies:
            return policies[suffix]
    return policies["default"]


def resolve_action_policy(policy: dict, action: str) -> dict:
    action_overrides = (policy.get("action_policies") or {}).get(action) or {}
    if not action_overrides:
        return policy
    merged = {**policy, **action_overrides}
    merged["action_policy"] = action
    return merged


def _load_state() -> dict:
    if not STATE_PATH.exists():
        return {"domains": {}, "meta": {}}
    raw = STATE_PATH.read_text()
    if not raw.strip():
        return {"domains": {}, "meta": {}}
    state = json.loads(raw)
    state.setdefault("domains", {})
    state.setdefault("meta", {})
    return state


def _save_state(state: dict, locked_file=None) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(state, indent=2, ensure_ascii=False)
    if locked_file is not None:
        locked_file.seek(0)
        locked_file.truncate()
        locked_file.write(payload)
        locked_file.flush()
        os.fsync(locked_file.fileno())
        return
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(payload)
    tmp.replace(STATE_PATH)


def _read_modify_write(modify_fn):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.touch(exist_ok=True)
    lock_path = STATE_PATH.with_suffix(".lock")
    with lock_path.open("a+b") as lock_handle:
        _locking.flock_ex(lock_handle.fileno())
        try:
            state = _load_state()
            result = modify_fn(state)
            _save_state(state)
            return result
        finally:
            _locking.flock_un(lock_handle.fileno())


def _get_domain_state(state: dict, domain: str) -> DomainState:
    domains = state.setdefault("domains", {})
    raw = domains.get(domain, {})
    defaults = DomainState()
    payload = {k: raw.get(k, getattr(defaults, k)) for k in DomainState.__annotations__}
    return DomainState(**payload)


def _set_domain_state(state: dict, domain: str, domain_state: DomainState) -> None:
    domains = state.setdefault("domains", {})
    domains[domain] = asdict(domain_state)


def _is_high_risk_domain(domain: str) -> bool:
    return normalize_domain(domain) in HIGH_RISK_DOMAINS


def _release_stale_locks(state: dict, now: float) -> None:
    for raw in state.get("domains", {}).values():
        if raw.get("active_lock_expires_at", 0) <= now:
            raw["active_lock_id"] = ""
            raw["active_lock_expires_at"] = 0.0


def _meta(state: dict) -> dict:
    return state.setdefault("meta", {})


def _active_run_platform_gate(state: dict, profile: str, domain: str) -> tuple[bool, str | None, str | None]:
    if profile != "game_sentiment_cn":
        return False, None, None
    for run_domain, raw in state.get("domains", {}).items():
        if run_domain == domain:
            continue
        if raw.get("active_lock_id") and raw.get("active_lock_expires_at", 0) > time.time() and normalize_domain(run_domain) in GAME_SENTIMENT_CN_REQUIRED_DOMAINS:
            return True, run_domain, "another_platform_active"
    return False, None, None


def _cross_domain_recent_gap_block(state: dict, domain: str, now: float, min_gap: int) -> tuple[bool, str | None, int]:
    latest_domain = None
    latest_access = 0.0
    for other_domain, raw in state.get("domains", {}).items():
        if other_domain == domain:
            continue
        last_access = raw.get("last_access", 0.0)
        if last_access > latest_access:
            latest_access = last_access
            latest_domain = other_domain
    if latest_domain and latest_access and now - latest_access < min_gap:
        return True, latest_domain, max(1, int(min_gap - (now - latest_access)))
    return False, None, 0


def _has_other_high_risk_lock(state: dict, domain: str, now: float) -> tuple[bool, str | None]:
    for other_domain, raw in state.get("domains", {}).items():
        if other_domain == domain:
            continue
        if raw.get("active_lock_expires_at", 0) > now and raw.get("active_lock_id") and _is_high_risk_domain(other_domain):
            return True, other_domain
    return False, None


def _last_global_high_risk_access(state: dict, domain: str) -> tuple[str | None, float]:
    latest_domain = None
    latest_access = 0.0
    for other_domain, raw in state.get("domains", {}).items():
        if other_domain == domain:
            continue
        if not _is_high_risk_domain(other_domain):
            continue
        last_access = raw.get("last_access", 0.0)
        if last_access > latest_access:
            latest_access = last_access
            latest_domain = other_domain
    return latest_domain, latest_access


def schedule(domain_or_url: str, action: str) -> dict:
    def modify(state: dict):
        domain = normalize_domain(domain_or_url)
        policy = resolve_action_policy(resolve_policy(domain), action)
        domain_state = _get_domain_state(state, domain)
        now = time.time()
        requester_lock_id = os.environ.get("SEARCH_FETCH_LOCK_ID", "")
        profile = os.environ.get("SEARCH_FETCH_PROFILE", "")
        current_session_kind = os.environ.get("SEARCH_FETCH_SESSION_KIND", "main")

        _release_stale_locks(state, now)

        if domain_state.run_started_at == 0:
            domain_state.run_started_at = now

        stale_run = bool(
            domain_state.run_started_at
            and now - domain_state.run_started_at > RUN_STALE_SEC
            and not domain_state.active_lock_id
            and domain_state.active_lock_expires_at <= now
        )
        if stale_run:
            domain_state.pages_fetched_in_run = 0
            domain_state.block_signals = 0
            domain_state.hard_stopped = False
            if domain_state.cooldown_until <= now:
                domain_state.cooldown_until = 0.0
            domain_state.run_started_at = now
            _set_domain_state(state, domain, domain_state)

        if domain_state.hard_stopped:
            return {
                "domain": domain,
                "action": action,
                "allowed": False,
                "reason": "hard_stopped",
                "wait_seconds": max(0, int(domain_state.cooldown_until - now)),
                "policy": policy,
                "state": asdict(domain_state),
            }

        meta = _meta(state)
        current_run_scope = os.environ.get("SEARCH_FETCH_RUN_SCOPE", "")
        if policy.get("isolated_run_only") and current_run_scope != domain:
            return {
                "domain": domain,
                "action": action,
                "allowed": False,
                "reason": "isolated_run_required",
                "wait_seconds": 0,
                "policy": policy,
                "state": asdict(domain_state),
                "required_scope": domain,
            }

        min_gap = int(policy.get("min_cross_domain_gap_sec", 0) or 0)
        if min_gap > 0:
            blocked, other_domain, wait_seconds = _cross_domain_recent_gap_block(state, domain, now, min_gap)
            if blocked:
                return {
                    "domain": domain,
                    "action": action,
                    "allowed": False,
                    "reason": "cross_domain_gap",
                    "wait_seconds": wait_seconds,
                    "policy": policy,
                    "state": asdict(domain_state),
                    "previous_domain": other_domain,
                }

        global_hard_gate = int(policy.get("global_hard_gate_sec", 0) or 0)
        if global_hard_gate > 0 and _is_high_risk_domain(domain):
            last_domain, last_access = _last_global_high_risk_access(state, domain)
            if last_domain and last_access and now - last_access < global_hard_gate:
                return {
                    "domain": domain,
                    "action": action,
                    "allowed": False,
                    "reason": "global_hard_gate",
                    "wait_seconds": max(1, int(global_hard_gate - (now - last_access))),
                    "policy": policy,
                    "state": asdict(domain_state),
                    "previous_domain": last_domain,
                }

        if domain_state.active_lock_id and domain_state.active_lock_expires_at > now and domain_state.active_lock_id != requester_lock_id:
            return {
                "domain": domain,
                "action": action,
                "allowed": False,
                "reason": "domain_locked",
                "wait_seconds": max(1, int(domain_state.active_lock_expires_at - now)),
                "policy": policy,
                "state": asdict(domain_state),
            }

        blocked_by_platform_gate, gated_domain, gated_reason = _active_run_platform_gate(state, profile, domain)
        if blocked_by_platform_gate:
            return {
                "domain": domain,
                "action": action,
                "allowed": False,
                "reason": "active_platform_not_completed",
                "wait_seconds": LOCK_STALE_SEC,
                "policy": policy,
                "state": asdict(domain_state),
                "locked_domain": gated_domain,
            }

        has_other_lock, locked_domain = _has_other_high_risk_lock(state, domain, now)
        if has_other_lock and _is_high_risk_domain(domain):
            return {
                "domain": domain,
                "action": action,
                "allowed": False,
                "reason": "global_high_risk_locked",
                "wait_seconds": LOCK_STALE_SEC,
                "policy": policy,
                "state": asdict(domain_state),
                "locked_domain": locked_domain,
            }

        if current_session_kind == "main" and action == "fetch" and _is_high_risk_domain(domain):
            active_high_risk = [
                other_domain for other_domain, raw in state.get("domains", {}).items()
                if other_domain != domain and raw.get("active_lock_expires_at", 0) > now and raw.get("active_lock_id") and _is_high_risk_domain(other_domain)
            ]
            if active_high_risk:
                return {
                    "domain": domain,
                    "action": action,
                    "allowed": False,
                    "reason": "main_session_parallel_playwright_forbidden",
                    "wait_seconds": LOCK_STALE_SEC,
                    "policy": policy,
                    "state": asdict(domain_state),
                    "locked_domain": active_high_risk[0],
                }

        if profile == "game_sentiment_cn" and domain in GAME_SENTIMENT_CN_REQUIRED_DOMAINS and not policy.get("global_high_risk_lock"):
            missing_policy_fields = [
                field for field in ("global_high_risk_lock", "global_hard_gate_sec", "content_fetch_mode")
                if not policy.get(field)
            ]
            return {
                "domain": domain,
                "action": action,
                "allowed": False,
                "reason": "profile_policy_incomplete",
                "detail": f"domain {domain} is in {profile} required_platforms but domain-policies.json is missing: {', '.join(missing_policy_fields)}",
                "wait_seconds": 0,
                "policy": policy,
                "state": asdict(domain_state),
            }

        if now < domain_state.cooldown_until:
            return {
                "domain": domain,
                "action": action,
                "allowed": False,
                "reason": "cooldown",
                "wait_seconds": max(0, int(domain_state.cooldown_until - now)),
                "policy": policy,
                "state": asdict(domain_state),
            }

        if domain_state.pages_fetched_in_run >= policy["max_pages_per_run"]:
            domain_state.cooldown_until = now + policy["cooldown_on_block_sec"]
            domain_state.hard_stopped = True
            _set_domain_state(state, domain, domain_state)
            return {
                "domain": domain,
                "action": action,
                "allowed": False,
                "reason": "max_pages_per_run_reached",
                "wait_seconds": int(policy["cooldown_on_block_sec"]),
                "policy": policy,
                "state": asdict(domain_state),
            }

        base_wait = max(0, policy["min_interval_sec"] - int(now - domain_state.last_access)) if domain_state.last_access else 0
        jitter_min, jitter_max = policy["jitter_sec"][:2]
        jitter = random.randint(jitter_min, jitter_max) if (jitter_max >= jitter_min) else 0
        wait_seconds = base_wait + jitter

        if requester_lock_id:
            domain_state.active_lock_id = requester_lock_id
            domain_state.active_lock_expires_at = now + LOCK_STALE_SEC
            meta["last_scheduled_domain"] = domain
            meta["last_schedule_at"] = now
            _set_domain_state(state, domain, domain_state)

        return {
            "domain": domain,
            "action": action,
            "allowed": True,
            "reason": "ok",
            "wait_seconds": wait_seconds,
            "policy": policy,
            "state": asdict(domain_state),
            "lock_id": requester_lock_id,
        }


    return _read_modify_write(modify)


def record_result(domain_or_url: str, outcome: str, blocked: bool = False, pages_increment: int = 1) -> dict:
    def modify(state: dict):
        domain = normalize_domain(domain_or_url)
        policy = resolve_policy(domain)
        domain_state = _get_domain_state(state, domain)
        now = time.time()

        domain_state.last_access = now
        domain_state.last_outcome = outcome
        domain_state.pages_fetched_in_run += max(0, pages_increment)

        if blocked:
            domain_state.block_signals += 1
            domain_state.cooldown_until = now + policy["cooldown_on_block_sec"]
            if domain_state.block_signals >= policy["max_block_signals_before_hard_stop"]:
                domain_state.hard_stopped = True

        domain_state.active_lock_id = ""
        domain_state.active_lock_expires_at = 0.0

        meta = _meta(state)
        meta["last_completed_domain"] = domain
        meta["last_completed_at"] = now
        _set_domain_state(state, domain, domain_state)
        return {
            "ok": True,
            "domain": domain,
            "outcome": outcome,
            "blocked": blocked,
            "policy": policy,
            "state": asdict(domain_state),
        }


    return _read_modify_write(modify)


def reset_domain(domain_or_url: str) -> dict:
    def modify(state: dict):
        domain = normalize_domain(domain_or_url)
        _set_domain_state(state, domain, DomainState())
        return {"ok": True, "domain": domain, "reset": True}


    return _read_modify_write(modify)


def reset_run(scope: str = "all") -> dict:
    def modify(state: dict):
        now = time.time()
        domains = state.setdefault("domains", {})
        reset_domains: list[str] = []

        for domain, raw in domains.items():
            if scope not in {"all", "*"} and normalize_domain(scope) != domain:
                continue
            raw["cooldown_until"] = 0.0
            raw["pages_fetched_in_run"] = 0
            raw["block_signals"] = 0
            raw["last_outcome"] = ""
            raw["run_started_at"] = now
            raw["hard_stopped"] = False
            raw["active_lock_id"] = ""
            raw["active_lock_expires_at"] = 0.0
            reset_domains.append(domain)

        meta = _meta(state)
        meta["last_run_reset_at"] = now
        if scope not in {"all", "*"} and not reset_domains:
            domain = normalize_domain(scope)
            domains[domain] = asdict(DomainState(run_started_at=now))
            reset_domains.append(domain)

        return {"ok": True, "scope": scope, "domains_reset": reset_domains}

    return _read_modify_write(modify)


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        raise SystemExit("usage: scheduler.py <schedule|record|reset|reset_run> <domain-or-url> [scope|action|outcome] [blocked] [pages_increment]; reset_run [scope]")

    command = sys.argv[1]
    target = sys.argv[2] if len(sys.argv) > 2 else None

    if not target and command != "reset_run":
        raise SystemExit("usage: scheduler.py <schedule|record|reset|reset_run> <domain-or-url> [scope|action|outcome] [blocked] [pages_increment]; reset_run [scope]")

    if command == "reset_run" and not target:
        target = "all"

    if command == "schedule":
        action = sys.argv[3] if len(sys.argv) > 3 else "fetch"
        print(json.dumps(schedule(target, action), ensure_ascii=False, indent=2))
    elif command == "record":
        outcome = sys.argv[3] if len(sys.argv) > 3 else "ok"
        blocked = (len(sys.argv) > 4 and sys.argv[4].lower() in {"1", "true", "yes", "blocked"})
        pages_increment = int(sys.argv[5]) if len(sys.argv) > 5 else 1
        print(json.dumps(record_result(target, outcome, blocked, pages_increment), ensure_ascii=False, indent=2))
    elif command == "reset":
        print(json.dumps(reset_domain(target), ensure_ascii=False, indent=2))
    elif command == "reset_run":
        print(json.dumps(reset_run(target), ensure_ascii=False, indent=2))
    else:
        raise SystemExit(f"unknown command: {command}")
