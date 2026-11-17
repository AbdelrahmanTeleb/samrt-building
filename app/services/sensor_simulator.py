"""Python demo-telemetry simulator (preview/demo mode only).

* Writes ONLY to devices whose devices.data_source = 'simulated'.
* Values are smooth deterministic functions of time plus a per-device phase, so
  history back-filled at seed time and live ticks form one continuous series.
* One task per process; a DB lease keeps additional worker processes from
  running a second simulator.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import zlib
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.config import settings
from app.db.connection import get_db_connection

log = logging.getLogger(__name__)
MAP_CONFIG = Path(__file__).resolve().parents[1] / "data" / "map_config.json"
TS_FMT = "%Y-%m-%d %H:%M:%S"  # UTC, same shape the real ingest path stores
LEASE_KEY = "simulator_lease"
_task_running = False

# Simulated peak head-count per room type (demo only; NOT a configured capacity).
PEAK = {"lecture_hall": 120, "seminar": 30, "canteen": 150, "demo": 12}


def fmt(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).strftime(TS_FMT)


def _seed(device_id: str) -> int:
    return zlib.crc32(device_id.encode())


def _wave(t: float, seed: int, period_s: float, amp: float) -> float:
    return amp * math.sin(2 * math.pi * t / period_s + (seed % 628) / 100)


def schedule(ts_local: datetime, seed: int) -> float:
    """0..1 'building activity' for a demo timetable (weekdays busier)."""
    h = ts_local.hour + ts_local.minute / 60
    wk = ts_local.weekday() < 5
    base = 0.0
    if wk and 8 <= h < 19:
        base = math.sin(math.pi * (h - 8) / 11) ** 0.7
        if 12 <= h < 13:
            base *= 0.55
    elif not wk and 10 <= h < 15:
        base = 0.12
    return max(0.0, min(1.0, base * (0.8 + 0.2 * ((seed >> 3) % 5) / 4)))


EPISODE_PERIOD_S = 5 * 3600     # each device has one excursion every ~5-8 h
EPISODE_LEN_S = 50 * 60         # lasting ~50 min (rise, peak, recovery)


def default_thresholds() -> dict:
    from app.services.analytics import DEFAULT_THRESHOLDS  # lazy: analytics imports this module
    return DEFAULT_THRESHOLDS


def thresholds(conn) -> dict:
    return {**default_thresholds(), **(get_setting(conn, "thresholds", {}) or {})}


def _episode(device: dict, t: float, seed: int, act: float, th: dict):
    """(metric, target, shape) while a reproducible excursion is under way, else None.

    The target is derived from the *configured* limits, so every episode crosses the
    warning limit and roughly one in three also crosses the critical limit. Which metric
    moves depends on the room: crowded teaching rooms -> CO2, kitchen/canteen -> PM2.5 or
    humidity, otherwise an HVAC fault -> room temperature.
    """
    period = EPISODE_PERIOD_S + (seed % 180) * 60
    k, phase = divmod(t + seed % period, period)
    if phase >= EPISODE_LEN_S:
        return None
    shape = math.sin(math.pi * phase / EPISODE_LEN_S)
    level = "crit" if (int(k) + seed) % 3 == 0 else "warn"
    room_type = device.get("room_type") or "demo"
    if room_type in ("lecture_hall", "seminar", "canteen"):
        if act < 0.2:          # people-driven excursions only happen while the room is in use
            return None
        metric = "co2" if room_type != "canteen" else ("pm25" if int(k) % 2 else "humidity")
    else:
        if int(k) % 2:         # HVAC faults are rarer: every other cycle
            return None
        metric = "temperature"
    limit = (th.get(metric) or {}).get(level)
    if limit is None:
        return None
    return metric, (limit + 0.8 if metric == "temperature" else limit * 1.06), shape


def sample(device: dict, ts: datetime, prev: dict | None, scenario: dict | None, th: dict | None = None) -> dict:
    """One simulated multi-sensor reading for `device` at `ts` (UTC)."""
    seed = _seed(device["device_id"])
    t = ts.timestamp()
    local = ts + timedelta(hours=1)  # demo timetable on ~Vienna wall time
    act = schedule(local, seed)
    peak = PEAK.get(device.get("room_type") or "demo", 12)
    occ = max(0, round(peak * act * (1 + _wave(t, seed, 7200, 0.12))))
    hh = local.hour + local.minute / 60
    day = max(0.0, math.sin(math.pi * (hh - 6) / 14)) if 6 <= hh < 20 else 0.0
    temp = 21.0 + 2.2 * day + 2.5 * act + _wave(t, seed, 5400, 0.35)
    hum = 46 - 0.8 * (temp - 22) + _wave(t, seed >> 2, 7200, 2.0)
    co2 = 430 + 780 * act * (0.45 + 0.55 * min(1.0, occ / max(peak, 1))) + _wave(t, seed, 9000, 18)
    pm25 = 6 + 4 * act + _wave(t, seed, 10800, 1.2)
    lux = 120 + 520 * day + (180 if act > 0.05 else 0) + _wave(t, seed, 9000, 15)
    power = 350 + 4200 * act * (peak / 60 if peak > 12 else 0.25) + 250 * day + _wave(t, seed, 5400, 40)
    water = max(0.0, 0.3 + 5.0 * act * (1.5 if device.get("room_type") == "canteen" else 0.4) + _wave(t, seed, 6000, 0.15))
    hvac_fault = 0
    ep = _episode(device, t, seed, act, th or default_thresholds())
    if ep:
        metric, target, shape = ep
        if metric == "temperature":
            temp += max(0.0, target - temp) * shape
            hvac_fault = 1
        elif metric == "co2":
            co2 += max(0.0, target - co2) * shape
        elif metric == "pm25":
            pm25 += max(0.0, target - pm25) * shape
        elif metric == "humidity":
            hum += max(0.0, target - hum) * shape
    out = {
        "temperature": round(temp, 1), "humidity": round(max(20, min(80, hum)), 1),
        "co2": int(co2), "pm25": round(max(1, pm25), 1), "pm10": round(max(2, pm25 * 1.6), 1),
        "lux": int(max(0, lux)), "occupancy": occ, "power_w": round(max(60, power), 0),
        "water_lpm": round(water, 2), "smoke": 0, "leak": 0, "gas": 0, "door_open": 0,
        "occupancy_kind": "estimate",
        # Demo-only building systems: HVAC runs when the room is used or warm; lights when occupied.
        "hvac_on": 0 if hvac_fault else (1 if (act > 0.05 or temp > 24.5) else 0), "hvac_fault": hvac_fault,
        "light_on": 1 if act > 0.05 else 0,
    }
    solar_w = round(max(0.0, power * 0.30 * day), 0)  # demo rooftop PV: up to ~30 % of the room load around noon
    # Cumulative meters continue from the previous reading using elapsed time.
    last_e = float((prev or {}).get("energy_kwh", 0.0) or 0.0)
    last_w = float((prev or {}).get("water_m3", 0.0) or 0.0)
    last_ts = (prev or {}).get("_ts")
    dt = max(0.0, min((ts - last_ts).total_seconds(), 3600.0)) if last_ts else 0.0
    out["energy_kwh"] = round(last_e + out["power_w"] / 1000 * dt / 3600, 4)
    out["solar_kwh"] = round(float((prev or {}).get("solar_kwh", 0.0) or 0.0) + solar_w / 1000 * dt / 3600, 4)
    out["water_m3"] = round(last_w + out["water_lpm"] / 1000 * dt / 60, 5)
    # Controlled, reproducible incidents (all flagged simulated).
    if scenario and scenario.get("until") and ts < scenario["until"]:
        if scenario["name"] == "safety_test" and device.get("room_number") == "01.305":
            out["smoke"] = 1
        if scenario["name"] == "safety_test" and device.get("room_number") == "01.634":
            out["co2"] = 1650
        if scenario["name"] == "leak_test" and device.get("room_type") == "canteen":
            out["leak"] = 1
    if device.get("room_number") == "01.625" and not (7 <= local.hour < 20) and local.minute < 4:
        out["door_open"] = 1
    return out


# ---------------------------------------------------------------- settings
def get_setting(conn, key, default=None):
    row = conn.execute("SELECT value FROM dashboard_settings WHERE key=?", (key,)).fetchone()
    if not row:
        return default
    try:
        return json.loads(row[0])
    except ValueError:
        return default


def set_setting(conn, key, value):
    conn.execute("INSERT INTO dashboard_settings(key,value,updated_at) VALUES(?,?,CURRENT_TIMESTAMP) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP",
                 (key, json.dumps(value)))


def simulation_enabled(conn=None) -> bool:
    """PREVIEW_MODE / SIMULATION_ENABLED gate everything; the UI switch can only pause it."""
    if not (settings.PREVIEW_MODE or settings.SIMULATION_ENABLED):
        return False
    if conn is None:
        with closing(get_db_connection()) as c:
            return bool(get_setting(c, "sim_enabled", True))
    return bool(get_setting(conn, "sim_enabled", True))


# ---------------------------------------------------------------- seeding
TELEMETRY_VERSION = 3  # 2: hvac/light/solar fields; 3: threshold excursions, alarm history, neutral names


def _backfill(conn, devices, backfill_days, alarm_days: int = 3):
    """Back-fill history (30-min steps, 10-min for the last `alarm_days`) and the matching alarm history."""
    th = thresholds(conn)
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    fine_from = now - timedelta(days=alarm_days)
    t = now - timedelta(days=backfill_days)
    prev_state = {d["device_id"]: None for d in devices}
    while t <= now:
        for d in devices:
            v = sample(d, t, prev_state[d["device_id"]], None, th)
            conn.execute("INSERT INTO historical_telemetry(device_id,telemetry,timestamp) VALUES (?,?,?)", (d["device_id"], json.dumps(v), fmt(t)))
            if t >= fine_from:
                _raise_or_clear_alarms(conn, d["device_id"], v, t, th)
            v["_ts"] = t
            prev_state[d["device_id"]] = v
        t += timedelta(minutes=10) if t >= fine_from else timedelta(minutes=30)
    for d in devices:
        v = {k: x for k, x in prev_state[d["device_id"]].items() if k != "_ts"}
        active = _active_alarms(conn, d["device_id"])
        conn.execute("INSERT INTO device_latest_telemetry(device_id,telemetry,alarm_active,alarm_message,updated_at) VALUES (?,?,?,?,?) "
                     "ON CONFLICT(device_id) DO UPDATE SET telemetry=excluded.telemetry, alarm_active=excluded.alarm_active, "
                     "alarm_message=excluded.alarm_message, updated_at=excluded.updated_at",
                     (d["device_id"], json.dumps(v), int(bool(active)), "Alarm active" if active else "OK", fmt(now)))


def ensure_demo_campus(conn, backfill_days: int = 14) -> dict:
    """Idempotently create the demo campus + simulated devices and back-fill history.

    Never touches existing rows. Refuses to run unless preview/simulation is on.
    """
    if not (settings.PREVIEW_MODE or settings.SIMULATION_ENABLED):
        raise RuntimeError("Demo data may only be created in preview/simulation mode")
    if conn.execute("SELECT 1 FROM dashboard_settings WHERE key='demo_campus_seeded'").fetchone():
        if get_setting(conn, "demo_telemetry_version", 1) < TELEMETRY_VERSION:
            _upgrade_simulated_history(conn, backfill_days)
        return {"seeded": False}
    cfg = json.loads(MAP_CONFIG.read_text())
    client = conn.execute("SELECT id FROM clients ORDER BY id LIMIT 1").fetchone()
    client_id = client[0] if client else conn.execute("INSERT INTO clients(name) VALUES ('University')").lastrowid
    camp, fp = cfg["campus"], cfg["floor_plan"]
    site_id = conn.execute(
        "INSERT INTO sites(client_id,name,campus_image_path,image_width,image_height) VALUES (?,?,?,?,?)",
        (client_id, "University Campus", camp["image"], camp["width"], camp["height"])).lastrowid
    conn.execute("INSERT INTO site_maps(site_id,image_path,image_width,image_height) VALUES (?,?,?,?)",
                 (site_id, camp["image"], camp["width"], camp["height"]))
    devices = []

    def add_device(name, building_id, building_name, floor_id, floor_name, room_id, room_name, room_type, rnum):
        did = f"sim-{(rnum or room_name).lower().replace('.', '').replace(' ', '-').replace('(', '').replace(')', '')}-{building_id}"
        n = len(devices)
        conn.execute(
            "INSERT INTO devices(chip_mac,device_id,dev_eui,node_type,room_id,label,status,last_seen,client_id,site_id,"
            "building_id,floor_id,building,floor,room,is_placed,x,y,data_source,configuration_status) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,0,0,'simulated','configured')",
            (f"5E:{n:02X}:00:00:00:00", did, f"5E{n:014X}", "environment", room_id, f"Multi-sensor {name}", "online",
             fmt(datetime.now(timezone.utc)), client_id, site_id, building_id, floor_id, building_name, floor_name, room_name))
        conn.execute("INSERT INTO device_capabilities(device_id,capability) VALUES (?, 'environment')", (did,))
        devices.append({"device_id": did, "room_type": room_type, "room_number": rnum})

    # Letter buildings printed on the supplied campus drawing: one demo room each.
    for b in camp["buildings"]:
        bid = conn.execute("INSERT INTO buildings(site_id,name,polygon_points,x,y) VALUES (?,?,?,?,?)",
                           (site_id, b["name"], "[]", int(b["x"] * camp["width"]), int(b["y"] * camp["height"]))).lastrowid
        conn.execute("INSERT INTO map_buildings(building_id,code,x,y,confirmed) VALUES (?,?,?,?,0)", (bid, b["code"], b["x"], b["y"]))
        fid = conn.execute("INSERT INTO floors(building_id,name,floor_number) VALUES (?,?,?)", (bid, "Floor 1", "1")).lastrowid
        rname = f"{b['code']}-101"
        rid = conn.execute("INSERT INTO rooms(floor_id,room_name,polygon_points,x,y) VALUES (?,?,?,?,?)", (fid, rname, "[]", 0, 0)).lastrowid
        add_device(f"{b['code']}-101", bid, b["name"], fid, "Floor 1", rid, rname, "demo", None)
    # The supplied architectural plan. Its position on the campus drawing is NOT
    # stated in the image, so this building has no campus marker until confirmed.
    omp = conn.execute("INSERT INTO buildings(site_id,name,polygon_points,x,y) VALUES (?,?,?,?,?)",
                       (site_id, "Oskar-Morgenstern-Platz 1", "[]", 0, 0)).lastrowid
    floor = conn.execute("INSERT INTO floors(building_id,name,floor_number,image_path,image_width,image_height) VALUES (?,?,?,?,?,?)",
                         (omp, "1 OG", "1", fp["image"], fp["width"], fp["height"])).lastrowid
    conn.execute("INSERT INTO floorplans(building,floor,floor_id,image_path,image_width,image_height) VALUES (?,?,?,?,?,?)",
                 ("Oskar-Morgenstern-Platz 1", "1 OG", floor, fp["image"], fp["width"], fp["height"]))
    for r in fp["rooms"]:
        poly_px = [{"x": int(x * fp["width"]), "y": int(y * fp["height"])} for x, y in r["polygon"]]
        rid = conn.execute("INSERT INTO rooms(floor_id,room_name,polygon_points,x,y) VALUES (?,?,?,?,?)",
                           (floor, f"{r['room_number']} {r['label']}", json.dumps(poly_px),
                            sum(p["x"] for p in poly_px) // len(poly_px), sum(p["y"] for p in poly_px) // len(poly_px))).lastrowid
        if r.get("capacity") is None:
            cap, src = {"lecture_hall": 150, "seminar": 40, "canteen": 200}.get(r["room_type"]), "demo"
        else:
            cap, src = r["capacity"], "configured"
        conn.execute("INSERT INTO map_rooms(room_id,room_number,label,room_type,capacity,capacity_source,polygon_json,geometry_source) VALUES (?,?,?,?,?,?,?,?)",
                     (rid, r["room_number"], r["label"], r["room_type"], cap, src, json.dumps(r["polygon"]), r.get("geometry_source")))
        add_device(f"{r['label']} ({r['room_number']})", omp, "Oskar-Morgenstern-Platz 1", floor, "1 OG", rid,
                   f"{r['room_number']} {r['label']}", r["room_type"], r["room_number"])
    _backfill(conn, devices, backfill_days)
    # The pre-existing preview seed devices are demo data too: label them (the simulator never writes to them).
    conn.execute("UPDATE devices SET data_source='preview' WHERE device_id LIKE 'demo-device-%' AND data_source='live'")
    set_setting(conn, "demo_campus_seeded", True)
    set_setting(conn, "demo_telemetry_version", TELEMETRY_VERSION)
    conn.commit()
    return {"seeded": True, "devices": len(devices)}


def _upgrade_simulated_history(conn, backfill_days):
    """Rewrite ONLY simulated devices' history with the current sample() fields. Real devices are never touched."""
    devs = _sim_devices(conn)
    if devs:
        ids = [d["device_id"] for d in devs]
        q = ",".join("?" * len(ids))
        conn.execute(f"DELETE FROM historical_telemetry WHERE device_id IN ({q})", ids)
        conn.execute(f"DELETE FROM alarm_history WHERE source='simulated' AND device_id IN ({q})", ids)
        # neutral display names (earlier versions printed SIMULATED/demo into names)
        conn.execute(f"UPDATE devices SET label='Multi-sensor ' || substr(label, 11) WHERE label LIKE 'SIMULATED %' AND device_id IN ({q})", ids)
        conn.execute(f"UPDATE devices SET room=replace(room, ' (demo room)', '') WHERE device_id IN ({q})", ids)
        conn.execute(f"UPDATE rooms SET room_name=replace(room_name, ' (demo room)', '') WHERE id IN (SELECT room_id FROM devices WHERE device_id IN ({q}))", ids)
        conn.execute("UPDATE sites SET name='University Campus' WHERE name='University Campus (demo)'")
        conn.execute("UPDATE clients SET name='University' WHERE name='Demo University'")
        _backfill(conn, devs, backfill_days)
    set_setting(conn, "demo_telemetry_version", TELEMETRY_VERSION)
    conn.commit()


