"""Graph delivery preserves filtered snapshots, public fields and link policy."""

import hashlib
import json
from types import SimpleNamespace

import pytest

from observatory.models import Filters
from observatory.service import Service

PRIVATE = "private-import-path-and-credential-marker"


def graph_row(record_id="a", version_id="v1"):
    body = "A source article describes an energy project."
    return {
        "record_id": record_id, "version_id": version_id, "dataset": "native",
        "title": "An energy project", "date": "2025-01-01", "sponsor": "exxonmobil",
        "publisher": "Example outlet", "url": "https://example.org/article",
        "archive_url": "javascript:bad()", "body": body,
        "body_hash": hashlib.sha256(body.encode()).hexdigest(), "retrievable": True,
        "annotations": [{"ordinal": 0, "payload": {
            "version": "claims-calibrated", "labels": ["green_binary"],
            "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
            "basis": "url_and_exact_body", "source": PRIVATE,
            "status": "historical_automatic_unverified", "explanations": {"secret": PRIVATE},
        }}],
        "issues": [], "raw": {"secret": PRIVATE}, "provenance": [{"path": PRIVATE}],
    }


class GraphDB:
    def __init__(self):
        self.calls = []

    def knowledge_page(self, filters, offset=0, limit=5):
        self.calls.append((filters.model_dump(), offset, limit))
        return {"rows": [graph_row()], "total": 11, "offset": offset, "limit": limit}


def graph_service(enabled=True):
    return Service(SimpleNamespace(show_source_links=enabled), db=GraphDB(), rag=object())


def test_graph_service_preserves_filter_scope_and_excludes_private_payloads():
    service = graph_service()
    filters = Filters(sponsors=["exxonmobil"], keywords=["energy"], labels=["green_binary"],
                      record_ids=["a"], include_unknown_dates=False)
    graph = service.knowledge_graph(filters, offset=5, limit=5)
    assert service.db.calls == [(filters.model_dump(), 5, 5)]
    assert graph["coverage"] == {"total_records": 11, "shown_records": 1, "offset": 5, "limit": 5}
    assert graph["filters"] == filters.model_dump(mode="json")
    assert "body" not in graph["records"][0]
    assert graph["records"][0]["archive_url"] == ""
    assert PRIVATE not in json.dumps(graph)


def test_source_links_disabled_also_skips_attachment_reads():
    def forbidden(_):
        raise AssertionError("Should not read attachments when source links are disabled")

    graph = graph_service(False).knowledge_graph(Filters(), record_details=SimpleNamespace(get=forbidden))
    assert "https://example.org/article" not in json.dumps(graph)
    assert graph["records"][0]["url"] == ""


def test_attachment_from_concurrently_changed_version_is_not_joined():
    details = SimpleNamespace(get=lambda _: {"version_id": "new-version", "attachments": [
        {"asset_id": "new-asset", "kind": "pdf", "url": "/records/a/attachments/new-asset",
         "sha256": "b" * 64, "identity_status": "reviewed_local_capture"},
    ]})
    graph = graph_service().knowledge_graph(Filters(), record_details=details)
    assert "new-asset" not in json.dumps(graph)


@pytest.mark.parametrize("payload", [None, {}, {"limit": True}, {"limit": 21}, {"offset": -1},
                                     {"filters": {"dataset": "social"}}, {"unknown": "x"}])
def test_graph_request_schema(payload):
    from flask import Flask

    from observatory.knowledge_routes import register_knowledge_routes

    server = Flask(__name__)
    register_knowledge_routes(server, graph_service())
    response = server.test_client().post("/api/knowledge-graph", json=payload)
    # Empty object is a valid explicit request for the first native page.
    assert response.status_code == (200 if payload == {} else 400)


def test_graph_route_returns_schema_and_sanitizes_service_error():
    from flask import Flask

    from observatory.knowledge_routes import register_knowledge_routes

    def fail(*args, **kwargs):
        raise RuntimeError(PRIVATE)

    server = Flask(__name__)
    register_knowledge_routes(server, SimpleNamespace(knowledge_graph=fail))
    client = server.test_client()
    assert client.get("/api/knowledge-graph/schema").json["schema_version"]
    response = client.post("/api/knowledge-graph", json={})
    assert response.status_code == 503 and PRIVATE not in response.text
