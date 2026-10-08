"""Grounded Responses adapter and persistent embedding cache."""

import json
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

import numpy as np
from openai import OpenAI
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field, StrictInt, create_model

from .budget import Budget, price
from .db import digest
from .language import POLICY_VERSION, check_claim_languages, language_hint
from .models import (
    Answer,
    AnswerSection,
    Citation,
    CitedStatement,
    Evidence,
    MediaAnswerEvidence,
)
from .prompts import ANSWER_SYSTEM as SYSTEM
from .prompts import PROMPT_POLICY_VERSION
from .question_policy import question_contract
from .segmentation import sentence_spans, text_regions
from .source_text_quality import text_quality_context

MAX_QUOTE_WORDS = 60
MAX_SUPPORT_PASSAGES = 5


class GroundedQuote(BaseModel):
    evidence_id: str
    quote: str


class GroundedClaim(GroundedQuote):
    text: str
    support_quotes: list[GroundedQuote] = Field(default_factory=list, max_length=MAX_SUPPORT_PASSAGES)


class ModelAnswer(BaseModel):
    status: Literal["answered", "insufficient_evidence"]
    claims: list[GroundedClaim]
    summary: list[CitedStatement] = Field(default_factory=list)
    sections: list[AnswerSection] = Field(default_factory=list)




@dataclass(frozen=True)
class _QuoteSource:
    evidence: object
    start: int
    end: int


@dataclass
class _EvidenceView:
    key: tuple
    start: int
    text: str
    sources: list[_QuoteSource]
    coverage_fallback: bool = False


_OBSERVATION_FIELDS = (
    "source_observation_id", "source_version_id", "source_body_hash",
    "source_observation_count", "source_conflicts", "source_quality_codes",
)


def _observation_metadata(source):
    defaults = {
        "source_observation_id": None, "source_version_id": None, "source_body_hash": None,
        "source_observation_count": 1, "source_conflicts": [], "source_quality_codes": [],
    }
    values = {field: getattr(source, field, defaults[field]) for field in _OBSERVATION_FIELDS}
    if values == defaults:
        return {}
    if not isinstance(source, Evidence):
        raise ValueError("Source observation evidence requires a validated evidence contract")
    if (type(source.start) is not int or type(source.end) is not int
            or not isinstance(source.text, str) or not source.text.strip()
            or not 0 <= source.start < source.end or source.end - source.start != len(source.text)):
        raise ValueError("Source observation evidence requires exact Unicode character offsets")
    # model_copy and mutable lists can bypass Pydantic validation after creation.
    # Inspect raw types first: JSON serialization can turn a forged bool into 0.
    checked = Evidence.model_validate(source.model_dump(mode="python"))
    return {field: getattr(checked, field) for field in _OBSERVATION_FIELDS}


def _observation_context(source):
    metadata = _observation_metadata(source)
    if not metadata:
        return {}
    return {
        **metadata,
        "source_kind": "supplied_social_post_observation",
        "source_status": "preserved_observation_not_adjudicated_post_text",
        "paid_ad_status": "unknown",
        "source_note": (
            "These are exact characters from one supplied observation. Any extra text or linked preview "
            "is part of that capture, not an adjudicated original post. Preserve unresolved differences "
            "and source quality limits; the text does not verify paid advertising or classification."
        ),
    }


def _evidence_views(evidence):
    """Join only verified, contiguous slices of one article version for segmentation."""
    groups = {}
    for index, e in enumerate(evidence):
        _observation_metadata(e)
        located = all(
            hasattr(e, field)
            for field in ("record_id", "version_id", "dataset", "start", "end")
        )
        if located:
            if (
                type(e.start) is not int
                or type(e.end) is not int
                or not 0 <= e.start < e.end
                or e.end - e.start != len(e.text)
            ):
                raise ValueError("Evidence offsets failed source validation")
            key = ("located", e.record_id, e.version_id, e.dataset) + tuple(
                getattr(e, field, None)
                for field in ("source_observation_id", "source_version_id", "source_body_hash")
            ) + tuple(
                getattr(e, field, "")
                for field in ("title", "publisher", "sponsor", "url", "archive_url")
            )
            source = _QuoteSource(e, e.start, e.end)
        else:
            # Small callers without source coordinates remain independent. There
            # is no defensible way to infer overlap from matching text alone.
            key = ("unlocated", index)
            source = _QuoteSource(e, 0, len(e.text))
        groups.setdefault(key, []).append(source)

    views = []
    for key, sources in groups.items():
        current = None
        for source in sorted(
            sources, key=lambda s: (s.start, s.end, s.evidence.evidence_id)
        ):
            if current is None or source.start > current.start + len(current.text):
                current = _EvidenceView(
                    key, source.start, source.evidence.text, [source],
                    coverage_fallback=getattr(source.evidence, "source_observation_id", None) is not None,
                )
                views.append(current)
                continue
            offset = source.start - current.start
            overlap = min(len(current.text) - offset, source.end - source.start)
            if current.text[offset : offset + overlap] != source.evidence.text[:overlap]:
                raise ValueError("Evidence overlap failed source validation")
            current.text += source.evidence.text[overlap:]
            current.sources.append(source)
    return views


