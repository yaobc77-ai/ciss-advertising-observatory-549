"""File-backed independent human review; no business DB or model integration."""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import os
import re
import secrets
import threading
from collections import Counter
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import parse_qs, urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

CODEBOOK_VERSION = "semantic-support-v1"
FROZEN_CODEBOOK_SHA256 = "a5f28af6b433b396a0858a7e6f7176cc143591f37d283a7859307b8ae1da24fe"
SUPPORT = ("supports", "partial", "unsupported", "contradicted", "uncertain")
Identity = Annotated[str, Field(min_length=1, max_length=240, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Text = Annotated[str, Field(min_length=1)]


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode("utf-8")).hexdigest()


def body_digest(body):
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def validate_codebook(value):
    if (value.get("schema_version") != "semantic-codebook-v1"
            or value.get("codebook_version") != CODEBOOK_VERSION
            or value.get("customer_approved") is not False
            or value.get("gold") is not False):
        raise ValueError("Codebook version or approval boundary is invalid")
    if digest(value) != FROZEN_CODEBOOK_SHA256:
        raise ValueError("Frozen v1 rule bytes changed; create a separately versioned codebook")
    if (value.get("review_schema") != ReviewSubmission.model_json_schema()
            or value.get("packet_schema") != ReviewPacket.model_json_schema()
            or value.get("adjudication_schema") != AdjudicationSubmission.model_json_schema()):
        raise ValueError("Frozen codebook schema differs from the runtime contract")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ReviewSource(StrictModel):
    source_id: Identity
    kind: Literal["original_text", "external_evidence", "synthetic_independent"]
    record_id: Identity
    version_id: Identity
    body_hash: Digest
    body: Text
    title: str = ""
    url: str = ""
    source_observation_id: Identity | None = None
    source_version_id: Digest | None = None
    source_body_hash: Digest | None = None

    @model_validator(mode="after")
    def source_integrity(self):
        if body_digest(self.body) != self.body_hash:
            raise ValueError("Source body hash mismatch")
        fields = (self.source_observation_id, self.source_version_id, self.source_body_hash)
        if any(v is not None for v in fields) and not all(v is not None for v in fields):
            raise ValueError("Observation source fields must be supplied together")
        if self.source_body_hash is not None and self.source_body_hash != self.body_hash:
            raise ValueError("Observation body hash mismatch")
        return self


class CandidateClaim(StrictModel):
    claim_id: Identity
    text: Text
    layer: Literal["mention", "stance", "in_document", "external_truth"]


class CandidateAnswer(StrictModel):
    summary: Text
    evidence: list[str]
    limitations: list[str]


class ReviewPacket(StrictModel):
    schema_version: Literal["semantic-review-packet-v1"] = "semantic-review-packet-v1"
    case_id: Identity
    provenance: Literal["synthetic_independent", "independent_source_packet"]
    task: Text
    sources: Annotated[list[ReviewSource], Field(min_length=1)]
    claims: Annotated[list[CandidateClaim], Field(min_length=1)]
    candidate_answer: CandidateAnswer
    source_scope: Text
    extraction_limitations: list[str]
    historical_auxiliary: list[str] = Field(default_factory=list)
    status: Literal["unreviewed"] = "unreviewed"
    gold: Literal[False] = False

    @model_validator(mode="after")
    def unique_members(self):
        for members, key in [(self.sources, "source_id"), (self.claims, "claim_id")]:
            ids = [getattr(v, key) for v in members]
            if len(ids) != len(set(ids)):
                raise ValueError("Duplicate packet identity")
        if self.provenance == "synthetic_independent" and any(
                s.kind != "synthetic_independent" for s in self.sources):
            raise ValueError("Synthetic packets must declare synthetic sources")
        return self


class LocatedQuotation(StrictModel):
    source_id: Identity
    start: Annotated[int, Field(ge=0)]
    end: Annotated[int, Field(gt=0)]
    quote: Text
    expression_subject: Text
    article_handling: Literal["asserts", "quotes_endorses", "quotes_distances",
                              "quotes_neutral", "mixed", "uncertain"]


class ClaimJudgment(StrictModel):
    claim_id: Identity
    support: Literal["supports", "partial", "unsupported", "contradicted", "uncertain"]
    mention: Literal["mentioned", "not_observed", "uncertain"]
    stance: Literal["favorable", "opposed", "neutral", "mixed", "uncertain"]
    external_truth: Literal["not_checked", "corroborated", "refuted", "uncertain"]
    summary: Text
    reason: Text
    evidence: list[LocatedQuotation]
    limitations: list[Text]


class ReviewSubmission(StrictModel):
    case_id: Identity
    packet_sha256: Digest
    codebook_sha256: Digest
    reviewer_id: Identity
    reviewer_kind: Literal["human"]
    independent_human_attestation: Literal[True]
    judgments: Annotated[list[ClaimJudgment], Field(min_length=1)]


class AdjudicationSubmission(ReviewSubmission):
    review_a_sha256: Digest
    review_b_sha256: Digest
    adjudication_reason: Text


class DemoSubmission(StrictModel):
    case_id: Identity
    packet_sha256: Digest
    codebook_sha256: Digest
    actor_kind: Literal["synthetic_demo"]
    judgments: Annotated[list[ClaimJudgment], Field(min_length=1)]


def check_judgments(packet, judgments):
    claim_ids = {claim.claim_id for claim in packet.claims}
    ids = [v.claim_id for v in judgments]
    if set(ids) != claim_ids or len(ids) != len(claim_ids):
        raise ValueError("Review must cover every claim exactly once")
    sources = {v.source_id: v for v in packet.sources}
    claims = {v.claim_id: v for v in packet.claims}
    for judgment in judgments:
        if not judgment.summary.strip() or not judgment.reason.strip() or any(not v.strip() for v in judgment.limitations):
            raise ValueError("请填写有内容的审核摘要、判断理由及限制，不要只输入空格")
        if judgment.support in {"supports", "partial", "contradicted"} and not judgment.evidence:
            raise ValueError("Support or contradiction requires located source evidence")
        if judgment.support in {"partial", "unsupported", "uncertain"} and not judgment.limitations:
            raise ValueError("Limited judgments require explicit limitations")
        external = False
        for quote in judgment.evidence:
            if not quote.expression_subject.strip():
                raise ValueError("请说明这句话由谁表达")
            source = sources.get(quote.source_id)
            if source is None or not (quote.start < quote.end <= len(source.body)):
                raise ValueError("Quotation source/range is invalid")
            if source.body[quote.start:quote.end] != quote.quote:
                raise ValueError("Quotation is not an exact original-body slice")
            external |= source.kind == "external_evidence"
        if judgment.external_truth in {"corroborated", "refuted"} and not external:
            raise ValueError("External truth judgments require separately supplied external evidence")
        if claims[judgment.claim_id].layer == "external_truth" and judgment.support == "supports" and not external:
            raise ValueError("Original text alone cannot establish external truth")


def _read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _write_new(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n")


class ReviewStore:
    """Separate A/B journals; authenticated readers never receive the other slot."""

    def __init__(self, root):
        self.root = Path(root).resolve()
        self.lock = threading.RLock()
        self.manifest = _read_json(self.root / "manifest.json")
        self.codebook = _read_json(self.root / "codebook.frozen.json")
        validate_codebook(self.codebook)
        if digest(self.codebook) != self.manifest["codebook_sha256"]:
            raise ValueError("Frozen codebook drift")
        self.packets = {}
        for line in (self.root / "packets.jsonl").read_text(encoding="utf-8").splitlines():
            packet = ReviewPacket.model_validate(json.loads(line))
            if packet.case_id in self.packets:
                raise ValueError("Duplicate review case")
            self.packets[packet.case_id] = packet
        actual = {key: digest(value.model_dump()) for key, value in self.packets.items()}
        if actual != self.manifest["packet_sha256"]:
            raise ValueError("Frozen packet drift")

    def assert_frozen(self):
        if _read_json(self.root / "manifest.json") != self.manifest:
            raise ValueError("Frozen manifest drift")
        if digest(_read_json(self.root / "codebook.frozen.json")) != self.manifest["codebook_sha256"]:
            raise ValueError("Frozen codebook drift")
        loaded = [ReviewPacket.model_validate(json.loads(line)) for line in
                  (self.root / "packets.jsonl").read_text(encoding="utf-8").splitlines()]
        actual = {v.case_id: digest(v.model_dump()) for v in loaded}
        if len(actual) != len(loaded) or actual != self.manifest["packet_sha256"]:
            raise ValueError("Frozen packet drift")

    @classmethod
    def initialize(cls, root, codebook, packets):
        root = Path(root).resolve()
        validate_codebook(codebook)
        parsed = [ReviewPacket.model_validate(v) for v in packets]
        if len({v.case_id for v in parsed}) != len(parsed):
            raise ValueError("Duplicate review case")
        root.mkdir(parents=True, exist_ok=False)
        _write_new(root / "codebook.frozen.json", codebook)
        with (root / "packets.jsonl").open("x", encoding="utf-8") as out:
            for packet in parsed:
                out.write(json.dumps(packet.model_dump(), ensure_ascii=False, sort_keys=True) + "\n")
        _write_new(root / "manifest.json", {
            "schema_version": "semantic-review-store-v1", "codebook_version": CODEBOOK_VERSION,
            "codebook_sha256": digest(codebook),
            "packet_sha256": {v.case_id: digest(v.model_dump()) for v in parsed},
            "initial_status": "unreviewed", "gold": False,
            "human_reviewers": "TBD", "customer_approval": False,
        })
        return cls(root)

    def assign(self, reviewer_a, reviewer_b, adjudicator):
        ids = (reviewer_a, reviewer_b, adjudicator)
        if len(set(ids)) != 3 or any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,239}", v)
                                    or v.casefold() in {"tbd", "ai", "model"} for v in ids):
            raise ValueError("Three distinct named human reviewer IDs are required")
        _write_new(self.root / "assignments.private.json", {
            role: {"reviewer_id": reviewer, "token": secrets.token_urlsafe(32)}
            for role, reviewer in zip(("A", "B", "adjudicator"), ids)
        })

    def assignment(self, role, token):
        if role not in {"A", "B", "adjudicator"}:
            raise PermissionError("Unknown reviewer slot")
        path = self.root / "assignments.private.json"
        assignment = _read_json(path).get(role) if path.exists() else None
        if assignment is None or not hmac.compare_digest(str(token), assignment["token"]):
            raise PermissionError("Reviewer slot is not assigned or token is invalid")
        return assignment

    def _entries(self, role):
        path = self.root / role / "reviews.jsonl"
        entries = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []
        assigned = _read_json(self.root / "assignments.private.json").get(role) if entries else None
        model = AdjudicationSubmission if role == "adjudicator" else ReviewSubmission
        for entry in entries:
            if set(entry) != {"review", "review_sha256", "slot", "saved_at_utc", "status", "gold", "material_provenance"}:
                raise ValueError("Saved review journal contains unknown or missing fields")
            if entry["slot"] != role or digest(entry["review"]) != entry["review_sha256"]:
                raise ValueError("Saved review journal drift")
            review = model.model_validate(entry["review"])
            packet = self.packets.get(review.case_id)
            if (assigned is None or review.reviewer_id != assigned["reviewer_id"] or packet is None
                    or review.packet_sha256 != self.manifest["packet_sha256"][review.case_id]
                    or review.codebook_sha256 != self.manifest["codebook_sha256"]
                    or entry["material_provenance"] != packet.provenance or entry["gold"] is not False):
                raise ValueError("Saved review reviewer or source binding drift")
            expected_status = "adjudication_saved_not_gold" if role == "adjudicator" else "independent_review_saved"
            if entry["status"] != expected_status:
                raise ValueError("Saved review status drift")
            check_judgments(packet, review.judgments)
            if role == "adjudicator":
                for source_role, source_hash in (("A", review.review_a_sha256), ("B", review.review_b_sha256)):
                    if not any(v["review_sha256"] == source_hash and v["review"]["case_id"] == review.case_id
                               for v in self._entries(source_role)):
                        raise ValueError("Saved adjudication references a missing independent review")
        return entries

    def _latest(self, role, case_id):
        matching = [v for v in self._entries(role) if v["review"]["case_id"] == case_id]
        return matching[-1] if matching else None

    def view(self, role, token, case_id):
        self.assert_frozen()
        self.assignment(role, token)
        packet = self.packets[case_id]
        public_packet = packet.model_dump()
        if role in {"A", "B"}:
            public_packet.pop("historical_auxiliary", None)
        result = {"packet": public_packet, "packet_sha256": self.manifest["packet_sha256"][case_id],
                  "codebook_sha256": self.manifest["codebook_sha256"], "slot": role,
                  "status": "unreviewed", "gold": False}
        own = self._latest(role, case_id)
        if own:
            result["own_review"] = own
            result["status"] = "independent_review_saved" if role != "adjudicator" else "adjudication_saved_not_gold"
        if role == "adjudicator":
            pair = [self._latest(v, case_id) for v in ("A", "B")]
            if not all(pair):
                raise ValueError("Both independent reviews must be saved before adjudication")
            result["independent_reviews"] = [
                {"slot": slot, "review_sha256": entry["review_sha256"],
                 "judgments": entry["review"]["judgments"]}
                for slot, entry in zip(("A", "B"), pair)
            ]
            if own and (own["review"]["review_a_sha256"], own["review"]["review_b_sha256"]) != (
                    pair[0]["review_sha256"], pair[1]["review_sha256"]):
                result["status"] = "adjudication_stale_after_review_revision"
        return result

    def demo_view(self, case_id=None):
        self.assert_frozen()
        candidates = [v for v in self.packets.values() if v.provenance == "synthetic_independent"]
        packet = next((v for v in candidates if case_id is None or v.case_id == case_id), None)
        if packet is None:
            raise ValueError("No fictional demonstration is available")
        value = packet.model_dump()
        value.pop("historical_auxiliary", None)
        return {"packet": value, "packet_sha256": self.manifest["packet_sha256"][packet.case_id],
                "codebook_sha256": self.manifest["codebook_sha256"], "slot": "synthetic_demo",
                "status": "synthetic_demonstration_not_human_review", "gold": False}

    def locate_quote(self, role, token, case_id, source_id, quote):
        view = self.demo_view(case_id) if role == "synthetic_demo" else self.view(role, token, case_id)
        if not isinstance(quote, str) or not quote:
            raise ValueError("请输入完整原句；不能以空文字匹配位置")
        source = next((v for v in view["packet"]["sources"] if v["source_id"] == source_id), None)
        if source is None:
            raise ValueError("请选择这个审核包中的来源")
        matches = []
        start = source["body"].find(quote)
        while start != -1:
            matches.append({"start": start, "end": start + len(quote),
                            "context": source["body"][max(0, start - 70):start + len(quote) + 70]})
            start = source["body"].find(quote, start + 1)
        if not matches:
            raise ValueError("原句未与所选来源逐字匹配。请保留原文、标点、空格和换行，不要用改写。")
        return {"matches": matches, "body_hash": source["body_hash"], "coordinate_system": "Python Unicode [start,end)"}

    def save_demo(self, value):
        """Explicit synthetic save; never inserts a human A/B/adjudicator entry."""
        with self.lock:
            review = DemoSubmission.model_validate(value)
            view = self.demo_view(review.case_id)
            if (review.packet_sha256 != view["packet_sha256"]
                    or review.codebook_sha256 != view["codebook_sha256"]):
                raise ValueError("Synthetic demo packet or codebook binding mismatch")
            check_judgments(self.packets[review.case_id], review.judgments)
            entry = {"review": review.model_dump(), "review_sha256": digest(review.model_dump()),
                     "status": "synthetic_demo_saved_not_human_review", "human_review": False,
                     "gold": False, "saved_at_utc": datetime.now(timezone.utc).isoformat()}
            path = self.root / "synthetic_demo" / "demonstrations.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as out:
                out.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
                out.flush()
                os.fsync(out.fileno())
            return {key: entry[key] for key in ("review_sha256", "status", "human_review", "gold")}

    def save(self, role, token, value):
        with self.lock:
            self.assert_frozen()
            assignment = self.assignment(role, token)
            model = AdjudicationSubmission if role == "adjudicator" else ReviewSubmission
            review = model.model_validate(value)
            packet = self.packets[review.case_id]
            if (review.reviewer_id != assignment["reviewer_id"]
                    or review.codebook_sha256 != self.manifest["codebook_sha256"]
                    or review.packet_sha256 != self.manifest["packet_sha256"][review.case_id]):
                raise ValueError("Reviewer, packet or codebook binding mismatch")
            check_judgments(packet, review.judgments)
            if role == "adjudicator":
                a, b = (self._latest(v, review.case_id) for v in ("A", "B"))
                if not a or not b or (review.review_a_sha256, review.review_b_sha256) != (
                        a["review_sha256"], b["review_sha256"]):
                    raise ValueError("Adjudication requires the exact latest independent pair")
            data = review.model_dump()
            entry = {"review": data, "review_sha256": digest(data), "slot": role,
                     "saved_at_utc": datetime.now(timezone.utc).isoformat(),
                     "status": "adjudication_saved_not_gold" if role == "adjudicator" else "independent_review_saved",
                     "gold": False, "material_provenance": packet.provenance}
            path = self.root / role / "reviews.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as out:
                out.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
                out.flush()
                os.fsync(out.fileno())
            return {"review_sha256": entry["review_sha256"], "status": entry["status"], "gold": False}

    def agreement(self):
        """Nominal raw agreement and Cohen kappa; unpaired units stay in coverage denominator."""
        self.assert_frozen()
        pairs = []
        case_pairs = 0
        for case in self.packets:
            a, b = (self._latest(role, case) for role in ("A", "B"))
            if a and b:
                case_pairs += 1
                bmap = {v["claim_id"]: v for v in b["review"]["judgments"]}
                pairs.extend((v, bmap[v["claim_id"]]) for v in a["review"]["judgments"])
        result = {"total_cases": len(self.packets), "paired_cases": case_pairs,
                  "total_claims": sum(len(v.claims) for v in self.packets.values()),
                  "paired_claims": len(pairs), "human_accuracy": None, "gold": False, "axes": {}}
        for axis in ("support", "mention", "stance", "external_truth"):
            n = len(pairs)
            counts_a, counts_b = (Counter(v[i][axis] for v in pairs) for i in (0, 1))
            matched = sum(a[axis] == b[axis] for a, b in pairs)
            po = matched / n if n else None
            pe = sum(counts_a[k] * counts_b[k] for k in counts_a.keys() | counts_b.keys()) / (n * n) if n else None
            kappa = (po - pe) / (1 - pe) if pe is not None and pe < 1 else None
            confusion = Counter((a[axis], b[axis]) for a, b in pairs)
            result["axes"][axis] = {"units": n, "matched": matched, "raw_agreement": po,
                                    "chance_agreement": pe, "cohen_kappa": kappa,
                                    "counts_a": dict(counts_a), "counts_b": dict(counts_b),
                                    "confusion": [{"a": a, "b": b, "count": count}
                                                  for (a, b), count in sorted(confusion.items())]}
        return result

    def export(self, destination):
        """Operator-only export. Never available through an A/B HTTP endpoint."""
        destination = Path(destination)
        destination.mkdir(parents=True, exist_ok=False)
        for role in ("A", "B", "adjudicator"):
            with (destination / f"{role}.jsonl").open("x", encoding="utf-8") as out:
                for entry in self._entries(role):
                    out.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
        _write_new(destination / "agreement.json", self.agreement())
        _write_new(destination / "manifest.json", {**self.manifest, "export_status": "review_material_not_gold"})


