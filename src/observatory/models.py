"""Shared import, query and evidence contracts. Offsets are Python Unicode characters."""

from datetime import date
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    field_validator,
    model_validator,
)

from .media_evidence import ImageLocation, TimeLocation
from .source_text_quality import TextQualityCode


def validate_postgres_text(value):
    """Reject NUL in text/JSONB, including nested strings and object keys.

    Never clean source content here. A source adapter may explicitly preserve
    an original value in a reversible, documented storage representation.
    """
    if isinstance(value, str):
        if "\x00" in value:
            raise ValueError("PostgreSQL text/JSONB cannot store U+0000; preserve it explicitly before import")
    elif isinstance(value, dict):
        for key, item in value.items():
            validate_postgres_text(key)
            validate_postgres_text(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            validate_postgres_text(item)


def validate_historical_label_scope(dataset, labels):
    """Keep social source-state IDs separate from native positive labels."""
    # The source adapter imports RecordInput, so resolve its fixed vocabulary
    # after model definitions have loaded rather than creating an import cycle.
    from .social_annotations import parse_social_state_id

    if not isinstance(labels, list):
        raise ValueError("Historical labels must be a list")
    for value in labels:
        parsed = parse_social_state_id(value)
        if dataset == "social" and parsed is None:
            raise ValueError("Social label filters require fixed historical source-state IDs")
        if dataset != "social" and parsed is not None:
            raise ValueError("Social historical label filters require the social collection")


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
    def validate_source_text_and_ranges(self):
        validate_postgres_text(self.model_dump())
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
    accounts: list[str] = Field(default_factory=list, description="Exact source channel.name values in the social collection; names do not establish unique account identities.")
    keywords: list[str] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)
    record_ids: list[str] = Field(default_factory=list)
    date_from: date | None = None
    date_to: date | None = None
    include_unknown_dates: bool = True
    date_presence: Literal["any", "known", "missing"] = Field(default="any", description="Publication-date presence, intersected with the date range and include_unknown_dates. Null or empty date is missing; not an ingestion or collection date.")
    include_inferred_dates: bool = Field(default=False, description="Use unreviewed inferred dates (date_inferences) where the source date is missing. Off by default; answers must state when inferred dates were used.")

    @model_validator(mode="after")
    def validate_account_scope(self):
        if self.accounts and self.dataset != "social":
            raise ValueError("Account filters require the social collection")
        validate_historical_label_scope(self.dataset, self.labels)
        return self


