from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from dotenv import load_dotenv

from models import (
    AgentDecision,
    CapabilityArtifact,
    CapabilityInput,
    CapabilityOutput,
    Checkpoint,
    Locator,
    RunResult,
    Step,
)

ROOT = Path(__file__).resolve().parent
ARTIFACTS = ROOT / "artifacts"
EVIDENCE = ROOT / "evidence"
ARTIFACTS.mkdir(exist_ok=True)
EVIDENCE.mkdir(exist_ok=True)
load_dotenv(ROOT / ".env")

DEFAULT_URL = os.getenv("TARGET_URL", "http://127.0.0.1:5000")
ALLOWED_HOSTS = {
    item.strip()
    for item in os.getenv(
        "ALLOWED_HOSTS",
        "127.0.0.1:5000,localhost:5000",
    ).split(",")
    if item.strip()
}
KNOWN_BUSINESS_OUTCOMES = {
    "member_not_found",
    "validation_error",
    "permission_denied",
}
RECOVERABLE_OUTCOMES = {"session_expired"}

SYSTEM_PROMPT = """You control a synthetic credit-union service console through a browser.

Complete the requested goal one action at a time.

Use only element references present in the current observation. Every observed element has an actions list. Choose an action only when that action appears in the target element's actions list.

For type, choose only an input, textarea, or contenteditable element whose actions include type. Never type into headings, labels, table cells, table headers, links, buttons, or plain text.

For click, choose only an element whose actions include click.

For select, choose only an element whose actions include select.

For extract, choose the exact element containing the requested value and whose actions include extract. Prefer elements with stable dataField, id, name, or ariaLabel metadata.

Never choose a control marked risky or irreversible. Prefer stable semantic controls over text-only controls.

Extract only values requested by the goal. Do not finish before the requested output has been extracted.

When the task is complete, provide a checkpoint that proves the expected page state. Prefer selector_present with a stable selector such as a data-field marker when possible. The checkpoint must remain valid when runtime input parameters change, so do not use a member-specific balance, member number, or other invocation-specific text as the checkpoint.

Return exactly one JSON object with these keys:
action, ref, value, output_name, reason, checkpoint_kind, checkpoint_value

Valid actions are click, type, select, extract, wait, finish.

Use null for fields that do not apply. Do not include markdown."""


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def write_json(path: Path, payload: Any):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def redact_text(value: str) -> str:
    patterns = [
        (r"AIza[A-Za-z0-9_-]{20,}", "[REDACTED_API_KEY]"),
        (r"AQ\.[A-Za-z0-9_-]{20,}", "[REDACTED_API_KEY]"),
        (r"sk-[A-Za-z0-9_-]+", "[REDACTED_API_KEY]"),
        (r"Bearer\s+[A-Za-z0-9._-]+", "Bearer [REDACTED]"),
        (
            r"(?i)(api[_-]?key|password|token|secret)\s*[:=]\s*[^\s,;]+",
            r"\1=[REDACTED]",
        ),
    ]
    result = value
    for pattern, replacement in patterns:
        result = re.sub(pattern, replacement, result)
    return result


def log_event(handle, payload: dict[str, Any]):
    event = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        **payload,
    }
    handle.write(
        redact_text(json.dumps(event, ensure_ascii=False)) + "\n"
    )
    handle.flush()


def parse_params(items: list[str]) -> dict[str, str]:
    params: dict[str, str] = {}
    for item in items:
        if "=" not in item:
            raise SystemExit(
                f"Invalid parameter '{item}'. Use key=value"
            )
        key, value = item.split("=", 1)
        key = key.strip()
        if not key:
            raise SystemExit("Parameter name cannot be empty")
        params[key] = value
    return params


def assert_allowed_url(url: str):
    parsed = urlparse(url)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.netloc not in ALLOWED_HOSTS
    ):
        raise RuntimeError(
            f"Navigation blocked by policy: {url}"
        )


