"""Progress is observable without exposing another session or inventing stages."""

import threading
from types import SimpleNamespace

import pytest

from observatory.app import create_app
from observatory.models import Answer
from observatory.query_progress import ProgressRegistry

TOKEN = "a-page-token-long-enough-for-validation"


def test_stages_come_from_reported_operations_and_finish_clears_them():
    registry = ProgressRegistry()
    request_id = registry.begin("visitor", TOKEN)
    report = registry.reporter("visitor", TOKEN, request_id)
    assert registry.snapshot("visitor", TOKEN)["label"] == "Preparing your search"
    report("database")
    assert registry.snapshot("visitor", TOKEN) == {
        "status": "running", "label": "Searching the advertising database",
    }
    report("Model has thought for 12 seconds; private text")
    report({"private": "model reasoning"})
    assert "private" not in str(registry.snapshot("visitor", TOKEN))
    report("web")
    assert registry.snapshot("visitor", TOKEN)["label"] == "Searching web sources"
    registry.finish("visitor", TOKEN, request_id)
    report("citations")
    assert registry.snapshot("visitor", TOKEN) == {"status": "complete", "label": ""}


def test_owner_is_required_and_superseded_requests_cannot_change_current_progress():
    registry = ProgressRegistry()
    old_id = registry.begin("owner", TOKEN)
    old_reporter = registry.reporter("owner", TOKEN, old_id)
    new_id = registry.begin("owner", TOKEN)
    registry.reporter("owner", TOKEN, new_id)("interpreting")
    old_reporter("web")
    registry.finish("owner", TOKEN, old_id)
    assert registry.snapshot("owner", TOKEN)["label"] == "Interpreting your question"
    assert registry.snapshot("another-owner", TOKEN) == {"status": "idle", "label": ""}
    assert registry.snapshot("owner", "invalid") == {"status": "idle", "label": ""}


def test_expiry_capacity_and_failure_are_bounded():
    now = [0]
    registry = ProgressRegistry(ttl_seconds=10, max_entries=1, clock=lambda: now[0])
    request_id = registry.begin("owner", TOKEN)
    registry.finish("owner", TOKEN, request_id, status="failed")
    registry.finish("owner", TOKEN, request_id)
    assert registry.snapshot("owner", TOKEN) == {"status": "failed", "label": ""}
    now[0] = 10
    assert registry.snapshot("owner", TOKEN)["status"] == "idle"
    registry.begin("first", TOKEN)
    registry.begin("second", TOKEN)
    assert registry.snapshot("first", TOKEN)["status"] == "idle"
    assert registry.begin("owner", "not-valid") is None


class ProgressService:
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()
        self.fail = False

    def health(self):
        return {"status": "ok", "record_counts": {"native": 0, "social": 0}}

    def facets(self, _dataset):
        return {}

    def answer(self, _question, _filters, visitor, progress=None):
        progress("database")
        self.started.set()
        assert self.release.wait(5)
        if self.fail:
            raise RuntimeError("private service failure")
        return Answer(status="insufficient_evidence", answer="No matching stored advertisements.")

    def search(self, *_args, **_kwargs):
        return []


def components(node):
    if isinstance(node, dict):
        if "props" in node:
            yield node
        for value in node.values():
            yield from components(value)
    elif isinstance(node, list):
        for value in node:
            yield from components(value)


def post_callback(app, client, output_name, values, changed):
    key, spec = next((key, spec) for key, spec in app.callback_map.items() if output_name in key)
    outputs = spec["output"]
    if isinstance(outputs, list):
        out = [{"id": item.component_id, "property": item.component_property} for item in outputs]
    else:
        out = {"id": outputs.component_id, "property": outputs.component_property}
    return client.post("/_dash-update-component", json={
        "output": key, "outputs": out, "changedPropIds": [changed],
        "inputs": [{**item, "value": values.get(f"{item['id']}.{item['property']}")}
                   for item in spec["inputs"]],
        "state": [{**item, "value": values.get(f"{item['id']}.{item['property']}")}
                  for item in spec["state"]],
    })


@pytest.mark.parametrize("fail", [False, True], ids=["success", "service-failure"])
def test_poll_observes_actual_inflight_operation_and_running_ui_cleans_up(fail):
    service = ProgressService()
    service.fail = fail
    app = create_app(service, SimpleNamespace(show_source_links=True, cookie_secret="test-progress-cookie"))
    client = app.server.test_client()
    layout = client.get("/_dash-layout")
    tree = {item["props"]["id"]: item["props"] for item in components(layout.json)
            if "id" in item["props"]}
    token = tree["research-progress-token"]["data"]
    cookie = client.get_cookie("session")
    answering = app.server.test_client()
    answering.set_cookie("session", cookie.value)
    values = {
        "answer-paid.n_clicks": 1, "search-free.n_clicks": 0,
        "research-question.value": "A realistic question", "search-scope.value": "current",
        "active-dataset.value": "native", "page-location.pathname": "/query",
        "research-progress-token.data": token,
    }
    for dataset in ("native", "social"):
        for field in ("publishers", "sponsors", "platforms", "keywords", "labels"):
            values[f"{dataset}-{field}.value"] = []
        values[f"{dataset}-unknown-dates.value"] = ["include"]
    response = []
    thread = threading.Thread(target=lambda: response.append(post_callback(
        app, answering, "research-results.children", values, "answer-paid.n_clicks",
    )))
    thread.start()
    assert service.started.wait(5)
    polled = post_callback(app, client, "research-progress-label.children", {
        "research-progress-poll.n_intervals": 1, "research-progress-token.data": token,
    }, "research-progress-poll.n_intervals")
    assert polled.status_code == 200
    assert polled.json["response"]["research-progress-label"]["children"] == "Searching the advertising database"
    other_session = app.server.test_client()
    hidden = post_callback(app, other_session, "research-progress-label.children", {
        "research-progress-poll.n_intervals": 1, "research-progress-token.data": token,
    }, "research-progress-poll.n_intervals")
    assert "Searching the advertising database" not in hidden.get_data(as_text=True)
    service.release.set()
    thread.join(5)
    assert not thread.is_alive()
    assert response[0].status_code == 200
    assert "private" not in response[0].get_data(as_text=True)
    finished = post_callback(app, client, "research-progress-label.children", {
        "research-progress-poll.n_intervals": 2, "research-progress-token.data": token,
    }, "research-progress-poll.n_intervals")
    assert finished.json["response"]["research-progress-label"]["children"] == "Preparing your search"
    dependencies = client.get("/_dash-dependencies").json
    answer_callback = next(item for item in dependencies if "research-results.children" in item["output"])
    assert answer_callback["running"]["runningOff"]["research-progress.style"] == {"display": "none"}
    assert answer_callback["running"]["runningOff"]["research-progress-poll.disabled"] is True
