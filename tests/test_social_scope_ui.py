"""Source posts must not acquire paid-ad claims or private fields on export."""
import csv
from io import StringIO

import pytest
from test_social_label_ui import export, refresh, values
from test_social_label_ui import source_app as _source_app

from observatory.app import _public_rows, _summary


@pytest.fixture
def scoped_app():
    return _source_app.__wrapped__()


def test_social_export_preserves_selection_and_explicit_source_scope(scoped_app):
    selection = values()
    shown = refresh(scoped_app, selection)
    result = export(scoped_app, selection, shown)['social-download']['data']
    rows = list(csv.DictReader(StringIO(result['content'])))
    assert result['filename'] == 'social-company-posts.csv'
    assert len(rows) == len(scoped_app[2].social)
    assert all(r['paid_ad_status'] == 'not_verified' for r in rows)
    assert all(r['collection_scope'] == 'collected_company_posts' for r in rows)
    assert all(r['count_unit'] == 'source_record' for r in rows)
    assert 'company_affiliation' in rows[0] and 'sponsor' not in rows[0]


def test_public_scope_projection_keeps_unique_unit_only_when_supplied():
    source = {'dataset': 'social', 'record_id': 'post', 'version_id': 'v',
              'collection_scope': 'collected_company_posts',
              'count_unit': 'platform_canonical_post_url',
              'raw': {'secret': 'private source payload'}, 'paid_ad_status': 'verified'}
    row = _public_rows([source], False)[0]
    assert row['count_unit'] == 'platform_canonical_post_url'
    assert row['paid_ad_status'] == 'not_verified'
    assert 'raw' not in row and 'private source payload' not in str(row)
    unknown = _public_rows([{'dataset': 'social'}], False)[0]
    assert unknown['count_unit'] == 'source_record'
    native = _public_rows([{'dataset': 'native'}], False)[0]
    assert 'paid_ad_status' not in native and 'collection_scope' not in native


def test_post_summary_labels_counting_unit_without_claiming_paid_ads():
    text = str([child.children for card in _summary({'total': 36182, 'retrievable': 36163,
                                                   'unknown_dates': 0}, 'social') for child in card.children])
    assert 'Unique posts' in text and 'source rows are preserved' in text
    assert 'paid ads' not in text
