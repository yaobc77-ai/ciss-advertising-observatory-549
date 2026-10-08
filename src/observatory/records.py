"""Public record details and reviewed local snapshots, without changing source data.

The historical PDF index is a candidate index, not an attachment registry. Only
the reviewed body-recovery manifest, or an explicitly mounted curated bundle,
binds a local PDF to a record here. Snapshots never fill ``archive_url`` or imply
that an online archive exists.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from io import BytesIO
from pathlib import Path
from urllib.parse import urlsplit

from flask import abort, jsonify, request, send_file

from observatory.asset_bundle import PDF_MAGIC, PNG_MAGIC, load_bundle, verified_bytes
from observatory.social_annotations import social_annotation_details

_LOGGER = logging.getLogger(__name__)

_PUBLIC_FIELDS = (
    "record_id", "version_id", "dataset", "title", "publisher", "sponsor",
    "date", "keyword", "platform", "account", "retrievable",
)
_QUALITY_NOTES = {
    "body_missing": "No article text was captured.",
    "body_numeric": "The stored source contains a numeric placeholder.",
    "body_wrong_type": "The source body needs format review.",
    "body_video_placeholder": "Only a video placeholder was captured; no transcript is available.",
    "body_truncated_suspected": "The source text may be truncated; it cannot establish what is absent from the complete article.",
    "body_garbled": "Some characters may be garbled and need source review.",
    "body_short": "The captured text is short and may be a caption or incomplete article.",
    "body_question_only": "The captured text may be a title rather than article text.",
    "body_footer_only": "The captured text may contain mostly footer or disclosure material.",
    "body_related_navigation": "Related links or navigation remain in the stored text and are excluded from retrieval.",
    "body_navigation_intervals_reviewed": "Only reviewed text intervals are used for retrieval; the original extraction is shown unchanged below.",
    "body_partial_recovery": "The PDF provides a partial text recovery. Some page-edge text and image content are missing from retrieval.",
    "body_source_partial": "The saved text is a partial capture of the original source. Missing content has not been reconstructed.",
    "body_source_completeness_unestablished": "The saved text has not been verified as a complete original source. Missing text, images or video may affect content findings.",
    "body_saved_text_reviewed": "This text was adopted from a supplied TXT under a recorded engineering review. Quotations refer to that saved text; they do not verify the original webpage, OCR accuracy, video or advertiser claims.",
    "historical_annotations_prior_body": "Historical labels describe an earlier body version and do not label the recovered text.",
    "duplicate_body": "Another record has identical captured text; record counts are not unique text counts.",
}


def _safe_url(value):
    if not isinstance(value, str) or any(c.isspace() for c in value):
        return ""
    try:
        parsed = urlsplit(value)
        return value if (
            parsed.scheme in {"http", "https"} and parsed.hostname
            and not parsed.username and not parsed.password
        ) else ""
    except ValueError:
        return ""


def _json(path, fallback):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return fallback


class RecordDetails:
    """Read-only details backed by a private project root or curated asset root.

    ``get`` and ``summaries`` return only public fields. ``attachment`` returns
    hash-checked bytes, never a user-controlled path. A missing or changed source
    file disables the link rather than silently serving another file.
    """

    def __init__(self, db, settings, root=None):
        self.db = db
        self.settings = settings
        configured_root = getattr(settings, "record_asset_root", "")
        self._bundled = bool(configured_root)
        self.root = Path(root or configured_root or Path.cwd()).resolve()
        self._pdf_root = (self.root / ("pdfs" if self._bundled else "sources/pdf_archive_20260915/pdfs")).resolve()
        self._preview_root = (self.root / ("previews" if self._bundled else "sources/recovered_native/previews")).resolve()
        self.asset_status = "local_workspace"
        if self._bundled:
            try:
                decisions = load_bundle(self.root, getattr(settings, "record_asset_manifest_sha256", ""))
                self.asset_status = "bundle_verified"
            except (OSError, ValueError):
                decisions = []
                self.asset_status = "bundle_unavailable"
                _LOGGER.warning("Configured record-asset bundle failed validation; attachments are disabled")
        else:
            manifest = _json(self.root / "config/native_body_recoveries.json", {})
            decisions = manifest.get("decisions", []) if isinstance(manifest, dict) else []
        self._reviewed = {}
        for item in decisions if isinstance(decisions, list) else []:
            if not isinstance(item, dict):
                continue
            required = ("record_id", "url", "source_pdf_path", "source_pdf_sha256",
                        "extracted_text_sha256", "identity_basis")
            if not all(isinstance(item.get(k), str) and item[k] for k in required):
                continue
            if not all(re.fullmatch(r"[0-9a-f]{64}", item[k]) for k in (
                "source_pdf_sha256", "extracted_text_sha256",
            )):
                continue
            asset_id = hashlib.sha256(
                f"{item['record_id']}:{item['source_pdf_sha256']}".encode()
            ).hexdigest()[:32]
            self._reviewed.setdefault(item["record_id"], []).append(
                {**item, "asset_id": asset_id}
            )
        self._candidates = {}
        index = [] if self._bundled else _json(self.root / "analysis/pdf_archive/source_index.json", [])
        for item in index if isinstance(index, list) else []:
            if not isinstance(item, dict) or not isinstance(item.get("source_urls"), list):
                continue
            for url in item["source_urls"]:
                if not isinstance(url, str):
                    continue
                self._candidates.setdefault(url, set()).add(item.get("id", ""))

    def _row(self, record_id):
        # No raw fields, disclosure strings, local provenance paths, or issue
        # details are projected into the public model.
        with self.db.connect() as conn:
            return conn.execute(
                """SELECT r.record_id,r.dataset,v.version_id,v.body,v.body_hash,
                v.payload->>'title' AS title,v.payload->>'publisher' AS publisher,
                v.payload->>'sponsor' AS sponsor,v.payload->>'published_at' AS date,
                v.payload->>'keyword' AS keyword,v.payload->>'platform' AS platform,
                v.payload->>'account' AS account,v.payload->>'url' AS url,
                v.payload->>'archive_url' AS archive_url,
                (v.payload->>'retrievable')::boolean AS retrievable,
                v.payload->'issues' AS issues,v.payload->'retrieval_ranges' AS retrieval_ranges,
                v.payload->'retrieval_end' AS retrieval_end,
                CASE WHEN r.dataset='social' THEN v.payload END AS social_source_payload,
                CASE WHEN r.dataset='social' THEN jsonb_build_object(
                    'social_admission',v.payload#>'{raw,social_admission}',
                    'source_variants',v.payload#>'{raw,source_variants}') END AS social_admission_payload,
                COALESCE((SELECT jsonb_agg(jsonb_build_object('ordinal',a.ordinal,
                    'payload',a.payload) ORDER BY a.ordinal)
                    FROM annotations a WHERE a.version_id=v.version_id),'[]'::jsonb) AS annotations
                FROM records r JOIN record_versions v ON v.version_id=r.current_version
                WHERE r.record_id=%s AND r.active AND (v.payload->>'countable')::boolean""",
                (record_id,),
            ).fetchone()

    def _bytes(self, item):
        # Check both containment in the project and in the one approved archive
        # root. resolve() also prevents symlink/junction traversal out of it.
        try:
            if self._bundled:
                return verified_bytes(self.root, item["source_pdf_path"], item["source_pdf_sha256"], PDF_MAGIC, "pdfs")
            if Path(item["source_pdf_path"]).is_absolute():
                return None
            path = (self.root / item["source_pdf_path"]).resolve(strict=True)
            if not path.is_relative_to(self.root) or not path.is_relative_to(self._pdf_root):
                return None
            if path.suffix.lower() != ".pdf" or not path.is_file():
                return None
            data = path.read_bytes()
            if not data.startswith(b"%PDF-"):
                return None
            return data if hashlib.sha256(data).hexdigest() == item["source_pdf_sha256"] else None
        except (OSError, ValueError):
            return None

    def _matched(self, row):
        for item in self._reviewed.get(row["record_id"], []):
            if item["url"] == row.get("url") and item["extracted_text_sha256"] == row.get("body_hash"):
                yield item

    def _preview_bytes(self, item):
        """Use a hash-bound maintenance artifact; never render in a web request."""
        pdf_hash = item["source_pdf_sha256"]
        try:
            if self._bundled:
                if not item.get("preview_path"):
                    return None
                return verified_bytes(self.root, item["preview_path"], item["preview_sha256"], PNG_MAGIC, "previews")
            image_path = (self._preview_root / f"{pdf_hash}.page1.png").resolve(strict=True)
            receipt_path = (self._preview_root / f"{pdf_hash}.page1.json").resolve(strict=True)
            if any(not path.is_relative_to(self.root) or not path.is_relative_to(self._preview_root)
                   for path in (image_path, receipt_path)):
                return None
            receipt = _json(receipt_path, {})
            if not isinstance(receipt, dict) or any((
                receipt.get("schema_version") != 1,
                receipt.get("source_pdf_sha256") != pdf_hash,
                receipt.get("page") != 1,
            )):
                return None
            image = image_path.read_bytes()
            if not image.startswith(b"\x89PNG\r\n\x1a\n"):
                return None
            return image if hashlib.sha256(image).hexdigest() == receipt.get("image_sha256") else None
        except (OSError, ValueError):
            return None

    def _attachments(self, row):
        if not self.settings.show_source_links:
            return []
        result = []
        for item in self._matched(row):
            if self._bytes(item) is None:
                continue
            url = f"/records/{row['record_id']}/attachments/{item['asset_id']}"
            result.append({
                "asset_id": item["asset_id"], "kind": "pdf",
                "label": "Reviewed local PDF snapshot", "url": url,
                "download_url": url + "?download=1", "sha256": item["source_pdf_sha256"],
                "pages": len(item.get("pages", [])),
                "preview_url": url + "/preview.png" if self._preview_bytes(item) else None,
                "preview_page": 1,
                "preview_label": "Page 1 of the reviewed local PDF",
                "identity_status": "reviewed_local_capture",
                "identity_note": "Local capture metadata and article text were reviewed together. This does not verify the current online page or the advertiser's claims.",
                "limitations": [str(note) for note in item.get("limitations", [])],
            })
        return result

    def get(self, record_id):
        """Return current countable record metadata and unchanged stored body."""
        row = self._row(record_id)
        if row is None:
            return None
        body = row.get("body") or ""
        codes = {i.get("code") for i in (row.get("issues") or []) if isinstance(i, dict)}
        notes = [_QUALITY_NOTES[code] for code in sorted(codes - {None}) if code in _QUALITY_NOTES]
        partial = bool(codes & {"body_truncated_suspected", "body_partial_recovery", "body_source_partial", "body_short"})
        attachments = self._attachments(row)
        candidate_count = len(self._candidates.get(row.get("url"), set()))
        original = _safe_url(row.get("url")) if self.settings.show_source_links else ""
        archive = _safe_url(row.get("archive_url")) if self.settings.show_source_links else ""
        result = {
            **{key: row.get(key) for key in _PUBLIC_FIELDS},
            "body": body, "body_hash": row.get("body_hash"),
            "body_characters": len(body), "body_label": "Stored source text",
            "body_status": "missing" if not body.strip() else ("partial" if partial else "captured_text"),
            "body_note": "This is the unchanged text captured in the current source version. It may contain navigation or extraction artifacts and is not a guarantee of a complete article.",
            "quality_notes": notes, "retrieval_ranges": row.get("retrieval_ranges"),
            "retrieval_end": row.get("retrieval_end"),
            "url": original, "archive_url": archive,
            "attachments": attachments, "candidate_snapshot_count": candidate_count,
            "archive_status": "Online archive available" if archive else (
                "Local PDF snapshot available" if attachments else "No verified archived copy linked"
            ),
            "archive_note": "Local PDFs and public online archives are separate. Candidate files matched by title remain unverified and are not linked as this record's source.",
            "record_asset_status": self.asset_status,
            "detail_url": f"/records/{row['record_id']}",
        }
        if row.get("dataset") == "social":
            from .social_admission import public_social_admission

            result["body_note"] = ("This is the unchanged post text captured in the current source version. "
                                   "It does not establish a complete post or full coverage of media and referenced posts.")
            result["social_historical_annotation"] = social_annotation_details(row)
            admission = public_social_admission(row.get("social_admission_payload"),
                                                 current_body=body, current_url=row.get("url"))
            if admission:
                if not self.settings.show_source_links:
                    for variant in admission["variants"]:
                        variant["url"] = variant["archive_url"] = ""
                result["social_admission"] = admission
                if "body" in admission["conflicting_fields"]:
                    result["body_label"] = "One supplied source observation — conflicting text"
                    result["body_note"] = ("Source observations of this original post contain different text. "
                                           "The text below is one preserved observation, selected by source ID for display only. "
                                           "It is not an adjudicated post text and is paused for RAG retrieval. Read all variants below.")
                if "sponsor" in admission["conflicting_fields"] or "account" in admission["conflicting_fields"]:
                    result["quality_notes"].append("Source observations disagree on company or account. Affected fields are displayed as unknown; the source variants below retain each observation.")
            from .social_source_binding import public_observations
            from .social_source_retrieval import source_version_id

            payload = row.get("social_source_payload")
            if (isinstance(payload, dict) and row.get("retrievable") is True
                    and source_version_id(payload) == row.get("version_id")):
                observations = public_observations(payload)
                result["source_observation_retrieval"] = {
                    "status": "enabled_source_observations", "observations": len(observations),
                    "semantic_completeness_verified": False,
                }
                if admission and "body" in admission["conflicting_fields"]:
                    result["body_note"] = ("Each preserved source observation is searchable separately. "
                                           "The text below is one observation selected for display only. "
                                           "Different text versions have not been adjudicated; extra text may include linked previews. "
                                           "Quotes identify their source observation. Read all variants below.")
                result["quality_notes"].append("Searchable saved observations do not establish a complete original post, image/video contents, or verified paid advertising.")
        return result

    def summaries(self, rows):
        """Small browse-table projection; only reviewed record IDs need a DB read."""
        result = {}
        for row in rows:
            archive = _safe_url(row.get("archive_url")) if self.settings.show_source_links else ""
            attachments = []
            if row["record_id"] in self._reviewed and self.settings.show_source_links:
                current = self._row(row["record_id"])
                if current and current["version_id"] == row.get("version_id"):
                    attachments = self._attachments(current)
            result[row["record_id"]] = {
                "detail_url": f"/records/{row['record_id']}",
                "snapshot_count": len(attachments),
                "snapshot_url": attachments[0]["url"] if attachments else "",
                "archive_status": "Online archive available" if archive else (
                    "Local PDF snapshot available" if attachments else "No verified archived copy linked"
                ),
                "candidate_snapshot_count": len(self._candidates.get(row.get("url"), set())),
            }
        return result

    def coverage(self, rows):
        summaries = self.summaries(rows)
        return {
            "records": len(rows),
            "records_with_public_archive": sum(bool(_safe_url(row.get("archive_url"))) for row in rows),
            "records_with_reviewed_local_snapshot": sum(v["snapshot_count"] > 0 for v in summaries.values()),
            "reviewed_local_snapshots": sum(v["snapshot_count"] for v in summaries.values()),
            "records_with_unverified_candidates": sum(v["candidate_snapshot_count"] > 0 and not v["snapshot_count"] for v in summaries.values()),
            "note": "Candidate title matches are not verified archive coverage.",
        }

    def attachment(self, record_id, asset_id):
        """Fetch one exact reviewed asset belonging to an active public record."""
        if not self.settings.show_source_links or not re.fullmatch(r"[0-9a-f]{32}", asset_id):
            return None
        row = self._row(record_id)
        if row is None:
            return None
        for item in self._matched(row):
            if item["asset_id"] == asset_id:
                data = self._bytes(item)
                if data is not None:
                    return data, "application/pdf", f"record-{record_id}-snapshot.pdf"
        return None

    def preview(self, record_id, asset_id):
        """Serve a cached first-page image only while the reviewed PDF is valid."""
        if not self.settings.show_source_links or not re.fullmatch(r"[0-9a-f]{32}", asset_id):
            return None
        row = self._row(record_id)
        if row is None:
            return None
        for item in self._matched(row):
            if item["asset_id"] == asset_id and self._bytes(item) is not None:
                data = self._preview_bytes(item)
                if data is not None:
                    return data, "image/png", f"record-{record_id}-page-1.png"
        return None


def register_record_routes(server, details):
    """Attach a read-only JSON detail endpoint and opaque PDF serving endpoint."""

    @server.get("/api/records/<record_id>")
    def record_details_api(record_id):
        value = details.get(record_id)
        if value is None:
            abort(404)
        response = jsonify(value)
        response.headers["Cache-Control"] = "no-store"
        return response

    @server.get("/records/<record_id>/attachments/<asset_id>")
    def record_attachment(record_id, asset_id):
        asset = details.attachment(record_id, asset_id)
        if asset is None:
            abort(404)
        data, mimetype, name = asset
        response = send_file(
            BytesIO(data), mimetype=mimetype, download_name=name,
            as_attachment=request.args.get("download") == "1", max_age=0,
        )
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @server.get("/records/<record_id>/attachments/<asset_id>/preview.png")
    def record_attachment_preview(record_id, asset_id):
        asset = details.preview(record_id, asset_id)
        if asset is None:
            abort(404)
        data, mimetype, name = asset
        response = send_file(BytesIO(data), mimetype=mimetype, download_name=name, max_age=0)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response
