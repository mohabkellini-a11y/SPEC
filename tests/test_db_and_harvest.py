"""DB upsert idempotency and an offline end-to-end harvest."""

from __future__ import annotations

from datetime import date

import httpx
import respx
from sqlmodel import Session, select

from eversafe_leads import db as dbmod
from eversafe_leads.adapters.socrata import SocrataAdapter
from eversafe_leads.harvest import harvest_jurisdiction
from eversafe_leads.models import Party, Permit, PermitStub


def _mem_engine():
    engine = dbmod.get_engine(":memory:")
    dbmod.init_db(engine)
    return engine


def test_upsert_is_idempotent(orlando_config, fire_rows):
    engine = _mem_engine()
    adapter = SocrataAdapter("orlando", orlando_config)
    with Session(engine) as session:
        juris = dbmod.get_or_create_jurisdiction(
            session,
            slug="orlando",
            display_name="City of Orlando",
            platform="socrata",
            base_url="https://data.cityoforlando.net",
        )
        row = fire_rows[0]
        stub = PermitStub(record_number=row["permit_number"], source_url="x", raw_payload=row)
        detail = adapter.fetch_detail(stub)

        _p1, created1 = dbmod.upsert_permit(session, juris.id, detail)
        _p2, created2 = dbmod.upsert_permit(session, juris.id, detail)

        assert created1 is True
        assert created2 is False  # second time updates, does not duplicate
        assert len(session.exec(select(Permit)).all()) == 1


@respx.mock
def test_harvest_end_to_end_offline(orlando_config, fire_rows, tmp_path):
    respx.get("https://data.cityoforlando.net/resource/ryhf-m453.json").mock(
        side_effect=[httpx.Response(200, json=fire_rows), httpx.Response(200, json=[])]
    )
    engine = dbmod.get_engine(tmp_path / "leads.db")
    dbmod.init_db(engine)

    result = harvest_jurisdiction("orlando", date(2026, 6, 1), date(2026, 7, 22), engine)

    assert result.ok
    assert result.seen == len(fire_rows)
    assert result.created == len(fire_rows)
    assert result.fire_related == len(fire_rows)
    assert not result.errors

    with Session(engine) as session:
        permits = session.exec(select(Permit)).all()
        assert len(permits) == len(fire_rows)
        # Contractor parties persisted for the fire permits.
        parties = session.exec(select(Party)).all()
        assert any("WAYNE AUTOMATIC" in p.raw_name for p in parties)