# ---------------------------------------------------------------- live loop
def _sim_devices(conn):
    rows = conn.execute("SELECT d.device_id, d.room_id, m.room_type, m.room_number FROM devices d "
                        "LEFT JOIN map_rooms m ON m.room_id=d.room_id WHERE d.data_source='simulated'").fetchall()
    return [{"device_id": r[0], "room_id": r[1], "room_type": r[2] or "demo", "room_number": r[3]} for r in rows]


def _scenario(conn):
    s = get_setting(conn, "sim_scenario")
    if not s:
        return None
    try:
        return {"name": s["name"], "until": datetime.fromisoformat(s["until"])}
    except (KeyError, ValueError):
        return None


ANALOG_ALARMS = {
    # metric: (high warning text, high critical text, low text, unit, hysteresis)
    "co2": ("High CO₂ concentration", "Critical CO₂ concentration", None, "ppm", 30.0),
    "temperature": ("High room temperature", "Critical room temperature", "Low room temperature", "°C", 0.3),
    "humidity": ("High humidity", "Critical humidity", "Low humidity", "%", 1.0),
    "pm25": ("High PM2.5 particulate level", "Critical PM2.5 particulate level", None, "µg/m³", 1.0),
}
BINARY_ALARMS = (("smoke", "smoke", "critical", "Smoke detected"),
                 ("water_leak", "leak", "critical", "Water leak detected"),
                 ("door", "door_open", "warning", "Door open outside opening hours"))


