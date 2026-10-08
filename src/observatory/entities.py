"""Shared organization and outlet identities over exact source values.

The registry maps source spellings (``exxonmobil`` in native ads, ``ExxonMobil``
in social posts) to one organization. Source values are never rewritten: a
caller expands an organization back into the exact spellings it filters on.
Every identity is ``ai_proposed`` until a named person reviews it, and the
registry hash is reported wherever a mapping changes a result.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

REGISTRY_PATH = Path(__file__).with_name("entity_registry.v1.json")
TYPES = {"company", "trade_association", "event"}
_FIELDS = {"sponsor": "sponsors", "publisher": "publishers"}
_LATIN = re.compile(r"[0-9A-Za-z]")


def normalize(value: str) -> str:
    """Case-, accent- and punctuation-insensitive key; a leading "the" is ignored."""
    text = unicodedata.normalize("NFKC", str(value)).casefold().strip()
    text = re.sub(r"[\s\-_.,'’&()]+", " ", text).strip()
    return re.sub(r"^the ", "", text)


@dataclass(frozen=True)
class Entity:
    id: str
    kind: str  # "organization" or "outlet"
    name: str
    type: str
    aliases: tuple[str, ...]
    former_names: tuple[str, ...]
    source_values: dict
    review_status: str
    basis: str
    record_counts: tuple = ()

    def surface_forms(self):
        values = [v for vals in self.source_values.values() for v in vals]
        return (self.name, *self.aliases, *self.former_names, *values)

    def values_for(self, field: str, datasets=("native", "social")):
        return [v for d in datasets for v in self.source_values.get(f"{d}.{field}", [])]

    def public(self):
        return {"id": self.id, "name": self.name, "type": self.type,
                "review_status": self.review_status, "registry_version": registry().version}


class Registry:
    def __init__(self, data: dict, raw: bytes):
        self.version = "entity-registry-v1:" + hashlib.sha256(raw).hexdigest()[:16]
        self.review = data.get("review", {})
        self.entities: dict[str, Entity] = {}
        self._by_source: dict[tuple[str, str, str], Entity] = {}
        self._by_key: dict[tuple[str, str], Entity] = {}
        for kind, items in (("organization", data["organizations"]), ("outlet", data["outlets"])):
            field = "sponsor" if kind == "organization" else "publisher"
            for item in items:
                entity = Entity(
                    id=item["id"], kind=kind, name=item["name"], type=item.get("type", "outlet"),
                    aliases=tuple(item.get("aliases", ())), former_names=tuple(item.get("former_names", ())),
                    source_values={k: tuple(v) for k, v in item["source_values"].items()},
                    review_status=item.get("review_status", "ai_proposed"), basis=item.get("basis", ""),
                    record_counts=tuple(sorted(item.get("record_counts", {}).items())))
                if entity.id in self.entities:
                    raise ValueError(f"Duplicate entity id {entity.id}")
                if kind == "organization" and entity.type not in TYPES:
                    raise ValueError(f"Unknown organization type for {entity.id}")
                self.entities[entity.id] = entity
                for key, values in entity.source_values.items():
                    dataset, source_field = key.split(".")
                    if source_field != field:
                        raise ValueError(f"{entity.id} lists a {source_field} value")
                    for value in values:
                        if (dataset, field, value) in self._by_source:
                            raise ValueError(f"Source value {value!r} belongs to two entities")
                        self._by_source[(dataset, field, value)] = entity
                for form in entity.surface_forms():
                    key = (field, normalize(form))
                    if not key[1]:
                        continue
                    other = self._by_key.get(key)
                    if other is not None and other.id != entity.id:
                        raise ValueError(f"Name {form!r} is shared by {other.id} and {entity.id}")
                    self._by_key[key] = entity
        self.supplemented = {(f["dataset"], f["record_id"], f["field"]): f
                             for f in data.get("supplemented_values", [])}
        self.out_of_scope = {(r["dataset"], r["record_id"]): r for r in data.get("out_of_scope_records", [])}
        for fill in self.supplemented.values():
            if fill["organization"] not in self.entities:
                raise ValueError("A supplemented value names an unknown organization")

    def for_source(self, dataset: str, field: str, value: str) -> Entity | None:
        return self._by_source.get((dataset, field, value))

    def owner(self, value: str, field: str) -> Entity | None:
        """Entity that lists ``value`` as one of its exact source spellings."""
        return self.for_source("native", field, value) or self.for_source("social", field, value)

    def resolve(self, name: str, field: str) -> Entity | None:
        """The one entity whose name, alias, former name or source value equals ``name``."""
        return self._by_key.get((field, normalize(name)))

    def entity_of(self, value: str, field: str) -> Entity | None:
        """Entity for an exact source value in any collection, else for a name."""
        for dataset in ("native", "social"):
            entity = self.for_source(dataset, field, value)
            if entity:
                return entity
        return self.resolve(value, field)


@lru_cache(maxsize=1)
def registry() -> Registry:
    raw = REGISTRY_PATH.read_bytes()
    return Registry(json.loads(raw.decode("utf-8")), raw)


def field_of(dimension: str) -> str | None:
    """Map a filter dimension ("sponsors") to a registry field ("sponsor")."""
    for field, name in _FIELDS.items():
        if dimension in (field, name):
            return field
    return None


def _occurs(form: str, question: str) -> bool:
    """Literal occurrence with word boundaries around Latin text.

    Short all-capital forms (BP, API, XOM) must match their exact case so that
    ordinary words never count as a mention. CJK forms have no word boundaries.
    """
    if not form.strip():
        return False
    exact_case = len(form) <= 4 and form.isupper()
    haystack, needle = (question, form) if exact_case else (question.casefold(), form.casefold())
    start = haystack.find(needle)
    while start >= 0:
        end = start + len(needle)
        left = start == 0 or not (_LATIN.match(needle[0]) and _LATIN.match(haystack[start - 1]))
        right = end == len(haystack) or not (_LATIN.match(needle[-1]) and _LATIN.match(haystack[end]))
        if left and right:
            return True
        start = haystack.find(needle, start + 1)
    return False


def mentioned_in(value: str, question: str, dimension: str) -> bool:
    """Whether the question names the entity that owns ``value`` by any recorded form."""
    field = field_of(dimension)
    entity = registry().entity_of(value, field) if field else None
    forms = entity.surface_forms() if entity else (value,)
    return any(_occurs(form, question) for form in forms)


def expand(value: str, dimension: str, known: set[str]) -> tuple[Entity | None, list[str]]:
    """Exact source values in ``known`` for the entity named by ``value``.

    Unknown names return ``(None, [])`` so existing ambiguity rules still apply.
    """
    field = field_of(dimension)
    entity = registry().entity_of(value, field) if field else None
    if entity is None:
        return None, []
    return entity, sorted(v for v in entity.values_for(field) if v in known)
