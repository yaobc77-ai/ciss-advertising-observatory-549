from itertools import pairwise

import pytest
import tiktoken

from observatory.chunking import chunk_body


def test_unicode_offsets_token_limits_and_full_content_coverage():
    body = (
        "中文与🙂 emoji; café e\u0301; fossil fuels and energy. " * 120
    ) + "\n\nFinal paragraph."
    chunks = chunk_body(body, max_tokens=73, overlap_tokens=13)
    assert len(chunks) > 2
    encoding = tiktoken.get_encoding("cl100k_base")
    covered = set()
    for chunk in chunks:
        assert chunk["text"] == body[chunk["start"] : chunk["end"]]
        assert chunk["token_count"] == len(
            encoding.encode(chunk["text"], disallowed_special=())
        )
        assert 0 < chunk["token_count"] <= 73
        assert chunk["paragraph_ids"]
        covered.update(range(chunk["start"], chunk["end"]))
    assert all(i in covered for i, char in enumerate(body) if not char.isspace())
    assert all(
        a["start"] < b["start"] and a["end"] < b["end"] for a, b in pairwise(chunks)
    )
    assert all(b["start"] <= a["end"] for a, b in pairwise(chunks))


def test_paragraph_boundary_preferred_and_separator_preserved():
    first = "Alpha energy sentence. " * 20
    second = "Beta fuel sentence. " * 20
    body = first + "\r\n\r\n" + second
    chunks = chunk_body(body, max_tokens=100, overlap_tokens=15)
    assert chunks[0]["end"] == len(first)
    assert chunks[0]["paragraph_ids"] == ["p1"]
    assert chunks[-1]["end"] == len(body)
    assert any("p2" in item["paragraph_ids"] for item in chunks)


def test_separate_calls_cannot_mix_articles_and_special_tokens_are_literal():
    first = "Article A <|endoftext|> " * 50
    second = "Article B " * 50
    assert all(
        chunk["text"] == first[chunk["start"] : chunk["end"]]
        for chunk in chunk_body(first, 40, 5)
    )
    assert all("Article A" not in chunk["text"] for chunk in chunk_body(second, 40, 5))
    assert chunk_body(" \n\t") == []


@pytest.mark.parametrize("maximum,overlap", [(0, 0), (20, 20), (20, -1), (10, 15)])
def test_invalid_chunk_parameters_fail_explicitly(maximum, overlap):
    with pytest.raises(ValueError):
        chunk_body("content", maximum, overlap)
