from __future__ import annotations

from typing import Any, Literal
from pydantic import BaseModel, Field, model_validator


class Locator(BaseModel):
    by: Literal["id", "name", "role", "text", "css"]
    value: str
    role: str | None = None


class Step(BaseModel):
    id: int
    action: Literal["navigate", "click", "type", "select", "extract", "wait"]
    description: str
    locators: list[Locator] = Field(default_factory=list)
    value: str | None = None
    output_name: str | None = None
    safe: bool = True

    @model_validator(mode="after")
    def validate_step(self):
        if self.action not in {"navigate", "wait"} and not self.locators:
            raise ValueError(f"{self.action} step requires locators")
        if self.action == "extract" and not self.output_name:
            raise ValueError("extract step requires output_name")
        return self


class CapabilityInput(BaseModel):
    name: str
    type: Literal["string", "number", "boolean"] = "string"
    required: bool = True
    description: str


class CapabilityOutput(BaseModel):
    name: str
    type: Literal["string", "number", "boolean"] = "string"
    description: str


class Checkpoint(BaseModel):
    kind: Literal["url_contains", "text_present", "selector_present"]
    value: str


class CapabilityArtifact(BaseModel):
    schema_version: str = "1.0"
    capability_name: str
    description: str
    app_family: str
    app_version: str
    inputs: list[CapabilityInput]
    outputs: list[CapabilityOutput] = Field(default_factory=list)
    steps: list[Step]
    checkpoint: Checkpoint
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_artifact(self):
        ids = [step.id for step in self.steps]
        if ids != list(range(1, len(ids) + 1)):
            raise ValueError("step ids must be sequential and start at 1")
        output_names = {output.name for output in self.outputs}
        extracted = {step.output_name for step in self.steps if step.action == "extract"}
        if not extracted.issubset(output_names):
            raise ValueError("every extract step must have a declared output")
        return self


class RunResult(BaseModel):
    status: Literal["success", "business_outcome", "recoverable", "failure", "escalated"]
    capability: str
    outputs: dict[str, Any] = Field(default_factory=dict)
    business_outcome: str | None = None
    error_code: str | None = None
    step_id: int | None = None
    message: str


class AgentDecision(BaseModel):
    action: Literal["click", "type", "select", "extract", "wait", "finish"]
    ref: str | None = None
    value: str | None = None
    output_name: str | None = None
    reason: str
    checkpoint_kind: Literal["url_contains", "text_present", "selector_present"] | None = None
    checkpoint_value: str | None = None

    @model_validator(mode="after")
    def validate_decision(self):
        if self.action in {"click", "type", "select", "extract"} and not self.ref:
            raise ValueError(f"{self.action} requires ref")
        if self.action in {"type", "select"} and self.value is None:
            raise ValueError(f"{self.action} requires value")
        if self.action == "extract" and not self.output_name:
            raise ValueError("extract requires output_name")
        if self.action == "finish" and (not self.checkpoint_kind or not self.checkpoint_value):
            raise ValueError("finish requires checkpoint_kind and checkpoint_value")
        return self