def page_observation(page) -> dict[str, Any]:
    elements = page.evaluate(
        r"""
        () => [...document.querySelectorAll(
          'a,button,input,select,textarea,[contenteditable="true"],[data-field]'
        )].map((el, index) => {
          const rect = el.getBoundingClientRect();
          const style = window.getComputedStyle(el);
          const tag = el.tagName.toLowerCase();
          const type = el.getAttribute('type') || '';
          const ariaLabel = el.getAttribute('aria-label') || '';
          const innerText = (el.innerText || '').trim().replace(/\s+/g, ' ');
          const value = 'value' in el ? String(el.value || '') : '';
          const name = el.getAttribute('name') || '';
          const dataField = el.getAttribute('data-field') || '';
          const placeholder = el.getAttribute('placeholder') || '';
          const contentEditable = el.isContentEditable === true;
          const risk = el.getAttribute('data-risk') || '';

          const actions = [];

          if (
            tag === 'input' &&
            !['button', 'submit', 'reset', 'checkbox', 'radio', 'hidden'].includes(type)
          ) {
            actions.push('type');
          }

          if (tag === 'textarea' || contentEditable) {
            actions.push('type');
          }

          if (tag === 'select') {
            actions.push('select');
          }

          if (
            tag === 'button' ||
            tag === 'a' ||
            ['button', 'submit', 'checkbox', 'radio'].includes(type)
          ) {
            actions.push('click');
          }

          if (
            dataField ||
            (
              !['input', 'select', 'textarea'].includes(tag) &&
              innerText
            )
          ) {
            actions.push('extract');
          }

          let label =
            ariaLabel ||
            placeholder ||
            name ||
            dataField ||
            innerText ||
            value;

          if (tag === 'input' && ['submit', 'button'].includes(type) && value) {
            label = value;
          }

          return {
            ref: `e${index + 1}`,
            tag,
            type,
            text: innerText.slice(0, 200),
            label: String(label).trim().replace(/\s+/g, ' ').slice(0, 160),
            id: el.id || '',
            name,
            href: el.getAttribute('href') || '',
            dataField,
            ariaLabel,
            placeholder,
            value: value.slice(0, 160),
            risk,
            disabled: Boolean(el.disabled),
            actions,
            visible: Boolean(
              rect.width &&
              rect.height &&
              style.visibility !== 'hidden' &&
              style.display !== 'none'
            )
          };
        }).filter(
          item => item.visible && item.actions.length > 0
        )
        """
    )

    return {
        "url": page.url,
        "title": page.title(),
        "visible_text": page.locator("body").inner_text()[:6000],
        "elements": elements,
    }


def element_by_ref(
    observation: dict[str, Any],
    ref: str,
) -> dict[str, Any]:
    for element in observation.get("elements", []):
        if element.get("ref") == ref:
            return element
    raise RuntimeError(
        f"Model referenced unknown element {ref}"
    )