def _active_alarms(conn, device_id) -> int:
    return conn.execute("SELECT COUNT(*) FROM alarm_history WHERE device_id=? AND resolved=0 AND source='simulated'", (device_id,)).fetchone()[0]


def _raise_or_clear_alarms(conn, device_id, v, now, th=None):
    """At most one open alarm per (device, type). Warning escalates to critical in place, the peak
    reading is kept, and the alarm auto-resolves once the value is back inside the limit (with a
    small hysteresis so it does not flap). Rows carry source='simulated': they never send e-mail."""
    th = th or default_thresholds()
    room = conn.execute("SELECT building,floor,room FROM devices WHERE device_id=?", (device_id,)).fetchone()

    def open_row(atype):
        return conn.execute("SELECT id, severity, actual_value_json, operator FROM alarm_history WHERE device_id=? AND alarm_type=? "
                            "AND resolved=0 AND source='simulated'", (device_id, atype)).fetchone()

    def insert(atype, sev, msg, field, val, limit, op):
        conn.execute("INSERT INTO alarm_history(device_id,node_type,building,floor,room,alarm_type,alarm_message,telemetry,triggered_at,resolved,"
                     "severity,field_key,actual_value_json,threshold_value,operator,source) VALUES (?,?,?,?,?,?,?,?,?,0,?,?,?,?,?,'simulated')",
                     (device_id, "environment", room[0], room[1], room[2], atype, msg, json.dumps(v), fmt(now), sev, field,
                      json.dumps(val), limit, op))

    def resolve(row_id, why):
        conn.execute("UPDATE alarm_history SET resolved=1, resolved_at=?, auto_resolved=1, resolved_reason=? WHERE id=?", (fmt(now), why, row_id))

    for atype, field, sev, msg in BINARY_ALARMS:
        row, active = open_row(atype), v.get(field) == 1
        if active and not row:
            insert(atype, sev, msg, field, 1, 1, "==")
        elif row and not active:
            resolve(row[0], "Sensor returned to normal")

    for metric, (hi_w, hi_c, lo_txt, _unit, hyst) in ANALOG_ALARMS.items():
        val, lim = v.get(metric), th.get(metric) or {}
        if val is None:
            continue
        sev = None
        if "crit" in lim and val >= lim["crit"]:
            sev, limit, op, msg = "critical", lim["crit"], ">=", hi_c
        elif "warn" in lim and val >= lim["warn"]:
            sev, limit, op, msg = "warning", lim["warn"], ">=", hi_w
        elif lo_txt and "low" in lim and val < lim["low"]:
            sev, limit, op, msg = "warning", lim["low"], "<", lo_txt
        atype = f"{metric}_threshold"
        row = open_row(atype)
        if sev and not row:
            insert(atype, sev, msg, metric, val, limit, op)
        elif sev and row:
            if sev == "critical" and row["severity"] != "critical":
                conn.execute("UPDATE alarm_history SET severity='critical', alarm_message=?, threshold_value=?, actual_value_json=? WHERE id=?",
                             (msg, limit, json.dumps(val), row["id"]))
            else:
                try:
                    peak = float(json.loads(row["actual_value_json"]))
                except (TypeError, ValueError):
                    peak = None
                if peak is None or (val > peak if row["operator"] != "<" else val < peak):
                    conn.execute("UPDATE alarm_history SET actual_value_json=? WHERE id=?", (json.dumps(val), row["id"]))
        elif row:
            inside = val < lim.get("warn", float("inf")) - hyst and val >= lim.get("low", float("-inf")) + hyst
            if inside:
                resolve(row["id"], "Value returned within limits")


