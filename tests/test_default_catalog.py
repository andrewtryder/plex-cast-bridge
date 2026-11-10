"""New installations should discover their own Cast destinations."""

import json
from pathlib import Path


def test_default_catalog_has_no_example_destinations():
    catalog = json.loads((Path(__file__).resolve().parents[1] / 'catalog.json').read_text())
    assert catalog['speakers'] == []
    assert catalog['inputs'] == []