def _quote_spans(text, *, coverage_fallback=False):
    """Group complete sentences without crossing blank paragraphs or page breaks."""
    groups = []
    for region_start, region_end in text_regions(text):
        region = text[region_start:region_end]
        group_start = group_end = None
        spans = (sentence_spans(region, coverage_fallback=True) if coverage_fallback
                 else sentence_spans(region))
        for start, end in spans:
            if (
                group_start is not None
                and len(region[group_start:end].split()) > MAX_QUOTE_WORDS
            ):
                groups.append((region_start + group_start, region_start + group_end))
                group_start = None
            if group_start is None:
                group_start = start
            group_end = end
        if group_start is not None:
            groups.append((region_start + group_start, region_start + group_end))
    for group_start, group_end in groups:
        # Long sentences still use 60-word windows with 15-word overlap. This is
        # deliberate excerpt overlap, distinct from duplicate retrieved chunks.
        words = list(re.finditer(r"\S+", text[group_start:group_end]))
        for start in range(0, len(words), MAX_QUOTE_WORDS - 15):
            end = min(start + MAX_QUOTE_WORDS, len(words))
            yield group_start + words[start].start(), group_start + words[end - 1].end()
            if end == len(words):
                break


def _source_pieces(view, start, end):
    """Every emitted quote must fit one original evidence, including across joins."""
    cursor = start
    spans = (sentence_spans(view.text[start:end], coverage_fallback=True) if view.coverage_fallback
             else sentence_spans(view.text[start:end]))
    boundaries = [start + stop for _, stop in spans]
    while cursor < end:
        while cursor < end and view.text[cursor].isspace():
            cursor += 1
        if cursor == end:
            break
        covering = [
            source for source in view.sources
            if source.start <= view.start + cursor < source.end
        ]
        if not covering:
            raise ValueError("Evidence quote crossed an uncovered source interval")
        # Prefer the longest remaining coverage, then stable source coordinates/ID.
        source = min(
            covering, key=lambda s: (-s.end, s.start, s.evidence.evidence_id)
        )
        stop = min(end, source.end - view.start)
        if stop < end:
            sentence_ends = [b for b in boundaries if cursor < b <= stop]
            if sentence_ends:
                stop = sentence_ends[-1]
            else:
                # A legacy chunk may itself cut a sentence (even a word). Do not
                # synthesize a spanning citation or claim it is a full sentence.
                word_ends = [
                    match.end() for match in re.finditer(r"\S+", view.text)
                    if cursor < match.end() <= stop
                ]
                if word_ends:
                    stop = word_ends[-1]
        quote_end = stop
        while quote_end > cursor and view.text[quote_end - 1].isspace():
            quote_end -= 1
        if cursor < quote_end:
            yield source, cursor, quote_end
        cursor = stop


def quote_catalog(evidence):
    """Deduplicate located intervals while retaining original evidence IDs and text."""
    catalog = {}
    seen = set()
    for view in _evidence_views(evidence):
        for start, end in _quote_spans(view.text, coverage_fallback=view.coverage_fallback):
            for source, quote_start, quote_end in _source_pieces(view, start, end):
                absolute_start, absolute_end = view.start + quote_start, view.start + quote_end
                # Metadata affects merge compatibility, but source identity and
                # original coordinates determine whether a passage is duplicated.
                identity = view.key[:7] if view.key[0] == "located" else view.key
                key = (identity, absolute_start, absolute_end)
                if key in seen:
                    continue
                quote = view.text[quote_start:quote_end]
                source_start = absolute_start - source.start
                if quote != source.evidence.text[source_start : source_start + len(quote)]:
                    raise ValueError("Evidence quote failed source validation")
                seen.add(key)
                catalog[f"Q{len(catalog) + 1}"] = {
                    "evidence_id": source.evidence.evidence_id,
                    "quote": quote,
                    "_view": view,
                    "_start": quote_start,
                    "_end": quote_end,
                }
    _attach_quote_context(catalog)
    return catalog


