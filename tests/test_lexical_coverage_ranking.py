"""Fresh independent ferry/library SQL ranking cases, never customer questions."""

import hashlib
import json
from collections import Counter
from pathlib import Path
from uuid import uuid4

import pytest
from pgvector.psycopg import register_vector
from postgres_fixtures import configured_test_url, verified_test_connection
from psycopg import sql

from observatory.db import Database
from observatory.import_records import CanonicalRecord
from observatory.models import Filters, ImportBatch, RecordInput
from observatory.social_admission import _admit_group

ROOT = Path(__file__).resolve().parents[1] / '.runtime/accuracy_followup_20261007/retrieval'


def native(identifier, body, **metadata):
    return RecordInput(record_id=identifier, dataset='native', title='Fixture ' + identifier,
        url='https://ferry.example.invalid/' + identifier, body=body,
        retrievable=metadata.pop('retrievable', True),
        publisher=metadata.pop('publisher', 'Harbor Bulletin'), **metadata)


def social_fixture(number, body, accounts):
    url = f'https://twitter.com/schoolferry/status/{number}'
    members = []
    for offset, account in enumerate(accounts, 1):
        source = CanonicalRecord(record_id=f'junkipedia:{number + offset}', dataset='social',
            platform='Twitter', url=url, body=body, account=account, countable=False,
            retrievable=False, raw={'body_sha256': hashlib.sha256(body.encode()).hexdigest(),
                                    'source_row': {'post_text': body}})
        members.append((offset, source))
    admitted, _ = _admit_group(members, url, 'a' * 64,
        {'data_sha256': 'b' * 64, 'selections': {'D02': {'id': 'synthetic-D02'}, 'D03': {'id': 'synthetic-D03'}}})
    return admitted


def fixture_records():
    # Every four-word target is an explicitly authored fixture, not an expected
    # answer copied from a production query or a relevance model.
    records = [native(f'ferry-density-{i:02}', 'Ferry ' * 24) for i in range(60)]
    records += [
        native('ferry-target', 'Ferry harbor timetable boarding. ' + 'Notice ' * 180),
        native('ferry-secondary', 'Ferry harbor information.'),
        native('library-target', 'Library inventory cart repairs. ' + 'Memo ' * 160, publisher='School Desk'),
        native('library-density', 'Library ' * 30, publisher='School Desk'),
        native('stem-target', 'Boats dock beside a pier.'),
        native('stem-density', 'Boat boat boat boat boating boats.'),
        native('out-of-scope', 'Ferry harbor timetable boarding.', publisher='Other District'),
        native('not-countable', 'Ferry harbor timetable boarding.', countable=False),
        native('not-retrievable', 'Ferry harbor timetable boarding.', retrievable=False),
        native('withdrawn', 'Ferry harbor timetable boarding.'),
        native('old-version', 'Current notice: library chairs only.'),
        native('vector-lexical', 'Ferry harbor boarding.'),
        native('vector-shared', 'Ferry harbor.'),
        native('vector-only', 'Ceramic mugs and painted shelves.'),
        native('multi-a', ('Ferry harbor timetable boarding. ' + 'Notice ' * 300 + '\n\n') * 4),
        native('multi-b', ('Ferry harbor timetable boarding. ' + 'Notice ' * 300 + '\n\n') * 4),
        social_fixture(880000, 'School ferry harbor timetable boarding.', ['School Fleet']),
        social_fixture(890000, 'School ferry harbor timetable boarding.', ['West Fleet', 'East Fleet']),
        RecordInput(record_id='social-literal', dataset='social', title='Synthetic literal notice',
                    body='学校渡轮 时刻安排 已公布。 A saved marker reads the and.', retrievable=True),
    ]
    return records


@pytest.fixture(scope='module')
def sample():
    url = configured_test_url()
    schema = 'obs_lexical_' + uuid4().hex
    with verified_test_connection(url) as conn:
        conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))

    class ScopedDatabase(Database):
        def connect(self, vector=False):
            conn = verified_test_connection(self.url, schema)
            if vector:
                register_vector(conn)
            return conn

    db = ScopedDatabase(url)
    db.initialize()
    records = fixture_records()
    db.import_batch(ImportBatch(records=[native('old-version', 'Ferry harbor timetable boarding.')]))
    db.import_batch(ImportBatch(records=records))
    with db.connect() as conn:
        conn.execute("UPDATE records SET active=false WHERE record_id='withdrawn'")
    ROOT.mkdir(parents=True, exist_ok=True)
    (ROOT / (schema + '.json')).write_text(json.dumps({
        'schema': schema, 'records': [record.model_dump(mode='json') for record in records],
        'synthetic_only': True, 'preserved': True,
    }, ensure_ascii=False, indent=2), encoding='utf-8')
    db.audit_schema = schema
    db.audit_records = {record.record_id: record for record in records}
    return db


@pytest.mark.integration
def test_distinct_query_coverage_beats_sixty_repeated_single_word_candidates(sample):
    scope = ['ferry-target'] + [f'ferry-density-{i:02}' for i in range(60)]
    rows = sample.search('ferry harbor timetable boarding',
        Filters(dataset='native', record_ids=scope), limit=5)
    assert rows[0].record_id == 'ferry-target'
    assert len(rows) == 5
    assert all(sample.validate_evidence(row) for row in rows)


@pytest.mark.integration
def test_different_domain_coverage_beats_density(sample):
    rows = sample.search('library inventory cart repairs',
                         Filters(dataset='native', publishers=['School Desk']), limit=1)
    assert [row.record_id for row in rows] == ['library-target']


@pytest.mark.integration
def test_repeated_question_words_do_not_consume_the_distinct_term_budget(sample):
    filters = Filters(record_ids=['ferry-target', 'ferry-secondary'])
    rows = sample.search('ferry ' * 45 + 'harbor timetable boarding', filters, limit=1)
    assert rows[0].record_id == 'ferry-target'


