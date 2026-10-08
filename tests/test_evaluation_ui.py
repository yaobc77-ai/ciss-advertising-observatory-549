"""Public scorecard coverage and routing; never a semantic performance evaluation."""

import json
import re
from types import SimpleNamespace

import pytest
from plotly.utils import PlotlyJSONEncoder
from test_app import SECRET, FakeService, callback, component_tree

from observatory.app import create_app
from observatory.evaluation_scorecard import pending_scorecard
from observatory.evaluation_ui import evaluation_panel


@pytest.fixture
def evaluation_app():
    service = FakeService()
    app = create_app(service, SimpleNamespace(
        show_source_links=True,
        cookie_secret="test-evaluation-cookie",
        monthly_budget_usd=100,
    ))
    return app, app.server.test_client(), service


def panel_nodes():
    panel = json.loads(json.dumps(evaluation_panel(), cls=PlotlyJSONEncoder))
    return list(component_tree(panel))


def test_public_panel_covers_every_customer_task_and_metric_definition():
    scorecard = pending_scorecard()
    nodes = panel_nodes()
    task_rows = {node["props"]["data-requirement-id"]: node
                 for node in nodes if "data-requirement-id" in node["props"]}
    metric_rows = {node["props"]["data-metric-id"]: node
                   for node in nodes if "data-metric-id" in node["props"]}
    assert len(task_rows) == 6
    assert set(task_rows) == {row["id"] for row in scorecard["requirements"]}
    assert set(metric_rows) == {row["id"] for row in scorecard["metrics"]}
    for metric in scorecard["metrics"]:
        row_text = json.dumps(metric_rows[metric["id"]], ensure_ascii=False)
        for field in ("label", "definition", "numerator_definition", "denominator_definition",
                      "formula", "unit", "direction", "method"):
            assert metric[field] in row_text
        details = [node for node in component_tree(metric_rows[metric["id"]])
                   if node["type"] == "Details"]
        assert len(details) == 1
        assert not details[0]["props"].get("open", False)


def test_missing_evaluation_is_displayed_as_pending_without_invented_score():
    scorecard = pending_scorecard()
    nodes = panel_nodes()
    metric_rows = [node for node in nodes if "data-metric-id" in node["props"]]
    for row in metric_rows:
        cells = row["props"]["children"]
        assert cells[2]["props"]["children"] == "Not evaluated"
        assert cells[3]["props"]["children"] == "Not evaluated"
    rendered = json.dumps(nodes, ensure_ascii=False)
    assert scorecard["definition_version"] in rendered
    assert "No evaluation run is available" in rendered
    assert "independent reviewer" in rendered
    assert not re.search(r"\b0(?:\.0+)?\s*%", rendered)
    assert SECRET not in rendered


def test_evaluation_is_in_tools_and_does_not_add_a_main_navigation_item(evaluation_app):
    _app, client, service = evaluation_app
    nodes = list(component_tree(client.get("/_dash-layout").json))
    nav = next(node for node in nodes if node["type"] == "Nav")
    assert [node["props"]["children"] for node in component_tree(nav)
            if node["type"] == "Link"] == ["Query", "Data"]
    toolbox = next(node for node in nodes if node["props"].get("id") == "toolbox")
    evaluation = next(node for node in component_tree(toolbox)
                      if node["props"].get("id") == "nav-evaluation")
    assert evaluation["props"]["href"] == "/evaluation"
    assert client.get("/evaluation").status_code == 200
    assert service.search_calls == service.answer_calls == service.browse_calls == []


@pytest.mark.parametrize("pathname, visible", [
    ("/query", "query"), ("/data", "data"), ("/wireframe", "wireframe"),
    ("/evaluation", "evaluation"), ("/evaluation/", "evaluation"),
    ("/missing", "not-found"),
])
def test_evaluation_navigation_hides_other_pages_without_running_queries(
    evaluation_app, pathname, visible
):
    app, client, service = evaluation_app
    result = callback(app, client, "query-page.hidden",
                      {"page-location.pathname": pathname}, "page-location.pathname")
    for page in ("query", "data", "wireframe", "evaluation", "not-found"):
        assert result[f"{page}-page"]["hidden"] == (page != visible)
    assert result["nav-evaluation"]["className"] == (
        "toolbox-link is-active" if visible == "evaluation" else "toolbox-link"
    )
    assert result["collection-workspace"]["hidden"] == (visible not in {"query", "data"})
    assert "research-results" not in result and "research-submission" not in result
    assert service.search_calls == service.answer_calls == service.browse_calls == []