def _attach_quote_context(catalog):
    """Link exact excerpts without duplicating source text in the model prompt.

    Sentence boundaries describe only the verified, retrieved contiguous view.
    They cannot establish context outside that view or semantic entailment.
    """
    views = {}
    for passage_id, passage in catalog.items():
        views.setdefault(id(passage["_view"]), []).append(passage_id)
    for passage_ids in views.values():
        if all("context" in catalog[pid] for pid in passage_ids):
            continue
        view = catalog[passage_ids[0]]["_view"]
        spans = sentence_spans(view.text, coverage_fallback=view.coverage_fallback)
        starts, ends = {start for start, _ in spans}, {end for _, end in spans}
        for index, passage_id in enumerate(passage_ids):
            passage = catalog[passage_id]
            intersected = [(start, end) for start, end in spans
                           if start < passage["_end"] and passage["_start"] < end]
            if not intersected:
                raise ValueError("Evidence quote has no source sentence context")
            sentence_start, sentence_end = intersected[0][0], intersected[-1][1]
            passage["_sentence_start"] = sentence_start
            passage["_sentence_end"] = sentence_end
            starts_sentence = passage["_start"] in starts
            ends_sentence = passage["_end"] in ends
            passage["context"] = {
                "previous_passage_id": passage_ids[index - 1] if index else None,
                "next_passage_id": passage_ids[index + 1] if index + 1 < len(passage_ids) else None,
                "starts_sentence": starts_sentence,
                "ends_sentence": ends_sentence,
                "sentence_clipped": not (starts_sentence and ends_sentence),
                "sentence_context_passage_ids": [pid for pid in passage_ids
                    if catalog[pid]["_start"] < sentence_end and sentence_start < catalog[pid]["_end"]],
                "context_scope": "retrieved_contiguous_source_view_only",
            }


def selection_schema(catalog):
    # Structured Outputs can restrict identifiers to real passages at generation time.
    choices = Literal.__getitem__(tuple(catalog))
    claim = create_model(
        "SelectedClaim",
        passage_id=(choices, Field(description="Choose the passage that directly supports this specific claim.")),
        support_passage_ids=(list[choices], Field(default_factory=list, max_length=MAX_SUPPORT_PASSAGES, description=(
            "Distinct additional passages from the same retrieved source view that jointly support this one claim. "
            "For a sentence_clipped primary passage, select enough of sentence_context_passage_ids to cover its "
            "entire detected sentence, preserving speaker, quantity and qualifications. Include relevant exact "
            "neighbour passages for attribution or pronouns. If required context is unavailable, abstain."
        ))),
        text=(str, Field(description=(
            "A self-contained, source-attributed paraphrase in the required answer language. "
            "Name the advertisement or its identified speaker in this claim. "
            "Distinguish who reports a claim from who performs the described work. "
            "If a quantity is stated, include what is measured, the substance/object, "
            "magnitude, full unit, time basis and source qualification in this same claim. "
            "Use only details present in the primary passage and explicit support_passage_ids together; "
            "do not invent missing units, baselines or a current operating status. Keep one atomic fact "
            "with its attribution, quantity and qualifications in this claim; put unrelated facts in "
            "separate claims. Keep attribution and qualifications even when repeating them feels less concise."
        ))),
    )
    summary = create_model(
        "SelectedSummaryPoint",
        text=(str, Field(description=(
            "A short direct answer synthesized entirely from the referenced atomic "
            "claims, for any supported content question. Attribute claims to the ads, preserve their qualifications, "
            "and do not add outside facts or factual verification. Preserve source-stated unknowns "
            "from cited claims. Unestablished requested details may be qualified only as limits "
            "of the supplied material, never as source-author assertions or whole-article negatives."
        ))),
        citation_indices=(list[StrictInt], Field(description=(
            "Nonempty, unique one-based positions in claims that support this entire point."
        ))),
    )
    section = create_model(
        "SelectedAnswerSection",
        title=(str, Field(description=(
            "A short neutral heading relevant to the question: an article, topic, "
            "process, entity, or comparison. No additional assertion."
        ))),
        citation_indices=(list[StrictInt], Field(description=(
            "One-based positions of claims in this section. Across sections, include "
            "every claim exactly once."
        ))),
    )
    return create_model(
        "SelectedAnswer",
        status=(Literal["answered", "insufficient_evidence"], Field(description=(
            "Use answered when at least one requested part has responsive source-supported content, "
            "including an explicit source-stated uncertainty or condition. Preserve unresolved "
            "requested details as scoped qualifications; this is not a certification of complete "
            "coverage or factual truth. Use insufficient_evidence when no requested part is "
            "supportable; nonempty evidence or a shared topic does not justify answered."
        ))),
        claims=(list[claim], ...),
        # Defaults preserve historical local fixtures. Responses' strict schema
        # conversion still makes these fields required for new API requests.
        summary=(list[summary], Field(default_factory=list)),
        sections=(list[section], Field(default_factory=list)),
    )