def escape_css_attr(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def build_locator_stack(
    element: dict[str, Any],
) -> list[Locator]:
    locators: list[Locator] = []

    element_id = str(element.get("id") or "").strip()
    name = str(element.get("name") or "").strip()
    data_field = str(
        element.get("dataField") or ""
    ).strip()
    aria_label = str(
        element.get("ariaLabel") or ""
    ).strip()
    label = str(
        element.get("label") or element.get("text") or ""
    ).strip()
    tag = str(element.get("tag") or "").strip()
    element_type = str(
        element.get("type") or ""
    ).strip()

    if element_id:
        locators.append(
            Locator(by="id", value=element_id)
        )

    if name:
        locators.append(
            Locator(by="name", value=name)
        )

    if data_field:
        locators.append(
            Locator(
                by="css",
                value=(
                    f'[data-field="'
                    f'{escape_css_attr(data_field)}"]'
                ),
            )
        )

    if aria_label:
        locators.append(
            Locator(
                by="css",
                value=(
                    f'[aria-label="'
                    f'{escape_css_attr(aria_label)}"]'
                ),
            )
        )

    role = None
    if tag == "a":
        role = "link"
    elif tag == "button" or (
        tag == "input"
        and element_type in {"submit", "button"}
    ):
        role = "button"

    if role and label:
        locators.append(
            Locator(
                by="role",
                value=label,
                role=role,
            )
        )

    if (
        label
        and tag not in {
            "input",
            "select",
            "textarea",
        }
        and not data_field
    ):
        locators.append(
            Locator(by="text", value=label)
        )

    unique: list[Locator] = []
    seen: set[tuple[str, str, str | None]] = set()

    for locator in locators:
        key = (
            locator.by,
            locator.value,
            locator.role,
        )
        if key not in seen:
            seen.add(key)
            unique.append(locator)

    if not unique:
        raise RuntimeError(
            "Unable to derive a stable locator "
            f"for element {element.get('ref')}"
        )

    return unique


def resolve_locator(page, locators: list[Locator]):
    attempts: list[str] = []

    for locator in locators:
        try:
            if locator.by == "id":
                candidate = page.locator(
                    f'[id="{escape_css_attr(locator.value)}"]'
                )
            elif locator.by == "name":
                candidate = page.locator(
                    f'[name="{escape_css_attr(locator.value)}"]'
                )
            elif locator.by == "role":
                candidate = page.get_by_role(
                    locator.role or "button",
                    name=locator.value,
                    exact=True,
                )
            elif locator.by == "text":
                candidate = page.get_by_text(
                    locator.value,
                    exact=True,
                )
            elif locator.by == "css":
                candidate = page.locator(locator.value)
            else:
                continue

            count = candidate.count()

            if count == 0:
                attempts.append(
                    f"{locator.by}:{locator.value}=not_found"
                )
                continue

            for index in range(count):
                current = candidate.nth(index)
                if current.is_visible():
                    return current

            attempts.append(
                f"{locator.by}:{locator.value}=not_visible"
            )
        except Exception as exc:
            attempts.append(
                f"{locator.by}:{locator.value}="
                f"{redact_text(str(exc))}"
            )

    summary = "; ".join(attempts[-6:])

    raise RuntimeError(
        "No locator strategy resolved a visible target. "
        f"Attempts: {summary}"
    )


def normalize_parameter(
    value: str,
    params: dict[str, str],
) -> str:
    result = value
    for name, actual in params.items():
        if actual:
            result = result.replace(
                actual,
                "{{" + name + "}}",
            )
    return result


def render_parameter(
    value: str | None,
    params: dict[str, str],
) -> str | None:
    if value is None:
        return None

    rendered = value
    for name, actual in params.items():
        rendered = rendered.replace(
            "{{" + name + "}}",
            actual,
        )
    return rendered


def detect_outcome(page) -> tuple[str, str] | None:
    marker = page.locator("[data-outcome]")
    if marker.count() == 0:
        return None

    first = marker.first
    return (
        first.get_attribute("data-outcome")
        or "unknown_outcome",
        first.inner_text().strip(),
    )


def checkpoint_satisfied(
    page,
    checkpoint: Checkpoint,
    params: dict[str, str] | None = None,
) -> bool:
    params = params or {}
    value = render_parameter(
        checkpoint.value,
        params,
    ) or ""

    if checkpoint.kind == "url_contains":
        return value in page.url

    if checkpoint.kind == "selector_present":
        try:
            candidate = page.locator(value)
            return (
                candidate.count() > 0
                and any(
                    candidate.nth(index).is_visible()
                    for index in range(candidate.count())
                )
            )
        except Exception:
            return False

    return value in page.locator("body").inner_text()


def checkpoint_from_locators(
    locators: list[Locator],
) -> Checkpoint | None:
    for locator in locators:
        if locator.by == "css":
            return Checkpoint(
                kind="selector_present",
                value=locator.value,
            )

    for locator in locators:
        if locator.by == "id":
            return Checkpoint(
                kind="selector_present",
                value=(
                    f'[id="'
                    f'{escape_css_attr(locator.value)}"]'
                ),
            )

    for locator in locators:
        if locator.by == "name":
            return Checkpoint(
                kind="selector_present",
                value=(
                    f'[name="'
                    f'{escape_css_attr(locator.value)}"]'
                ),
            )

    return None


def create_failure_evidence(
    page,
    run_dir: Path,
    step_id: int | str,
):
    page.screenshot(
        path=str(
            run_dir / f"failure_{step_id}.png"
        ),
        full_page=True,
    )
    (
        run_dir / f"failure_{step_id}.html"
    ).write_text(
        page.content(),
        encoding="utf-8",
    )


def request_human(
    page,
    run_dir: Path,
    reason: str,
) -> tuple[bool, str]:
    state = {
        "reason": reason,
        "url_before": page.url,
        "control": "human",
        "started_at": datetime.now(
            timezone.utc
        ).isoformat(),
    }

    page.screenshot(
        path=str(run_dir / "handoff_before.png"),
        full_page=True,
    )
    write_json(
        run_dir / "intervention.json",
        state,
    )

    if not sys.stdin.isatty():
        state["control"] = "automation_paused"
        state["completed"] = False
        write_json(
            run_dir / "intervention.json",
            state,
        )
        return (
            False,
            "Human intervention required but no "
            "interactive terminal is attached",
        )

    print("\nHuman intervention required")
    print(reason)
    print(
        "Use the open browser window. "
        "The automation is paused on the same live session."
    )

    note = input(
        "After completing the manual step, "
        "describe what you did and press Enter: "
    ).strip()

    state["control"] = "automation"
    state["completed"] = True
    state["completed_at"] = datetime.now(
        timezone.utc
    ).isoformat()
    state["url_after"] = page.url
    state["operator_note"] = (
        note or "Operator resumed the session"
    )

    page.screenshot(
        path=str(run_dir / "handoff_after.png"),
        full_page=True,
    )
    write_json(
        run_dir / "intervention.json",
        state,
    )

    return True, state["operator_note"]


def model_decision(
    client,
    model: str,
    goal: str,
    params: dict[str, str],
    observation: dict[str, Any],
    page,
) -> AgentDecision:
    from google.genai import types

    payload = {
        "goal": goal,
        "parameters": params,
        "observation": observation,
    }

    screenshot = page.screenshot(
        type="jpeg",
        quality=65,
        full_page=False,
    )

    contents = [
        types.Content(
            role="user",
            parts=[
                types.Part.from_text(
                    text=(
                        SYSTEM_PROMPT
                        + "\n\nCurrent task state:\n"
                        + json.dumps(
                            payload,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                    )
                ),
                types.Part.from_bytes(
                    data=screenshot,
                    mime_type="image/jpeg",
                ),
            ],
        )
    ]

    last_error: Exception | None = None

    for attempt in range(3):
        try:
            response = client.models.generate_content(
                model=model,
                contents=contents,
                config=types.GenerateContentConfig(
                    temperature=0,
                    response_mime_type=(
                        "application/json"
                    ),
                    response_schema=AgentDecision,
                    max_output_tokens=1200,
                ),
            )

            if not response.text:
                raise RuntimeError(
                    "Gemini returned an empty response"
                )

            return AgentDecision.model_validate_json(
                response.text
            )
        except Exception as exc:
            last_error = exc
            error_text = redact_text(str(exc))

            retryable = (
                "Server disconnected" in error_text
                or "RemoteProtocolError"
                in error_text
                or "timed out"
                in error_text.lower()
                or "timeout"
                in error_text.lower()
                or "503" in error_text
                or "429" in error_text
            )

            if not retryable or attempt == 2:
                raise RuntimeError(
                    "Gemini decision request failed "
                    f"after {attempt + 1} attempt(s): "
                    f"{error_text}"
                ) from exc

            time.sleep(2**attempt)

    raise RuntimeError(
        "Gemini decision request failed: "
        f"{redact_text(str(last_error))}"
    )


def validate_agent_decision(
    decision: AgentDecision,
    observation: dict[str, Any],
) -> tuple[bool, str]:
    if decision.action in {"finish", "wait"}:
        return True, ""

    if not decision.ref:
        return (
            False,
            f"Action '{decision.action}' requires ref",
        )

    element = next(
        (
            item
            for item in observation.get(
                "elements",
                [],
            )
            if item.get("ref") == decision.ref
        ),
        None,
    )

    if not element:
        return (
            False,
            f"Unknown element ref '{decision.ref}'",
        )

    allowed_actions = element.get(
        "actions",
        [],
    )

    if decision.action not in allowed_actions:
        return (
            False,
            f"Element {decision.ref} is "
            f"<{element.get('tag')}> and supports "
            f"{allowed_actions}, not "
            f"'{decision.action}'",
        )

    if element.get("disabled"):
        return (
            False,
            f"Element {decision.ref} is disabled",
        )

    if element.get("risk"):
        return (
            False,
            f"Element {decision.ref} is marked "
            f"risky: {element.get('risk')}",
        )

    return True, ""


def ensure_editable_target(target):
    tag_name = target.evaluate(
        "(el) => el.tagName.toLowerCase()"
    )
    is_content_editable = target.evaluate(
        "(el) => el.isContentEditable"
    )
    input_type = (
        target.get_attribute("type") or ""
    ).lower()

    editable = (
        tag_name in {"input", "textarea"}
        or is_content_editable
    )

    non_text_input = (
        tag_name == "input"
        and input_type
        in {
            "button",
            "submit",
            "reset",
            "checkbox",
            "radio",
            "hidden",
        }
    )

    if not editable or non_text_input:
        raise RuntimeError(
            "Refusing to type into non-editable "
            f"element <{tag_name}> "
            f"type='{input_type}'"
        )


def read_target_value(target) -> str:
    tag_name = target.evaluate(
        "(el) => el.tagName.toLowerCase()"
    )

    if tag_name in {
        "input",
        "textarea",
        "select",
    }:
        return target.input_value().strip()

    return target.inner_text().strip()


def discover(args):
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise SystemExit(
            "GEMINI_API_KEY is required"
        )

    assert_allowed_url(args.url)
    params = parse_params(args.param)
    model = os.getenv(
        "GEMINI_MODEL",
        "gemini-3.5-flash-lite",
    )

    from google import genai
    from playwright.sync_api import sync_playwright

    client = genai.Client(api_key=api_key)

    run_dir = (
        EVIDENCE / f"discovery_{utc_stamp()}"
    )
    run_dir.mkdir(parents=True)

    steps = [
        Step(
            id=1,
            action="navigate",
            description="Open target application",
            value=args.url,
        )
    ]
    declared_outputs: dict[
        str,
        CapabilityOutput,
    ] = {}
    checkpoint: Checkpoint | None = None
    stable_output_checkpoint: (
        Checkpoint | None
    ) = None
    last_model_error: str | None = None
    invalid_decisions = 0

    with (
        (run_dir / "events.jsonl").open(
            "w",
            encoding="utf-8",
        ) as events,
        sync_playwright() as playwright,
    ):
        browser = playwright.chromium.launch(
            headless=args.headless
        )
        context = browser.new_context(
            viewport={
                "width": 1280,
                "height": 900,
            }
        )
        page = context.new_page()

        try:
            page.goto(
                args.url,
                wait_until="domcontentloaded",
                timeout=args.timeout_ms,
            )

            log_event(
                events,
                {
                    "type": "discovery_started",
                    "goal": args.goal,
                    "model": model,
                    "url": args.url,
                },
            )

            for turn in range(
                1,
                args.max_steps + 1,
            ):
                observation = page_observation(page)

                if last_model_error:
                    observation[
                        "previous_action_error"
                    ] = last_model_error

                write_json(
                    run_dir
                    / f"observation_{turn:02d}.json",
                    observation,
                )
                page.screenshot(
                    path=str(
                        run_dir
                        / f"screen_{turn:02d}.png"
                    ),
                    full_page=True,
                )

                outcome = detect_outcome(page)

                if (
                    outcome
                    and outcome[0]
                    in RECOVERABLE_OUTCOMES
                ):
                    log_event(
                        events,
                        {
                            "type":
                                "recoverable_outcome",
                            "code": outcome[0],
                            "message": outcome[1],
                        },
                    )
                    resumed, note = request_human(
                        page,
                        run_dir,
                        outcome[1],
                    )
                    if not resumed:
                        raise RuntimeError(note)
                    log_event(
                        events,
                        {
                            "type":
                                "human_handoff_complete",
                            "note": note,
                        },
                    )
                    last_model_error = None
                    continue

                decision = model_decision(
                    client,
                    model,
                    args.goal,
                    params,
                    observation,
                    page,
                )

                log_event(
                    events,
                    {
                        "type": "llm_decision",
                        "turn": turn,
                        "decision":
                            decision.model_dump(),
                    },
                )

                valid, validation_error = (
                    validate_agent_decision(
                        decision,
                        observation,
                    )
                )

                if not valid:
                    invalid_decisions += 1
                    last_model_error = (
                        validation_error
                    )
                    log_event(
                        events,
                        {
                            "type":
                                "invalid_model_action",
                            "turn": turn,
                            "decision":
                                decision.model_dump(),
                            "error":
                                validation_error,
                        },
                    )

                    if invalid_decisions >= 3:
                        create_failure_evidence(
                            page,
                            run_dir,
                            f"invalid_{turn}",
                        )
                        raise RuntimeError(
                            "Discovery stopped after "
                            "three invalid model actions. "
                            f"Last error: "
                            f"{validation_error}"
                        )

                    continue

                invalid_decisions = 0
                last_model_error = None

                if decision.action == "finish":
                    proposed = Checkpoint(
                        kind=(
                            decision.checkpoint_kind
                        ),
                        value=normalize_parameter(
                            decision.checkpoint_value
                            or "",
                            params,
                        ),
                    )

                    if stable_output_checkpoint:
                        checkpoint = (
                            stable_output_checkpoint
                        )
                    else:
                        checkpoint = proposed

                    if not checkpoint_satisfied(
                        page,
                        checkpoint,
                        params,
                    ):
                        last_model_error = (
                            "The proposed completion "
                            "checkpoint is not currently "
                            "satisfied. Continue until a "
                            "verifiable stable checkpoint "
                            "is available."
                        )
                        checkpoint = None
                        continue

                    break

                if decision.action == "wait":
                    page.wait_for_timeout(700)
                    continue

                element = element_by_ref(
                    observation,
                    decision.ref or "",
                )

                if element.get("risk"):
                    resumed, note = request_human(
                        page,
                        run_dir,
                        "Risky action requires human "
                        f"approval: "
                        f"{element.get('risk')}",
                    )
                    if not resumed:
                        raise RuntimeError(note)

                    log_event(
                        events,
                        {
                            "type":
                                "human_handoff_complete",
                            "turn": turn,
                            "note": note,
                        },
                    )
                    continue

                step_id = len(steps) + 1

                try:
                    if page.url != observation.get("url"):
                        last_model_error = (
                            "The page changed after the observation was captured. "
                            "Re-observe the current page and choose a target from the new state."
                        )
                        log_event(
                            events,
                            {
                                "type": "stale_observation",
                                "turn": turn,
                                "observed_url": observation.get("url"),
                                "current_url": page.url,
                                "decision": decision.model_dump(),
                            },
                        )
                        continue

                    locators = build_locator_stack(
                        element
                    )

                    try:
                        target = resolve_locator(
                            page,
                            locators,
                        )
                    except RuntimeError as exc:
                        last_model_error = (
                            "The selected element was present in the observation but "
                            "could not be resolved when the action was about to run. "
                            "The page may have changed or the element may have been "
                            "re-rendered. Re-observe the current page and choose a "
                            "currently visible target. "
                            f"Resolution details: {redact_text(str(exc))}"
                        )
                        log_event(
                            events,
                            {
                                "type": "stale_or_missing_target",
                                "turn": turn,
                                "ref": decision.ref,
                                "element": element,
                                "locators": [
                                    locator.model_dump()
                                    for locator in locators
                                ],
                                "observed_url": observation.get("url"),
                                "current_url": page.url,
                                "error": redact_text(str(exc)),
                            },
                        )
                        continue

                    if decision.action == "click":
                        target.click(
                            timeout=args.timeout_ms
                        )
                        steps.append(
                            Step(
                                id=step_id,
                                action="click",
                                description=(
                                    decision.reason
                                ),
                                locators=locators,
                            )
                        )

                    elif decision.action == "type":
                        ensure_editable_target(
                            target
                        )
                        value = (
                            decision.value or ""
                        )
                        target.fill(
                            value,
                            timeout=args.timeout_ms,
                        )
                        steps.append(
                            Step(
                                id=step_id,
                                action="type",
                                description=(
                                    decision.reason
                                ),
                                locators=locators,
                                value=(
                                    normalize_parameter(
                                        value,
                                        params,
                                    )
                                ),
                            )
                        )

                    elif (
                        decision.action == "select"
                    ):
                        value = (
                            decision.value or ""
                        )
                        target.select_option(
                            label=value,
                            timeout=args.timeout_ms,
                        )
                        steps.append(
                            Step(
                                id=step_id,
                                action="select",
                                description=(
                                    decision.reason
                                ),
                                locators=locators,
                                value=(
                                    normalize_parameter(
                                        value,
                                        params,
                                    )
                                ),
                            )
                        )

                    elif (
                        decision.action == "extract"
                    ):
                        output_name = (
                            decision.output_name
                            or "value"
                        )
                        extracted_value = (
                            read_target_value(
                                target
                            )
                        )
                        declared_outputs[
                            output_name
                        ] = CapabilityOutput(
                            name=output_name,
                            description=(
                                "Value extracted for "
                                f"{output_name}"
                            ),
                        )
                        steps.append(
                            Step(
                                id=step_id,
                                action="extract",
                                description=(
                                    decision.reason
                                ),
                                locators=locators,
                                output_name=(
                                    output_name
                                ),
                            )
                        )
                        stable_output_checkpoint = (
                            checkpoint_from_locators(
                                locators
                            )
                        )
                        log_event(
                            events,
                            {
                                "type":
                                    "output_observed",
                                "name":
                                    output_name,
                                "value":
                                    "[REDACTED_IN_LOG]",
                                "non_empty":
                                    bool(
                                        extracted_value
                                    ),
                            },
                        )

                        if (
                            extracted_value
                            and decision.checkpoint_kind
                            and decision.checkpoint_value
                        ):
                            proposed_checkpoint = Checkpoint(
                                kind=decision.checkpoint_kind,
                                value=normalize_parameter(
                                    decision.checkpoint_value,
                                    params,
                                ),
                            )

                            if checkpoint_satisfied(
                                page,
                                proposed_checkpoint,
                                params,
                            ):
                                checkpoint = proposed_checkpoint
                                log_event(
                                    events,
                                    {
                                        "type":
                                            "goal_completed_after_extract",
                                        "turn": turn,
                                        "output_name":
                                            output_name,
                                        "checkpoint":
                                            checkpoint.model_dump(),
                                    },
                                )
                                break

                        if (
                            extracted_value
                            and stable_output_checkpoint
                            and checkpoint_satisfied(
                                page,
                                stable_output_checkpoint,
                                params,
                            )
                        ):
                            checkpoint = stable_output_checkpoint
                            log_event(
                                events,
                                {
                                    "type":
                                        "goal_completed_after_extract",
                                    "turn": turn,
                                    "output_name":
                                        output_name,
                                    "checkpoint":
                                        checkpoint.model_dump(),
                                },
                            )
                            break

                except Exception as exc:
                    last_model_error = (
                        "The selected action could not "
                        "be executed on the resolved "
                        f"element: "
                        f"{redact_text(str(exc))}"
                    )
                    log_event(
                        events,
                        {
                            "type":
                                "action_execution_error",
                            "turn": turn,
                            "action":
                                decision.action,
                            "ref": decision.ref,
                            "error":
                                last_model_error,
                        },
                    )
                    continue

                page.wait_for_load_state(
                    "domcontentloaded",
                    timeout=args.timeout_ms,
                )

                outcome = detect_outcome(page)

                if (
                    outcome
                    and outcome[0]
                    in KNOWN_BUSINESS_OUTCOMES
                ):
                    raise RuntimeError(
                        "Discovery goal ended in "
                        "business outcome "
                        f"{outcome[0]}: "
                        f"{outcome[1]}"
                    )
            else:
                raise RuntimeError(
                    "Discovery exceeded max steps: "
                    f"{args.max_steps}"
                )

            if checkpoint is None:
                raise RuntimeError(
                    "Discovery finished without "
                    "a checkpoint"
                )

            artifact = CapabilityArtifact(
                capability_name=args.name,
                description=args.goal,
                app_family=(
                    "northstar-service-console"
                ),
                app_version="demo-1",
                inputs=[
                    CapabilityInput(
                        name=name,
                        description=(
                            f"Runtime input: {name}"
                        ),
                    )
                    for name in params
                ],
                outputs=list(
                    declared_outputs.values()
                ),
                steps=steps,
                checkpoint=checkpoint,
                metadata={
                    "discovered_at":
                        datetime.now(
                            timezone.utc
                        ).isoformat(),
                    "model": model,
                    "target_origin":
                        urlparse(
                            args.url
                        ).netloc,
                    "locator_policy":
                        "id/name/data-field/"
                        "aria-label before "
                        "role/text fallback",
                    "risk_policy":
                        "irreversible controls "
                        "require human control",
                },
            )

            artifact_path = (
                ARTIFACTS
                / f"{args.name}.json"
            )

            write_json(
                artifact_path,
                artifact.model_dump(),
            )
            write_json(
                run_dir / "artifact.json",
                artifact.model_dump(),
            )
            page.screenshot(
                path=str(
                    run_dir / "final.png"
                ),
                full_page=True,
            )
            log_event(
                events,
                {
                    "type":
                        "discovery_complete",
                    "artifact":
                        str(
                            artifact_path.relative_to(
                                ROOT
                            )
                        ),
                    "checkpoint":
                        checkpoint.model_dump(),
                },
            )

        except Exception as exc:
            try:
                create_failure_evidence(
                    page,
                    run_dir,
                    "discovery",
                )
            except Exception:
                pass

            log_event(
                events,
                {
                    "type":
                        "discovery_failed",
                    "error":
                        redact_text(str(exc)),
                },
            )
            raise

        finally:
            browser.close()

    print(
        json.dumps(
            {
                "status": "success",
                "artifact":
                    str(artifact_path),
                "evidence":
                    str(run_dir),
            },
            indent=2,
        )
    )


def replay(args):
    from playwright.sync_api import (
        TimeoutError as PlaywrightTimeoutError,
        sync_playwright,
    )

    artifact_path = Path(args.artifact)
    if not artifact_path.is_absolute():
        artifact_path = ROOT / artifact_path

    artifact = (
        CapabilityArtifact.model_validate_json(
            artifact_path.read_text(
                encoding="utf-8"
            )
        )
    )

    params = parse_params(args.param)
    missing = [
        item.name
        for item in artifact.inputs
        if item.required
        and item.name not in params
    ]

    if missing:
        raise SystemExit(
            "Missing required parameters: "
            + ", ".join(missing)
        )

    run_dir = (
        EVIDENCE / f"replay_{utc_stamp()}"
    )
    run_dir.mkdir(parents=True)

    outputs: dict[str, Any] = {}
    result: RunResult | None = None

    with (
        (run_dir / "events.jsonl").open(
            "w",
            encoding="utf-8",
        ) as events,
        sync_playwright() as playwright,
    ):
        browser = playwright.chromium.launch(
            headless=args.headless
        )
        context = browser.new_context(
            viewport={
                "width": 1280,
                "height": 900,
            }
        )
        page = context.new_page()

        try:
            log_event(
                events,
                {
                    "type": "replay_started",
                    "capability":
                        artifact.capability_name,
                    "schema_version":
                        artifact.schema_version,
                },
            )

            for step in artifact.steps:
                try:
                    log_event(
                        events,
                        {
                            "type":
                                "step_started",
                            "step_id":
                                step.id,
                            "action":
                                step.action,
                            "description":
                                step.description,
                        },
                    )

                    if step.action == "navigate":
                        url = (
                            render_parameter(
                                step.value,
                                params,
                            )
                            or DEFAULT_URL
                        )
                        assert_allowed_url(url)
                        page.goto(
                            url,
                            wait_until=(
                                "domcontentloaded"
                            ),
                            timeout=args.timeout_ms,
                        )

                    elif step.action == "wait":
                        page.wait_for_timeout(
                            int(
                                render_parameter(
                                    step.value,
                                    params,
                                )
                                or "500"
                            )
                        )

                    else:
                        target = resolve_locator(
                            page,
                            step.locators,
                        )

                        if step.action == "click":
                            risk = (
                                target.get_attribute(
                                    "data-risk"
                                )
                            )

                            if (
                                risk
                                and not args.allow_risky
                            ):
                                resumed, note = (
                                    request_human(
                                        page,
                                        run_dir,
                                        "Risky action "
                                        "requires human "
                                        "approval: "
                                        f"{risk}",
                                    )
                                )

                                if not resumed:
                                    result = (
                                        RunResult(
                                            status=(
                                                "escalated"
                                            ),
                                            capability=(
                                                artifact.capability_name
                                            ),
                                            step_id=(
                                                step.id
                                            ),
                                            error_code=(
                                                "HUMAN_REQUIRED"
                                            ),
                                            message=note,
                                        )
                                    )
                                    break
                            else:
                                target.click(
                                    timeout=(
                                        args.timeout_ms
                                    )
                                )

                        elif step.action == "type":
                            ensure_editable_target(
                                target
                            )
                            target.fill(
                                render_parameter(
                                    step.value,
                                    params,
                                )
                                or "",
                                timeout=(
                                    args.timeout_ms
                                ),
                            )

                        elif (
                            step.action == "select"
                        ):
                            target.select_option(
                                label=render_parameter(
                                    step.value,
                                    params,
                                ),
                                timeout=(
                                    args.timeout_ms
                                ),
                            )

                        elif (
                            step.action == "extract"
                        ):
                            outputs[
                                step.output_name
                                or "value"
                            ] = read_target_value(
                                target
                            )

                    page.wait_for_load_state(
                        "domcontentloaded",
                        timeout=args.timeout_ms,
                    )

                    outcome = detect_outcome(page)

                    if outcome:
                        code, message = outcome

                        if (
                            code
                            in KNOWN_BUSINESS_OUTCOMES
                        ):
                            result = RunResult(
                                status=(
                                    "business_outcome"
                                ),
                                capability=(
                                    artifact.capability_name
                                ),
                                business_outcome=(
                                    code
                                ),
                                step_id=step.id,
                                message=message,
                            )
                            log_event(
                                events,
                                {
                                    "type":
                                        "business_outcome",
                                    "step_id":
                                        step.id,
                                    "code": code,
                                    "message":
                                        message,
                                },
                            )
                            break

                        if (
                            code
                            in RECOVERABLE_OUTCOMES
                        ):
                            resumed, note = (
                                request_human(
                                    page,
                                    run_dir,
                                    message,
                                )
                            )

                            if not resumed:
                                result = (
                                    RunResult(
                                        status=(
                                            "recoverable"
                                        ),
                                        capability=(
                                            artifact.capability_name
                                        ),
                                        business_outcome=(
                                            code
                                        ),
                                        step_id=(
                                            step.id
                                        ),
                                        message=note,
                                    )
                                )
                                break

                    log_event(
                        events,
                        {
                            "type":
                                "step_completed",
                            "step_id": step.id,
                        },
                    )

                except (
                    PlaywrightTimeoutError,
                    RuntimeError,
                ) as exc:
                    create_failure_evidence(
                        page,
                        run_dir,
                        step.id,
                    )
                    result = RunResult(
                        status="failure",
                        capability=(
                            artifact.capability_name
                        ),
                        error_code=(
                            "REPLAY_STEP_FAILED"
                        ),
                        step_id=step.id,
                        message=redact_text(
                            str(exc)
                        ),
                    )
                    log_event(
                        events,
                        {
                            "type":
                                "hard_failure",
                            "step_id":
                                step.id,
                            "error":
                                redact_text(
                                    str(exc)
                                ),
                        },
                    )
                    break

            if result is None:
                if not checkpoint_satisfied(
                    page,
                    artifact.checkpoint,
                    params,
                ):
                    create_failure_evidence(
                        page,
                        run_dir,
                        "checkpoint",
                    )
                    result = RunResult(
                        status="failure",
                        capability=(
                            artifact.capability_name
                        ),
                        error_code=(
                            "CHECKPOINT_FAILED"
                        ),
                        message=(
                            "Checkpoint not "
                            "satisfied: "
                            f"{artifact.checkpoint.model_dump()}"
                        ),
                    )
                else:
                    result = RunResult(
                        status="success",
                        capability=(
                            artifact.capability_name
                        ),
                        outputs=outputs,
                        message=(
                            "Replay completed and "
                            "checkpoint verified"
                        ),
                    )

            page.screenshot(
                path=str(
                    run_dir / "final.png"
                ),
                full_page=True,
            )
            write_json(
                run_dir / "result.json",
                result.model_dump(),
            )
            log_event(
                events,
                {
                    "type":
                        "replay_result",
                    **result.model_dump(),
                },
            )

        finally:
            browser.close()

    print(
        json.dumps(
            result.model_dump(),
            indent=2,
        )
    )
    print(f"Evidence: {run_dir}")

    return (
        0
        if result.status
        in {
            "success",
            "business_outcome",
        }
        else 2
    )


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Computer-use discovery and "
            "deterministic replay"
        )
    )
    sub = parser.add_subparsers(
        dest="command",
        required=True,
    )

    discover_parser = sub.add_parser(
        "discover"
    )
    discover_parser.add_argument(
        "--goal",
        required=True,
    )
    discover_parser.add_argument(
        "--name",
        required=True,
    )
    discover_parser.add_argument(
        "--url",
        default=DEFAULT_URL,
    )
    discover_parser.add_argument(
        "--param",
        action="append",
        default=[],
    )
    discover_parser.add_argument(
        "--max-steps",
        type=int,
        default=14,
    )
    discover_parser.add_argument(
        "--timeout-ms",
        type=int,
        default=7000,
    )
    discover_parser.add_argument(
        "--headless",
        action="store_true",
    )
    discover_parser.set_defaults(
        func=discover
    )

    replay_parser = sub.add_parser(
        "replay"
    )
    replay_parser.add_argument(
        "--artifact",
        required=True,
    )
    replay_parser.add_argument(
        "--param",
        action="append",
        default=[],
    )
    replay_parser.add_argument(
        "--timeout-ms",
        type=int,
        default=7000,
    )
    replay_parser.add_argument(
        "--allow-risky",
        action="store_true",
    )
    replay_parser.add_argument(
        "--headless",
        action="store_true",
    )
    replay_parser.set_defaults(
        func=replay
    )

    return parser.parse_args()


if __name__ == "__main__":
    cli_args = parse_args()
    exit_code = cli_args.func(cli_args)

    if isinstance(exit_code, int):
        raise SystemExit(exit_code)
