from types import SimpleNamespace

import pytest

from observatory.config import Settings
from observatory.rag import Rag


@pytest.mark.parametrize("profile", [None, "sentence600-v1"])
def test_index_embeds_only_repository_selected_profile(profile):
    requested = []
    calls = []

    def pending(model, *, profile_id):
        requested.append((model, profile_id))
        return [{"text": "New candidate evidence."}, {"text": "Another candidate."}]

    rag = Rag(SimpleNamespace(pending_embeddings=pending), Settings())
    rag.embed = lambda texts, *, visitor: calls.append((texts, visitor))
    result = rag.index(batch_size=1, profile_id=profile, visitor="bounded-release")

    assert requested == [("text-embedding-3-small", profile)]
    assert calls == [(["New candidate evidence."], "bounded-release"),
                     (["Another candidate."], "bounded-release")]
    assert result["embedded_chunks"] == 2
    assert result["profile"] == (profile or "active")


def test_index_with_complete_cache_dispatches_no_request():
    rag = Rag(SimpleNamespace(pending_embeddings=lambda *args, **kwargs: []), Settings())

    def unexpected(*args, **kwargs):
        raise AssertionError("A complete profile must not make an API call")

    rag.embed = unexpected
    assert rag.index()["embedded_chunks"] == 0