@pytest.mark.integration
def test_natural_question_stopwords_and_keywords_have_same_first_target(sample):
    filters = Filters(record_ids=['ferry-target', 'ferry-secondary'])
    keyword = sample.search('ferry harbor timetable boarding', filters, limit=1)
    question = sample.search('What is the timetable for boarding the ferry at the harbor?', filters, limit=1)
    assert keyword[0].record_id == question[0].record_id == 'ferry-target'


@pytest.mark.integration
def test_stem_variants_do_not_buy_multiple_coverage_votes(sample):
    filters = Filters(record_ids=['stem-target', 'stem-density'])
    plain = sample.search('boat dock pier', filters, limit=2)
    repeated = sample.search('boat boats boating dock pier', filters, limit=2)
    assert [row.record_id for row in plain] == [row.record_id for row in repeated]
    assert repeated[0].record_id == 'stem-target'


@pytest.mark.integration
def test_or_recall_is_retained_when_one_requested_term_is_absent(sample):
    rows = sample.search_report('ferry telescopeunmatched', Filters(record_ids=['ferry-target']), limit=1)
    assert rows['evidence'][0].record_id == 'ferry-target'
    assert 'telescopeunmatched' in rows['diagnostics']['missing_from_scope']
    assert rows['diagnostics']['operator'] == 'OR'


@pytest.mark.integration
@pytest.mark.parametrize('query', ['the and is of', 'atlaszxq482071'])
def test_stopwords_and_no_match_return_empty_without_errors(sample, query):
    assert sample.search(query, Filters(dataset='native')) == []


@pytest.mark.integration
def test_explicit_scope_current_versions_and_eligibility_remain_enforced(sample):
    scope = ['ferry-target', 'not-countable', 'not-retrievable', 'withdrawn', 'old-version']
    rows = sample.search('ferry harbor timetable boarding', Filters(record_ids=scope), limit=10)
    assert {row.record_id for row in rows} == {'ferry-target'}
    assert all(row.dataset == 'native' and sample.validate_evidence(row) for row in rows)


@pytest.mark.integration
def test_account_disagreement_does_not_establish_requested_account(sample):
    good = next(record for record in sample.audit_records.values() if record.account == 'School Fleet')
    unknown = next(record for record in sample.audit_records.values()
        if record.raw.get('social_admission', {}).get('conflicting_fields') == ['account'])
    rows = sample.search('school ferry harbor timetable boarding',
                         Filters(dataset='social', accounts=['School Fleet']), limit=5)
    assert [row.record_id for row in rows] == [good.record_id]
    assert sample.search('school ferry harbor timetable boarding',
                        Filters(dataset='social', accounts=['West Fleet'])) == []
    assert unknown.account == ''


@pytest.mark.integration
def test_social_literal_fallback_and_source_offsets_remain_valid(sample):
    # English stop words produce no lexical hit, but an exact saved social
    # phrase remains eligible for the existing bounded literal fallback.
    rows = sample.search_report('the and',
        Filters(dataset='social', record_ids=['social-literal']), limit=5)
    assert rows['diagnostics']['operator'] == 'literal_phrase'
    assert rows['evidence'][0].record_id == 'social-literal'
    assert sample.validate_evidence(rows['evidence'][0])


@pytest.mark.integration
def test_nonenglish_saved_text_does_not_become_an_absence_claim(sample):
    rows = sample.search_report('学校渡轮 时刻安排',
        Filters(dataset='social', record_ids=['social-literal']), limit=5)
    assert rows['evidence'][0].record_id == 'social-literal'
    assert rows['diagnostics']['status'] == 'unavailable'
    assert rows['diagnostics']['missing_from_scope'] == []
    assert sample.validate_evidence(rows['evidence'][0])


@pytest.mark.integration
@pytest.mark.parametrize('chunks_per_record', [1, 2])
def test_articles_remain_distinct_with_bounded_passages(sample, chunks_per_record):
    rows = sample.search('ferry harbor timetable boarding',
        Filters(record_ids=['multi-a', 'multi-b']), limit=2, chunks_per_record=chunks_per_record)
    assert Counter(row.record_id for row in rows) == {'multi-a': chunks_per_record, 'multi-b': chunks_per_record}
    assert all(sample.validate_evidence(row) for row in rows)


@pytest.mark.integration
def test_supplied_synthetic_vectors_still_fuse_with_lexical_channel(sample):
    import numpy as np

    axes = {'vector-shared': [1.0, 0.0], 'vector-only': [0.8, 0.2], 'vector-lexical': [0.1, 0.9]}
    with sample.connect(vector=True) as conn:
        for record_id, axis in axes.items():
            hashes = conn.execute('SELECT DISTINCT text_hash FROM chunks WHERE record_id=%s', (record_id,)).fetchall()
            for row in hashes:
                conn.execute('INSERT INTO embeddings(text_hash,model,embedding) VALUES(%s,%s,%s) '
                    'ON CONFLICT(text_hash,model) DO NOTHING',
                    (row['text_hash'], 'synthetic-local-vector', np.array(axis + [0.0] * 1534)))
    rows = sample.search('ferry harbor boarding', Filters(record_ids=list(axes)), limit=3,
        vector=[1.0] + [0.0] * 1535, model='synthetic-local-vector')
    assert rows[0].record_id == 'vector-shared'
    assert rows[0].retrieval_sources == ['keyword', 'vector']
    assert set(row.record_id for row in rows) == set(axes)
    assert next(row for row in rows if row.record_id == 'vector-only').retrieval_sources == ['vector']
    assert all(sample.validate_evidence(row) for row in rows)
