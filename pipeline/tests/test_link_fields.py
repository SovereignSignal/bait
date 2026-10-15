"""Shared link-field cases: publishing rules must agree across Python and the web."""
import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from bait.links import web_url, github_url, x_handle

CASES = json.loads((Path(__file__).resolve().parents[2] / 'web/fixtures/link-fields.json').read_text())

@pytest.mark.parametrize('case', CASES)
def test_link_fields(case):
    assert bool(web_url(case['value'])) == case['web']
    assert bool(github_url(case['value'])) == case['github']
    assert x_handle(case['value']) == case['x']


def test_enrichment_replaces_invalid_stored_fields_with_valid_new_candidates():
    from bait.steps.enrich import what_values
    p = SimpleNamespace(website='javascript:alert(1)', github='no repository', x_handle='No X handle identified')
    out = what_values(p, {'what_line': 'Example', 'category': 'other'}, {'parallel': {
        'website': 'https://example.org', 'github': 'https://github.com/Uniswap/v4-core', 'x_handle': 'https://x.com/Uniswap'}})
    assert out['website'] == 'https://example.org'
    assert out['github'] == 'https://github.com/Uniswap/v4-core'
    assert out['x_handle'] == 'Uniswap'
