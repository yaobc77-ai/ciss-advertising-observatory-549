"""Organization overview: every registry organization, both collections kept apart."""

import hashlib
from types import SimpleNamespace

from flask import Flask

from observatory.knowledge_graph import build_graph, organization_overview
from observatory.knowledge_routes import register_knowledge_routes

COUNTS = {("native", "sponsor", "exxonmobil"): 15, ("social", "sponsor", "ExxonMobil"): 2975,
          ("social", "sponsor", "Sasol"): 1967, ("native", "publisher", "The New York Times"): 19}


def test_overview_lists_social_only_organizations_and_separate_units():
    overview = organization_overview(COUNTS)
    by_id = {item["id"]: item for item in overview["organizations"]}
    assert by_id["org:exxonmobil"]["countable_records"] == {"native": 15, "social": 2975}
    assert by_id["org:sasol"]["countable_records"] == {"social": 1967}
    assert by_id["outlet:nyt"]["countable_records"] == {"native": 19}
    assert by_id["org:api"]["type"] == "trade_association" and by_id["org:ceraweek"]["type"] == "event"
    assert overview["review"]["reviewer"] is None and "never summed" in overview["note"]
    assert overview["organizations"][0]["id"] == "org:exxonmobil"


def test_overview_route_is_read_only_and_reports_failures_without_details():
    server = Flask(__name__)
    service = SimpleNamespace(db=SimpleNamespace(source_value_counts=lambda: COUNTS))
    register_knowledge_routes(server, service)
    response = server.test_client().get("/api/knowledge-graph/organizations")
    assert response.status_code == 200 and response.headers["Cache-Control"] == "no-store"
    assert response.get_json()["registry_version"].startswith("entity-registry-v1:")

    def broken():
        raise RuntimeError("password=secret")
    service.db.source_value_counts = broken
    failed = server.test_client().get("/api/knowledge-graph/organizations")
    assert failed.status_code == 503 and "secret" not in failed.get_data(as_text=True)


def test_supplement_never_overrides_a_listed_source_sponsor():
    from observatory.entities import registry
    fill = next(iter(registry().supplemented.values()))
    body = "Fictional body."
    row = {"record_id": fill["record_id"], "version_id": fill["version_id"], "dataset": "native",
           "title": "t", "sponsor": "bp", "publisher": "The Times", "body": body,
           "body_hash": hashlib.sha256(body.encode()).hexdigest(), "annotations": []}
    graph = build_graph([row])
    assert not [edge for edge in graph["edges"] if edge["predicate"] == "supplemented_sponsor"]
