#!/usr/bin/env python3
"""Content quality heuristics for profile-driven high-risk community fetches."""

from __future__ import annotations

from typing import Any

import research_manifest  # type: ignore


def evaluate(profile: str, domain: str, text: str) -> dict[str, Any]:
    config = research_manifest.load_profile(profile)
    min_lengths = config.get("min_content_text_length", {})
    forbidden_markers = config.get("forbidden_text_markers", {})
    required_markers = config.get("required_text_markers", {})

    min_length = min_lengths.get(domain, min_lengths.get("default", 0))
    text_length = len(text or "")
    notes: list[str] = []
    passed = True

    if text_length < min_length:
        passed = False
        notes.append(f"content too short ({text_length} < {min_length})")

    domain_forbidden = forbidden_markers.get(domain, [])
    default_forbidden = forbidden_markers.get("default", [])
    all_forbidden = domain_forbidden if domain_forbidden else default_forbidden
    if any(marker in text for marker in all_forbidden) and all_forbidden:
        matched = [m for m in all_forbidden if m in text]
        passed = False
        notes.append(f"content matches forbidden shell markers: {', '.join(matched)}")

    domain_required = required_markers.get(domain, [])
    if domain_required and not any(marker in text for marker in domain_required):
        passed = False
        notes.append(f"content missing required markers: {', '.join(domain_required)}")

    return {
        "passed": passed,
        "text_length": text_length,
        "notes": notes,
    }
