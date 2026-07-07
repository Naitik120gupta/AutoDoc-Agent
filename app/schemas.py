from pydantic import BaseModel, Field
from typing import Any, Optional


class AgentRequest(BaseModel):
    request: str = Field(..., min_length=3, description="Natural language task request")


class TaskStep(BaseModel):
    id: int
    action: str          # "gather_info" | "draft_section" | "generate_docx"
    description: str
    inputs: dict[str, Any] = Field(default_factory=dict)


class ReflectionResult(BaseModel):
    needs_retry: bool
    issues: list[str] = Field(default_factory=list)
    notes: str = ""


class AgentResponse(BaseModel):
    request: str
    plan: list[TaskStep]
    assumptions_made: list[str] = Field(default_factory=list)
    reflection: ReflectionResult
    retried: bool
    document_filename: str
    download_url: str
    summary: str
