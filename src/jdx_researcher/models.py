"""Versioned public contracts. Scores describe relevance, never factual confidence."""
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION = "1.0"
Status = Literal["queued", "running", "awaiting_review", "partial", "completed", "cancelled", "failed"]
Relation = Literal["supports", "contradicts", "insufficient"]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Budget(Contract):
    max_queries: int = Field(12, ge=1, le=50)
    max_documents: int = Field(30, ge=1, le=100)
    max_agent_steps: int = Field(60, ge=1, le=100)
    max_runtime_seconds: int = Field(1200, ge=1, le=7200)
    max_gap_rounds: int = Field(2, ge=0, le=2)


class ResearchRequest(Contract):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    objective: str = Field(min_length=5, max_length=6000)
    constraints: dict[str, Any] = Field(default_factory=dict)
    languages: list[Literal["es", "en"]] = Field(default_factory=lambda: ["es", "en"], min_length=1, max_length=2)
    output_language: Literal["es", "en"] = "es"
    subquestions: list[str] = Field(default_factory=list, max_length=4)
    seed_urls: list[str] = Field(default_factory=list, max_length=30)
    as_of: date | None = None
    budget_profile: Literal["balanced"] = "balanced"
    budget: Budget = Field(default_factory=Budget)
    idempotency_key: str = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def bounded_input(self):
        if any(not q.strip() or len(q) > 1200 for q in self.subquestions):
            raise ValueError("Subquestions must contain 1–1200 characters")
        if any(len(url) > 4096 for url in self.seed_urls):
            raise ValueError("Seed URL too long")
        if len(str(self.constraints)) > 12000:
            raise ValueError("Constraints too large")
        return self


class Query(Contract):
    query: str = Field(min_length=2, max_length=800)
    provider: Literal["web", "github", "arxiv"] = "web"
    language: Literal["es", "en"] = "en"
    subquestion: int = Field(0, ge=0, le=3)


class Plan(Contract):
    subquestions: list[str] = Field(min_length=1, max_length=4)
    queries: list[Query] = Field(min_length=1, max_length=12)


class SearchResult(Contract):
    url: str
    title: str
    snippet: str = ""
    provider: str
    published_at: str | None = None


class Passage(Contract):
    passage_id: str
    source_id: str
    text: str
    start: int
    end: int
    page: int | None = None


class Source(Contract):
    source_id: str
    original_url: str
    final_url: str
    title: str
    captured_at: str
    published_at: str | None = None
    body_sha256: str
    text_sha256: str
    media_type: str
    extractor: str
    # Same bytes, including mirrors, belong to one exact-copy family.
    family_id: str
    passages: list[Passage]


class EvidenceLink(Contract):
    passage_id: str
    relation: Relation


class Finding(Contract):
    text: str = Field(min_length=5, max_length=2000)
    subquestion: int = Field(ge=0, le=3)
    conditions: dict[str, Any] = Field(default_factory=dict)
    evidence: list[EvidenceLink] = Field(min_length=1, max_length=12)
    origin: Literal["local_model", "demo"] = "local_model"
    review_status: Literal["proposed"] = "proposed"


class ReportClaim(Contract):
    text: str = Field(min_length=1, max_length=4000)
    kind: Literal["sourced", "inference", "uncertain"] = "sourced"
    citations: list[str] = Field(default_factory=list, max_length=30)
    verified: bool = False
    reviewed_by: str | None = Field(None, max_length=200)

    @model_validator(mode="after")
    def require_review(self):
        if self.kind == "sourced" and not self.citations:
            raise ValueError("A sourced claim requires passage citations")
        if self.verified and (not self.citations or not self.reviewed_by or self.kind != "sourced"):
            raise ValueError("Verified claims require citations and a named reviewer")
        return self


class FinalReport(Contract):
    title: str = Field(min_length=1, max_length=400)
    markdown: str = Field(min_length=1, max_length=120000)
    claims: list[ReportClaim] = Field(min_length=1, max_length=100)
    # Server always retains open issues; the caller cannot remove them.
    reviewer_notes: str = Field("", max_length=8000)


class ConflictError(ValueError):
    pass


class BudgetExceeded(RuntimeError):
    pass


class JobCancelled(RuntimeError):
    pass


class LeaseLost(RuntimeError):
    pass


class ReaderError(RuntimeError):
    pass
