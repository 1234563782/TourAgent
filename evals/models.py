"""Typed contracts for evaluation datasets and evidence results."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


CaseType = Literal["golden", "regression", "edge", "adversarial", "fault_injection", "production"]
Severity = Literal["low", "medium", "high", "critical"]


class ConversationTurn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["user", "assistant", "system", "tool"]
    content: str = Field(min_length=1)


class EvaluationCase(BaseModel):
    model_config = ConfigDict(extra="allow")

    case_id: str = Field(pattern=r"^[a-z][a-z0-9_-]+$")
    category: str = Field(min_length=1)
    oracle_type: Literal[
        "schema_assertion", "state_assertion", "retrieval_assertion", "human_review", "fault_injection"
    ]
    case_type: CaseType
    severity: Severity
    source: str = Field(min_length=1)
    expected_outcome: str = Field(min_length=1)
    expected: dict[str, Any]
    conversation: list[ConversationTurn] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    forbidden_behaviors: list[str] = Field(default_factory=list)
    allowed_tools: list[str] = Field(default_factory=list)
    forbidden_tools: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_input_shape(self) -> "EvaluationCase":
        extra = self.model_extra or {}
        if not self.conversation and not any(key in extra for key in ("request", "setup", "sequence")):
            raise ValueError("case needs conversation, request, setup, or sequence input")
        return self


class EvidenceResult(BaseModel):
    selector: str
    status: Literal["passed", "failed", "error", "skipped", "not_found"]
    duration_ms: int = Field(ge=0)
    detail: str = ""


class CaseResult(BaseModel):
    case_id: str
    category: str
    case_type: CaseType
    severity: Severity
    status: Literal["passed", "failed", "blocked", "unmapped"]
    evidence: list[EvidenceResult]
    hard_gate_failed: bool = False

