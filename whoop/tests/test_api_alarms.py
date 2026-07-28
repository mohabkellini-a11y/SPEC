"""Alarm API tests.

The contract these pin: an alarm the strap did not accept must never look like
one it did. Every response carries `ok` and a sentence, and the alarm's status
string says whether it is really on the strap.
"""

from __future__ import annotations

import importlib
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ble.transport import FailureKind, SendResult  # noqa: E402


class FakeTransport:
    name = "fake"

    def __init__(self, ok: bool = True, kind: FailureKind | None = None):
        self.ok = ok
        self.kind = kind
        self.sent: list[bytes] = []

    @property
    def available(self) -> bool:
        return True

    async def send(self, frame: bytes, *, expect_reply: bool = False) -> SendResult:
        self.sent.append(frame)
        return SendResult(ok=self.ok, frame_hex=frame.hex(),
                          kind=None if self.ok else (self.kind or FailureKind.UNKNOWN))


def make_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                transport=None) -> tuple[TestClient, object]:
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "app.sqlite3"))
    monkeypatch.setenv("NOOP_DB_PATH", "")
    monkeypatch.setenv("DISPLAY_TZ", "UTC")
    import app.config
    import app.routes_journal
    import app.routes_alarms
    import app.main
    importlib.reload(app.config)
    importlib.reload(app.routes_journal)
    importlib.reload(app.routes_alarms)
    importlib.reload(app.main)

    if transport is not None:
        app.routes_alarms.transport = transport
        sched = app.routes_alarms.get_scheduler()
        sched.transport = transport
    return TestClient(app.main.app), app.routes_alarms