def answer_quote_catalog(evidence, media_evidence=()):
    """One selection namespace; derived media never inherits body coordinates."""
    media = [MediaAnswerEvidence.model_validate(item) for item in media_evidence]
    identities = [item.evidence_id for item in [*evidence, *media]]
    if len(identities) != len(set(identities)):
        raise ValueError("Duplicate answer evidence identity")
    catalog = quote_catalog(evidence)
    for item in media:
        view = _EvidenceView(("media", item.evidence_id), 0, item.evidence_text, [])
        for start, end in _quote_spans(item.evidence_text):
            catalog[f"Q{len(catalog) + 1}"] = {
                "evidence_id": item.evidence_id,
                "quote": item.evidence_text[start:end],
                "_view": view,
                "_start": start,
                "_end": end,
            }
    _attach_quote_context(catalog)
    return catalog


def evidence_inventory(evidence, media, catalog):
    """Describe validated supplied sources without turning chunks into records.

    Source identity uses saved bindings, never a shared title or matching text.
    Counts include redundant supplied chunks even when their quotes are omitted.
    This is an input inventory, not a population query or semantic review.
    """
    records, sources, record_versions = {}, {}, set()
    passages = {}
    for passage_id, passage in catalog.items():
        passages.setdefault(passage["evidence_id"], []).append(passage_id)
    unlocated = 0
    for item, kind in [*((e, "article_text") for e in evidence),
                       *((e, "derived_media") for e in media)]:
        dataset = getattr(item, "dataset", None)
        record_id = getattr(item, "record_id", None)
        version_id = getattr(item, "version_id", None)
        located = all(isinstance(value, str) and value for value in (dataset, record_id, version_id))
        record = None
        if located:
            key = (dataset, record_id)
            if key not in records:
                records[key] = {
                    "record_ref": f"R{len(records) + 1}", "record_id": record_id,
                    "dataset": dataset, "version_ids": [], "source_refs": [],
                }
            record = records[key]
            if version_id not in record["version_ids"]:
                record["version_ids"].append(version_id)
            record_versions.add((dataset, record_id, version_id))
        else:
            unlocated += 1
        if kind == "article_text":
            binding = {field: getattr(item, field, None) for field in (
                "source_observation_id", "source_version_id", "source_body_hash",
            )}
        else:
            binding = {field: getattr(item, field) for field in (
                "asset_id", "asset_sha256", "text_artifact_id", "artifact_sha256", "origin",
            )}
        source_key = ((kind, dataset, record_id, version_id, *binding.values()) if located
                      else ("unlocated", item.evidence_id))
        if source_key not in sources:
            sources[source_key] = {
                "source_ref": f"S{len(sources) + 1}", "source_kind": kind,
                "record_ref": record["record_ref"] if record else None,
                "version_id": version_id, "identity_complete": located,
                **{field: value for field, value in binding.items() if value is not None},
                "evidence_ids": [], "included_evidence_ids": [], "passage_ids": [],
            }
        source = sources[source_key]
        if record and source["source_ref"] not in record["source_refs"]:
            record["source_refs"].append(source["source_ref"])
        source["evidence_ids"].append(item.evidence_id)
        if item.evidence_id in passages:
            source["included_evidence_ids"].append(item.evidence_id)
            source["passage_ids"].extend(passages[item.evidence_id])
    return {
        "scope": "supplied_retrieved_evidence_only", "population_statistics": False,
        "record_count": len(records), "record_version_count": len(record_versions),
        "source_count": len(sources), "text_chunk_count": len(evidence),
        "quoted_text_chunk_count": sum(e.evidence_id in passages for e in evidence),
        "media_evidence_count": len(media), "passage_count": len(catalog),
        "unlocated_evidence_count": unlocated,
        "count_units": {
            "record": "distinct supplied dataset + record_id; unidentified records excluded",
            "record_version": "distinct supplied dataset + record_id + version_id",
            "source": ("record version with its exact observation or derived media artifact binding; "
                       "each unlocated entry remains separate"),
            "text_chunk": "supplied original-text evidence entry, including redundant entries",
            "passage": "selectable exact excerpt in the quote catalog",
        },
        "records": list(records.values()), "sources": list(sources.values()),
    }


