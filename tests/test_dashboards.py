"""Tests for the analytics dashboards, simulator, comparisons, energy maths, reports and access control."""
from __future__ import annotations

import importlib.util
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.db.connection import get_db_connection, run_migrations
from app.main import app, create_login_session, create_password_hash
from app.services import analytics as A
from app.services import sensor_simulator as sim

FMT = "%Y-%m-%d %H:%M:%S"
PW = "DashTestPass123!"

T = {  # two tenants + an admin, ids chosen not to clash with other test modules
    "a": dict(user=9801, email="dash_a@test.local", client=9810, site=9811, bld=9812, floor=9813, room=9814, dev="dash_a_dev", mac="AA:BB:CC:98:0A:01", access=9815),
    "b": dict(user=9901, email="dash_b@test.local", client=9910, site=9911, bld=9912, floor=9913, room=9914, dev="dash_b_dev", mac="AA:BB:CC:98:0B:01", access=9915),
}
ADMIN_ID = 9700


def _ts(dt):
    return dt.strftime(FMT)


def _seed_user(conn, uid, email, role):
    pw = create_password_hash(PW)
    conn.execute("INSERT OR REPLACE INTO users (id,email,name,role,enabled,password_hash,password_salt) VALUES (?,?,?,?,1,?,?)",
                 (uid, email, "Dash Test", role, pw["password_hash"], pw["password_salt"]))


def _seed_tenant(conn, t):
    _seed_user(conn, t["user"], t["email"], "client")
    conn.execute("INSERT OR REPLACE INTO clients (id,name) VALUES (?,?)", (t["client"], f"Tenant {t['client']}"))
    conn.execute("INSERT OR REPLACE INTO sites (id,client_id,name) VALUES (?,?,'Site')", (t["site"], t["client"]))
    conn.execute("INSERT OR REPLACE INTO buildings (id,site_id,name) VALUES (?,?,'Bldg')", (t["bld"], t["site"]))
    conn.execute("INSERT OR REPLACE INTO floors (id,building_id,name,floor_number) VALUES (?,?,'Floor',1)", (t["floor"], t["bld"]))
    conn.execute("INSERT OR REPLACE INTO rooms (id,floor_id,room_name,polygon_points,x,y) VALUES (?,?,'Room','[]',0,0)", (t["room"], t["floor"]))
    conn.execute("INSERT OR REPLACE INTO devices (chip_mac,device_id,node_type,client_id,site_id,building_id,floor_id,room_id,is_placed) "
                 "VALUES (?,?,'environment',?,?,?,?,?,1)", (t["mac"], t["dev"], t["client"], t["site"], t["bld"], t["floor"], t["room"]))
    conn.execute("INSERT OR REPLACE INTO user_access (id,user_id,client_id,site_id,building_id,floor_id,access_level) VALUES (?,?,?,?,?,?,'view')",
                 (t["access"], t["user"], t["client"], t["site"], t["bld"], t["floor"]))
    now = datetime.now(timezone.utc)
    conn.execute("DELETE FROM historical_telemetry WHERE device_id=?", (t["dev"],))
    for i in range(48):  # two hours of 2.5-min samples, real ("live") data
        ts = now - timedelta(minutes=2.5 * (47 - i))
        v = {"temperature": 21.5, "humidity": 40, "co2": 600, "occupancy": 5, "power_w": 1000, "energy_kwh": 100 + i * 0.05}
        conn.execute("INSERT INTO historical_telemetry (device_id,telemetry,timestamp) VALUES (?,?,?)", (t["dev"], json.dumps(v), _ts(ts)))
    conn.execute("INSERT OR REPLACE INTO device_latest_telemetry (device_id,telemetry,alarm_active,alarm_message,updated_at) VALUES (?,?,0,'OK',?)",
                 (t["dev"], json.dumps(v), _ts(now)))


