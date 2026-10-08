"""Production transport/scope safeguards, without a database or paid model."""

import asyncio
from types import SimpleNamespace

import pytest

from observatory.config import Settings
from observatory.models import Filters
from observatory.research_tools import ToolCatalog
from observatory.service import Service


class MediaService:
    def __init__(self, *, drift=False):
        self.calls = []
        self.health_reads = 0
        self.drift = drift

    def health(self):
        self.health_reads += 1
        return {"status": "ok", "countable_record_counts": {"native": 2},
                "data_version": "changed" if self.drift and self.health_reads > 1 else "frozen"}

    def facets(self, dataset):
        return {"publishers": ["Outlet"], "sponsors": [], "platforms": [],
                "accounts": [], "keywords": [], "labels": []}

    def media_evidence(self, filters, **arguments):
        self.calls.append((filters.model_copy(deep=True), arguments))
        return {"status": "missing_material", "candidates": [], "model_calls": 0}


def test_mcp_exposes_media_but_body_only_web_citations_do_not():
    catalog = ToolCatalog(MediaService(), Filters(dataset="native"))
    assert "get_media_evidence" in {item["name"] for item in catalog.mcp_definitions()}
    assert "get_media_evidence" not in {item["name"] for item in catalog.definitions()}


@pytest.mark.parametrize("argument", [{"path": "C:/private/file"}, {"url": "https://example.org"},
                                      {"version_id": "a" * 64}, {"limit": True}, {"limit": 6},
                                      {"media_types": ["audio"]}])
def test_media_arguments_cannot_choose_source_paths_or_unbounded_reads(argument):
    service = MediaService()
    result = ToolCatalog(service, Filters(dataset="native")).call(
        "get_media_evidence", {"query": "hydrogen", **argument})
    assert result["status"] == "invalid_request"
    assert not service.calls and service.health_reads == 0


def test_media_request_retains_trusted_scope_and_rejects_expansion():
    service = MediaService()
    catalog = ToolCatalog(service, Filters(dataset="native", publishers=["Outlet"], record_ids=["r1"]))
    result = catalog.call("get_media_evidence", {"query": "hydrogen", "media_types": ["image"], "limit": 2})
    assert result["status"] == "missing_material" and result["data_version"] == "frozen"
    filters, arguments = service.calls[0]
    assert filters.publishers == ["Outlet"] and filters.record_ids == ["r1"]
    assert arguments == {"query": "hydrogen", "media_types": ["image"], "limit": 2}
    rejected = catalog.call("get_media_evidence", {"query": "hydrogen", "filters": {"record_ids": ["r2"]}})
    assert rejected["status"] == "clarify" and len(service.calls) == 1


def test_media_read_refuses_source_version_change():
    service = MediaService(drift=True)
    result = ToolCatalog(service, Filters(dataset="native")).call("get_media_evidence", {"query": "hydrogen"})
    assert result["status"] == "unavailable" and "changed" in result["message"]


def test_operator_media_settings_are_private(monkeypatch):
    monkeypatch.setattr("observatory.config.load_dotenv", lambda **_: None)
    monkeypatch.setenv("OBS_MEDIA_BUNDLE_PATH", "C:/private/frozen-bundle.json")
    monkeypatch.setenv("OBS_MEDIA_BUNDLE_SHA256", "b" * 64)
    monkeypatch.setenv("OBS_MEDIA_ASSET_ROOT", "C:/private/assets")
    settings = Settings.from_env()
    assert settings.media_bundle_path.endswith("frozen-bundle.json")
    assert settings.media_bundle_sha256 == "b" * 64
    assert settings.media_asset_root.endswith("assets")
    assert "C:/private" not in repr(settings) and "b" * 64 not in repr(settings)


def test_unconfigured_service_media_read_does_not_read_a_record():
    def forbidden_read(*_):
        raise AssertionError("Unconfigured media must not read records")

    service = Service(Settings(), db=SimpleNamespace(versioned_record=forbidden_read), rag=object())
    result = service.media_evidence(Filters(dataset="native"), query="hydrogen")
    assert result["status"] == "not_configured" and result["model_calls"] == 0


def test_official_mcp_protocol_keeps_media_missing_status_and_read_only_hint():
    mcp = pytest.importorskip("mcp")
    from observatory.mcp_server import build_mcp_server

    async def check():
        async with mcp.Client(build_mcp_server(ToolCatalog(MediaService(), Filters(dataset="native"))),
                              mode="legacy") as client:
            listed = await client.list_tools()
            definition = next(tool for tool in listed.tools if tool.name == "get_media_evidence")
            assert definition.annotations.read_only_hint is True
            assert definition.input_schema["additionalProperties"] is False
            result = await client.call_tool("get_media_evidence", {"query": "hydrogen", "media_types": ["video"]})
            assert result.structured_content["status"] == "missing_material"
            assert result.structured_content["model_calls"] == 0

    asyncio.run(check())
