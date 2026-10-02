"""Shared import, query and evidence contracts. Offsets are Python Unicode characters."""

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field, StrictInt, model_validator


class Issue(BaseModel):
    code: str
    severity: Literal["info", "review", "error"] = "review"
    detail: str
    source: str = ""
    row: int | None = None


class RecordInput(BaseModel):
    record_id: str
    dataset: Literal["native", "social"]
    url: str = ""
    publisher: str = ""
    title: str = ""
    published_at: date | None = None
    sponsor: str = ""
    keyword: str = ""
    platform: str = ""
    account: str = ""
    body: str = ""
    disclosure: str = ""
    archive_url: str = ""
    countable: bool = True
    retrievable: bool = False
    retrieval_end: int | None = Field(default=None, ge=0)
    retrieval_ranges: list[tuple[StrictInt, StrictInt]] | None = None
    raw: dict[str, Any] = Field(default_factory=dict)
    provenance: list[dict[str, Any]] = Field(default_factory=list)
    issues: list[Issue] = Field(default_factory=list)
    annotations: list[dict[str, Any]] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_retrieval_ranges(self):
        if self.retrieval_ranges is not None:
            from .chunking import retrieval_spans

            retrieval_spans(self.body, retrieval_ranges=self.retrieval_ranges)
        return self


class ImportBatch(BaseModel):
    records: list[RecordInput] = Field(default_factory=list)
    rejected: list[Issue] = Field(default_factory=list)
    candidates: list[dict[str, Any]] = Field(default_factory=list)
    source_hashes: dict[str, str] = Field(default_factory=dict)


class Filters(BaseModel):
    dataset: Literal["native", "social", "all"] = "native"
    publishers: list[str] = Field(default_factory=list)
    sponsors: list[str] = Field(default_factory=list)
    platforms: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)
    record_ids: list[str] = Field(default_factory=list)
    date_from: date | None = None
    date_to: date | None = None
    include_unknown_dates: bool = True
    date_presence: Literal["any", "known", "missing"] = Field(default="any", description="Publication-date presence, intersected with the date range and include_unknown_dates. Null or empty date is missing; not an ingestion or collection date.")
    include_inferred_dates: bool = Field(default=False, description="Use unreviewed inferred dates (date_inferences) where the source date is missing. Off by default; answers must state when inferred dates were used.")


class Evidence(BaseModel):
    evidence_id: str
    record_id: str
    version_id: str
    dataset: str
    title: str
    publisher: str = ""
    sponsor: str = ""
    url: str = ""
    archive_url: str = ""
    published_at: date | None = None
    text: str
    start: int
    end: int
    paragraph_ids: list[str] = Field(default_factory=list)
    score: float = 0.0
    retrieval_rank: int | None = None
    retrieval_sources: list[str] = Field(default_factory=list)
    matched_terms: list[str] = Field(default_factory=list)
    literal_matched_terms: list[str] = Field(default_factory=list)


class Citation(BaseModel):
    evidence_id: str
    quote: str


class CitedStatement(BaseModel):
    """A displayed statement bound to the answer's one-based citation numbers."""

    text: str
    citation_indices: list[StrictInt] = Field(default_factory=list)


class AnswerSection(BaseModel):
    """A neutral heading grouping existing cited claims, without extra prose."""

    title: str
    citation_indices: list[StrictInt] = Field(default_factory=list)


class Answer(BaseModel):
    status: Literal[
        "answered", "insufficient_evidence", "service_unavailable", "limited"
    ]
    answer: str
    answer_mode: Literal["rag", "statistics", "clarification", "tools", "web_supplement"] = "rag"
    structured_result: dict[str, Any] | None = None
    research_trace: dict[str, Any] = Field(default_factory=dict)
    citations: list[Citation] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    summary: list[CitedStatement] = Field(default_factory=list)
    sections: list[AnswerSection] = Field(default_factory=list)
    cited_claims: list[CitedStatement] = Field(default_factory=list)
    external_research: dict[str, Any] = Field(default_factory=dict)
    cost_usd: float = 0.0
    latency_ms: int = 0
    failure_reason: str = ""
    language_check: dict[str, Any] = Field(default_factory=dict)
