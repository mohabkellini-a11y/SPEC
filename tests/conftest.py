import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def orlando_config() -> dict:
    return {
        "platform": "socrata",
        "display_name": "City of Orlando",
        "base_url": "https://data.cityoforlando.net",
        "timezone": "America/New_York",
        "rate_limit": {"min_interval_seconds": 0, "jitter_seconds": 0},
        "socrata": {
            "dataset_id": "ryhf-m453",
            "date_field": "processed_date",
            "fire_worktypes": ["FireSupp", "FA"],
            "exclude_worktypes": ["Roof", "Fence", "Sign", "Pool"],
        },
    }


@pytest.fixture
def fire_rows() -> list[dict]:
    with (FIXTURES / "orlando_socrata_fire_permits.json").open() as fh:
        return json.load(fh)
