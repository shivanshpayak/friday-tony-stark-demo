"""Generic gated web automation runner backed by persistent learned context."""

from __future__ import annotations

import json
import time
from typing import Any

from mcp.server.fastmcp import FastMCP

from friday.web_automation_context import (
    get_flow,
    load_context,
    promote_fallback_to_primary,
    record_flow_result,
    upsert_flow,
)
def _parse_json_list(raw: str, field: str) -> list[dict[str, Any]]:
    try:
        parsed = json.loads(raw)
    except Exception as e:
        raise ValueError(f"{field} must be valid JSON: {e}") from e
    if not isinstance(parsed, list):
        raise ValueError(f"{field} must be a JSON array.")
    out: list[dict[str, Any]] = []
    for idx, item in enumerate(parsed):
        if not isinstance(item, dict):
            raise ValueError(f"{field}[{idx}] must be an object.")
        out.append(item)
    return out
def _selector_from_step(step: dict[str, Any]) -> str:
    if step.get("selector"):
        return str(step["selector"])
    if step.get("testid"):
        return f"[data-testid='{step['testid']}']"
    if step.get("aria_label"):
        return f"[aria-label='{step['aria_label']}']"
    raise ValueError("Step missing selector/testid/aria_label.")
def _check_success(page, checks: list[dict[str, Any]]) -> tuple[bool, str]:
    for check in checks:
        ctype = str(check.get("type", "")).strip()
        value = str(check.get("value", "")).strip()
        if not ctype:
            continue
        if ctype == "url_contains":
            if value not in page.url:
                return False, f"url does not contain {value!r}"
        elif ctype == "title_contains":
            if value.lower() not in page.title().lower():
                return False, f"title does not contain {value!r}"
        elif ctype == "selector_visible":
            if not page.locator(value).first.is_visible():
                return False, f"selector not visible: {value!r}"
        else:
            return False, f"unknown success check type: {ctype!r}"
    return True, ""
def _run_single_action(page, step: dict[str, Any], idx: int) -> None:
    action = str(step.get("action", "")).strip().lower()
    if not action:
        raise RuntimeError(f"Step {idx} missing action.")

    if action == "goto":
        page.goto(str(step.get("url", "")), wait_until="domcontentloaded")
    elif action == "click":
        page.locator(_selector_from_step(step)).first.click()
    elif action == "fill":
        page.locator(_selector_from_step(step)).first.fill(str(step.get("value", "")))
    elif action == "press":
        page.locator(_selector_from_step(step)).first.press(str(step.get("key", "Enter")))
    elif action == "wait_for":
        page.locator(_selector_from_step(step)).first.wait_for(state="visible")
    elif action == "sleep":
        ms = int(step.get("ms", 500))
        time.sleep(max(0, ms) / 1000)
    elif action == "go_back":
        page.go_back(wait_until="domcontentloaded")
    elif action == "reload":
        page.reload(wait_until="domcontentloaded")
    else:
        raise RuntimeError(f"Unsupported action {action!r} at step {idx}.")
def _apply_on_fail(page, behavior: str) -> None:
    b = (behavior or "").strip().lower()
    if not b:
        return
    if b == "go_back":
        page.go_back(wait_until="domcontentloaded")
    elif b == "reload":
        page.reload(wait_until="domcontentloaded")
    elif b == "continue":
        return
    elif b == "abort":
        return
    else:
        raise RuntimeError(f"Unknown on_fail behavior: {behavior!r}")
def _run_steps_with_resilience(page, steps: list[dict[str, Any]]) -> None:
    for idx, step in enumerate(steps, start=1):
        retries = max(0, int(step.get("retries", 0)))
        retry_delay_ms = max(0, int(step.get("retry_delay_ms", 400)))
        on_fail = str(step.get("on_fail", "abort"))

        last_err: Exception | None = None
        for attempt in range(retries + 1):
            try:
                _run_single_action(page, step, idx)
                last_err = None
                break
            except Exception as e:
                last_err = e
                if attempt < retries:
                    time.sleep(retry_delay_ms / 1000.0)
                    continue
        if last_err is None:
            continue

        if on_fail in ("go_back", "reload", "continue"):
            _apply_on_fail(page, on_fail)
            if on_fail == "continue":
                continue
            # Try the same step once more after recovery.
            _run_single_action(page, step, idx)
            continue
        raise RuntimeError(f"Step {idx} failed after retries: {last_err}")