def _selection_supports(selection, source, catalog):
    support_ids = getattr(selection, "support_passage_ids", [])
    if (not isinstance(support_ids, list) or len(support_ids) > MAX_SUPPORT_PASSAGES
            or any(not isinstance(pid, str) or pid not in catalog or pid == selection.passage_id for pid in support_ids)
            or len(set(support_ids)) != len(support_ids)):
        raise ValueError("Invalid passage support")
    supports = [catalog[pid] for pid in support_ids]
    for support in supports:
        related = (support.get("_view") is source["_view"] if "_view" in source
                   else support["evidence_id"] == source["evidence_id"])
        if not related:
            raise ValueError("Unrelated passage support")
    if source.get("context", {}).get("sentence_clipped"):
        cursor = source["_sentence_start"]
        required_end = source["_sentence_end"]
        view = source["_view"]
        for passage in sorted([source, *supports], key=lambda p: (p["_start"], p["_end"])):
            start, end = passage["_start"], passage["_end"]
            if end <= cursor:
                continue
            if start > cursor and view.text[cursor:start].strip():
                break
            cursor = max(cursor, end)
            if cursor >= required_end:
                break
        if cursor < required_end:
            raise ValueError("Incomplete sentence support")
    return [GroundedQuote(evidence_id=item["evidence_id"], quote=item["quote"]) for item in supports]


def _check_answer_state(parsed):
    """Contradictory abstentions are invalid output, not silently empty answers."""
    if parsed.status == "insufficient_evidence" and any(
        getattr(parsed, field, []) for field in ("claims", "summary", "sections")
    ):
        raise ValueError("Contradictory answer status")


def materialize_selections(parsed, catalog):
    _check_answer_state(parsed)
    # Historical replay fixtures may omit the new fields. New strict API output
    # includes them, and an answered response cannot silently omit its overview.
    structured_fields = {"summary", "sections"} & getattr(parsed, "model_fields_set", set())
    if parsed.status == "answered" and structured_fields and (
        not getattr(parsed, "summary", []) or not getattr(parsed, "sections", [])
    ):
        raise ValueError("Incomplete answer structure")
    claims = []
    for selection in parsed.claims:
        source = catalog.get(selection.passage_id)
        if not source:
            raise ValueError("Unverifiable citation")
        claims.append(GroundedClaim(
            text=selection.text, evidence_id=source["evidence_id"], quote=source["quote"],
            support_quotes=_selection_supports(selection, source, catalog),
        ))
    return ModelAnswer(
        status=parsed.status,
        claims=claims,
        summary=[CitedStatement.model_validate(s.model_dump()) for s in getattr(parsed, "summary", [])],
        sections=[AnswerSection.model_validate(s.model_dump()) for s in getattr(parsed, "sections", [])],
    )