_ObservationId = Annotated[str, Field(strict=True, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,239}$")]
_SourceHash = Annotated[str, Field(strict=True, pattern=r"^[0-9a-f]{64}$")]
_QualityCode = Annotated[str, Field(strict=True, pattern=r"^[a-z][a-z0-9_]{0,119}$")]


class _ObservationSourceMetadata(BaseModel):
    source_observation_id: _ObservationId | None = None
    source_version_id: _SourceHash | None = None
    source_body_hash: _SourceHash | None = None
    source_observation_count: Annotated[StrictInt, Field(ge=1)] = 1
    source_conflicts: list[Literal["body", "sponsor", "account", "published_at", "platform", "historical_labels"]] = Field(default_factory=list, strict=True)
    source_quality_codes: list[_QualityCode] = Field(default_factory=list, strict=True)

    @model_validator(mode="after")
    def complete_observation_binding(self):
        identities = (self.source_observation_id, self.source_version_id, self.source_body_hash)
        if any(value is not None for value in identities) and not all(value is not None for value in identities):
            raise ValueError("Source observation identity, version and body hash must be supplied together")
        if self.source_observation_id is None and (
            self.source_observation_count != 1 or self.source_conflicts or self.source_quality_codes
        ):
            raise ValueError("Source observation context requires an exact observation binding")
        if (len(self.source_conflicts) != len(set(self.source_conflicts))
                or len(self.source_quality_codes) != len(set(self.source_quality_codes))):
            raise ValueError("Source observation context cannot repeat conflict or quality codes")
        return self


class Evidence(_ObservationSourceMetadata):
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
    source_text_quality_codes: list[TextQualityCode] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def strict_observation_offsets(cls, value):
        if isinstance(value, dict) and any(value.get(key) is not None for key in (
            "source_observation_id", "source_version_id", "source_body_hash",
        )):
            start, end, text = value.get("start"), value.get("end"), value.get("text")
            if (type(start) is not int or type(end) is not int or not isinstance(text, str)
                    or not 0 <= start < end or end - start != len(text) or not text.strip()):
                raise ValueError("Source observation evidence requires exact Unicode character offsets")
        return value

    @model_validator(mode="after")
    def observation_dataset(self):
        if self.source_observation_id is not None and self.dataset != "social":
            raise ValueError("Source observation evidence belongs to the social collection")
        return self


class MediaAnswerEvidence(BaseModel):
    """Saved derived media text, never an article-body quotation.

    The hashes and derived offsets bind this public projection to its saved
    asset and extraction. They do not verify visual meaning or speech accuracy.
    A reader must revalidate that binding before and after answer generation.
    """

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, revalidate_instances="always")

    evidence_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,239}$")]
    asset_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,239}$")]
    record_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,239}$")]
    version_id: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    dataset: Literal["native", "social"]
    title: Annotated[str, Field(max_length=300)]
    source_url: str = ""
    media_type: Literal["image", "video"]
    origin: Literal["ocr", "vision", "human_description", "publisher_caption", "automatic_caption", "transcript"]
    locator: Annotated[ImageLocation | TimeLocation, Field(discriminator="kind")]
    evidence_text: Annotated[str, Field(min_length=1, max_length=6000)]
    quality_label: str
    asset_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    text_artifact_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,239}$")]
    artifact_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    derived_start: Annotated[StrictInt, Field(ge=0)]
    derived_end: Annotated[StrictInt, Field(gt=0)]
    quote_from_original_body: Literal[False] = False
    image_or_speech_semantics_verified: Literal[False] = False

    @classmethod
    def from_source_ref(cls, source_ref):
        """Narrow a reader result without exposing paths or extraction metadata."""
        if isinstance(source_ref, BaseModel):
            source_ref = source_ref.model_dump()
        if not isinstance(source_ref, dict):
            raise ValueError("A media source reference must be a mapping")
        return cls.model_validate({
            name: source_ref[name] for name in cls.model_fields if name in source_ref
        })

    @field_validator("quote_from_original_body", "image_or_speech_semantics_verified", mode="before")
    @classmethod
    def no_original_or_semantic_promotion(cls, value):
        if value is not False:
            raise ValueError("Derived media cannot become verified original evidence")
        return value

    @field_validator("locator", mode="before")
    @classmethod
    def revalidate_nested_location(cls, value):
        # ImageLocation.region is a mutable list even on a frozen instance.
        return value.model_dump() if isinstance(value, (ImageLocation, TimeLocation)) else value

    @field_validator("source_url")
    @classmethod
    def public_source_link(cls, value):
        if value:
            try:
                parsed = urlsplit(value)
                valid = (parsed.scheme in {"http", "https"} and parsed.hostname
                         and not parsed.username and not parsed.password)
            except ValueError:
                valid = False
            if not valid:
                raise ValueError("Media source links must be public HTTP(S) URLs")
        return value

    @model_validator(mode="after")
    def own_derived_location(self):
        if (self.derived_end - self.derived_start != len(self.evidence_text)
                or not self.evidence_text.strip()):
            raise ValueError("Media evidence must keep its exact derived-text interval")
        expected = ImageLocation if self.media_type == "image" else TimeLocation
        allowed = ({"ocr", "vision", "human_description"} if self.media_type == "image"
                   else {"publisher_caption", "automatic_caption", "transcript", "human_description"})
        if not isinstance(self.locator, expected) or self.origin not in allowed:
            raise ValueError("Media origin and location must match their own medium")
        if isinstance(self.locator, ImageLocation) and self.locator.screenshot_sha256 != self.asset_sha256:
            raise ValueError("Image location must bind to its own asset hash")
        validate_postgres_text(self.model_dump())
        return self


class Citation(_ObservationSourceMetadata):
    evidence_id: str
    quote: str
    evidence_type: Literal["article_text", "image", "video"] = "article_text"
    origin: str = "original_text"

    @model_validator(mode="after")
    def observation_origin(self):
        if self.source_observation_id is not None and (
            self.evidence_type != "article_text" or self.origin != "original_text"
        ):
            raise ValueError("Source observation citations must reference their supplied original text")
        return self


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
    media_evidence: list[MediaAnswerEvidence] = Field(default_factory=list)
    summary: list[CitedStatement] = Field(default_factory=list)
    sections: list[AnswerSection] = Field(default_factory=list)
    cited_claims: list[CitedStatement] = Field(default_factory=list)
    external_research: dict[str, Any] = Field(default_factory=dict)
    cost_usd: float = 0.0
    latency_ms: int = 0
    failure_reason: str = ""
    language_check: dict[str, Any] = Field(default_factory=dict)