@pytest.fixture
def world(db_conn):
    _seed_user(db_conn, ADMIN_ID, "dash_admin@test.local", "admin")
    for t in T.values():
        _seed_tenant(db_conn, t)
    db_conn.commit()
    A._cache.clear()
    return db_conn


def _as(client, db_conn, uid):
    client.cookies.clear()
    client.cookies.set("sbms_session", create_login_session(db_conn, uid))
    return client


JSON = {"content-type": "application/json"}


# --------------------------------------------------------------- pure functions
def test_compare_never_divides_by_zero_or_invents_a_baseline():
    assert A.compare(None, 5) == {"available": False}
    assert A.compare(5, None) == {"available": False}
    z = A.compare(5, 0)
    assert z["available"] and z["diff"] == 5 and z["pct"] is None
    r = A.compare(120, 100)
    assert r["diff"] == 20 and r["pct"] == 20.0
    assert A.compare(0, 0)["diff"] == 0


def test_meter_delta_handles_reset_gaps_and_absurd_jumps():
    base = 1_700_000_000 // 3600 * 3600
    b = {base + i * 3600: v for i, v in enumerate([100, 101, 102.5, 5, 6, None, 8, 9_999_999])}
    hours = A.meter_hours(b, base)
    assert hours[base + 3600] == pytest.approx(1)
    assert hours[base + 2 * 3600] == pytest.approx(1.5)
    assert base + 3 * 3600 not in hours          # reset: no negative energy, no phantom energy
    assert hours[base + 4 * 3600] == pytest.approx(1)
    assert base + 5 * 3600 not in hours           # missing hour is unknown, not zero
    assert max(hours.values()) < 100              # absurd jump discarded


def test_missing_values_are_not_zero_and_not_normal():
    c = {"thresholds": A.DEFAULT_THRESHOLDS}
    assert A.threshold_status("co2", None, c) is None
    assert A.threshold_status("co2", 400, c) == "normal"
    assert A.threshold_status("co2", 5000, c) == "critical"
    assert A.threshold_status("temperature", 22, c) in ("normal", None)  # never "approaching" for comfort values


def test_resolve_period_validates_custom_ranges():
    s = datetime.now(timezone.utc) - timedelta(days=2)
    with pytest.raises(ValueError):
        A.resolve_period(None, s.isoformat(), (s - timedelta(days=1)).isoformat())
    with pytest.raises(ValueError):
        A.resolve_period(None, (s - timedelta(days=500)).isoformat(), s.isoformat())
    a, b, bucket, label = A.resolve_period(None, s.isoformat(), (s + timedelta(days=1)).isoformat())
    assert label == "custom" and b > a and bucket > 0


# --------------------------------------------------------------- simulator
def _tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DB_FILE", str(tmp_path / "sim.db"))
    run_migrations()


def test_simulator_is_inert_in_production_mode(tmp_path, monkeypatch):
    _tmp_db(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "PREVIEW_MODE", False)
    monkeypatch.setattr(settings, "SIMULATION_ENABLED", False)
    assert sim.simulation_enabled() is False
    assert sim.tick() == 0
    with sqlite3.connect(settings.DB_FILE) as c:
        assert c.execute("SELECT COUNT(*) FROM devices").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM historical_telemetry").fetchone()[0] == 0
    with get_db_connection() as conn, pytest.raises(RuntimeError, match="preview/simulation"):
        sim.ensure_demo_campus(conn)


