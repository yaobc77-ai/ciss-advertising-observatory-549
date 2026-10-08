# Organization identities

The application uses a versioned organization registry to connect registered company and outlet spellings across collections. Exact source values are preserved. Matching a registered alias expands it only within the current selection; similar unregistered names do not silently merge.

The registry contains organization types, aliases, former names, source spellings and review status. The current registry is **AI-proposed**, with no named human approval. Its content hash identifies the mapping version used by a result.

`entities.py` loads the adjacent `entity_registry.v1.json`. Duplicate IDs, ambiguous aliases and incompatible source mappings are rejected. Query tools and graph views use this shared identity layer.

The registry also records one proposed sponsor supplement and records flagged as outside the intended subject scope. The supplement is version-bound and appears as a separate graph relation; it does not replace the original field or enter SQL counts. Flagged records remain counted until a separate scope decision is made. Stored counts in the registry are audit metadata, not live collection totals.

The optional question interpreter remains disabled by default. Engineering checks do not establish semantic accuracy, client approval, or a live deployment.
