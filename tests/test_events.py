from pathlib import Path

import pytest
from fastapi.testclient import TestClient

SAMPLE = Path(__file__).resolve().parent.parent / "tasker" / "sample_phone_log.jsonl"
SECRET = "test-secret"
H = {"X-Jarvis-Secret": SECRET}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_SHARED_SECRET", SECRET)
    monkeypatch.setenv("JARVIS_DB_PATH", str(tmp_path / "t.db"))
    from jarvis.app import app

    with TestClient(app) as c:
        yield c


def count(client):
    return client.app.state.db.execute("SELECT COUNT(*) FROM events").fetchone()[0]


EV = {"ts": 1794270606, "event": "app_changed", "app": "com.instagram.android", "batt": 65}


def test_requires_secret(client):
    assert client.post("/event", json=EV).status_code == 401
    assert client.post("/event", json=EV, headers={"X-Jarvis-Secret": "nope"}).status_code == 401
    assert count(client) == 0


def test_store_and_dedupe(client):
    r = client.post("/event", json=EV, headers=H)
    assert r.status_code == 200 and r.json() == {"stored": True, "duplicate": False}
    r = client.post("/event", json=EV, headers=H)
    assert r.json() == {"stored": False, "duplicate": True}
    # Same event 1s later (the double-unlock case) is NOT an exact duplicate.
    client.post("/event", json={**EV, "ts": EV["ts"] + 1}, headers=H)
    assert count(client) == 2


def test_body_without_json_content_type(client):
    r = client.post(
        "/event",
        content='{"ts":1,"event":"screen_off","app":"","batt":50}',
        headers={**H, "Content-Type": "text/plain"},
    )
    assert r.status_code == 200 and r.json()["stored"]


def test_validation(client):
    assert client.post("/event", content="not json", headers=H).status_code == 400
    assert client.post("/event", json={"event": "x"}, headers=H).status_code == 422
    assert client.post("/event", json={"ts": 1}, headers=H).status_code == 422


def test_unset_tasker_var_normalized(client):
    client.post("/event", json={**EV, "app": "%app_package"}, headers=H)
    row = client.app.state.db.execute("SELECT app FROM events").fetchone()
    assert row["app"] == ""


def test_extra_fields_kept(client):
    client.post("/event", json={"ts": 5, "event": "charging", "batt": 40, "plug": "usb"}, headers=H)
    row = client.app.state.db.execute("SELECT extra FROM events").fetchone()
    assert row["extra"] == '{"plug":"usb"}'


def test_importer_idempotent_and_merges_with_http(client):
    from jarvis.importer import import_file

    conn = client.app.state.db
    lines = [l for l in SAMPLE.read_text().splitlines() if l.strip()]
    s1 = import_file(conn, SAMPLE)
    assert s1["bad"] == 0
    assert s1["stored"] + s1["duplicate"] == len(lines)
    s2 = import_file(conn, SAMPLE)
    assert s2 == {"stored": 0, "duplicate": len(lines), "bad": 0}
    # An event that already arrived via HTTP is a duplicate on import.
    assert client.post("/event", json=EV, headers=H).json()["duplicate"]


def test_importer_skips_bad_lines(client, tmp_path):
    from jarvis.importer import import_file

    p = tmp_path / "log.jsonl"
    p.write_text('{"ts":1,"event":"screen_off","app":"","batt":5}\ngarbage\n\n{"ts":2}\n')
    assert import_file(client.app.state.db, p) == {"stored": 1, "duplicate": 0, "bad": 2}


def test_sessions_endpoint(client):
    from jarvis.importer import import_file

    assert client.get("/sessions?day=2026-11-10").status_code == 401
    import_file(client.app.state.db, SAMPLE)
    r = client.get("/sessions?day=2026-11-10", headers=H)
    assert r.status_code == 200
    body = r.json()
    assert any(s["name"] == "Telegram" for s in body["sessions"])
    assert "Totals:" in body["summary"]
    assert client.get("/sessions?day=bad", headers=H).status_code == 422
