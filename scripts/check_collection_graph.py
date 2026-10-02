"""Read-only real-corpus verification of the complete selected source map."""

import json
from pathlib import Path

from observatory.collection_graph_visual import map_elements, visible_map
from observatory.config import Settings
from observatory.knowledge_map import validate_collection_map
from observatory.models import Filters
from observatory.service import Service


class NoModel:
    def __getattr__(self, name):
        raise AssertionError(f"The source map must not call a paid component: {name}")


def main():
    service = Service(Settings.from_env(), rag=NoModel())
    before = service.health()
    selections = [
        ("complete", Filters()),
        ("exxonmobil", Filters(sponsors=["exxonmobil"])),
        ("washington_post", Filters(publishers=["The Washington Post"])),
        ("exxonmobil_at_washington_post", Filters(sponsors=["exxonmobil"], publishers=["The Washington Post"])),
        ("known_dates", Filters(include_unknown_dates=False)),
        ("empty", Filters(publishers=["unavailable-outlet"])),
    ]
    results = []
    for name, filters in selections:
        graph = service.knowledge_map(filters)
        validate_collection_map(graph)
        lookup = {node["id"]: node for node in graph["nodes"]}
        pairs = {
            (lookup[edge["source"]]["properties"]["source_value"],
             lookup[edge["target"]]["properties"]["source_value"]): edge["count"]
            for edge in graph["summary_edges"]
        }
        expected = {
            (item["sponsor"], item["publisher"]): item["count"]
            for item in service.statistics(filters)["relationships"]
            if item["sponsor"] != "(Unknown)" and item["publisher"] != "(Unknown)"
        }
        assert pairs == expected, f"Graph/matrix disagreement for {name}"
        assert sum(pairs.values()) == graph["coverage"]["associated_records"]
        assert len(map_elements(graph, "articles")) == len(graph["nodes"]) + len(graph["article_edges"])
        node_counts = {
            node["label"]: node["record_count"] for node in graph["nodes"] if node["type"] != "Article"
        }
        results.append({
            "selection": name, "filters": graph["filters"], "counts": graph["counts"],
            "coverage": graph["coverage"], "visible_entity_nodes": len(visible_map(graph)[0]),
            "node_record_counts": node_counts,
            "associations": [{"sponsor": sponsor, "outlet": outlet, "count": count}
                             for (sponsor, outlet), count in sorted(pairs.items())],
            "matrix_matches": True,
        })
    assert results[0]["counts"] == {"articles": 263, "sponsors": 19, "outlets": 8,
                                    "source_edges": 525, "summary_edges": 35}
    assert results[1]["counts"]["articles"] == 15 and results[1]["counts"]["outlets"] == 4
    assert results[2]["counts"]["articles"] == 18 and results[2]["counts"]["sponsors"] == 7
    assert results[3]["counts"]["articles"] == 5
    assert results[4]["counts"]["articles"] == 241
    assert results[5]["counts"]["articles"] == 0
    after = service.health()
    assert before["data_version"] == after["data_version"]
    payload = {"status": "passed", "no_model_calls": True, "source_data_unchanged": True,
               "data_version": after["data_version"], "active_profile": after["active_profile"],
               "results": results}
    Path("reports/collection_graph_smoke_20260929.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": "passed", "selections": len(results),
                      "complete_counts": results[0]["counts"], "no_model_calls": True}, indent=2))


if __name__ == "__main__":
    main()
