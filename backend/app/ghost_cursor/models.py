"""Browser workflow contracts; no JobPilot or ATS-specific dependencies."""
from enum import Enum
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator


class State(str, Enum):
    QUEUED = "queued"
    PREPARING = "preparing"
    OPENING = "opening"
    FILLING = "filling"
    READY_FOR_REVIEW = "ready_for_review"
    NEEDS_HUMAN = "needs_human"
    CONFIRMING = "confirming"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


TERMINAL = {"succeeded", "failed", "cancelled", "expired"}
TRANSITIONS = {
    "queued": {"preparing"}, "preparing": {"opening"}, "opening": {"filling"},
    "filling": {"needs_human", "ready_for_review"}, "needs_human": {"filling"},
    "ready_for_review": {"confirming", "needs_human"}, "confirming": {"succeeded", "needs_human"},
}


def validate_transition(old: str, new: str) -> None:
    if old in TERMINAL or new not in TRANSITIONS.get(old, set()) | {"failed", "cancelled", "expired"}:
        raise ValueError(f"Invalid execution transition: {old} -> {new}")


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExecutionResult(Contract):
    execution_id: str
    state: State
    reason_code: str
    unresolved_fields: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)


class Target(Contract):
    names: list[str] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)
    role: str | None = None
    value: str | None = None


class Action(Contract):
    id: str
    kind: Literal["fill", "select", "check", "upload", "click", "wait", "intervene", "final_submit"]
    target: Target = Field(default_factory=Target)
    binding: str | None = None
    optional: bool = False
    when_binding: str | None = None
    when_value: str | None = None
    review_key: str | None = None


class Workflow(Contract):
    version: str = "1"
    name: str
    url: str
    steps: list[Action]
    preconditions: list[str] = Field(default_factory=list)
    postconditions: list[str] = Field(default_factory=list)
    review_test_id: str = "review-stage"
    receipt_test_id: str = "submission-receipt"
    receipt_storage_key: str = "test-ats:last-submission"

    @model_validator(mode="after")
    def validate_steps(self):
        if not self.steps or self.steps[-1].kind != "final_submit":
            raise ValueError("Workflow must end with an explicit final_submit action")
        if sum(s.kind == "final_submit" for s in self.steps) != 1:
            raise ValueError("Exactly one final action is allowed")
        if len({s.id for s in self.steps}) != len(self.steps):
            raise ValueError("Step IDs must be unique")
        return self


class Policy(Contract):
    origin: str
    paths: list[str]
    timeout_ms: int = Field(default=10000, ge=100, le=60000)
    recovery_limit: int = Field(default=2, ge=0, le=5)

    def allows(self, url: str, *, navigation: bool = True) -> bool:
        try:
            target, base = urlsplit(url), urlsplit(self.origin)
            same = (target.scheme, target.hostname, target.port) == (base.scheme, base.hostname, base.port)
            return bool(same and not target.username and not target.password
                        and (not navigation or target.path in self.paths))
        except ValueError:
            return False


class Candidate(Contract):
    index: int
    name: str
    label: str
    role: str
    input_type: str
    required: bool
    options: list[str] = Field(default_factory=list)


class Observation(Contract):
    url: str
    controls: list[Candidate]


class Trace(Contract):
    step: str
    at: str
    candidates: list[str] = Field(default_factory=list)
    selected: str | None = None
    verified: bool = False
    reason: str = ""


class Confirmation(Contract):
    review_version: int = Field(ge=1)

    def validate_current(self, state: str, version: int, permitted: bool) -> None:
        if not permitted or state != "ready_for_review" or version != self.review_version:
            raise ValueError("Confirmation is not authorized for the current review")
