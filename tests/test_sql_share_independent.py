"""New synthetic SQL fixtures; expected counts are directly enumerated here."""
from datetime import date
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pgvector.psycopg import register_vector
from postgres_fixtures import configured_test_url, verified_test_connection
from psycopg import sql

from observatory.date_inference import apply_inferences, propose_url_inferences
from observatory.db import Database
from observatory.models import Filters, ImportBatch, RecordInput
from observatory.service import Service


@pytest.fixture(scope='module')
def sample():
    url = configured_test_url()
    schema = 'obs_sql_share_' + uuid4().hex[:12]
    with verified_test_connection(url) as conn:
        conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))

    class SyntheticDB(Database):
        def connect(self, vector=False):
            conn = verified_test_connection(self.url, schema=schema)
            if vector:
                register_vector(conn)
            return conn

    db = SyntheticDB(url)
    db.initialize()
    entries = [
        # Source missing; explicit URL supplies an unreviewed 2042 date.
        RecordInput(record_id='share-estimated', dataset='native', sponsor='maple',
                    publisher='North Bulletin', title='Synthetic estimated article',
                    url='https://north.example/2042/06/09/article'),
        RecordInput(record_id='share-old', dataset='native', sponsor='maple',
                    publisher='North Bulletin', title='Synthetic dated article',
                    published_at=date(2040, 1, 1)),
        RecordInput(record_id='share-missing', dataset='native', sponsor='cedar',
                    title='Synthetic undated article'),
        RecordInput(record_id='share-post', dataset='social', sponsor='maple',
                    title='Synthetic company post', published_at=date(2041, 1, 1)),
        RecordInput(record_id='share-excluded', dataset='native', sponsor='maple',
                    countable=False, title='Not admitted to statistics'),
    ]
    db.import_batch(ImportBatch(records=entries))
    with db.connect() as conn:
        assert apply_inferences(conn, propose_url_inferences(conn)) == 1
    service = Service(SimpleNamespace(show_source_links=True), db=db, rag=object())
    return db, service


def share(service, base, target):
    plan = SimpleNamespace(kind='share', filters=target, denominator_filters=base,
                           trusted_filters=base, denominator_basis='current_selection_before_question_targets',
                           group_by=None, scope_notes=())
    return service._share_statistics_answer(plan).structured_result


@pytest.mark.integration
def test_share_unknown_dates_use_the_same_effective_date_as_dashboard(sample):
    db, service = sample
    base = Filters(include_inferred_dates=True)
    target = base.model_copy(update={'sponsors': ['maple']})
    result = share(service, base, target)['collections'][0]
    assert result['numerator'] == 2 and result['denominator'] == 3
    assert result['unknown_dates'] == db.dashboard(target)['stats']['unknown_dates'] == 0


@pytest.mark.integration
def test_share_sources_show_used_date_basis_and_order(sample):
    _, service = sample
    base = Filters(include_inferred_dates=True)
    result = share(service, base, base.model_copy(update={'sponsors': ['maple']}))
    first, second = result['records']
    assert first['record_id'] == 'share-estimated'
    assert first['date'] is None  # Never overwrite the source date.
    assert first['effective_date'] == '2042-06-09'
    assert first['date_basis'] == 'inferred:url_path'
    assert first['inferred_tier'] == 'A'
    assert second['record_id'] == 'share-old'


@pytest.mark.integration
def test_share_source_only_retains_unknown_and_counts_unretrievable(sample):
    _, service = sample
    base = Filters(include_inferred_dates=False)
    target = base.model_copy(update={'sponsors': ['maple']})
    result = share(service, base, target)['collections'][0]
    assert result['numerator'] == 2 and result['denominator'] == 3
    assert result['unknown_dates'] == 1
    assert result['retrievable'] == 0
    assert result['percentage'] == pytest.approx(200 / 3)


@pytest.mark.integration
def test_share_both_collections_keep_units_and_denominators_separate(sample):
    _, service = sample
    base = Filters(dataset='all', include_inferred_dates=False)
    rows = share(service, base, base.model_copy(update={'sponsors': ['maple']}))['collections']
    assert [(r['dataset'], r['numerator'], r['denominator']) for r in rows] == [
        ('native', 2, 3), ('social', 1, 1)]


@pytest.mark.integration
def test_share_empty_denominator_is_undefined(sample):
    _, service = sample
    base = Filters(publishers=['No matching synthetic outlet'])
    row = share(service, base, base)['collections'][0]
    assert row['numerator'] == row['denominator'] == 0
    assert row['percentage'] is None
    assert row['percentage_status'] == 'empty_selection'