def test_simulator_only_writes_simulated_devices_and_never_touches_real_ones(tmp_path, monkeypatch):
    _tmp_db(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "PREVIEW_MODE", False)
    monkeypatch.setattr(settings, "SIMULATION_ENABLED", True)
    conn = get_db_connection()
    conn.execute("INSERT INTO clients (id,name) VALUES (501,'Real client')")
    conn.execute("INSERT INTO sites (id,client_id,name) VALUES (501,501,'Real site')")
    conn.execute("INSERT INTO buildings (id,site_id,name) VALUES (501,501,'Real bld')")
    conn.execute("INSERT INTO devices (chip_mac,device_id,node_type,client_id,site_id,building_id,is_placed) VALUES ('AA:00:00:00:00:01','real-1','environment',501,501,501,0)")
    real = {"temperature": 19.5, "co2": 555}
    conn.execute("INSERT INTO historical_telemetry (device_id,telemetry,timestamp) VALUES ('real-1',?,?)", (json.dumps(real), _ts(datetime.now(timezone.utc))))
    conn.execute("INSERT INTO device_latest_telemetry (device_id,telemetry,alarm_active,alarm_message,updated_at) VALUES ('real-1',?,0,'OK',?)",
                 (json.dumps(real), _ts(datetime.now(timezone.utc))))
    conn.commit()
    before = conn.execute("SELECT COUNT(*) FROM historical_telemetry WHERE device_id='real-1'").fetchone()[0]

    info = sim.ensure_demo_campus(conn, backfill_days=1)
    conn.commit()
    assert info
    n = sim.tick()
    assert n > 0
    sim.thin_simulated_history()

    assert conn.execute("SELECT data_source FROM devices WHERE device_id='real-1'").fetchone()[0] == "live"
    assert conn.execute("SELECT COUNT(*) FROM historical_telemetry WHERE device_id='real-1'").fetchone()[0] == before
    assert json.loads(conn.execute("SELECT telemetry FROM device_latest_telemetry WHERE device_id='real-1'").fetchone()[0]) == real
    # everything the simulator created is labelled as such, and only those devices received new rows
    simdevs = {r[0] for r in conn.execute("SELECT device_id FROM devices WHERE data_source IN ('simulated','preview')")}
    assert simdevs
    written = {r[0] for r in conn.execute("SELECT DISTINCT device_id FROM historical_telemetry")} - {"real-1"}
    assert written <= simdevs
    # idempotent: a second call does not duplicate the demo campus
    count = conn.execute("SELECT COUNT(*) FROM devices").fetchone()[0]
    sim.ensure_demo_campus(conn, backfill_days=1)
    assert conn.execute("SELECT COUNT(*) FROM devices").fetchone()[0] == count
    conn.close()


def test_simulator_lease_allows_only_one_holder(tmp_path, monkeypatch):
    _tmp_db(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "SIMULATION_ENABLED", True)
    assert sim._acquire_lease(5.0) is True       # this process takes it
    monkeypatch.setattr(sim.os, "getpid", lambda: 10 ** 7)
    assert sim._acquire_lease(5.0) is False      # a second process must not