def demo_packet():
    body = 'Oriole Grid says: "Our pilot may reduce water use." The author notes that no independent measurements are available.'
    return {"case_id": "synthetic-oriole-001", "provenance": "synthetic_independent",
            "task": "Judge whether the candidate faithfully reports this fictional source.",
            "sources": [{"source_id": "source-1", "kind": "synthetic_independent",
                         "record_id": "fictional:oriole", "version_id": "fictional-v1",
                         "body_hash": body_digest(body), "body": body}],
            "claims": [{"claim_id": "claim-1", "text": "Oriole Grid says its pilot may reduce water use.",
                        "layer": "in_document"}],
            "candidate_answer": {"summary": "Oriole Grid states a possible reduction.",
                                 "evidence": ['Our pilot may reduce water use.'],
                                 "limitations": ["No independent measurement is supplied."]},
            "source_scope": "Only the complete fictional paragraph shown here.",
            "extraction_limitations": ["Fictional development example; not a real reviewed source."]}


def render_review_page(view, reviewer_id=None):
    """Natural-language form; structured JSON is a collapsed diagnostic view."""
    esc = html.escape
    labels = {
        "support": [("supports", "完整支持"), ("partial", "部分支持"), ("unsupported", "证据不支持"),
                    ("contradicted", "明确矛盾"), ("uncertain", "无法可靠判断")],
        "mention": [("mentioned", "文中提及"), ("not_observed", "所供完整范围内未观察到"), ("uncertain", "不确定")],
        "stance": [("favorable", "积极推广／赞同"), ("opposed", "反对／质疑"), ("neutral", "中性描述"),
                   ("mixed", "立场混合"), ("uncertain", "立场不明确")],
        "external_truth": [("not_checked", "没有外部核实"), ("corroborated", "独立材料支持"),
                           ("refuted", "独立材料反驳"), ("uncertain", "外部核实不确定")],
    }

    def select(name, values):
        return '<select class="' + name + '"><option value="">请选择</option>' + ''.join(
            '<option value="' + esc(value) + '">' + esc(label) + '</option>' for value, label in values) + '</select>'

    packet = view["packet"]
    demo = view["slot"] == "synthetic_demo"
    page = '''<!doctype html><html lang="zh-CN"><meta charset=utf-8><title>独立语义审核</title>
    <style>body{max-width:1100px;margin:24px auto;padding:0 16px;font:16px/1.6 sans-serif;color:#202838;background:#f8fafc}
    h1,h2,h3{line-height:1.35}section,.source,.answer{padding:18px;margin:18px 0;background:white;border:1px solid #cad5e2;border-radius:10px}
    pre{white-space:pre-wrap;overflow-wrap:anywhere}label{display:block;margin:12px 0}textarea,input{box-sizing:border-box;width:100%;padding:9px;font:inherit}
    textarea{min-height:85px}select{padding:8px;max-width:100%;font:inherit}button{padding:9px 16px;margin:8px 8px 8px 0;cursor:pointer}
    .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:12px}.quote{border-left:3px solid #5186a4;padding:12px;margin:12px 0}
    .notice{padding:12px;background:#e9f2fa}.error{color:#9c2431}input[type=checkbox]{width:auto}.position{margin-left:12px;color:#365671}</style>
    <h1>独立语义审核 v1</h1>'''
    page += '<p class="notice">' + (
        '虚构材料演示。尚未指定真人角色；演示保存单独标记 synthetic_demo，不进入双人标注，不计人审。'
        if demo else '真人独立审核 · ' + esc(view["slot"]) + '。只显示你自己的历史审核；保存不会覆盖旧记录，也不会自动成为gold。') + '</p>'
    page += '<h2>本次任务</h2><p>' + esc(packet["task"]) + '</p><p><b>审核范围：</b>' + esc(packet["source_scope"]) + '</p>'
    page += '<div class="answer"><h2>待审核答案</h2><p><b>摘要：</b>' + esc(packet["candidate_answer"]["summary"]) + '</p>'
    for name, label in [("evidence", "候选证据"), ("limitations", "候选限制")]:
        page += '<p><b>' + label + '：</b></p><ul>' + ''.join('<li>' + esc(v) + '</li>' for v in packet["candidate_answer"][name]) + '</ul>'
    page += '</div><h2>原始材料</h2>'
    for source in packet["sources"]:
        page += '<details class="source"><summary>展开完整来源 · ' + esc(source["title"] or source["source_id"]) + '</summary><pre>' + esc(source["body"]) + '</pre>'
        page += '<details><summary>来源身份与位置依据</summary><p>' + esc(source["record_id"]) + ' / ' + esc(source["version_id"]) + '</p><p>正文SHA-256：' + esc(source["body_hash"]) + '</p></details></details>'
    if packet["extraction_limitations"]:
        page += '<p><b>材料限制：</b>' + '；'.join(esc(v) for v in packet["extraction_limitations"]) + '</p>'
    if view["slot"] == "adjudicator":
        page += '<h2>双方独立审核</h2>'
        for entry in view["independent_reviews"]:
            page += '<div class="answer"><h3>审核者 ' + esc(entry["slot"]) + '</h3>'
            for judgment in entry["judgments"]:
                state = dict(labels["support"])[judgment["support"]]
                page += '<p><b>' + esc(judgment["claim_id"]) + ' · ' + esc(state) + '</b></p><p>' + esc(judgment["reason"]) + '</p>'
                for quote in judgment["evidence"]:
                    page += '<blockquote>' + esc(quote["quote"]) + '</blockquote>'
            page += '</div>'
        page += '<label>裁决理由<textarea id="adjudicationReason" placeholder="说明双方分歧与选择依据"></textarea></label>'
    page += '<h2>逐条判断</h2><p>分别判断来源支持、文中提及、谁表达及立场、外部是否核实。原文有一句话，并不自动证明它在现实中为真。</p>'
    for index, claim in enumerate(packet["claims"], 1):
        page += '<section class="claim" data-claim="' + esc(claim["claim_id"]) + '"><h3>' + str(index) + '. ' + esc(claim["text"]) + '</h3><div class="grid">'
        for axis, label in [("support", "证据支持程度"), ("mention", "文中是否提及"), ("stance", "表达主体的立场"), ("external_truth", "外部核实情况")]:
            page += '<label>' + label + '<br>' + select(axis, labels[axis]) + '</label>'
        page += '</div><label>审核摘要<textarea class="summary" placeholder="用一句话说明你能支持的结论"></textarea></label>'
        page += '<label>判断理由<textarea class="reason" placeholder="说明原句与这个命题的关系，不只重复标签"></textarea></label>'
        page += '<label>限制与未解决项<textarea class="limitations" placeholder="每行一项；部分支持、无支持或不确定必须填写"></textarea></label>'
        page += '<h4>支持判断的原句</h4><p>选择来源，粘贴原句，再点“匹配原句”。程序填写精确位置；重复原句须选择所在位置。</p><div class="quotes"></div><button type="button" class="addQuote">＋添加原句</button></section>'
    if not demo:
        page += '<label><input id="attest" type="checkbox"> 我是已登记的真人审核者，本次独立完成判断；没有用AI或另一人的标签冒充独立标注。</label>'
    page += '<button id="save">' + ('保存虚构演示（不计人审）' if demo else '保存本次独立审核') + '</button><pre id="result" role="status"></pre>'
    page += '<details><summary>高级：查看结构化数据</summary><button id="preview" type="button">生成预览</button><pre id="advanced">请先填写表单。</pre></details>'
    context = {**view, "reviewer_id": reviewer_id}
    page += '<script type="application/json" id="context">' + json.dumps(context, ensure_ascii=False).replace('<', '\\u003c') + '</script>'
    page += r'''<script>
    const ctx=JSON.parse(document.getElementById('context').textContent), isDemo=ctx.slot==='synthetic_demo';
    const esc=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    async function post(path,value){const r=await fetch(path+location.search,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(value)});const data=await r.json();if(!r.ok)throw Error(data.error||'请求失败');return data;}
    function addQuote(section, prior){
      const div=document.createElement('div');div.className='quote';
      const opts=ctx.packet.sources.map(s=>'<option value="'+esc(s.source_id)+'">'+esc(s.title||s.source_id)+'</option>').join('');
      div.innerHTML='<label>来源<select class="sourceId">'+opts+'</select></label><label>原句<textarea class="quoteText" placeholder="逐字粘贴原句，保留标点与空格"></textarea></label><button type="button" class="match">匹配原句</button><select class="matchPosition" hidden></select><span class="position"></span><div><b>原句上下文</b><pre class="contextPreview">匹配原句后显示。</pre></div><label>这句话由谁表达<input class="subject" placeholder="例如：公司声明、作者、被引述的研究者"></label><label>文章如何处理这句话<select class="handling"><option value="">请选择</option><option value="asserts">作者直接陈述</option><option value="quotes_endorses">引述并认可</option><option value="quotes_distances">引述并质疑／保持距离</option><option value="quotes_neutral">中性引述</option><option value="mixed">处理方式混合</option><option value="uncertain">无法判断</option></select></label><button type="button" class="remove">删除这条原句</button>';
      const reset=()=>{delete div.dataset.start;delete div.dataset.end;div.querySelector('.position').textContent='原句或来源变化后须重新匹配';div.querySelector('.matchPosition').hidden=true;};
      div.querySelector('.sourceId').onchange=reset;div.querySelector('.quoteText').oninput=reset;
      div.querySelector('.match').onclick=()=>locate(div).catch(e=>{div.querySelector('.position').textContent=e.message;});
      div.querySelector('.remove').onclick=()=>div.remove();section.querySelector('.quotes').append(div);
      if(prior){div.querySelector('.sourceId').value=prior.source_id;div.querySelector('.quoteText').value=prior.quote;div.querySelector('.subject').value=prior.expression_subject;div.querySelector('.handling').value=prior.article_handling;div.dataset.start=prior.start;div.dataset.end=prior.end;div.querySelector('.position').textContent='已保存位置 ['+prior.start+', '+prior.end+')';const body=ctx.packet.sources.find(s=>s.source_id===prior.source_id).body;div.querySelector('.contextPreview').textContent=[...body].slice(Math.max(0,prior.start-70),prior.end+70).join('');}
    }
    async function locate(div){
      const data=await post(isDemo?'/demo-locate':'/locate',{case_id:ctx.packet.case_id,source_id:div.querySelector('.sourceId').value,quote:div.querySelector('.quoteText').value});
      const menu=div.querySelector('.matchPosition'),note=div.querySelector('.position');
      const pick=m=>{div.dataset.start=m.start;div.dataset.end=m.end;note.textContent='精确位置 ['+m.start+', '+m.end+')';div.querySelector('.contextPreview').textContent=m.context;};
      if(data.matches.length===1){menu.hidden=true;pick(data.matches[0]);return;}
      menu.innerHTML='<option value="">同一句出现多处，请选具体位置</option>'+data.matches.map((m,i)=>'<option value="'+i+'">'+esc(m.context)+' ['+m.start+', '+m.end+')</option>').join('');menu.hidden=false;note.textContent='需要选择位置';delete div.dataset.start;delete div.dataset.end;menu.onchange=()=>{if(menu.value!=='')pick(data.matches[Number(menu.value)]);};
    }
    document.querySelectorAll('.claim').forEach(section=>{
      section.querySelector('.addQuote').onclick=()=>addQuote(section);
      const prior=ctx.own_review?.review.judgments.find(v=>v.claim_id===section.dataset.claim);
      if(prior){['support','mention','stance','external_truth','summary','reason'].forEach(k=>section.querySelector('.'+k).value=prior[k]);section.querySelector('.limitations').value=prior.limitations.join('\n');prior.evidence.forEach(q=>addQuote(section,q));}
      else addQuote(section);
    });
    async function payload(){
      const judgments=[];
      for(const section of document.querySelectorAll('.claim')){
        const value={claim_id:section.dataset.claim,evidence:[],limitations:section.querySelector('.limitations').value.split(/\r?\n/).map(v=>v.trim()).filter(Boolean)};
        ['support','mention','stance','external_truth','summary','reason'].forEach(k=>value[k]=section.querySelector('.'+k).value);
        const required={support:'证据支持程度',mention:'文中是否提及',stance:'表达主体的立场',external_truth:'外部核实情况',summary:'审核摘要',reason:'判断理由'};
        for(const [k,label] of Object.entries(required))if(!String(value[k]).trim())throw Error('请填写或选择：'+label);
        for(const div of section.querySelectorAll('.quote')){
          const quote=div.querySelector('.quoteText').value;if(!quote)continue;
          if(!div.dataset.start)await locate(div);if(!div.dataset.start)throw Error('原句重复出现，请先选择具体位置。');
          value.evidence.push({source_id:div.querySelector('.sourceId').value,start:Number(div.dataset.start),end:Number(div.dataset.end),quote,expression_subject:div.querySelector('.subject').value,article_handling:div.querySelector('.handling').value});
          if(!div.querySelector('.subject').value.trim()||!div.querySelector('.handling').value)throw Error('请填写原句表达主体并选择文章处理方式。');
        }
        if(['supports','partial','contradicted'].includes(value.support)&&!value.evidence.length)throw Error('这个支持程度需要至少一条匹配原句。');
        if(['partial','unsupported','uncertain'].includes(value.support)&&!value.limitations.length)throw Error('这个支持程度需要填写限制与未解决项。');
        judgments.push(value);
      }
      const value={case_id:ctx.packet.case_id,packet_sha256:ctx.packet_sha256,codebook_sha256:ctx.codebook_sha256,judgments};
      if(isDemo)value.actor_kind='synthetic_demo';else Object.assign(value,{reviewer_id:ctx.reviewer_id,reviewer_kind:'human',independent_human_attestation:document.getElementById('attest').checked});
      if(ctx.slot==='adjudicator')Object.assign(value,{review_a_sha256:ctx.independent_reviews[0].review_sha256,review_b_sha256:ctx.independent_reviews[1].review_sha256,adjudication_reason:document.getElementById('adjudicationReason').value});
      return value;
    }
    document.getElementById('preview').onclick=async()=>{try{document.getElementById('advanced').textContent=JSON.stringify(await payload(),null,2)}catch(e){document.getElementById('advanced').textContent=e.message}};
    document.getElementById('save').onclick=async()=>{const result=document.getElementById('result');try{if(!isDemo&&!document.getElementById('attest').checked)throw Error('请由已登记真人独立审核并勾选声明。');const value=await payload();const saved=await post(isDemo?'/demo-save':'/save',value);result.className='';result.textContent=(isDemo?'虚构演示已保存，不计人审。':'本次独立审核已保存；非gold。')+'\n记录指纹：'+saved.review_sha256;document.getElementById('advanced').textContent=JSON.stringify(value,null,2)}catch(e){result.className='error';result.textContent='未保存：'+e.message}};
    </script></html>'''
    return page


