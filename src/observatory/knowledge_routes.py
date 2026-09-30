"""Read-only, bounded graph exports using the same filters as the dashboard."""

from flask import jsonify, request
from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError

from .models import Filters


class GraphRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    filters: Filters = Field(default_factory=Filters)
    offset: StrictInt = Field(default=0, ge=0)
    limit: StrictInt = Field(default=5, ge=1, le=20)


def register_knowledge_routes(server, service, record_details=None):
    @server.get("/api/knowledge-graph/schema")
    def knowledge_schema():
        from .knowledge_graph import build_graph

        graph = build_graph([], links_enabled=False)
        return jsonify({key: value for key, value in graph.items()
                        if key not in {"nodes", "edges", "warnings"}})

    @server.post("/api/knowledge-graph")
    def knowledge_export():
        try:
            payload = GraphRequest.model_validate(request.get_json(silent=True))
            if payload.filters.dataset != "native":
                raise ValueError("Unsupported collection")
        except (ValueError, TypeError, ValidationError):
            return jsonify({"error": "Use native collection filters, offset >= 0 and limit 1–20."}), 400
        try:
            graph = service.knowledge_graph(payload.filters, offset=payload.offset,
                                            limit=payload.limit, record_details=record_details)
        except Exception:
            return jsonify({"error": "The knowledge graph is temporarily unavailable."}), 503
        response = jsonify(graph)
        response.headers["Cache-Control"] = "no-store"
        return response