class Rag:
    def __init__(self, db, settings, client=None):
        self.db = db
        self.settings = settings
        self.budget = Budget(db, settings)
        # Explicit official endpoint; no implicit user-defined proxy endpoint.
        self._client = client

    @property
    def client(self):
        if self._client is None:
            self._client = OpenAI(
                base_url="https://api.openai.com/v1", timeout=60, max_retries=0
            )
        return self._client

    def embed(self, texts, visitor="maintenance", cost_sink=None):
        model = self.settings.embedding_model
        if model != "text-embedding-3-small":
            raise ValueError("Unconfigured embedding price/dimension")
        result = {}
        missing = {digest(t): t for t in texts}
        with self.db.connect(vector=True) as conn:
            for h in list(missing):
                row = conn.execute(
                    "SELECT embedding FROM embeddings WHERE text_hash=%s AND model=%s",
                    (h, model),
                ).fetchone()
                if row:
                    result[h] = row["embedding"].to_list()
                    del missing[h]
        if missing:
            # UTF-8 byte count is a conservative tokenizer bound for ordinary text;
            # no automatic SDK retries can spend outside this reservation.
            estimate = price(
                model, sum(len(t.encode("utf-8")) for t in missing.values()) + 100
            )
            rid = self.budget.reserve(estimate, visitor, "embedding", model)
            try:
                response = self.client.embeddings.create(
                    model=model, input=list(missing.values()), dimensions=1536
                )
                usage = response.usage.model_dump()
                actual = price(model, response.usage.prompt_tokens)
                self.budget.settle(rid, actual, usage)
                if cost_sink is not None:
                    cost_sink.append(float(actual))
                ordered = sorted(response.data, key=lambda x: x.index)
                if len(ordered) != len(missing):
                    raise ValueError("Embedding count mismatch")
                with self.db.connect(vector=True) as conn:
                    for (h, _), item in zip(missing.items(), ordered):
                        if len(item.embedding) != 1536:
                            raise ValueError("Embedding dimension mismatch")
                        conn.execute(
                            "INSERT INTO embeddings(text_hash,model,embedding) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                            (h, model, np.array(item.embedding)),
                        )
                        result[h] = item.embedding
            except Exception as exc:
                self.budget.uncertain(rid, type(exc).__name__)
                if cost_sink is not None and not cost_sink:
                    cost_sink.append(self.budget.reservation_cost(rid))
                raise
        return [result[digest(t)] for t in texts]

    def index(self, batch_size=32, *, profile_id=None, visitor="maintenance"):
        """Fill the selected profile's cache; historical chunks stay untouched."""
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        rows = self.db.pending_embeddings(
            self.settings.embedding_model, profile_id=profile_id
        )
        processed = 0
        for start in range(0, len(rows), batch_size):
            group = rows[start : start + batch_size]
            self.embed([r["text"] for r in group], visitor=visitor)
            processed += len(group)
        return {
            "embedded_chunks": processed,
            "model": self.settings.embedding_model,
            "profile": profile_id or "active",
        }

    def generate(self, question, evidence, visitor, reservation=None, *, progress=None,
                 media_evidence=(), validate_media=None):
        try:
            media = [MediaAnswerEvidence.model_validate(item) for item in media_evidence]
        except Exception as exc:
            if reservation:
                self.budget.cancel_unsent(reservation)
            raise ValueError("Media evidence failed source validation") from exc
        if not evidence and not media:
            if reservation:
                self.budget.cancel_unsent(reservation)
            return Answer(
                status="insufficient_evidence",
                answer="The selected records do not provide sufficient evidence.",
            )
        try:
            if not all(self.db.validate_evidence(e) for e in evidence):
                raise ValueError("Evidence failed original-version validation")
        except Exception as exc:
            if reservation:
                self.budget.cancel_unsent(reservation)
            raise ValueError("Evidence failed original-version validation") from exc
        def media_current():
            if not media:
                return True
            try:
                return callable(validate_media) and validate_media(media) is True
            except Exception:
                return False

        if not media_current():
            if reservation:
                self.budget.cancel_unsent(reservation)
            raise ValueError("Media evidence failed source validation")
        try:
            catalog = answer_quote_catalog(evidence, media)
            inventory = evidence_inventory(evidence, media, catalog)
            source_refs = {eid: source["source_ref"] for source in inventory["sources"]
                           for eid in source["evidence_ids"]}
            output_schema = selection_schema(catalog)
            target = language_hint(question)
            system = SYSTEM
            if any(_observation_metadata(item) for item in evidence):
                system += (
                    "\nFor supplied social source observations, attribute claims to the specific observation. "
                    "Preserve source_conflicts and source_quality_codes. Extra captured text or a linked "
                    "preview is not an adjudicated original post. Do not resolve differing observations, "
                    "infer a complete thread, or treat company affiliation as paid advertising or verified classification."
                )
            if target["code"]:
                system += (
                    f"\nRequired answer language: {target['name']} ({target['code']}). "
                    "Write EVERY claim.text in this language. Keep proper names and "
                    "technical abbreviations where needed. The source language does "
                    "not change the required answer language."
                )
        except Exception:
            # Local preparation has not dispatched a generation request.
            if reservation:
                self.budget.cancel_unsent(reservation)
            raise
        task_contract = question_contract(question)
        retrieval_scope = {
            "coverage": "retrieved_subset",
            "complete_matching_list": False,
            "reviewed_content_membership": False,
        }
        payload = json.dumps(
            {
                "question": question,
                "task_contract": task_contract,
                "retrieval_scope": retrieval_scope,
                "evidence_inventory": inventory,
                "evidence": [
                    {
                        "id": e.evidence_id,
                        "source_ref": source_refs[e.evidence_id],
                        "record_id": getattr(e, "record_id", None),
                        "version_id": getattr(e, "version_id", None),
                        "start": getattr(e, "start", None),
                        "end": getattr(e, "end", None),
                        "title": e.title,
                        "dataset": e.dataset,
                        "publisher": e.publisher,
                        "sponsor": e.sponsor,
                        "url": getattr(e, "url", ""),
                        "archive_url": getattr(e, "archive_url", ""),
                        "published_at": (e.published_at.isoformat()
                                         if getattr(e, "published_at", None) is not None else None),
                        "source_text_quality": text_quality_context(getattr(e, "source_text_quality_codes", [])),
                        **_observation_context(e),
                        "quote_catalog": {
                            pid: passage["quote"]
                            for pid, passage in catalog.items()
                            if passage["evidence_id"] == e.evidence_id
                        },
                        "passage_context": {
                            pid: passage["context"] for pid, passage in catalog.items()
                            if passage["evidence_id"] == e.evidence_id
                        },
                    }
                    for e in evidence
                    if any(p["evidence_id"] == e.evidence_id for p in catalog.values())
                ],
                "media_evidence": [
                    {
                        "id": item.evidence_id, "title": item.title,
                        "source_ref": source_refs[item.evidence_id],
                        "record_id": item.record_id, "version_id": item.version_id,
                        "source_url": item.source_url,
                        "dataset": item.dataset, "media_type": item.media_type,
                        "origin": item.origin, "locator": item.locator.model_dump(mode="json"),
                        "quality_label": item.quality_label,
                        "quote_from_original_body": False,
                        "quote_catalog": {
                            pid: passage["quote"] for pid, passage in catalog.items()
                            if passage["evidence_id"] == item.evidence_id
                        },
                        "passage_context": {
                            pid: passage["context"] for pid, passage in catalog.items()
                            if passage["evidence_id"] == item.evidence_id
                        },
                    }
                    for item in media
                ],
            },
            ensure_ascii=False,
        )
        # Include schema and message overhead, then charge all input as uncached.
        upper_input = (
            len(
                (
                    system + payload + json.dumps(output_schema.model_json_schema())
                ).encode("utf-8")
            )
            + 2048
        )
        if upper_input > 100_000:
            if reservation:
                self.budget.cancel_unsent(reservation)
            raise ValueError("Evidence prompt exceeds app budget")
        estimate = price(
            self.settings.generation_model, upper_input, self.settings.max_output_tokens
        ) * Decimal("1.25")
        rid = reservation or self.budget.reserve(
            estimate, visitor, "generation", self.settings.generation_model
        )
        try:
            response = self.client.responses.parse(
                model=self.settings.generation_model,
                input=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": payload},
                ],
                text_format=output_schema,
                max_output_tokens=self.settings.max_output_tokens,
                reasoning={"effort": "none"},
                store=False,
            )
            usage = response.usage
            if usage is None:
                raise ValueError("Provider omitted usage")
            cached = getattr(usage.input_tokens_details, "cached_tokens", 0) or 0
            writes = getattr(usage.input_tokens_details, "cache_write_tokens", 0) or 0
            actual = price(
                self.settings.generation_model,
                usage.input_tokens,
                usage.output_tokens,
                cached,
                writes,
            )
            audit_usage = usage.model_dump()
            audit_usage["observatory_request"] = {
                "prompt_sha256": digest(system),
                "base_prompt_sha256": digest(SYSTEM),
                "prompt_policy": PROMPT_POLICY_VERSION,
                "task_contract": task_contract,
                "retrieval_scope": retrieval_scope,
                "evidence_inventory": inventory,
                "payload_sha256": digest(payload),
                "target_language": target,
                "language_policy": POLICY_VERSION,
                "schema_sha256": digest(
                    json.dumps(output_schema.model_json_schema(), sort_keys=True)
                ),
                "provider_model": getattr(response, "model", None),
                "media_source_bindings": [item.model_dump(mode="json") for item in media],
            }
            self.budget.settle(rid, actual, audit_usage)
            parsed = response.output_parsed
            with self.db.connect() as conn:
                conn.execute(
                    "INSERT INTO generation_outputs(reservation_id,evidence_ids,parsed,response_status) VALUES (%s,%s,%s,%s)",
                    (
                        rid,
                        Jsonb([e.evidence_id for e in [*evidence, *media]]),
                        Jsonb(parsed.model_dump()) if parsed else None,
                        response.status,
                    ),
                )
            # A completed paid request does not grant continued source validity.
            if not media_current():
                raise ValueError("Media evidence failed source validation")
            if response.status != "completed" or parsed is None:
                return Answer(
                    status="service_unavailable",
                    answer="The model did not finish an answer. Evidence search remains available.",
                    evidence=evidence,
                    media_evidence=media,
                    cost_usd=float(actual),
                )
            if progress:
                try:
                    progress("citations")
                except Exception:
                    # Progress is optional UI observation. A disconnected
                    # browser cannot turn a settled answer into an API failure.
                    pass
            return validate_answer(
                materialize_selections(parsed, catalog), evidence, float(actual),
                target=target, media_evidence=media,
            )
        except Exception as exc:
            self.budget.uncertain(rid, type(exc).__name__)
            raise