def make_server(store, host="127.0.0.1", port=8072):
    if host != "127.0.0.1":
        raise ValueError("Review server is restricted to IPv4 loopback")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass  # Do not log role bearer tokens from request URLs.

        def respond(self, value, code=200, content_type="application/json; charset=utf-8"):
            data = value.encode("utf-8") if isinstance(value, str) else json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            query = parse_qs(urlsplit(self.path).query)
            path = urlsplit(self.path).path
            if path == "/":
                text = """<!doctype html><meta charset=utf-8><title>语义审核</title>
                <h1>独立语义审核 v1</h1><p>初始未审核，非gold，真实材料人审尚未完成。</p>
                <p>协调员须用 assign 子命令指定两名独立真人及第三名裁决者，分别交付私有访问链接。
                A/B 页面仅显示自己的记录，不显示对方标签或旧AI答案。三人标识不得相同。</p>
                <p>逐条判断 supports / partial / unsupported / contradicted / uncertain，分别记录提及、立场和外部真伪。
                原句必须保留来源ID、版本、正文哈希和精确[start,end)位置。保存不自动成为gold。</p>
                <p>协调员可用 export 子命令导出原始日志和一致性。未双标时不计算分数。</p>
                <p><a href="/demo">打开可读表单与虚构材料演示</a>（演示保存不计人审）</p>"""
                return self.respond(text, content_type="text/html; charset=utf-8")
            try:
                role, token, case = (query.get(k, [""])[0] for k in ("role", "token", "case"))
                if path == "/demo":
                    return self.respond(render_review_page(store.demo_view(case or None)), content_type="text/html; charset=utf-8")
                if path == "/packet":
                    return self.respond(store.view(role, token, case))
                if path != "/review":
                    return self.respond({"error": "Not found"}, 404)
                view = store.view(role, token, case)
                assignment = store.assignment(role, token)
                return self.respond(render_review_page(view, assignment["reviewer_id"]), content_type="text/html; charset=utf-8")
            except PermissionError as exc:
                return self.respond({"error": str(exc)}, 403)
            except (ValueError, KeyError) as exc:
                return self.respond({"error": str(exc)}, 400)

        def do_POST(self):
            path = urlsplit(self.path).path
            if path not in {"/save", "/locate", "/demo-locate", "/demo-save"}:
                return self.respond({"error": "Not found"}, 404)
            origin = self.headers.get("Origin")
            if origin and origin != f"http://127.0.0.1:{self.server.server_port}":
                return self.respond({"error": "Cross-origin review submission denied"}, 403)
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 1_000_000:
                    raise ValueError("Invalid request length")
                if self.headers.get("Content-Type", "").split(";", 1)[0] != "application/json":
                    raise ValueError("Submit application/json")
                query = parse_qs(urlsplit(self.path).query)
                role, token = (query.get(k, [""])[0] for k in ("role", "token"))
                value = json.loads(self.rfile.read(length))
                if path in {"/locate", "/demo-locate"}:
                    return self.respond(store.locate_quote(
                        "synthetic_demo" if path == "/demo-locate" else role, token,
                        value["case_id"], value["source_id"], value["quote"]))
                if path == "/demo-save":
                    return self.respond(store.save_demo(value))
                return self.respond(store.save(role, token, value))
            except PermissionError as exc:
                return self.respond({"error": str(exc)}, 403)
            except (ValueError, KeyError) as exc:
                return self.respond({"error": str(exc)}, 400)

    return ThreadingHTTPServer((host, port), Handler)