def test_safety_scenario_creates_simulated_alarms_that_auto_resolve(tmp_path, monkeypatch):
    _tmp_db(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "SIMULATION_ENABLED", True)
    conn = get_db_connection()
    sim.ensure_demo_campus(conn, backfill_days=1)
    conn.commit()
    sim.set_setting(conn, "sim_scenario", {"name": "safety_test", "until": (datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat()})
    conn.commit()
    sim.tick()
    rows = conn.execute("SELECT source, resolved FROM alarm_history WHERE alarm_type='smoke' AND source='simulated'").fetchall()
    assert rows and all(r[1] == 0 for r in rows)
    sim.set_setting(conn, "sim_scenario", None)
    conn.commit()
    sim.tick(datetime.now(timezone.utc) + timedelta(seconds=10))
    assert conn.execute("SELECT COUNT(*) FROM alarm_history WHERE alarm_type='smoke' AND source='simulated' AND resolved=0").fetchone()[0] == 0
    conn.close()


# --------------------------------------------------------------- migration
def test_migration_012_is_additive_and_repeatable(tmp_path, monkeypatch):
    _tmp_db(tmp_path, monkeypatch)
    with sqlite3.connect(settings.DB_FILE) as c:
        c.execute("INSERT INTO clients (id,name) VALUES (502,'keep me')")
        c.commit()
        path = Path(__file__).resolve().parents[1] / "app/db/migrations/012_dashboards.py"
        spec = importlib.util.spec_from_file_location("m012", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.up(c)  # running it again must not fail or lose data
        assert c.execute("SELECT name FROM clients WHERE id=502").fetchone()[0] == "keep me"
        cols = {r[1] for r in c.execute("PRAGMA table_info(devices)")}
        assert "data_source" in cols
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"dashboard_settings", "map_buildings", "map_rooms"} <= tables


def test_production_startup_creates_no_simulated_data(world):
    assert world.execute("SELECT COUNT(*) FROM devices WHERE data_source IN ('simulated','preview')").fetchone()[0] == 0


# --------------------------------------------------------------- access control
@pytest.mark.parametrize("path", ["/dashboards/overview", "/api/dashboards/overview", "/api/dashboards/meta", "/api/campus/map",
                                  "/api/reports/overview/pdf", "/api/rooms/1/telemetry"])
def test_unauthenticated_requests_are_rejected(client, path):
    client.cookies.clear()
    r = client.get(path, follow_redirects=False)
    assert r.status_code in (301, 302, 303, 307, 401, 403)
    if path.startswith("/api/"):
        assert r.status_code in (401, 403)


def test_client_user_sees_only_own_data(world, client):
    c = _as(client, world, T["a"]["user"])
    for section in ("overview", "energy", "environment", "occupancy", "safety"):
        r = c.get(f"/api/dashboards/{section}")
        assert r.status_code == 200, (section, r.text[:200])
        body = r.text
        assert T["b"]["dev"] not in body and f"Tenant {T['b']['client']}" not in body
    meta = c.get("/api/dashboards/meta").json()
    assert {f["floor_id"] for f in meta["floors"]} == {T["a"]["floor"]}
    assert {r["room_id"] for r in meta["rooms"]} == {T["a"]["room"]}


@pytest.mark.parametrize("path", [
    f"/api/dashboards/overview?building_id={T['b']['bld']}",
    f"/api/dashboards/energy?floor_id={T['b']['floor']}",
    f"/api/dashboards/safety?room_id={T['b']['room']}",
    f"/api/rooms/{T['b']['room']}/telemetry",
    f"/api/reports/overview/pdf?building_id={T['b']['bld']}",
])
def test_client_cannot_address_another_tenants_objects(world, client, path):
    r = _as(client, world, T["a"]["user"]).get(path)
    assert r.status_code in (403, 404), (path, r.status_code)


def test_client_cannot_change_settings_or_run_scenarios(world, client):
    c = _as(client, world, T["a"]["user"])
    assert c.put("/api/dashboards/settings", json={"freshness_seconds": 60}, headers=JSON).status_code == 403
    assert c.put(f"/api/dashboards/rooms/{T['a']['room']}/capacity", json={"capacity": 10}, headers=JSON).status_code == 403
    assert c.put(f"/api/dashboards/buildings/{T['a']['bld']}/marker", json={"x": .1, "y": .1}, headers=JSON).status_code == 403
    assert c.post("/api/dashboards/simulator/scenario", json={"name": "safety_test"}, headers=JSON).status_code == 403


def test_admin_settings_validation_and_audit(world, client):
    c = _as(client, world, ADMIN_ID)
    assert c.put("/api/dashboards/settings", json={"freshness_seconds": "x"}, headers=JSON).status_code == 422
    assert c.put("/api/dashboards/settings", json={"tariff": {"currency": "EURO"}}, headers=JSON).status_code == 422
    assert c.put("/api/dashboards/settings", json={"thresholds": {"co2": {"warn": 2000, "crit": 1000}}}, headers=JSON).status_code == 422
    assert c.put("/api/dashboards/settings", json={"unknown_key": 1}, headers=JSON).status_code == 422
    assert c.put("/api/dashboards/settings", content="freshness_seconds=60", headers={"content-type": "text/plain"}).status_code in (415, 422)
    r = c.put("/api/dashboards/settings", json={"tariff": {"currency": "EUR", "tariff_type": "flat", "rate_per_kwh": 0.25}}, headers=JSON)
    assert r.status_code == 200
    assert world.execute("SELECT COUNT(*) FROM audit_log WHERE action='update_dashboard_settings'").fetchone()[0] >= 1
    # simulator cannot be switched on from the UI in production mode
    assert c.put("/api/dashboards/settings", json={"sim_enabled": True}, headers=JSON).status_code == 409
    assert c.post("/api/dashboards/simulator/scenario", json={"name": "safety_test"}, headers=JSON).status_code == 409
    c.put("/api/dashboards/settings", json={"tariff": {"currency": "EUR", "tariff_type": "flat", "rate_per_kwh": None}}, headers=JSON)


def test_unconfigured_tariff_gives_no_cost_and_unconfigured_capacity_gives_no_utilisation(world, client):
    c = _as(client, world, ADMIN_ID)
    A._cache.clear()
    e = c.get(f"/api/dashboards/energy?room_id={T['a']['room']}").json()
    assert e["kpis"]["cost"]["value"] is None and "tariff" in e["kpis"]["cost"]["unavailable_reason"].lower()
    assert e["kpis"]["energy_kwh"]["value"] is not None
    o = c.get(f"/api/dashboards/occupancy?room_id={T['a']['room']}").json()
    rows = [r for r in o["by_room"] if r["room_id"] == T["a"]["room"]]
    assert rows and rows[0]["capacity"] is None and rows[0]["utilization"] is None  # never invented
    assert o["kpis"]["utilization"]["value"] is None and o["kpis"]["utilization"]["state"] == "capacity_not_configured"


def test_real_telemetry_is_labelled_live_and_missing_safety_is_not_normal(world, client):
    c = _as(client, world, T["a"]["user"])
    o = c.get(f"/api/dashboards/overview?room_id={T['a']['room']}").json()
    assert o["meta"]["source"] in ("live", "LIVE", "Live") or "LIVE" in json.dumps(o["meta"]).upper()
    assert "SIMULATED" not in json.dumps(o["meta"]).upper()
    cards = {x["key"]: x for x in o["safety"]["cards"]}
    for key in ("smoke", "leak"):
        assert cards[key]["state"] != "normal", "a device without a smoke/leak sensor must not show 'normal'"
    assert o["kpis"]["temperature"]["value"] == pytest.approx(21.5)
    assert o["kpis"]["water_lpm"]["value"] is None  # missing is None, never 0


def test_stale_data_is_flagged(world, client):
    old = _ts(datetime.now(timezone.utc) - timedelta(hours=3))
    world.execute("UPDATE device_latest_telemetry SET updated_at=? WHERE device_id=?", (old, T["a"]["dev"]))
    world.commit()
    A._cache.clear()
    o = _as(client, world, T["a"]["user"]).get(f"/api/dashboards/overview?room_id={T['a']['room']}").json()
    assert o["kpis"]["temperature"]["state"] in ("stale", "no_data")
    world.execute("UPDATE device_latest_telemetry SET updated_at=? WHERE device_id=?", (_ts(datetime.now(timezone.utc)), T["a"]["dev"]))
    world.commit()


# --------------------------------------------------------------- PDF reports
@pytest.mark.parametrize("kind", ["overview", "energy", "environment", "occupancy", "safety"])
def test_pdf_reports_are_valid_distinct_and_correctly_named(world, client, kind):
    r = _as(client, world, T["a"]["user"]).get(f"/api/reports/{kind}/pdf?room_id={T['a']['room']}&range=24h")
    assert r.status_code == 200
    assert r.content.startswith(b"%PDF-") and len(r.content) > 1500
    assert r.headers["content-type"] == "application/pdf"
    assert f"smart_building_{kind}_report_" in r.headers["content-disposition"] and r.headers["content-disposition"].count(".pdf") == 1


def test_pdf_reports_differ_by_type_and_show_the_title(world, client):
    pytest.importorskip("pypdf") if False else None
    c = _as(client, world, T["a"]["user"])
    sizes = {k: c.get(f"/api/reports/{k}/pdf?room_id={T['a']['room']}").content for k in ("overview", "energy", "environment", "occupancy", "safety")}
    assert len(set(sizes.values())) == 5
    try:
        from pypdf import PdfReader
    except ImportError:
        return
    import io
    for k, data in sizes.items():
        text = " ".join(p.extract_text() or "" for p in PdfReader(io.BytesIO(data)).pages).lower()
        assert k in text


def test_pdf_rejects_unknown_type_and_bad_filters(world, client):
    c = _as(client, world, T["a"]["user"])
    assert c.get("/api/reports/payroll/pdf").status_code == 404
    assert c.get("/api/reports/energy/pdf?building_id=abc").status_code == 422
    assert c.get("/api/reports/energy/pdf?start=2024-01-02T00:00:00Z").status_code == 422
    assert c.get("/api/reports/energy/pdf?start=2024-02-01T00:00:00Z&end=2024-01-01T00:00:00Z").status_code == 422


def test_pages_render_for_every_dashboard(world, client):
    c = _as(client, world, T["a"]["user"])
    for p in ("overview", "energy", "environment", "occupancy", "safety", "floor-plan", "reports", "settings"):
        assert c.get(f"/dashboards/{p}").status_code == 200
    assert c.get("/dashboards/nope").status_code == 404


def test_utilisation_is_occupancy_divided_by_configured_capacity(world, client):
    world.execute("INSERT OR REPLACE INTO map_rooms(room_id,room_number,label,room_type,capacity,capacity_source,polygon_json,geometry_source) "
                  "VALUES (?,?,?,?,?,?,?,?)", (T["a"]["room"], "X.1", "X1", "seminar", 10, "configured", "[]", "test"))
    world.commit()
    A._cache.clear()
    try:
        o = _as(client, world, T["a"]["user"]).get(f"/api/dashboards/occupancy?room_id={T['a']['room']}").json()
        row = next(r for r in o["by_room"] if r["room_id"] == T["a"]["room"])
        assert row["occupancy"] == 5 and row["capacity"] == 10 and row["utilization"] == 50.0
        assert o["kpis"]["utilization"]["value"] == 50.0
    finally:
        world.execute("DELETE FROM map_rooms WHERE room_id=?", (T["a"]["room"],))
        world.commit()
        A._cache.clear()


def test_startup_creates_demo_campus_when_simulation_enabled(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DB_FILE", str(tmp_path / "boot.db"))
    monkeypatch.setattr(settings, "PREVIEW_MODE", False)
    monkeypatch.setattr(settings, "SIMULATION_ENABLED", True)
    run_migrations()
    with TestClient(app):  # runs the lifespan
        pass
    with sqlite3.connect(settings.DB_FILE) as c:
        assert c.execute("SELECT COUNT(*) FROM devices WHERE data_source='simulated'").fetchone()[0] > 0


def test_demo_has_hvac_lighting_and_renewable_and_old_demo_db_is_upgraded(tmp_path, monkeypatch):
    _tmp_db(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "SIMULATION_ENABLED", True)
    conn = get_db_connection()
    sim.ensure_demo_campus(conn, backfill_days=2)
    # simulate a demo database created by the previous version: no new fields, old version marker
    conn.execute("UPDATE historical_telemetry SET telemetry=json_remove(telemetry,'$.hvac_on','$.light_on','$.solar_kwh')")
    conn.execute("DELETE FROM dashboard_settings WHERE key='demo_telemetry_version'")
    conn.execute("INSERT INTO clients (id,name) VALUES (601,'Real')")
    conn.execute("INSERT INTO devices (chip_mac,device_id,node_type,client_id,is_placed) VALUES ('AA:00:00:00:06:01','real-x','environment',601,0)")
    conn.execute("INSERT INTO historical_telemetry (device_id,telemetry,timestamp) VALUES ('real-x','{\"temperature\":20}',?)", (_ts(datetime.now(timezone.utc)),))
    conn.commit()
    sim.ensure_demo_campus(conn, backfill_days=2)   # runs the one-time upgrade
    assert conn.execute("SELECT COUNT(*) FROM historical_telemetry WHERE device_id='real-x'").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM historical_telemetry WHERE telemetry LIKE '%solar_kwh%'").fetchone()[0] > 0
    assert sim.get_setting(conn, "demo_telemetry_version") == sim.TELEMETRY_VERSION
    n = conn.execute("SELECT COUNT(*) FROM historical_telemetry").fetchone()[0]
    sim.ensure_demo_campus(conn, backfill_days=2)   # second call is a no-op
    assert conn.execute("SELECT COUNT(*) FROM historical_telemetry").fetchone()[0] == n
    conn.close()
    A._cache.clear()
    from app.services import dashboard_data as D
    with get_db_connection() as c2:
        user = {"id": 1, "role": "admin", "email": "x"}
        f = {"range": "24h"}
        o = D.build(c2, user, "overview", f)
        systems = {s["name"]: s for s in o["systems"]}
        assert systems["HVAC"]["state"] in ("normal", "warning") and "SIMULATED" in systems["HVAC"]["note"]
        assert systems["Lighting"]["state"] == "normal"
        e = D.build(c2, user, "energy", f)
        r = e["kpis"]["renewable"]
        assert r["value"] is not None and 0 <= r["value"] <= 100 and r["state"] == "simulated"


# --------------------------------------------------------------- threshold alarms + hidden provenance labels
def _sim_world(tmp_path, monkeypatch, days=2):
    _tmp_db(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "SIMULATION_ENABLED", True)
    conn = get_db_connection()
    sim.ensure_demo_campus(conn, backfill_days=days)
    return conn


def test_threshold_alarm_lifecycle_warning_escalation_peak_and_auto_resolve(tmp_path, monkeypatch):
    conn = _sim_world(tmp_path, monkeypatch, days=1)
    th = sim.thresholds(conn)
    dev = conn.execute("SELECT device_id FROM devices WHERE data_source='simulated' LIMIT 1").fetchone()[0]
    conn.execute("DELETE FROM alarm_history")
    t0 = datetime.now(timezone.utc)
    base = {"temperature": 22.0, "humidity": 45.0, "co2": 600, "pm25": 8.0, "smoke": 0, "leak": 0, "door_open": 0}

    def step(i, **kw):
        sim._raise_or_clear_alarms(conn, dev, {**base, **kw}, t0 + timedelta(minutes=i), th)
        return conn.execute("SELECT severity, actual_value_json, resolved, threshold_value FROM alarm_history WHERE device_id=? AND alarm_type='co2_threshold' "
                            "ORDER BY id DESC LIMIT 1", (dev,)).fetchone()

    assert step(0, co2=900) is None                                  # below the warning limit: nothing
    r = step(1, co2=th["co2"]["warn"] + 20)
    assert r["severity"] == "warning" and r["resolved"] == 0 and r["threshold_value"] == th["co2"]["warn"]
    r = step(2, co2=th["co2"]["crit"] + 50)                          # escalates in place, no second row
    assert r["severity"] == "critical" and r["threshold_value"] == th["co2"]["crit"]
    step(3, co2=th["co2"]["crit"] + 10)
    assert json.loads(step(4, co2=th["co2"]["warn"] + 5)["actual_value_json"]) == th["co2"]["crit"] + 50  # peak kept
    assert step(5, co2=th["co2"]["warn"] - 10)["resolved"] == 0      # inside hysteresis band: stays open
    assert step(6, co2=th["co2"]["warn"] - 100)["resolved"] == 1     # clearly back to normal: auto-resolved
    assert conn.execute("SELECT COUNT(*) FROM alarm_history WHERE alarm_type='co2_threshold'").fetchone()[0] == 1
    conn.close()


def test_simulated_history_crosses_configured_limits_and_creates_alarm_history(tmp_path, monkeypatch):
    conn = _sim_world(tmp_path, monkeypatch, days=3)
    rows = conn.execute("SELECT alarm_type, severity, resolved FROM alarm_history WHERE source='simulated'").fetchall()
    kinds = {r[0] for r in rows}
    assert {"co2_threshold", "temperature_threshold"} <= kinds
    assert any(r[1] == "critical" for r in rows) and any(r[1] == "warning" for r in rows)
    assert sum(1 for r in rows if r[2] == 1) > len(rows) / 2        # most episodes have already cleared
    # a lower configured limit is honoured by the generator (targets follow Settings)
    sim.set_setting(conn, "thresholds", {"co2": {"warn": 700, "crit": 900}})
    conn.commit()
    v = None
    dev = {"device_id": "x-1", "room_type": "seminar", "room_number": "X"}
    th = sim.thresholds(conn)
    for i in range(0, 9 * 3600, 300):
        v = sim.sample(dev, datetime(2026, 11, 2, 9, tzinfo=timezone.utc) + timedelta(seconds=i), None, None, th)
        if v["co2"] >= 700:
            break
    assert v["co2"] >= 700
    conn.close()


def test_hidden_labels_remove_every_simulated_or_demo_wording(tmp_path, monkeypatch):
    import io

    conn = _sim_world(tmp_path, monkeypatch, days=1)
    sim.set_setting(conn, "show_source_labels", False)
    conn.commit()
    names = " ".join(r[0] or "" for r in conn.execute(
        "SELECT label FROM devices UNION ALL SELECT room_name FROM rooms UNION ALL SELECT name FROM sites UNION ALL SELECT name FROM clients "
        "UNION ALL SELECT alarm_message FROM alarm_history"))
    assert "simulat" not in names.lower() and "demo" not in names.lower()
    A._cache.clear()
    from app.services import dashboard_data as D
    from app.services.reports import render_report
    user = {"id": 1, "role": "admin", "email": "a@b", "name": "Admin"}
    for kind in ("overview", "energy", "environment", "occupancy", "safety"):
        data = D.build(conn, user, kind, {"range": "24h"})
        assert data["meta"]["labels"] is False
        if "systems" in data:
            assert all("SIMULATED" not in (x.get("note") or "") for x in data["systems"])
        pdf = render_report(kind, data, {"range": "24h"}, {"floors": A.hierarchy(conn, user)}, user)
        try:
            from pypdf import PdfReader
        except ImportError:
            continue
        text = " ".join(p.extract_text() or "" for p in PdfReader(io.BytesIO(pdf)).pages).lower()
        assert "simulat" not in text and "demo" not in text and "data source" not in text, kind
    conn.close()


def test_labels_setting_is_admin_only_and_validated(world, client):
    c = _as(client, world, ADMIN_ID)
    assert c.put("/api/dashboards/settings", json={"show_source_labels": "no"}, headers=JSON).status_code == 422
    assert c.put("/api/dashboards/settings", json={"show_source_labels": False}, headers=JSON).status_code == 200
    A._cache.clear()
    assert c.get("/api/dashboards/meta").json()["settings"]["show_source_labels"] is False
    assert c.put("/api/dashboards/settings", json={"show_source_labels": True}, headers=JSON).status_code == 200
    assert _as(client, world, T["a"]["user"]).put("/api/dashboards/settings", json={"show_source_labels": False}, headers=JSON).status_code == 403
