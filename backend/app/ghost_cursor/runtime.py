"""Execute semantic steps on a caller-owned Page; consume final approval once."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from playwright.sync_api import Page, expect

from .models import Candidate, Confirmation, ExecutionResult, Observation, Policy, State, Trace, Workflow


class NeedsHuman(Exception):
    def __init__(self, field: str, reason: str):
        self.field, self.reason = field, reason
        super().__init__(reason)


def observe(page: Page) -> Observation:
    controls = page.locator("input, select, textarea, button").evaluate_all("""els => els.map((e, index) => ({
        index, name: e.name || '', label: e.getAttribute('aria-label') ||
          [...(e.labels || [])].map(l => l.innerText).join(' ') || e.innerText || '',
        role: e.getAttribute('role') || (e.tagName === 'BUTTON' ? 'button' : e.tagName.toLowerCase()),
        input_type: e.type || '', required: !!e.required,
        options: e.options ? [...e.options].map(o => o.value) : []
    }))""")
    return Observation(url=page.url, controls=[Candidate(**c) for c in controls])


class Runner:
    def __init__(self, page: Page, workflow: Workflow, bindings: dict, policy: Policy,
                 execution_id: str, guard: Callable[[], None] = lambda: None,
                 progress: Callable[[str], None] = lambda _step: None):
        self.page, self.workflow, self.bindings, self.policy = page, workflow, dict(bindings), policy
        self.execution_id, self.guard, self.progress = execution_id, guard, progress
        self.trace: list[Trace] = []
        self.expected: dict[str, str] = {}
        self.review_digest = ""
        self.review_version = 0
        self.consumed = False
        self.target_choices: dict[str, list[dict[str, str]]] = {}
        self.selected_targets: dict[str, str] = {}
        self.started_at = datetime.now(timezone.utc)
        self.page.set_default_timeout(policy.timeout_ms)

    def _progress(self, step: str):
        try:
            self.progress(step)
        except Exception:
            # Streaming is observational and must never change execution outcome.
            pass

    def _guard(self):
        self.guard()
        if not self.policy.allows(self.page.url):
            raise ValueError("destination_blocked")

    def _target(self, action):
        target, matches, visible_semantic = action.target, [], []
        for c in observe(self.page).controls:
            semantic_match = (c.role == target.role and c.label in target.labels) if target.role else (
                c.name in target.names or c.label.rstrip(" *").casefold() in {s.casefold() for s in target.labels})
            if not semantic_match:
                continue
            loc = self.page.locator("input, select, textarea, button").nth(c.index)
            if loc.is_visible():
                visible_semantic.append(loc)
                if target.value is not None and loc.get_attribute("value") != target.value:
                    continue
                # Bounded observation token; no arbitrary selector accepted from a client.
                token = hashlib.sha256(c.model_dump_json().encode()).hexdigest()
                matches.append((loc, token, c))
        if target.value is not None and visible_semantic and not matches:
            raise NeedsHuman(action.binding or action.id, "invalid_choice")
        selected = self.selected_targets.get(action.id)
        if selected:
            chosen = [loc for loc, token, _ in matches if token == selected]
            if len(chosen) == 1:
                return chosen[0]
            self.selected_targets.pop(action.id, None)
        if len(matches) != 1:
            self.target_choices[action.id] = [{"token": token, "label": c.label or c.name,
                "name": c.name, "description": f"{c.role} {c.input_type}, observed control {c.index + 1}"}
                for _, token, c in matches]
            raise NeedsHuman(action.binding or action.id, "target_missing_or_ambiguous")
        return matches[0][0]

    def _review_values(self) -> dict[str, str]:
        review = self.page.get_by_test_id(self.workflow.review_test_id)
        expect(review).to_be_visible()
        return review.locator("dl > div").evaluate_all(
            "els => Object.fromEntries(els.map(e => [e.querySelector('dt').textContent.replaceAll(' ', '_'), e.querySelector('dd').textContent]))")

    def _verify_review(self):
        self._guard()
        self._target(self.workflow.steps[-1])
        values = self._review_values()
        if any(values.get(k) != v for k, v in self.expected.items()):
            raise NeedsHuman("review", "review_values_changed")
        if self.page.evaluate("key => sessionStorage.getItem(key)", self.workflow.receipt_storage_key) is not None:
            raise ValueError("unexpected_receipt_before_confirmation")
        expect(self.page.get_by_test_id(self.workflow.receipt_test_id)).to_have_count(0)
        return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()

    def run(self) -> ExecutionResult:
        self.guard()
        if not self.policy.allows(self.workflow.url):
            raise ValueError("destination_blocked")
        self.page.goto(self.workflow.url)
        self._guard()
        self.page.evaluate("key => sessionStorage.removeItem(key)", self.workflow.receipt_storage_key)
        self.expected = {}
        self.target_choices = {}
        self._progress("opened")
        for action in self.workflow.steps:
            self._guard()
            if action.kind == "final_submit":
                break
            if action.when_binding and str(self.bindings.get(action.when_binding, "")) != action.when_value:
                continue
            try:
                value = self.bindings.get(action.binding) if action.binding else None
                if action.binding and (value is None or value == ""):
                    if action.optional:
                        continue
                    raise NeedsHuman(action.binding, "missing_answer")
                if action.kind == "intervene":
                    raise NeedsHuman(action.binding or action.id, "human_action_required")
                if action.kind == "wait":
                    self.page.get_by_label(action.target.labels[0], exact=False).wait_for(state="visible")
                    self._progress(action.id)
                    continue
                if action.kind == "check" and value is False:
                    raise NeedsHuman(action.binding or action.id, "explicit_acknowledgement_required")
                target_action = action
                if action.kind == "check" and action.target.value == "$binding":
                    target_action = action.model_copy(update={"target": action.target.model_copy(update={"value": str(value)})})
                loc = self._target(target_action)
                if action.kind == "fill":
                    loc.fill(str(value))
                    expect(loc).to_have_value(str(value))
                elif action.kind == "select":
                    loc.select_option(label=str(value))
                    expect(self._target(target_action)).to_have_value(str(value))
                elif action.kind == "check":
                    loc.check()
                    expect(self._target(target_action)).to_be_checked()
                elif action.kind == "upload":
                    path = Path(str(value))
                    if not path.is_file():
                        raise NeedsHuman(action.binding, "artifact_missing")
                    loc.set_input_files(str(path))
                    if loc.evaluate("e => e.files[0]?.name") != path.name:
                        raise NeedsHuman(action.binding, "upload_verification_failed")
                elif action.kind == "click":
                    if loc.get_attribute("data-final-submit") == "true":
                        raise ValueError("unclassified_final_action")
                    invalid = self.page.locator("input:invalid, select:invalid, textarea:invalid")
                    if invalid.count():
                        name = invalid.first.get_attribute("name") or action.id
                        binding = next((s.binding for s in self.workflow.steps if s.review_key == name), name)
                        raise NeedsHuman(binding or name, "invalid_required_value")
                    loc.click()
                if action.review_key:
                    self.expected[action.review_key] = (Path(str(value)).name if action.kind == "upload"
                        else ("yes" if value is True else str(value)))
                self.trace.append(Trace(step=action.id, at=datetime.now(timezone.utc).isoformat(),
                    candidates=action.target.names or action.target.labels, selected=action.review_key or action.id, verified=True))
                self._progress(action.id)
            except NeedsHuman as exc:
                return self._human(exc)
            except Exception as exc:
                self._guard()
                if isinstance(exc, ValueError):
                    raise
                return self._human(NeedsHuman(action.binding or action.id, "action_verification_failed"))
        try:
            self.review_digest = self._verify_review()
        except NeedsHuman as exc:
            return self._human(exc)
        except AssertionError:
            return self._human(NeedsHuman("review", "review_not_reached"))
        self.review_version += 1
        self._progress("review")
        return ExecutionResult(execution_id=self.execution_id, state=State.READY_FOR_REVIEW,
                               reason_code="review_checks_passed")

    def _human(self, exc):
        self.trace.append(Trace(step=exc.field, at=datetime.now(timezone.utc).isoformat(), reason=exc.reason))
        self._progress(exc.field)
        return ExecutionResult(execution_id=self.execution_id, state=State.NEEDS_HUMAN,
                               reason_code=exc.reason, unresolved_fields=[exc.field])

    def resume(self, answers: dict, selected_targets: dict[str, str] | None = None) -> ExecutionResult:
        self.bindings.update(answers)
        self.selected_targets.update(selected_targets or {})
        # Re-observe from a fresh navigation; restores values cleared by remounts.
        return self.run()

    def confirm(self, confirmation: Confirmation, *, permitted: bool) -> dict:
        confirmation.validate_current("ready_for_review" if not self.consumed else "confirming",
                                      self.review_version, permitted)
        if self._verify_review() != self.review_digest:
            raise NeedsHuman("review", "stale_review")
        self._guard()
        self.consumed = True
        self._target(self.workflow.steps[-1]).click()
        expect(self.page.get_by_test_id(self.workflow.receipt_test_id)).to_be_visible()
        receipt = self.page.evaluate("key => JSON.parse(sessionStorage.getItem(key))", self.workflow.receipt_storage_key)
        if not receipt or receipt.get("execution_id") != self.execution_id or receipt.get("kind") != self.workflow.name or receipt.get("localOnly") is not True:
            raise ValueError("receipt_correlation_failed")
        if datetime.fromisoformat(receipt["at"].replace("Z", "+00:00")) < self.started_at:
            raise ValueError("stale_receipt")
        if any(receipt.get("values", {}).get(k) != v for k, v in self.expected.items()):
            raise ValueError("receipt_values_mismatch")
        self._progress("submitted")
        return {"execution_id": self.execution_id, "kind": receipt["kind"], "at": receipt["at"], "localOnly": True}
