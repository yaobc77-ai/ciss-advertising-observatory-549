"""Read-only linkage of saved CLAIMS paragraphs to versioned original text.

Numeric upstream IDs are used only within the supplied input manifest. A text
match is a source-association candidate, not approval of identity or semantics.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import sqlite3
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from importlib.metadata import version as package_version
from pathlib import Path

from .chunking import retrieval_spans
from .claims_spans import ClaimSpan, ClaimSpanLocator, normalize_claim_text
from .claims_taxonomy import load_taxonomy_bundle


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class InputParagraph:
    source_id: str
    article_id: str
    text: str
    csv_row: int


@dataclass(frozen=True)
class SourceVersion:
    record_id: str
    version_id: str
    body: str
    body_hash: str
    url: str
    dataset: str
    current: bool
    active: bool
    retrievable: bool
    allowed_spans: tuple[tuple[int, int], ...]
    issue_codes: tuple[str, ...] = ()
    title: str = ""
    publisher: str = ""
    sponsor: str = ""

    def __post_init__(self):
        if text_hash(self.body) != self.body_hash:
            raise ValueError("Source body does not match its stored hash.")
        if self.dataset not in {"native", "social"}:
            raise ValueError("Unknown source dataset.")
        if self.allowed_spans:
            retrieval_spans(self.body, retrieval_ranges=list(self.allowed_spans))


def load_inputs(path: Path) -> list[InputParagraph]:
    return load_input_bytes(path.read_bytes())


def load_input_bytes(data: bytes) -> list[InputParagraph]:
    """Parse a frozen CSV byte snapshot so its text and digest cannot diverge."""
    with io.StringIO(data.decode("utf-8-sig"), newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != ["id", "article_id", "text"]:
            raise ValueError("Expected the supplied paragraph id/article_id/text CSV.")
        result = []
        for number, row in enumerate(reader, 2):
            if set(row) != {"id", "article_id", "text"} or any(not isinstance(v, str) for v in row.values()):
                raise ValueError("Input CSV row width does not match its declared fields.")
            result.append(InputParagraph(row["id"], row["article_id"], row["text"], number))
    if not result or any(not p.source_id or not p.article_id or not p.text.strip() for p in result):
        raise ValueError("Input paragraphs need nonempty IDs and text.")
    if len({p.source_id for p in result}) != len(result):
        raise ValueError("Duplicate paragraph IDs make the input join ambiguous.")
    return result


def validate_retained_inputs(paragraphs: list[InputParagraph], cleaned_path: Path) -> dict:
    """Check selected retained groups against their upstream cleaned manifest."""
    return validate_retained_input_bytes(paragraphs, cleaned_path.read_bytes())


def validate_retained_input_bytes(paragraphs: list[InputParagraph], data: bytes) -> dict:
    """Validate a frozen cleaned CSV without rereading a mutable source path."""
    with io.StringIO(data.decode("utf-8-sig"), newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != ["id", "text"]:
            raise ValueError("Expected the upstream cleaned article-id/text CSV.")
        cleaned = defaultdict(list)
        for row in reader:
            if set(row) != {"id", "text"} or any(not isinstance(v, str) for v in row.values()):
                raise ValueError("Cleaned CSV row width does not match its declared fields.")
            if not row["id"] or not row["text"].strip():
                raise ValueError("Cleaned input contains empty identifiers or text.")
            cleaned[row["id"]].append(row["text"])
    sampled = defaultdict(list)
    for paragraph in paragraphs:
        sampled[paragraph.article_id].append(paragraph.text)
    if any(texts != cleaned.get(article_id) for article_id, texts in sampled.items()):
        raise ValueError("Sampled paragraphs do not reproduce each retained article in original order.")
    return {"all_sampled_groups_match_cleaned_sequence": True,
            "sampled_article_groups": len(sampled), "cleaned_article_groups": len(cleaned),
            "cleaned_paragraphs": sum(map(len, cleaned.values())),
            "complete_original_article_coverage": "not_established"}


def load_saved_results(path: Path) -> list[dict]:
    """Open the supplied SQLite as read-only; no upstream code is imported."""
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        required = {"original_id", "text", "metadata_json", "raw_response",
                    "matched_categories", "new_categories", "updated_categories", "source_snippet"}
        columns = {r["name"] for r in connection.execute("PRAGMA table_info(post_analysis)")}
        if not required <= columns:
            raise ValueError("Saved output is missing the paragraph analysis contract.")
        return [dict(row) for row in connection.execute("SELECT * FROM post_analysis ORDER BY original_id")]


def load_source_versions(connection, dataset: str) -> list[SourceVersion]:
    rows = connection.execute(
        "SELECT r.record_id,r.current_version,r.active,r.dataset,v.version_id,v.body,v.body_hash,v.payload "
        "FROM records r JOIN record_versions v USING(record_id) WHERE r.dataset=%s "
        "ORDER BY r.record_id,v.version_id", (dataset,),
    ).fetchall()
    result = []
    for row in rows:
        payload = row["payload"]
        if payload.get("record_id") != row["record_id"] or payload.get("dataset") != row["dataset"]:
            raise ValueError("Source payload identity does not match its record.")
        spans = retrieval_spans(row["body"], retrieval_ranges=payload.get("retrieval_ranges"),
                                retrieval_end=payload.get("retrieval_end"))
        result.append(SourceVersion(
            row["record_id"], row["version_id"], row["body"], row["body_hash"],
            payload.get("url", ""), row["dataset"], row["current_version"] == row["version_id"],
            row["active"], payload.get("retrievable", False), tuple(spans),
            tuple(sorted(i["code"] for i in payload.get("issues", []))),
            payload.get("title", ""), payload.get("publisher", ""), payload.get("sponsor", ""),
        ))
    return result


def source_fingerprint(versions: list[SourceVersion]) -> str:
    manifest = [{k: v for k, v in asdict(item).items() if k != "body"}
                for item in sorted(versions, key=lambda v: (v.record_id, v.version_id))]
    return text_hash(json.dumps(manifest, sort_keys=True, separators=(",", ":")))


class LinkageIndex:
    """Search once per original body; keep all current and historical versions."""

    def __init__(self, versions: list[SourceVersion], *, projection="strict"):
        if projection not in {"strict", "upstream-ascii-v1"}:
            raise ValueError("Unknown CLAIMS source projection.")
        if len({v.version_id for v in versions}) != len(versions):
            raise ValueError("Duplicate source version IDs.")
        if len({v.dataset for v in versions}) > 1:
            raise ValueError("A linkage audit must use one explicit dataset.")
        self.versions = {v.version_id: v for v in versions}
        self.by_hash = defaultdict(list)
        for version in versions:
            self.by_hash[version.body_hash].append(version)
        self.locators = {key: ClaimSpanLocator(items[0].body) for key, items in self.by_hash.items()}
        self.projected_locators = {}
        if projection == "upstream-ascii-v1":
            from .claims_projection import UpstreamAsciiProjectionLocator

            self.projected_locators = {key: UpstreamAsciiProjectionLocator(items[0].body)
                                      for key, items in self.by_hash.items()}
        self.cache = {}

    def locate(self, text: str) -> dict[str, list[ClaimSpan]]:
        if text not in self.cache:
            matches = {}
            for body_hash, locator in self.locators.items():
                spans = locator.locate(text)
                if body_hash in self.projected_locators:
                    canonical = normalize_claim_text(text)
                    additional = self.projected_locators[body_hash].locate(canonical) if canonical.isascii() else []
                    existing = {(span.start, span.end) for span in spans}
                    spans += [span for span in additional if (span.start, span.end) not in existing]
                    spans.sort(key=lambda span: (span.start, span.end))
                if spans:
                    for version in self.by_hash[body_hash]:
                        matches[version.version_id] = spans
            self.cache[text] = matches
        return self.cache[text]

    def audit(self, paragraphs: list[InputParagraph], saved: list[dict]) -> tuple[list[dict], list[dict]]:
        inputs = {p.source_id: p for p in paragraphs}
        if len(inputs) != len(paragraphs):
            raise ValueError("Duplicate input paragraph IDs.")
        if len({r["original_id"] for r in saved}) != len(saved):
            raise ValueError("Duplicate saved result IDs.")
        groups = defaultdict(list)
        for paragraph in paragraphs:
            groups[paragraph.article_id].append(paragraph)
        article_rows = []
        group_candidates = {}
        for article_id, items in groups.items():
            locations = [self.locate(p.text) for p in items]
            matches = [set(found) for found in locations]
            shared = set.intersection(*matches) if matches else set()
            shared = {version for version in shared if _ordered_occurrences(
                [found[version] for found in locations]
            )}
            group_candidates[article_id] = shared
            coverage = Counter(version for candidates in matches for version in candidates)
            current = sorted(v for v in shared if self.versions[v].current and self.versions[v].active)
            article_rows.append({
                "upstream_article_id": article_id, "supplied_paragraphs": len(items),
                "located_paragraphs": sum(bool(m) for m in matches),
                "all_retained_paragraph_version_candidates": sorted(shared),
                "current_version_candidates": current,
                "current_record_candidates": sorted({self.versions[v].record_id for v in current}),
                "best_partial_coverage": max(coverage.values(), default=0),
                "input_is_complete_original_article": False,
                "retained_paragraph_order_checked": True,
                "association_is_approved": False,
            })
        rows = []
        for result in saved:
            paragraph = inputs.get(result["original_id"])
            metadata = _json_object(result["metadata_json"])
            article_id = metadata.get("article_id")
            input_valid = bool(paragraph and result["text"] == paragraph.text
                               and type(article_id) in {str, int} and str(article_id) == paragraph.article_id)
            if not input_valid:
                rows.append({"source_result_id": result["original_id"], "state": "input_mismatch",
                             "candidate_version_ids": [], "candidates": [], "input_join_valid": False})
                continue
            matches = self.locate(paragraph.text)
            shared = group_candidates[paragraph.article_id]
            candidates = []
            for version_id, spans in sorted(matches.items()):
                version = self.versions[version_id]
                for span in spans:
                    allowed = version.retrievable and any(
                        start <= span.start < span.end <= end for start, end in version.allowed_spans
                    )
                    candidates.append({
                        "record_id": version.record_id, "version_id": version_id,
                        "body_hash": version.body_hash, "dataset": version.dataset, "url": version.url,
                        "current": version.current, "active": version.active,
                        "matches_all_retained_article_paragraphs": version_id in shared,
                        "inside_retrieval_scope": bool(allowed), "source_issues": list(version.issue_codes),
                        **asdict(span),
                    })
            eligible = [c for c in candidates if c["current"] and c["active"]
                        and c["matches_all_retained_article_paragraphs"]]
            if not candidates:
                state = "not_located"
            elif len(eligible) == 1:
                state = "unique_current_text_candidate" if eligible[0]["inside_retrieval_scope"] else "excluded_or_unretrievable"
            elif len(eligible) > 1:
                state = "ambiguous_current"
            elif shared:
                state = "historical_only_or_inactive"
            else:
                state = "partial_article_candidate"
            rows.append({
                "source_result_id": result["original_id"], "upstream_article_id": paragraph.article_id,
                "input_csv_row": paragraph.csv_row, "input_join_valid": True,
                "input_sha256": text_hash(paragraph.text), "state": state,
                "candidate_version_ids": sorted(matches), "candidates": candidates,
                "association_is_approved": False,
                "review_state": "automatic_unverified",
            })
        return rows, article_rows


def _json_object(value: str) -> dict:
    def unique_keys(pairs):
        parsed = {}
        for key, item in pairs:
            if key in parsed:
                raise ValueError("Duplicate metadata key.")
            parsed[key] = item
        return parsed

    def finite_json(value):
        raise ValueError("Invalid JSON constant.")

    try:
        parsed = json.loads(value, object_pairs_hook=unique_keys, parse_constant=finite_json)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _ordered_occurrences(paragraph_spans: list[list[ClaimSpan]]) -> bool:
    """Test existence of an ordered, nonoverlapping retained paragraph sequence."""
    previous_end = 0
    for spans in paragraph_spans:
        candidates = [span for span in spans if span.start >= previous_end]
        if not candidates:
            return False
        previous_end = min(span.end for span in candidates)
    return True


def attach_claim_evidence(index: LinkageIndex, source_rows: list[dict], saved: list[dict],
                          paragraphs: list[InputParagraph], taxonomy, *, run_id: str) -> tuple[list[dict], dict]:
    """Pair each raw action/category with its own snippet, never the flat list."""
    from .claims_results import parse_saved_result

    source_lookup = {row["source_result_id"]: row for row in source_rows}
    inputs = {p.source_id: p for p in paragraphs}
    candidates = []
    quote_states = Counter()
    decoder_states = Counter()
    decoder_issues = Counter()
    for row in saved:
        decoded = parse_saved_result(row, taxonomy).to_dict()
        decoded["run_id"] = run_id
        decoded["upstream_article_id"] = inputs[row["original_id"]].article_id if row["original_id"] in inputs else None
        decoded["prompt_identifier"] = None
        decoded["processing_code_revision"] = None
        decoder_states[decoded["report_state"]] += 1
        decoder_issues.update(issue["code"] for issue in decoded["errors"])
        linkage = source_lookup[row["original_id"]]
        for claim in decoded["claims"]:
            snippet = claim["source_snippet"]
            paragraph = inputs.get(row["original_id"])
            input_locations = ClaimSpanLocator(paragraph.text).locate(snippet) if (
                paragraph and linkage["input_join_valid"] and isinstance(snippet, str) and snippet.strip()
            ) else []
            source_locations = []
            if input_locations:
                hits = index.locate(snippet)
                seen = set()
                for parent in linkage["candidates"]:
                    for span in hits.get(parent["version_id"], []):
                        key = (parent["version_id"], span.start, span.end)
                        if not parent["start"] <= span.start < span.end <= parent["end"] or key in seen:
                            continue
                        seen.add(key)
                        version = index.versions[parent["version_id"]]
                        allowed = version.retrievable and any(
                            start <= span.start < span.end <= end for start, end in version.allowed_spans
                        )
                        evidence = {
                            "record_id": version.record_id, "version_id": version.version_id,
                            "body_hash": version.body_hash, "dataset": version.dataset, "url": version.url,
                            "title": version.title, "publisher": version.publisher, "sponsor": version.sponsor,
                            "current": version.current, "active": version.active,
                            "matches_all_retained_article_paragraphs": parent["matches_all_retained_article_paragraphs"],
                            "inside_retrieval_scope": bool(allowed), **asdict(span),
                        }
                        evidence["candidate_key"] = text_hash(json.dumps({
                            "upstream_result_id": row["original_id"], "raw_response_sha256": decoded["raw_response_sha256"],
                            "run_id": run_id,
                            "response_pointer": claim["raw_json_pointer"], "nc_id": claim["nc_id"],
                            "taxonomy": taxonomy.bundle_fingerprint,
                            "version_id": version.version_id, "start": span.start, "end": span.end,
                        }, sort_keys=True))
                        source_locations.append(evidence)
            eligible = [e for e in source_locations if e["current"] and e["active"]
                        and e["matches_all_retained_article_paragraphs"] and e["inside_retrieval_scope"]]
            if not input_locations:
                quote_state = "snippet_not_in_input"
            elif len(input_locations) > 1:
                quote_state = "ambiguous_input_snippet"
            elif len(eligible) == 1:
                quote_state = "unique_current_quote_candidate"
            elif len(eligible) > 1:
                quote_state = "ambiguous_current_quote"
            elif source_locations:
                partial = [e for e in source_locations if e["current"] and e["active"] and e["inside_retrieval_scope"]]
                if len(partial) == 1:
                    quote_state = "unique_current_partial_article_quote"
                elif len(partial) > 1:
                    quote_state = "ambiguous_current_partial_article_quote"
                elif any(e["current"] and e["active"] for e in source_locations):
                    quote_state = "excluded_current_quote"
                else:
                    quote_state = "historical_or_inactive_quote"
            else:
                quote_state = "snippet_not_in_original"
            claim["input_snippet_locations"] = [asdict(span) for span in input_locations]
            claim["source_evidence_candidates"] = source_locations
            claim["quote_state"] = quote_state
            claim["association_is_approved"] = False
            claim["review_state"] = "automatic_unverified"
            quote_states[quote_state] += 1
        decoded["source_linkage_state"] = linkage["state"]
        candidates.append(decoded)
    return candidates, {
        "decoded_row_states": dict(sorted(decoder_states.items())),
        "decoded_issue_counts": dict(sorted(decoder_issues.items())),
        "category_candidates": sum(len(row["claims"]) for row in candidates),
        "quote_states": dict(sorted(quote_states.items())),
        "definition_drift_candidates": sum(bool(claim["definition_drift"]) for row in candidates for claim in row["claims"]),
        "lossy_current_quote_candidates": sum(bool(e.get("lossy")) for row in candidates
                                               for claim in row["claims"] for e in claim["source_evidence_candidates"]
                                               if e["current"] and e["active"]),
        "results_published": 0, "semantic_support": "not_evaluated",
    }


def run_linkage_audit(db, bundle_directory: Path, output_directory: Path, *, dataset="native",
                      projection="strict") -> dict:
    """Write a new private audit folder; neither source database can be changed."""
    if dataset not in {"native", "social"}:
        raise ValueError("Choose one explicit dataset for source linkage.")
    if output_directory.exists():
        raise ValueError("Choose a new output directory; prior audits are not overwritten.")
    bundle = load_taxonomy_bundle(bundle_directory)
    paths = [bundle_directory / name for name in (
        "sampled_50_articles_paragraphs.csv", "native_ads_paragraphs_cleaned.csv",
        "greenwashing_discourse_analysis.db",
    )]
    hashes_before = {p.name: file_hash(p) for p in paths}
    hashes_before.update(bundle.file_hashes)
    if projection == "upstream-ascii-v1":
        from .claims_projection import UPSTREAM_NOTEBOOK_SHA256

        notebook = bundle_directory.parent / "notebooks/clean_paragraph_data.ipynb"
        if file_hash(notebook) != UPSTREAM_NOTEBOOK_SHA256:
            raise ValueError("Upstream notebook does not match the implemented projection contract.")
        paths.append(notebook)
        hashes_before[notebook.name] = UPSTREAM_NOTEBOOK_SHA256
    paragraphs = load_inputs(paths[0])
    retained_input_check = validate_retained_inputs(paragraphs, paths[1])
    saved = load_saved_results(paths[2])
    with db.connect() as connection:
        connection.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
        if connection.execute("SHOW transaction_read_only").fetchone()["transaction_read_only"] != "on":
            raise RuntimeError("Source snapshot must be read-only.")
        versions = load_source_versions(connection, dataset)
        if not versions:
            raise ValueError("The selected source dataset has no article versions to audit.")
        index = LinkageIndex(versions, projection=projection)
        result_rows, article_rows = index.audit(paragraphs, saved)
        claim_candidates, claim_summary = attach_claim_evidence(
            index, result_rows, saved, paragraphs, bundle,
            run_id="saved-claims2:" + hashes_before[paths[2].name],
        )
        from .indexing import profile_snapshot

        snapshot = profile_snapshot(connection)
    with db.connect() as connection:
        connection.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
        source_unchanged = source_fingerprint(versions) == source_fingerprint(load_source_versions(connection, dataset))
    all_paths = paths + [bundle_directory / name for name in bundle.file_hashes]
    if not source_unchanged or hashes_before != {p.name: file_hash(p) for p in all_paths}:
        raise RuntimeError("Source changed during the audit; do not publish these candidates.")
    current = [v for v in versions if v.current and v.active]
    saved_ids = {r["original_id"] for r in saved}
    report = {
        "checked_at_utc": datetime.now(timezone.utc).isoformat(),
        "scope": "read_only_saved_claims_paragraph_source_linkage",
        "checker_package_version": package_version("ciss-observatory"),
        "checker_source_sha256_lf": {
            name: hashlib.sha256((Path(__file__).parent / name).read_bytes().replace(b"\r\n", b"\n")).hexdigest()
            for name in ("claims_linkage.py", "claims_projection.py", "claims_results.py", "claims_spans.py", "claims_taxonomy.py")
        },
        "dataset": dataset, "source_projection": projection,
        "taxonomy": bundle.to_manifest(), "input_file_sha256": hashes_before,
        "upstream_run_id": "saved-claims2:" + hashes_before[paths[2].name],
        "input_paragraphs": len(paragraphs), "saved_results": len(saved), "upstream_articles": len(article_rows),
        "retained_input_check": retained_input_check,
        "missing_saved_result_ids": [p.source_id for p in paragraphs if p.source_id not in saved_ids],
        "source_versions": len(versions), "current_active_records": len(current),
        "source_snapshot_sha256": source_fingerprint(versions), "retrieval_snapshot": snapshot,
        "input_join_valid": sum(r["input_join_valid"] for r in result_rows),
        "linkage_states": dict(sorted(Counter(r["state"] for r in result_rows).items())),
        "claim_candidate_validation": claim_summary,
        "articles_with_unique_current_text_candidate": sum(len(a["current_record_candidates"]) == 1 for a in article_rows),
        "source_unchanged_after_audit": source_unchanged, "model_calls": 0, "database_writes": 0,
        "results_published": 0, "candidate_associations_approved": 0,
        "limits": [
            "A unique text association is not customer approval, semantic support or a greenwashing finding.",
            "All retained cleaned paragraphs are checked; upstream filtering means they are not the complete original article.",
            "Strict matching accepts exact or whitespace variants. The optional upstream ASCII projection preserves original offsets and marks lossy candidates for review.",
            "Missing saved results are unprocessed, not negative. Historical, ambiguous and excluded evidence remains unresolved.",
            "Taxonomy and run are candidate versions; authoritative owner and review remain TODO.",
            "Saved model/time metadata is preserved. A processing code revision or prompt ID absent from the saved run is not invented.",
        ],
    }
    output_directory.mkdir(parents=True, exist_ok=False)
    if projection != "strict":
        from .claims_projection import (
            NORMALIZATION_CONTRACT_SHA256,
            projection_contract,
        )

        report["normalization_contract"] = projection_contract()
        report["normalization_fingerprint"] = NORMALIZATION_CONTRACT_SHA256
    for name, content in (("claims_integration_dry_run.json", report),
                          ("selected_bundle_manifest.json", bundle.to_manifest()),
                          ("claims_source_linkage.json", result_rows),
                          ("claims_article_linkage.json", article_rows),
                          ("claims_result_candidates.json", claim_candidates)):
        (output_directory / name).write_text(json.dumps(content, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    _write_candidate_csv(output_directory / "claims_source_linkage_candidates.csv", result_rows)
    _write_evidence_review_csv(output_directory / "claims_evidence_review.csv", claim_candidates)
    artifact_hashes = {path.name: file_hash(path) for path in sorted(output_directory.iterdir())}
    (output_directory / "audit_output_manifest.json").write_text(json.dumps({
        "schema_version": 1, "artifacts": artifact_hashes,
        "complete": True, "labels_published": False,
    }, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return report


def _write_candidate_csv(path, rows):
    fields = ["source_result_id", "upstream_article_id", "state", "input_join_valid", "record_id",
              "version_id", "body_hash", "current", "active", "matches_all_retained_article_paragraphs",
              "inside_retrieval_scope", "start", "end", "match_method", "quote"]
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            for candidate in row["candidates"] or [{}]:
                writer.writerow({**{k: v for k, v in row.items() if k != "candidates"}, **candidate})


def _write_evidence_review_csv(path, rows):
    fields = ["original_id", "upstream_article_id", "run_id", "recorded_model", "timestamp", "report_state", "source_linkage_state",
              "response_index", "action_type", "nc_id", "sc_id", "mapping_state", "definition_drift",
              "current_candidate_definition", "historical_inline_definition", "source_snippet", "quote_state",
              "record_id", "version_id", "body_hash", "title", "publisher", "sponsor", "url", "current", "active",
              "matches_all_retained_article_paragraphs", "inside_retrieval_scope", "start", "end", "quote",
              "match_method", "lossy", "raw_json_pointer", "candidate_key", "issue_codes",
              "source_identity_review", "semantic_support_review", "reviewer", "reviewed_at"]
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            for claim in row["claims"] or [{}]:
                evidence = claim.get("source_evidence_candidates", [])
                current = [e for e in evidence if e["current"] and e["active"]]
                issues = sorted({i["code"] for i in (*row["errors"], *claim.get("errors", ()))})
                for candidate in current or evidence or [{}]:
                    writer.writerow({**row, **claim, **candidate,
                                     "report_state": row["report_state"], "issue_codes": "|".join(issues),
                                     "source_identity_review": "pending", "semantic_support_review": "pending",
                                     "reviewer": "", "reviewed_at": ""})
