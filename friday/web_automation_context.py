"""Persistent learned context for gated web automations.

This store is intentionally selector-first (not coordinate-first) so flows remain
stable across monitor resolutions and minor layout changes.
"""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from friday.config import WEB_AUTOMATION_CONTEXT_PATH

SCHEMA_VERSION = 1


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_store() -> dict[str, Any]:
    ts = _now_iso()
    return {
        "schema_version": SCHEMA_VERSION,
        "created_at": ts,
        "updated_at": ts,
        "providers": {},
    }


def load_context() -> dict[str, Any]:
    if not WEB_AUTOMATION_CONTEXT_PATH.exists():
        return _default_store()
    try:
        data = json.loads(WEB_AUTOMATION_CONTEXT_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return _default_store()
        if int(data.get("schema_version", 0)) != SCHEMA_VERSION:
            # Keep migration simple for now: start fresh on schema mismatch.
            return _default_store()
        data.setdefault("providers", {})
        data.setdefault("created_at", _now_iso())
        data.setdefault("updated_at", _now_iso())
        return data
    except Exception:
        return _default_store()


def save_context(data: dict[str, Any]) -> None:
    payload = deepcopy(data)
    payload["schema_version"] = SCHEMA_VERSION
    payload["updated_at"] = _now_iso()
    payload.setdefault("providers", {})
    payload.setdefault("created_at", _now_iso())
    WEB_AUTOMATION_CONTEXT_PATH.write_text(
        json.dumps(payload, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def get_flow(provider: str, flow_id: str) -> dict[str, Any] | None:
    data = load_context()
    p = data.get("providers", {}).get(provider, {})
    return deepcopy(p.get("flows", {}).get(flow_id))


def upsert_flow(
    provider: str,
    flow_id: str,
    *,
    url_patterns: list[str],
    steps: list[dict[str, Any]],
    success_checks: list[dict[str, Any]],
    login_gate: str = "",
    tags: list[str] | None = None,
) -> dict[str, Any]:
    """Create/update a learned flow profile.

    `steps` should prioritize selectors (`testid`, `aria`, stable css/xpath) and
    avoid raw screen coordinates except as a last resort fallback.
    """
    data = load_context()
    providers = data.setdefault("providers", {})
    provider_entry = providers.setdefault(provider, {"flows": {}})
    flows = provider_entry.setdefault("flows", {})

    previous = flows.get(flow_id, {})
    attempts = int(previous.get("run_stats", {}).get("attempts", 0))
    successes = int(previous.get("run_stats", {}).get("successes", 0))
    failures = int(previous.get("run_stats", {}).get("failures", 0))

    flows[flow_id] = {
        "flow_id": flow_id,
        "provider": provider,
        "status": previous.get("status", "active"),
        "confidence": float(previous.get("confidence", 0.5)),
        "last_verified_at": previous.get("last_verified_at"),
        "login_gate": login_gate or previous.get("login_gate", ""),
        "url_patterns": url_patterns,
        "tags": tags or previous.get("tags", []),
        "steps": steps,
        "success_checks": success_checks,
        "fallbacks": previous.get("fallbacks", []),
        "run_stats": {
            "attempts": attempts,
            "successes": successes,
            "failures": failures,
            "last_run_at": previous.get("run_stats", {}).get("last_run_at"),
            "last_error": previous.get("run_stats", {}).get("last_error"),
        },
        "notes": previous.get("notes", ""),
        "updated_at": _now_iso(),
    }
    save_context(data)
    return deepcopy(flows[flow_id])


def record_flow_result(
    provider: str,
    flow_id: str,
    *,
    success: bool,
    error: str = "",
) -> None:
    data = load_context()
    flow = data.get("providers", {}).get(provider, {}).get("flows", {}).get(flow_id)
    if not flow:
        return

    stats = flow.setdefault("run_stats", {})
    stats["attempts"] = int(stats.get("attempts", 0)) + 1
    if success:
        stats["successes"] = int(stats.get("successes", 0)) + 1
        flow["last_verified_at"] = _now_iso()
        flow["status"] = "active"
        stats["last_error"] = ""
    else:
        stats["failures"] = int(stats.get("failures", 0)) + 1
        stats["last_error"] = error[:500]
    stats["last_run_at"] = _now_iso()

    attempts = max(1, int(stats["attempts"]))
    flow["confidence"] = round(int(stats.get("successes", 0)) / attempts, 3)
    flow["updated_at"] = _now_iso()
    save_context(data)


def promote_fallback_to_primary(
    provider: str,
    flow_id: str,
    *,
    fallback_index: int,
) -> bool:
    """Promote a successful fallback path to primary flow steps.

    Returns True when promotion happened, False when flow/fallback was missing.
    """
    data = load_context()
    flow = data.get("providers", {}).get(provider, {}).get("flows", {}).get(flow_id)
    if not flow:
        return False

    fallbacks = flow.get("fallbacks", [])
    if not isinstance(fallbacks, list):
        return False
    if fallback_index < 0 or fallback_index >= len(fallbacks):
        return False

    chosen = fallbacks[fallback_index]
    if not isinstance(chosen, dict):
        return False
    chosen_steps = chosen.get("steps")
    if not isinstance(chosen_steps, list) or not chosen_steps:
        return False

    old_primary_steps = flow.get("steps", [])
    old_primary_checks = flow.get("success_checks", [])

    flow["steps"] = deepcopy(chosen_steps)
    if isinstance(chosen.get("success_checks"), list):
        flow["success_checks"] = deepcopy(chosen["success_checks"])

    # Keep the old primary as the last fallback for rollback/debug.
    if isinstance(old_primary_steps, list) and old_primary_steps:
        fallbacks.append(
            {
                "name": "previous_primary_auto_saved",
                "steps": deepcopy(old_primary_steps),
                "success_checks": deepcopy(old_primary_checks)
                if isinstance(old_primary_checks, list)
                else [],
            }
        )
    flow["fallbacks"] = fallbacks
    flow["updated_at"] = _now_iso()
    save_context(data)
    return True