@pytest.fixture
def working(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    transport = FakeTransport(ok=True)
    client, _mod = make_client(tmp_path, monkeypatch, transport)
    with client:
        yield client, transport


@pytest.fixture
def broken(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    transport = FakeTransport(ok=False, kind=FailureKind.CLAIMED)
    client, _mod = make_client(tmp_path, monkeypatch, transport)
    with client:
        yield client, transport


# --- creating ---------------------------------------------------------------


def test_create_an_alarm_by_offset(working):
    client, transport = working
    res = client.post("/api/alarms", json={"in_seconds": 1200, "kind": "timer",
                                           "label": "Nap"})
    assert res.status_code == 201
    body = res.json()
    assert body["ok"] is True
    assert body["armed"] is True
    assert body["alarm"]["status"] == "On the strap"
    assert body["alarm"]["will_survive_shutdown"] is True
    assert "even if this machine is off" in body["message"]
    assert len(transport.sent) == 1


def test_create_an_alarm_by_absolute_time(working):
    client, _ = working
    when = int(time.time()) + 3600
    body = client.post("/api/alarms", json={"fire_at": when}).json()
    assert body["alarm"]["fire_at"] == when
    assert body["alarm"]["fire_at_iso"].startswith("20")


def test_a_timer_is_just_an_alarm_at_now_plus_n(working):
    client, transport = working
    before = int(time.time())
    body = client.post("/api/alarms", json={"in_seconds": 600, "kind": "timer"}).json()
    assert before + 600 <= body["alarm"]["fire_at"] <= before + 602

    from app.ble import packets as pk
    import struct
    payload = pk.decode_frame(transport.sent[-1]).payload
    assert struct.unpack_from("<I", payload, 4)[0] == body["alarm"]["fire_at"]


def test_times_in_the_past_are_refused(working):
    client, _ = working
    assert client.post("/api/alarms", json={"fire_at": int(time.time()) - 60}).status_code == 422


def test_times_too_far_ahead_are_refused(working):
    client, _ = working
    far = int(time.time()) + 60 * 60 * 24 * 400
    assert client.post("/api/alarms", json={"fire_at": far}).status_code == 422


def test_an_alarm_needs_a_time(working):
    client, _ = working
    assert client.post("/api/alarms", json={"label": "when?"}).status_code == 422


# --- failure is loud --------------------------------------------------------


def test_an_alarm_that_could_not_be_set_says_so_plainly(broken):
    """The brief: 'I need to know if an alarm did NOT get set.'"""
    client, _ = broken
    res = client.post("/api/alarms", json={"in_seconds": 1200, "label": "Wake"})
    assert res.status_code == 201            # it IS scheduled...
    body = res.json()
    assert body["ok"] is False               # ...but not on the strap
    assert body["armed"] is False
    assert "Another app is holding the strap" in body["message"]
    assert body["alarm"]["on_strap"] is False
    assert body["alarm"]["will_survive_shutdown"] is False


def test_a_failed_alarm_never_claims_to_be_on_the_strap(broken):
    client, _ = broken
    client.post("/api/alarms", json={"in_seconds": 1200})
    listing = client.get("/api/alarms").json()
    assert listing["armed_alarm_id"] is None
    assert all(not a["on_strap"] for a in listing["alarms"])


def test_retry_after_a_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    transport = FakeTransport(ok=False, kind=FailureKind.NOT_FOUND)
    client, _mod = make_client(tmp_path, monkeypatch, transport)
    with client:
        created = client.post("/api/alarms", json={"in_seconds": 1200}).json()
        assert created["ok"] is False

        transport.ok = True                  # strap comes back
        retried = client.post(f"/api/alarms/{created['alarm']['id']}/retry").json()
        assert retried["ok"] is True
        assert retried["alarm"]["status"] == "On the strap"


def test_retrying_a_past_alarm_is_refused(working):
    client, _ = working
    created = client.post("/api/alarms", json={"in_seconds": 10}).json()
    sched_store = None
    import app.routes_alarms as ra
    sched_store = ra.get_scheduler().store
    sched_store.update_alarm(created["alarm"]["id"], fire_at=int(time.time()) - 5)
    res = client.post(f"/api/alarms/{created['alarm']['id']}/retry")
    assert res.status_code == 400


# --- the one-alarm limit ----------------------------------------------------


def test_only_the_next_alarm_is_on_the_strap(working):
    client, _ = working
    client.post("/api/alarms", json={"in_seconds": 7200, "label": "later"})
    second = client.post("/api/alarms", json={"in_seconds": 600, "label": "sooner"}).json()

    assert second["armed"] is True
    assert second["displaced"]

    listing = client.get("/api/alarms").json()
    by_label = {a["label"]: a for a in listing["alarms"]}
    assert by_label["sooner"]["on_strap"] is True
    assert by_label["later"]["on_strap"] is False
    assert "not on the strap" in by_label["later"]["status"].lower()
    assert "one alarm at a time" in listing["one_alarm_limit"]


def test_queued_alarms_explain_that_they_need_the_dashboard_running(working):
    client, _ = working
    client.post("/api/alarms", json={"in_seconds": 600})
    client.post("/api/alarms", json={"in_seconds": 7200, "label": "later"})
    later = next(a for a in client.get("/api/alarms").json()["alarms"]
                 if a["label"] == "later")
    assert "dashboard running" in later["note"]


# --- editing and cancelling -------------------------------------------------


def test_edit_the_time_and_re_arm(working):
    client, transport = working
    created = client.post("/api/alarms", json={"in_seconds": 600}).json()
    sent_before = len(transport.sent)

    new_time = int(time.time()) + 4000
    res = client.patch(f"/api/alarms/{created['alarm']['id']}",
                       json={"fire_at": new_time}).json()
    assert res["alarm"]["fire_at"] == new_time
    assert res["ok"] is True
    assert len(transport.sent) > sent_before
    assert "may still be set on the strap" in res["warning"]


def test_edit_the_label_only(working):
    client, _ = working
    created = client.post("/api/alarms", json={"in_seconds": 600}).json()
    res = client.patch(f"/api/alarms/{created['alarm']['id']}",
                       json={"label": "Renamed"}).json()
    assert res["alarm"]["label"] == "Renamed"
    assert res["warning"] is None


def test_cancelling_an_armed_alarm_warns_it_may_still_buzz(working):
    client, _ = working
    created = client.post("/api/alarms", json={"in_seconds": 1200}).json()
    body = client.delete(f"/api/alarms/{created['alarm']['id']}").json()

    assert body["cancelled"] is True
    assert body["was_on_strap"] is True
    assert body["strap_may_still_fire"] is True
    assert "no verified command to un-set it" in body["warning"]


def test_cancelling_a_queued_alarm_has_no_warning(working):
    client, _ = working
    client.post("/api/alarms", json={"in_seconds": 600})
    queued = client.post("/api/alarms", json={"in_seconds": 7200}).json()
    body = client.delete(f"/api/alarms/{queued['alarm']['id']}").json()
    assert body["strap_may_still_fire"] is False
    assert body["warning"] is None


def test_cancelling_promotes_the_next_alarm(working):
    client, _ = working
    first = client.post("/api/alarms", json={"in_seconds": 600}).json()
    client.post("/api/alarms", json={"in_seconds": 7200, "label": "next"})
    client.delete(f"/api/alarms/{first['alarm']['id']}")

    listing = client.get("/api/alarms").json()
    promoted = next(a for a in listing["alarms"] if a["label"] == "next")
    assert promoted["on_strap"] is True


def test_cancelled_alarms_are_hidden_unless_asked_for(working):
    client, _ = working
    created = client.post("/api/alarms", json={"in_seconds": 600}).json()
    client.delete(f"/api/alarms/{created['alarm']['id']}")

    assert client.get("/api/alarms").json()["alarms"] == []
    assert client.get("/api/alarms?include_done=true").json()["alarms"]


def test_unknown_alarm_ids_are_404(working):
    client, _ = working
    assert client.delete("/api/alarms/9999").status_code == 404
    assert client.patch("/api/alarms/9999", json={"label": "x"}).status_code == 404
    assert client.post("/api/alarms/9999/retry").status_code == 404


# --- strap state ------------------------------------------------------------


def test_strap_state_reports_the_never_hold_policy(working):
    client, _ = working
    body = client.get("/api/strap/state").json()
    assert body["state"] == "idle"
    assert "disconnects" in body["connection_policy"]
    assert "one device at a time" in body["connection_policy"]


def test_strap_state_without_an_address_explains_what_to_do(tmp_path: Path,
                                                            monkeypatch: pytest.MonkeyPatch):
    client, _mod = make_client(tmp_path, monkeypatch, transport=None)
    with client:
        body = client.get("/api/strap/state").json()
        assert body["available"] is False
        assert body["configured_address"] is None
        assert "bench_strap" in body["hint"]


def test_strap_test_endpoint_sets_a_throwaway_alarm(working):
    client, transport = working
    body = client.post("/api/strap/test?seconds=20").json()
    assert body["ok"] is True
    assert body["alarm"]["label"] == "Connection test"
    assert "Wear the strap" in body["reminder"]
    assert len(transport.sent) == 1


def test_strap_test_reports_failure_honestly(broken):
    client, _ = broken
    body = client.post("/api/strap/test?seconds=20").json()
    assert body["ok"] is False
    assert "Another app is holding the strap" in body["message"]


# --- status and persistence -------------------------------------------------


def test_status_shows_the_scheduler_is_running(working):
    client, _ = working
    body = client.get("/api/alarms/status").json()
    assert body["running"] is True
    assert body["strap"]["connection_policy"]


def test_alarms_survive_a_restart(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The requirement: a pending alarm survives a reboot."""
    transport = FakeTransport(ok=True)
    client, _mod = make_client(tmp_path, monkeypatch, transport)
    with client:
        created = client.post("/api/alarms", json={"in_seconds": 3600,
                                                   "label": "Survivor"}).json()
    alarm_id = created["alarm"]["id"]

    # Same database, brand-new process-equivalent.
    transport2 = FakeTransport(ok=True)
    client2, _mod2 = make_client(tmp_path, monkeypatch, transport2)
    with client2:
        listing = client2.get("/api/alarms").json()
        assert [a["id"] for a in listing["alarms"]] == [alarm_id]
        assert listing["alarms"][0]["label"] == "Survivor"
        assert listing["alarms"][0]["on_strap"] is True


def test_health_reports_the_alarm_table(working):
    client, _ = working
    client.post("/api/alarms", json={"in_seconds": 600})
    body = client.get("/api/health").json()
    assert body["phase"] == 4
    assert body["app_db"]["alarms"] == 1


def test_queued_is_not_reported_as_a_failure(working):
    """Waiting behind an earlier alarm is normal. Crying wolf here would teach
    you to ignore the warning that matters."""
    client, _ = working
    client.post("/api/alarms", json={"in_seconds": 600, "label": "sooner"})
    later = client.post("/api/alarms", json={"in_seconds": 7200, "label": "later"}).json()

    assert later["armed"] is False
    assert later["queued"] is True
    assert later["ok"] is True                       # not an error
    assert "one alarm at a time" in later["message"]
    assert "NOT set" not in later["message"]


def test_a_genuine_failure_is_still_not_queued(broken):
    client, _ = broken
    body = client.post("/api/alarms", json={"in_seconds": 600}).json()
    assert body["armed"] is False
    assert body["queued"] is False
    assert body["ok"] is False