def tick(now: datetime | None = None) -> int:
    """Write one reading per simulated device. Returns the number written."""
    now = (now or datetime.now(timezone.utc)).replace(microsecond=0)
    with closing(get_db_connection()) as conn:
        if not simulation_enabled(conn):
            return 0
        devs, scen, th, n = _sim_devices(conn), _scenario(conn), thresholds(conn), 0
        conn.execute("BEGIN IMMEDIATE")
        try:
            for d in devs:
                row = conn.execute("SELECT telemetry, updated_at FROM device_latest_telemetry WHERE device_id=?", (d["device_id"],)).fetchone()
                prev = None
                if row:
                    prev = json.loads(row[0])
                    prev["_ts"] = datetime.strptime(row[1][:19].replace("T", " "), TS_FMT).replace(tzinfo=timezone.utc)
                v = sample(d, now, prev, scen, th)
                conn.execute("INSERT INTO historical_telemetry(device_id,telemetry,timestamp) VALUES (?,?,?)", (d["device_id"], json.dumps(v), fmt(now)))
                _raise_or_clear_alarms(conn, d["device_id"], v, now, th)
                active = _active_alarms(conn, d["device_id"])
                conn.execute("INSERT INTO device_latest_telemetry(device_id,telemetry,alarm_active,alarm_message,updated_at) VALUES (?,?,?,?,?) "
                             "ON CONFLICT(device_id) DO UPDATE SET telemetry=excluded.telemetry, alarm_active=excluded.alarm_active, "
                             "alarm_message=excluded.alarm_message, updated_at=excluded.updated_at",
                             (d["device_id"], json.dumps(v), int(bool(active)), "Alarm active" if active else "OK", fmt(now)))
                conn.execute("UPDATE devices SET last_seen=?, status='online' WHERE device_id=? AND data_source='simulated'", (fmt(now), d["device_id"]))
                n += 1
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return n


