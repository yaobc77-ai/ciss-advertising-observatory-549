"""Read-only smoke check of client metadata questions. Never calls a model."""

import json
from pathlib import Path

from observatory.config import Settings
from observatory.models import Filters
from observatory.service import Service


class NoModel:
    def __getattr__(self, name):
        raise AssertionError(f"No paid or retrieval component may run: {name}")


def main():
    # Historical deterministic baseline; the deployed model-first mode has a
    # separate paid interpretation check and must not silently change this test.
    from dataclasses import replace

    service = Service(replace(Settings.from_env(), research_agent_enabled=False), rag=NoModel())
    questions = [
        "How many native ads are from the New York Times?",
        "Which publishers is ExxonMobil working with?",
        "Which fossil fuel companies has the Washington Post worked with?",
        "How many native ads are from the New York Times in 2021?",
        "How many records match the current filters?",
        "How many native ads contain greenwashing claims?",
    ]
    before = service.health()
    results = []
    for question in questions:
        result = service.answer(question, Filters(), "local-readonly-client-check")
        data = result.structured_result or {}
        results.append({
            "question": question, "status": result.status, "mode": result.answer_mode,
            "answer": result.answer, "cost_usd": result.cost_usd,
            "filters": data.get("filters"), "collections": data.get("collections"),
            "groups": data.get("groups"), "shown_record_count": len(data.get("records", [])),
        })
        assert result.cost_usd == 0
    assert results[0]["collections"][0]["total"] == 19
    assert sum(g["count"] for g in results[1]["groups"]) == 15
    assert len(results[1]["groups"]) == 4
    assert sum(g["count"] for g in results[2]["groups"]) == 18
    assert len(results[2]["groups"]) == 7
    after = service.health()
    assert before["data_version"] == after["data_version"]
    payload = {"status": "passed", "no_model_calls": True,
               "data_version": after["data_version"], "results": results}
    destination = Path("reports/michelle_statistics_smoke_20260929.json")
    destination.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