def validate_answer(parsed, evidence, cost=0.0, *, target=None, media_evidence=()):
    _check_answer_state(parsed)
    media = [MediaAnswerEvidence.model_validate(item) for item in media_evidence]
    by_id = {e.evidence_id: e for e in evidence}
    media_by_id = {e.evidence_id: e for e in media}
    if (len(by_id) != len(evidence) or len(media_by_id) != len(media)
            or by_id.keys() & media_by_id.keys()):
        raise ValueError("Duplicate answer evidence identity")
    if parsed.status == "insufficient_evidence":
        return Answer(
            status="insufficient_evidence",
            answer="The selected records do not provide sufficient evidence to answer this question.",
            evidence=evidence,
            media_evidence=media,
            cost_usd=cost,
        )
    if not 1 <= len(parsed.claims) <= 6:
        raise ValueError("Missing or excessive claims")
    summary = getattr(parsed, "summary", [])
    sections = getattr(parsed, "sections", [])
    if summary or sections:
        if not 1 <= len(summary) <= 3 or not 1 <= len(sections) <= 6:
            raise ValueError("Incomplete answer structure")
        valid_refs = set(range(1, len(parsed.claims) + 1))

        def checked_refs(item):
            refs = item.citation_indices
            if (
                not refs
                or any(type(ref) is not int or ref not in valid_refs for ref in refs)
                or len(refs) != len(set(refs))
            ):
                raise ValueError("Unverifiable answer-structure citation")
            return set(refs)

        summary_refs = set()
        for point in summary:
            if not point.text.strip() or len(point.text) > 1200:
                raise ValueError("Empty or excessive summary point")
            if re.search(r"\[\d+\]", point.text):
                raise ValueError("Citation numbers must use structured references")
            summary_refs.update(checked_refs(point))
        grouped_refs = []
        headings = set()
        for section in sections:
            title = section.title.strip()
            if not title or len(title) > 100 or title.casefold() in headings:
                raise ValueError("Invalid answer-section heading")
            headings.add(title.casefold())
            refs = checked_refs(section)
            if not refs & summary_refs:
                raise ValueError("Summary omits an answer section")
            grouped_refs.extend(section.citation_indices)
        if len(grouped_refs) != len(set(grouped_refs)) or set(grouped_refs) != valid_refs:
            raise ValueError("Answer sections must cover each claim exactly once")
    citations = []
    citation_numbers = {}
    sentences = []
    claim_refs = {}
    for i, c in enumerate(parsed.claims, 1):
        if not c.text.strip():
            raise ValueError("Empty claim")
        supports = getattr(c, "support_quotes", [])
        if not isinstance(supports, list) or len(supports) > MAX_SUPPORT_PASSAGES:
            raise ValueError("Invalid passage support")
        refs = []
        for quote in [c, *supports]:
            source = by_id.get(quote.evidence_id) or media_by_id.get(quote.evidence_id)
            source_text = (source.evidence_text if quote.evidence_id in media_by_id
                           else getattr(source, "text", ""))
            if source is None or not quote.quote.strip() or quote.quote not in source_text:
                raise ValueError("Unverifiable citation")
            if len(quote.quote.split()) > MAX_QUOTE_WORDS:
                raise ValueError("Citation exceeds short-quote limit")
            media_source = media_by_id.get(quote.evidence_id)
            citation = Citation(
                evidence_id=quote.evidence_id, quote=quote.quote,
                evidence_type=media_source.media_type if media_source else "article_text",
                origin=media_source.origin if media_source else "original_text",
                **(_observation_metadata(source) if not media_source else {}),
            )
            # Reuse only identical, validated evidence bindings. Matching words
            # in another evidence/version/observation remain separate sources.
            key = citation.model_dump_json()
            if key not in citation_numbers:
                citations.append(citation)
                citation_numbers[key] = len(citations)
            number = citation_numbers[key]
            if number not in refs:
                refs.append(number)
        claim_refs[i] = refs
        sentences.append(f"{c.text} " + " ".join(f"[{ref}]" for ref in refs))

    def citation_refs(item):
        return list(dict.fromkeys(ref for claim in item.citation_indices for ref in claim_refs[claim]))

    summary = [item.model_copy(update={"citation_indices": citation_refs(item)}) for item in summary]
    sections = [item.model_copy(update={"citation_indices": citation_refs(item)}) for item in sections]
    language_check = check_claim_languages(
        [c.text for c in parsed.claims] + [s.text for s in summary], target,
        source_titles=[e.title for e in [*evidence, *media]],
    )
    if language_check["status"] == "mismatch":
        return Answer(
            status="service_unavailable",
            answer="The generated answer used a different language from the question. Browse the original evidence below.",
            evidence=evidence,
            media_evidence=media,
            cost_usd=cost,
            failure_reason="answer_language_mismatch",
            language_check=language_check,
        )
    return Answer(
        status="answered",
        answer="\n\n".join(sentences),
        citations=citations,
        evidence=evidence,
        media_evidence=media,
        summary=summary,
        sections=sections,
        cited_claims=[
            CitedStatement(text=claim.text, citation_indices=claim_refs[i])
            for i, claim in enumerate(parsed.claims, 1)
        ],
        cost_usd=cost,
        language_check=language_check,
    )