def thin_simulated_history(keep_recent_hours: int = 24, bucket_minutes: int = 5) -> int:
    """Bound storage: older SIMULATED rows keep one sample per bucket. Real rows are never touched."""
    cutoff = fmt(datetime.now(timezone.utc) - timedelta(hours=keep_recent_hours))
    b = bucket_minutes * 60
    with closing(get_db_connection()) as conn:
        cur = conn.execute(
            "DELETE FROM historical_telemetry WHERE timestamp < ? AND device_id IN (SELECT device_id FROM devices WHERE data_source='simulated') "
            "AND id NOT IN (SELECT MIN(id) FROM historical_telemetry WHERE timestamp < ? GROUP BY device_id, CAST(strftime('%s', timestamp) AS INTEGER)/?)",
            (cutoff, cutoff, b))
        conn.commit()
        return cur.rowcount


def _acquire_lease(interval: float) -> bool:
    """Single-simulator guarantee across worker processes (DB lease, ~3x interval)."""
    me, now = os.getpid(), datetime.now(timezone.utc)
    with closing(get_db_connection()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        cur = get_setting(conn, LEASE_KEY)
        if cur and cur.get("pid") != me and datetime.fromisoformat(cur["at"]) > now - timedelta(seconds=max(30, interval * 3)):
            conn.rollback()
            return False
        set_setting(conn, LEASE_KEY, {"pid": me, "at": now.isoformat()})
        conn.commit()
        return True


async def simulator_loop():
    global _task_running
    if _task_running:  # never two loops in one process
        return
    _task_running = True
    last_thin = 0.0
    try:
        while True:
            with closing(get_db_connection()) as conn:
                interval = float(get_setting(conn, "sim_interval", settings.SIMULATION_INTERVAL_SECONDS))
            interval = max(1.0, interval)
            try:
                if await asyncio.to_thread(_acquire_lease, interval):
                    await asyncio.to_thread(tick)
                    if asyncio.get_running_loop().time() - last_thin > 3600:
                        await asyncio.to_thread(thin_simulated_history)
                        last_thin = asyncio.get_running_loop().time()
            except asyncio.CancelledError:
                raise
            except Exception:  # transient DB errors must not kill the simulator
                log.exception("simulator tick failed; retrying")
            await asyncio.sleep(interval)
    finally:
        _task_running = False