def _execute_flow_with_playwright(
    flow: dict[str, Any],
    *,
    headless: bool,
    timeout_seconds: int,
) -> tuple[str, int | None]:
    from playwright.sync_api import sync_playwright

    steps = flow.get("steps", [])
    checks = flow.get("success_checks", [])
    flow_id = flow.get("flow_id", "flow")
    fallback_flows = flow.get("fallbacks", [])

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context()
        page = context.new_page()
        page.set_default_timeout(max(1000, timeout_seconds * 1000))

        def _attempt_current_flow(candidate_steps: list[dict[str, Any]], candidate_checks: list[dict[str, Any]]) -> tuple[bool, str]:
            _run_steps_with_resilience(page, candidate_steps)
            ok, reason = _check_success(page, candidate_checks)
            return ok, reason

        try:
            ok, reason = _attempt_current_flow(steps, checks)
            if ok:
                return (
                    f"Flow '{flow_id}' completed successfully. Final URL: {page.url}",
                    None,
                )

            # Try inline fallback routes if provided in context.
            for i, fallback in enumerate(fallback_flows, start=1):
                fb_steps = fallback.get("steps")
                if not isinstance(fb_steps, list):
                    continue
                fb_checks = fallback.get("success_checks", checks)
                try:
                    ok, reason = _attempt_current_flow(fb_steps, fb_checks)
                except Exception as e:
                    reason = str(e)
                    ok = False
                if ok:
                    return (
                        f"Flow '{flow_id}' completed via fallback #{i}. "
                        f"Final URL: {page.url}",
                        i - 1,
                    )

            raise RuntimeError(f"Success check failed: {reason}. Final URL: {page.url}")
        finally:
            browser.close()


def register(mcp: FastMCP):
    @mcp.tool(name="save_web_flow_context")
    def save_web_flow_context(
        provider: str,
        flow_id: str,
        url_patterns_json: str,
        steps_json: str,
        success_checks_json: str,
        login_gate: str = "",
        tags_json: str = "[]",
    ) -> str:
        """Save or update a learned web flow profile in context storage.

        Inputs:
        - `url_patterns_json`: JSON string array of URL patterns
        - `steps_json`: JSON string array of step objects
        - `success_checks_json`: JSON string array of success-check objects
        - `tags_json`: optional JSON string array
        """
        try:
            url_patterns = json.loads(url_patterns_json)
            if not isinstance(url_patterns, list):
                return "url_patterns_json must be a JSON array of strings."
            url_patterns = [str(x) for x in url_patterns]
            steps = _parse_json_list(steps_json, "steps_json")
            checks = _parse_json_list(success_checks_json, "success_checks_json")
            tags_parsed = json.loads(tags_json)
            if not isinstance(tags_parsed, list):
                return "tags_json must be a JSON array of strings."
            tags = [str(x) for x in tags_parsed]
        except Exception as e:
            return f"Invalid flow payload: {e}"

        saved = upsert_flow(
            provider=provider.strip().lower(),
            flow_id=flow_id.strip(),
            url_patterns=url_patterns,
            steps=steps,
            success_checks=checks,
            login_gate=login_gate.strip().lower(),
            tags=tags,
        )
        return (
            f"Saved flow '{saved['flow_id']}' for provider '{saved['provider']}' "
            f"with {len(saved.get('steps', []))} step(s)."
        )

    @mcp.tool(name="list_web_flow_context")
    def list_web_flow_context(provider: str = "") -> str:
        """List saved learned web-flow profiles from context.json."""
        data = load_context()
        providers = data.get("providers", {})
        if provider.strip():
            providers = {provider.strip().lower(): providers.get(provider.strip().lower(), {})}

        lines: list[str] = []
        for p_name, p_data in providers.items():
            flows = (p_data or {}).get("flows", {})
            for flow_id, flow in flows.items():
                status = flow.get("status", "unknown")
                conf = flow.get("confidence", 0.0)
                lines.append(f"- {p_name}/{flow_id} [{status}] confidence={conf}")

        if not lines:
            return "No web flow context profiles found."
        return "Saved web flows:\n" + "\n".join(lines)

    @mcp.tool(name="run_web_flow")
    def run_web_flow(
        provider: str,
        flow_id: str,
        headless: bool = False,
        timeout_seconds: int = 30,
    ) -> str:
        """Run a learned web flow from context storage using Playwright.

        This is generic: ClassLink -> Canvas/GAVS and similar gated flows should
        all be represented as saved flow profiles rather than hardcoded tools.

        Supported resilient step fields:
        - retries: int
        - retry_delay_ms: int
        - on_fail: "abort" | "continue" | "go_back" | "reload"
        - action: includes "go_back" and "reload"

        Self-healing context behavior:
        - if fallback route N succeeds, that fallback is promoted to primary
          `steps` in context.json for future runs.
        """
        provider = provider.strip().lower()
        flow_id = flow_id.strip()
        flow = get_flow(provider, flow_id)
        if not flow:
            return f"No saved flow found for {provider}/{flow_id}."

        try:
            result = _execute_flow_with_playwright(
                flow,
                headless=headless,
                timeout_seconds=max(5, min(180, int(timeout_seconds))),
            )
            message, used_fallback_index = result
            if used_fallback_index is not None:
                promote_fallback_to_primary(provider, flow_id, fallback_index=used_fallback_index)
            record_flow_result(provider, flow_id, success=True)
            return message
        except ModuleNotFoundError:
            record_flow_result(
                provider,
                flow_id,
                success=False,
                error="Playwright not installed.",
            )
            return (
                "Playwright is not installed. Install it with `pip install playwright` "
                "then run `playwright install chromium`."
            )
        except Exception as e:
            record_flow_result(provider, flow_id, success=False, error=str(e))
            return f"Web flow failed for {provider}/{flow_id}: {e}"

