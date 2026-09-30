"""Durable local application coordinator. HTTP threads never touch Playwright."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
from pathlib import Path
import re
from urllib.parse import urlsplit
from uuid import uuid4

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.controls import effective_settings
from app.ghost_cursor.models import Confirmation, Policy, TERMINAL, Trace, validate_transition
from app.ghost_cursor.runtime import NeedsHuman, Runner
from app.models import Application, CandidateProfileReview, Job, Profile, TestExecution, utcnow
from app.pipeline.fixture_workflows import profile_bindings, workflow
from app.pipeline.tailor import tailor_application
from app.submit.test_ats import TEST_ATS_SCENARIOS, fixture_url, validate_base_url

OLD_BLOCK = "real submission blocked while local test ATS mode is enabled"
_PUBLIC_EMAIL = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_PUBLIC_PHONE = re.compile(r"(?<!\w)\+?\d[\d\s().-]{7,}\d(?!\w)")
_PUBLIC_URL = re.compile(r"\b(?:https?://|www\.)\S+", re.IGNORECASE)
log = logging.getLogger(__name__)


class ExecutionError(Exception):
    def __init__(self, reason: str, status: int = 409):
        self.reason, self.status = reason, status
        super().__init__(reason)


def aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


class Coordinator:
    def __init__(self, engine, worker=None, answer_resolver=None):
        self.engine, self.worker = engine, worker
        self.answer_resolver = answer_resolver

    def _worker(self):
        if self.worker is None:
            from app.submit.assist_session import _session
            self.worker = _session()
        return self.worker

    def settings(self, session, *, confirm=False):
        settings = effective_settings(session)
        return self._validate_settings(settings, confirm=confirm)

    @staticmethod
    def _validate_settings(settings, *, confirm=False):
        if not settings.test_ats_enabled:
            raise ExecutionError("Local test mode is disabled")
        if settings.submit_kill_switch:
            raise ExecutionError("Kill switch blocks local execution")
        if confirm and settings.dry_run:
            raise ExecutionError("Dry run permits review only; final confirmation is disabled")
        try:
            validate_base_url(settings.test_ats_base_url)
        except ValueError as exc:
            raise ExecutionError(str(exc), 400) from exc
        return settings

    @staticmethod
    def _expired(row) -> bool:
        return bool(row.expires_at and aware(row.expires_at) <= utcnow()
                    and row.state not in TERMINAL | {"confirming"})

    @staticmethod
    def _candidate_brain_drafts(row) -> list[dict]:
        drafts = []
        for item in row.trace or []:
            step, reason = str(item.get("step", "")), str(item.get("reason", ""))
            if not step.startswith("answer:") or not reason.startswith("candidate_brain_grounded:"):
                continue
            evidence = []
            for value in item.get("candidates", [])[:3]:
                text = " ".join(str(value).split())[:240]
                if (not text or _PUBLIC_EMAIL.search(text) or _PUBLIC_PHONE.search(text)
                        or _PUBLIC_URL.search(text)):
                    continue
                evidence.append(text)
            question = " ".join(str(item.get("selected") or step[7:].replace("_", " ")).split())[:160]
            if evidence:
                drafts.append({"field": step[7:][:80], "question": question, "evidence": evidence})
        return drafts

    def _status_payload(self, row, settings, ahead=0):
        blocked = None
        try:
            self._validate_settings(settings, confirm=True)
        except ExecutionError as exc:
            blocked = exc.reason
        return {
            "execution_id": row.id, "application_id": row.application_id, "mode": "test",
            "state": row.state, "scenario": row.scenario, "review_version": row.review_version,
            "target_choices": row.target_choices,
            "reason_code": row.reason_code, "unresolved_fields": row.unresolved_fields,
            "can_confirm": row.state == "ready_for_review" and blocked is None,
            "confirmation_blocked_reason": blocked,
            "can_cancel": row.state not in TERMINAL | {"confirming"},
            "can_resume": row.state == "needs_human" and not settings.submit_kill_switch and settings.test_ats_enabled,
            "queue_position": ahead if row.state == "queued" else 0,
            "current_step": row.current_step, "receipt": row.receipt,
            "candidate_brain_drafts": self._candidate_brain_drafts(row),
            "expires_at": row.expires_at.isoformat() if row.expires_at else None,
        }

    def get(self, execution_id: str) -> TestExecution:
        with Session(self.engine) as session:
            row = session.get(TestExecution, execution_id)
            if row is None:
                raise ExecutionError("Execution not found", 404)
            session.expunge(row)
        if self._expired(row):
            self.transition(row.id, row.state, "expired", reason_code="review_timeout")
            return self.get(execution_id)
        return row

    def transition(self, execution_id, old, new, **fields) -> bool:
        validate_transition(old, new)
        fields.update(state=new, updated_at=utcnow())
        if new in TERMINAL:
            fields.update(active_application_id=None, answers={})
        with Session(self.engine) as session:
            result = session.execute(update(TestExecution).where(TestExecution.id == execution_id,
                TestExecution.state == old).values(**fields))
            session.commit()
            return result.rowcount == 1

    def status(self, execution_id):
        row = self.get(execution_id)
        with Session(self.engine) as session:
            settings = effective_settings(session)
            ahead = session.exec(select(TestExecution).where(TestExecution.active_application_id != None,
                TestExecution.created_at < row.created_at)).all()
        return self._status_payload(row, settings, len(ahead))

    def latest_many(self, application_ids) -> dict[int, dict]:
        ids = list(application_ids)
        if not ids:
            return {}
        with Session(self.engine) as session:
            settings = effective_settings(session)
            rows = session.exec(select(TestExecution).where(TestExecution.application_id.in_(ids))
                .order_by(TestExecution.created_at.desc())).all()
            latest = {}
            for row in rows:
                latest.setdefault(row.application_id, row)
            changed = False
            for row in latest.values():
                if not self._expired(row):
                    continue
                result = session.execute(update(TestExecution).where(TestExecution.id == row.id,
                    TestExecution.state == row.state).values(state="expired", active_application_id=None,
                        reason_code="review_timeout", answers={}, updated_at=utcnow()))
                if result.rowcount:
                    row.state, row.active_application_id = "expired", None
                    row.reason_code, row.answers = "review_timeout", {}
                    changed = True
            if changed:
                session.commit()
            active = sorted((row for row in rows if row.active_application_id is not None),
                            key=lambda row: row.created_at)
            positions = {row.id: index for index, row in enumerate(active)}
            return {app_id: self._status_payload(row, settings, positions.get(row.id, 0))
                    for app_id, row in latest.items()}

    def latest(self, application_id):
        with Session(self.engine) as session:
            row = session.exec(select(TestExecution).where(TestExecution.application_id == application_id)
                .order_by(TestExecution.created_at.desc())).first()
            return self.status(row.id) if row else None

    def start(self, application_id: int):
        with Session(self.engine, expire_on_commit=False) as session:
            settings = self.settings(session)
            record = session.exec(select(Application, Job,
                select(Profile.id).limit(1).scalar_subquery()).join(Job)
                .where(Application.id == application_id)).first()
            if record is None:
                raise ExecutionError("Application not found", 404)
            app, job, profile_id = record
            if app.status == "submitted":
                raise ExecutionError("Already submitted applications are excluded from local execution")
            if profile_id is None:
                raise ExecutionError("No profile configured", 400)
            active = session.exec(select(TestExecution).where(TestExecution.active_application_id == application_id)).first()
            if active and self._expired(active):
                result = session.execute(update(TestExecution).where(TestExecution.id == active.id,
                    TestExecution.state == active.state).values(state="expired", active_application_id=None,
                        reason_code="review_timeout", answers={}, updated_at=utcnow()))
                session.commit()
                if result.rowcount:
                    active = None
            if active:
                ahead = session.exec(select(TestExecution.id).where(
                    TestExecution.active_application_id != None,
                    TestExecution.created_at < active.created_at)).all()
                return self._status_payload(active, settings, len(ahead)), False
            ahead = session.exec(select(TestExecution.id).where(
                TestExecution.active_application_id != None)).all()
            row = TestExecution(id=str(uuid4()), application_id=application_id,
                active_application_id=application_id, scenario=TEST_ATS_SCENARIOS[(job.id - 1) % 4])
            session.add(row)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                active = session.exec(select(TestExecution).where(TestExecution.active_application_id == application_id)).one()
                earlier = session.exec(select(TestExecution.id).where(
                    TestExecution.active_application_id != None,
                    TestExecution.created_at < active.created_at)).all()
                return self._status_payload(active, settings, len(earlier)), False
            execution_id = row.id
        try:
            self._worker().enqueue_task(application_id, lambda owner: self.process(execution_id, owner))
        except Exception:
            self.transition(execution_id, "queued", "failed", reason_code="queue_unavailable")
            raise ExecutionError("Browser queue unavailable", 503)
        return self._status_payload(row, settings, len(ahead)), True

    def confirm(self, execution_id: str, confirmation: Confirmation):
        row = self.get(execution_id)
        if row.review_version != confirmation.review_version:
            raise ExecutionError("Stale review version")
        if row.state in {"confirming", "succeeded"}:
            return self.status(execution_id)
        with Session(self.engine) as session:
            self.settings(session, confirm=True)
        try:
            confirmation.validate_current(row.state, row.review_version, True)
        except ValueError as exc:
            raise ExecutionError(str(exc)) from exc
        # Compare version as well as state: a new review cannot inherit old approval.
        with Session(self.engine) as session:
            result = session.execute(update(TestExecution).where(TestExecution.id == execution_id,
                TestExecution.state == "ready_for_review", TestExecution.review_version == confirmation.review_version)
                .values(state="confirming", reason_code="explicit_confirmation", updated_at=utcnow()))
            session.commit()
        if not result.rowcount:
            current = self.get(execution_id)
            if current.state not in {"confirming", "succeeded"} or current.review_version != confirmation.review_version:
                raise ExecutionError("Review changed before confirmation")
        return self.status(execution_id)

    def intervene(self, execution_id: str, answers: dict, targets: dict | None = None):
        targets = targets or {}
        row = self.get(execution_id)
        with Session(self.engine) as session:
            self.settings(session)
        if row.state != "needs_human":
            raise ExecutionError("Execution is not awaiting intervention")
        if (not answers and not targets) or set(answers) - set(row.unresolved_fields) or "resume" in answers:
            raise ExecutionError("Supply only the requested answers; artifacts must be prepared by JobPilot", 400)
        if any(step not in row.target_choices or token not in {c["token"] for c in row.target_choices[step]}
               for step, token in targets.items()):
            raise ExecutionError("Select a target from the current observed candidates", 400)
        if row.resume_attempts >= 5:
            raise ExecutionError("Intervention limit reached; cancel and start a new run")
        if not all(isinstance(v, (str, bool)) and len(str(v)) <= 10000 for v in answers.values()):
            raise ExecutionError("Answers must be text or boolean values", 400)
        if not self.transition(row.id, "needs_human", "filling", answers={**row.answers, **answers},
                               selected_targets={**row.selected_targets, **targets},
                               resume_attempts=row.resume_attempts + 1, reason_code="resuming", expires_at=None):
            raise ExecutionError("Execution changed before intervention")
        return self.status(row.id)

    def cancel(self, execution_id):
        for _ in range(3):
            row = self.get(execution_id)
            if row.state in TERMINAL:
                return self.status(row.id)
            if row.state == "confirming":
                raise ExecutionError("Final action already authorized; wait for its verified outcome")
            if self.transition(row.id, row.state, "cancelled", reason_code="user_cancelled"):
                return self.status(row.id)
        raise ExecutionError("Execution changed repeatedly during cancellation")

    def recover(self):
        """Startup migration of lost live state, never pretending to restore a page."""
        with Session(self.engine) as session:
            session.execute(update(TestExecution).where(TestExecution.active_application_id != None)
                .values(state="expired", active_application_id=None, reason_code="backend_restarted",
                        answers={}, updated_at=utcnow()))
            session.commit()

    def bulk(self):
        with Session(self.engine) as session:
            self.settings(session)
            if session.exec(select(Profile)).first() is None:
                raise ExecutionError("No profile configured", 400)
            apps = session.exec(select(Application).where(Application.status != "submitted")).all()
        result = {"mode": "test", "queued_test": 0, "already_active": 0, "no_tailored": 0, "blocked": 0, "reasons": []}
        for app in apps:
            if not app.resume_path or not Path(app.resume_path).is_file():
                result["no_tailored"] += 1
                continue
            try:
                _, created = self.start(app.id)
                result["queued_test" if created else "already_active"] += 1
            except ExecutionError as exc:
                result["blocked"] += 1
                result["reasons"].append({"application_id": app.id, "reason": exc.reason})
        return result

    def process(self, execution_id, owner):
        page = None
        context = None
        route_handler = None
        popup_handler = None
        runner = None
        origin = None
        blocked_request = [False]

        def guard():
            row = self.get(execution_id)
            if row.state in TERMINAL:
                raise ExecutionError("execution_ended")
            if getattr(owner, "_closing", owner._stop).is_set():
                raise ExecutionError("worker_stopped")
            if blocked_request[0]:
                raise ExecutionError("destination_blocked")
            with Session(self.engine) as session:
                current_settings = self.settings(session, confirm=row.state == "confirming")
                if origin and validate_base_url(current_settings.test_ats_base_url) != origin:
                    raise ExecutionError("destination_settings_changed")

        try:
            if not self.transition(execution_id, "queued", "preparing", reason_code="preparing_artifacts"):
                return
            guard()
            with Session(self.engine) as session:
                settings = self.settings(session)
                row = session.get(TestExecution, execution_id)
                app = session.get(Application, row.application_id)
                profile = session.exec(select(Profile)).first()
                approved_candidate = None
                candidate_review = session.get(CandidateProfileReview, 1)
                if candidate_review and candidate_review.approved_profile:
                    from app.candidate_brain.models import Candidate
                    approved_candidate = Candidate.model_validate(candidate_review.approved_profile)
                job = session.get(Job, app.job_id)
                old_status, old_reason = app.status, app.needs_human_reason
                if not app.resume_path or not Path(app.resume_path).is_file() or not app.cover_letter_path or not Path(app.cover_letter_path).is_file():
                    try:
                        tailor_application(session, app, job, profile, settings)
                    except Exception as exc:
                        raise ExecutionError("artifact_preparation_failed") from exc
                if not Path(app.resume_path).is_file() or not Path(app.cover_letter_path).is_file():
                    raise ExecutionError("prepared_artifact_missing")
                if old_status == "needs_human" and old_reason != OLD_BLOCK:
                    app.status, app.needs_human_reason = old_status, old_reason
                elif old_reason == OLD_BLOCK:
                    app.status, app.needs_human_reason = "tailored", ""
                session.add(app)
                session.commit()
                bindings = profile_bindings(profile, app)
                origin = validate_base_url(settings.test_ats_base_url)
                url = fixture_url(job.id, origin) + "?execution_id=" + execution_id
                scenario = row.scenario
                artifacts = {"resume": app.resume_path, "cover_letter": app.cover_letter_path}
            flow = workflow(scenario, url)
            if self.answer_resolver is None:
                from app.candidate_brain.services import resolve_application_answers
                resolution = resolve_application_answers(
                    profile, job, flow, bindings, candidate=approved_candidate,
                )
            else:
                resolution = self.answer_resolver(profile, job, flow, bindings)
            bindings.update(resolution.answers)
            guard()
            if not self.transition(execution_id, "preparing", "opening", artifact_refs=artifacts):
                return
            owner._ensure_browser()
            context = owner._context
            policy = Policy(origin=origin, paths=[urlsplit(url).path])

            def route_handler(route):
                request = route.request
                if policy.allows(request.url, navigation=request.is_navigation_request()):
                    # Browser routing does not intercept subsequent redirect hops.
                    # Fetch exactly one permitted response and reject every redirect.
                    try:
                        response = route.fetch(max_redirects=0, timeout=policy.timeout_ms)
                        if 300 <= response.status < 400:
                            blocked_request[0] = True
                            route.abort()
                        else:
                            route.fulfill(response=response)
                    except Exception:
                        blocked_request[0] = True
                        route.abort()
                else:
                    blocked_request[0] = True
                    route.abort()

            context.route("**/*", route_handler)
            page = context.new_page()
            # No popup can become an unguarded alternate page.
            def popup_handler(popup):
                if popup != page:
                    blocked_request[0] = True
                    popup.close()
            context.on("page", popup_handler)
            # Fixtures need no websocket (including Vite HMR) during execution.
            page.route_web_socket("**/*", lambda socket: None)
            if not self.transition(execution_id, "opening", "filling", reason_code="filling"):
                return
            runner = Runner(page, flow, bindings, policy, execution_id, guard,
                progress=lambda _step: owner.emit_frame(execution_id, page))
            runner.trace.extend(Trace(step=f"answer:{draft.field}", at=utcnow().isoformat(),
                candidates=draft.evidence_snippets, selected=draft.question, verified=True,
                reason="candidate_brain_grounded:" + ",".join(draft.evidence_ids))
                for draft in resolution.drafts)
            result = runner.run()
            self._save_result(runner, result, settings.test_review_timeout_seconds)
            while True:
                row = self.get(execution_id)
                if row.state in TERMINAL:
                    break
                guard()
                if page.is_closed():
                    raise ExecutionError("browser_disconnected")
                if row.state == "confirming":
                    with Session(self.engine) as session:
                        self.settings(session, confirm=True)
                    receipt = runner.confirm(Confirmation(review_version=row.review_version), permitted=True)
                    self.transition(row.id, "confirming", "succeeded", receipt=receipt, reason_code="test_completed")
                    break
                if row.state == "filling":
                    result = runner.resume(row.answers, row.selected_targets)
                    self._save_result(runner, result, settings.test_review_timeout_seconds)
                elif row.state == "ready_for_review":
                    if runner._verify_review() != row.review_digest:
                        raise NeedsHuman("review", "stale_review")
                owner.emit_frame(execution_id, page, full_page=True)
                page.wait_for_timeout(150)
        except NeedsHuman as exc:
            row = self.get(execution_id)
            if row.state not in TERMINAL:
                self.transition(row.id, row.state, "failed", reason_code=exc.reason)
        except Exception as exc:
            log.exception("Local test execution %s failed", execution_id)
            row = self.get(execution_id)
            if row.state not in TERMINAL:
                reason = exc.reason if isinstance(exc, ExecutionError) else (str(exc) if isinstance(exc, ValueError) else "browser_execution_failed")
                self.transition(row.id, row.state, "failed", reason_code=reason)
        finally:
            if page is not None:
                try:
                    page.close()
                except Exception:
                    pass
            if context is not None:
                try:
                    if route_handler:
                        context.unroute("**/*", route_handler)
                    if popup_handler:
                        context.remove_listener("page", popup_handler)
                except Exception:
                    pass  # A disconnected context is cleared by the owner's close callback.

    def _save_result(self, runner, result, timeout):
        self.transition(runner.execution_id, "filling", result.state.value,
            reason_code=result.reason_code, unresolved_fields=result.unresolved_fields,
            target_choices=runner.target_choices,
            review_version=runner.review_version, review_digest=runner.review_digest,
            trace=[t.model_dump() for t in runner.trace],
            current_step=runner.trace[-1].step if runner.trace else "",
            expires_at=utcnow() + timedelta(seconds=timeout))
