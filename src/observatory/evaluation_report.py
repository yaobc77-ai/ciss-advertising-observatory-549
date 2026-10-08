"""Read explicitly pinned evaluation evidence; expose aggregates only.

Reports describe their frozen run. Loading one does not verify applicability to
the running application, reviewer authority, semantic truth or client acceptance.
There is no browser upload, automatic promotion of older scores or model call.
"""

from __future__ import annotations

import hashlib
import re
from copy import deepcopy
from pathlib import Path

from .metric_report import MAX_JSON_BYTES, MetricReportInvalid, load_metric_report

_HASH = re.compile(r"[0-9a-f]{64}\Z")


def published_report(settings):
    """Load a bounded server-selected report, or return a score-free state."""
    path = getattr(settings, "evaluation_report_path", "")
    digest = getattr(settings, "evaluation_report_sha256", "")
    plan_digest = getattr(settings, "evaluation_plan_sha256", "")
    if not any((path, digest, plan_digest)):
        return {"state": "pending", "report": None}
    if not (isinstance(path, str) and path and isinstance(digest, str)
            and _HASH.fullmatch(digest) and isinstance(plan_digest, str)
            and _HASH.fullmatch(plan_digest)):
        return {"state": "unavailable", "report": None}
    try:
        with Path(path).open("rb") as stream:
            data = stream.read(MAX_JSON_BYTES + 1)
        if len(data) > MAX_JSON_BYTES or hashlib.sha256(data).hexdigest() != digest:
            return {"state": "unavailable", "report": None}
        summary = load_metric_report(data, expected_plan_sha256=plan_digest)
    except (OSError, ValueError, MetricReportInvalid):
        # File paths, raw contents and diagnostic values never reach the browser.
        return {"state": "unavailable", "report": None}
    packet = summary["raw_packet"]
    fields = ("report_id", "definition_version", "created_at", "versions", "status",
              "review_policy_authority", "client_acceptance", "semantic_truth", "notice", "metrics")
    public = {key: deepcopy(summary[key]) for key in fields}
    for metric in public["metrics"]:
        metric["review_policy"].pop("policy_record")
    public.update(
        applicability="frozen_run_only",
        sampling_description=packet["sampling_description"],
        limitations=list(packet["limitations"]),
        reviewer_count=len({row["reviewer_id"] for row in packet["reviews"]}),
        reviewed_at=sorted({row["reviewed_at"] for row in packet["reviews"]}),
        run_started_at=packet["run"]["started_at"],
        run_ended_at=packet["run"]["ended_at"],
    )
    return {"state": "available", "report": public}
