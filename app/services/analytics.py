"""Analytics queries shared by the dashboards API and the PDF reports.

Everything displayed comes from historical_telemetry / device_latest_telemetry.
Missing data is None (never 0); the data state is always explicit.
"""
from __future__ import annotations

import json
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from app.config import settings
from app.services.sensor_simulator import fmt, get_setting

# canonical metric -> (accepted telemetry keys, unit, how to combine devices)
METRICS = {
    "temperature": (["temperature", "temp", "temperature_c"], "°C", "avg"),
    "humidity": (["humidity", "relative_humidity"], "%", "avg"),
    "co2": (["co2", "co2_ppm", "eco2"], "ppm", "avg"),
    "pm25": (["pm25", "pm2_5"], "µg/m³", "avg"),
    "pm10": (["pm10"], "µg/m³", "avg"),
    "lux": (["lux", "light", "illuminance"], "lux", "avg"),
    "occupancy": (["occupancy", "people_count", "occupants"], "people", "sum"),
    "power_w": (["power_w", "power", "active_power"], "W", "sum"),
    "energy_kwh": (["energy_kwh"], "kWh", "sum"),       # cumulative meter
    "water_lpm": (["water_lpm", "water_flow"], "L/min", "sum"),
    "water_m3": (["water_m3"], "m³", "sum"),             # cumulative meter
    "smoke": (["smoke", "smoke_alarm"], "", "max"),
    "leak": (["leak", "water_leak"], "", "max"),
    "gas": (["gas", "gas_alarm"], "", "max"),
    "door_open": (["door_open"], "", "max"),
    "hvac_on": (["hvac_on"], "", "max"),
    "hvac_fault": (["hvac_fault"], "", "max"),
    "light_on": (["light_on"], "", "max"),
    "solar_kwh": (["solar_kwh", "renewable_kwh"], "kWh", "sum"),  # cumulative generation meter
}
# Defaults only; every value is editable in Settings and persisted in dashboard_settings.
DEFAULT_THRESHOLDS = {
    "temperature": {"low": 18, "warn": 26, "crit": 30},
    "humidity": {"low": 25, "warn": 65, "crit": 75},
    "co2": {"warn": 1000, "crit": 1500},
    "pm25": {"warn": 25, "crit": 50},
    "approach_fraction": 0.9,
}
DEFAULT_TARIFF = {"currency": "EUR", "tariff_type": "flat", "rate_per_kwh": None, "tou": []}
DEFAULT_HEAT = {"medium": 50, "high": 80}
RANGES = {"1h": (3600, 60), "24h": (86400, 900), "7d": (7 * 86400, 7200), "30d": (30 * 86400, 21600)}
_cache: dict = {}


def now_utc():
    return datetime.now(timezone.utc)


def parse_ts(s):
    if not s:
        return None
    try:
        d = datetime.fromisoformat(str(s).replace(" ", "T"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def cached(key, ttl, fn):
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < ttl:
        return hit[1]
    val = fn()
    _cache[key] = (time.monotonic(), val)
    if len(_cache) > 300:
        for k in sorted(_cache, key=lambda k: _cache[k][0])[:100]:
            _cache.pop(k, None)
    return val


def cfg(conn):
    th = {**DEFAULT_THRESHOLDS, **(get_setting(conn, "thresholds", {}) or {})}
    return {
        "thresholds": th, "tariff": {**DEFAULT_TARIFF, **(get_setting(conn, "tariff", {}) or {})},
        "freshness_seconds": int(get_setting(conn, "freshness_seconds", settings.DEVICE_STALE_AFTER_SECONDS)),
        "timezone": get_setting(conn, "timezone", "Europe/Vienna"),
        "heatmap": {**DEFAULT_HEAT, **(get_setting(conn, "heatmap", {}) or {})},
        "show_source_labels": bool(get_setting(conn, "show_source_labels", settings.SHOW_DATA_SOURCE_LABELS)),
    }


def tzinfo(name):
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:
        return timezone.utc


# ----------------------------------------------------------------- scope
def _access_rows(conn, user):
    return conn.execute("SELECT * FROM user_access WHERE user_id=? AND can_view_telemetry=1", (user["id"],)).fetchall()


def _allowed(rows, client_id, site_id, building_id, floor_id):
    for a in rows:
        if a["floor_id"]:
            ok = a["floor_id"] == floor_id
        elif a["building_id"]:
            ok = a["building_id"] == building_id
        elif a["site_id"]:
            ok = a["site_id"] == site_id
        elif a["client_id"]:
            ok = a["client_id"] == client_id
        else:
            ok = False
        if ok:
            return True
    return False


def is_admin(user):
    return (user.get("role") or "").lower() == "admin"


def hierarchy(conn, user):
    """Floors/rooms the user may see (server-side authorisation)."""
    rows = conn.execute(
        "SELECT s.client_id, s.id site_id, b.id building_id, b.name building, f.id floor_id, f.name floor, f.image_path, "
        "f.image_width, f.image_height FROM floors f JOIN buildings b ON b.id=f.building_id JOIN sites s ON s.id=b.site_id").fetchall()
    acc = None if is_admin(user) else _access_rows(conn, user)
    out = [dict(r) for r in rows if acc is None or _allowed(acc, r["client_id"], r["site_id"], r["building_id"], r["floor_id"])]
    ids = [r["floor_id"] for r in out]
    rooms = []
    if ids:
        q = ",".join("?" * len(ids))
        rooms = [dict(r) for r in conn.execute(
            f"SELECT r.id room_id, r.floor_id, r.room_name, m.room_number, m.label, m.room_type, m.capacity, m.capacity_source "
            f"FROM rooms r LEFT JOIN map_rooms m ON m.room_id=r.id WHERE r.floor_id IN ({q}) ORDER BY r.room_name", ids)]
    return out, rooms


def scope_devices(conn, user, building_id=None, floor_id=None, room_id=None, device_id=None):
    sql = ("SELECT d.device_id, d.room_id, d.floor_id, d.building_id, d.site_id, d.client_id, d.data_source, d.building, d.floor, "
           "d.room, d.last_seen, d.status, d.label AS device_label, m.room_number, m.label, m.room_type, m.capacity, m.capacity_source "
           "FROM devices d LEFT JOIN map_rooms m ON m.room_id=d.room_id WHERE d.is_placed=1 AND d.floor_id IS NOT NULL")
    args = []
    for col, val in (("d.building_id", building_id), ("d.floor_id", floor_id), ("d.room_id", room_id), ("d.device_id", device_id)):
        if val is not None:
            sql += f" AND {col}=?"
            args.append(val)
    rows = [dict(r) for r in conn.execute(sql, args)]
    if is_admin(user):
        return rows
    acc = _access_rows(conn, user)
    return [r for r in rows if _allowed(acc, r["client_id"], r["site_id"], r["building_id"], r["floor_id"])]


# ----------------------------------------------------------------- helpers
def expr(metric):
    parts = [f"json_extract(telemetry,'$.{k}')" for k in METRICS[metric][0]]
    e = parts[0] if len(parts) == 1 else "COALESCE(" + ",".join(parts) + ")"
    return f"CASE WHEN typeof({e}) IN ('integer','real') THEN {e} END"


def pick(tele, metric):
    for k in METRICS[metric][0]:
        if k in tele and isinstance(tele[k], bool):
            return 1.0 if tele[k] else 0.0
        if k in tele and isinstance(tele[k], (int, float)):
            return float(tele[k])
    return None


def has_key(tele, metric):
    return any(k in tele for k in METRICS[metric][0])


def latest(conn, devices):
    ids = [d["device_id"] for d in devices]
    out = {}
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        for r in conn.execute(f"SELECT device_id, telemetry, updated_at FROM device_latest_telemetry WHERE device_id IN ({','.join('?' * len(chunk))})", chunk):
            try:
                out[r[0]] = (json.loads(r[1]), parse_ts(r[2]))
            except ValueError:
                pass
    return out


def is_sim(dev):
    """'simulated' devices are written by the simulator; 'preview' devices are the pre-existing demo seed."""
    return dev["data_source"] in ("simulated", "preview")


def point_state(dev, tele, ts, metric, fresh_s):
    """live / simulated / stale / no_data / not_configured for one device+metric."""
    if tele is None:
        return "no_data"
    if not has_key(tele, metric):
        return "not_configured"
    if pick(tele, metric) is None:
        return "no_data"
    if ts is None or (now_utc() - ts).total_seconds() > fresh_s:
        return "stale"
    return "simulated" if is_sim(dev) else "live"


def source_label(devices):
    s = {"simulated" if is_sim(d) else "live" for d in devices}
    if not s:
        return "none"
    return "simulated" if s == {"simulated"} else "live" if s == {"live"} else "mixed"


def is_fresh(ts, c):
    return bool(ts) and (now_utc() - ts).total_seconds() <= c["freshness_seconds"]


def kpi(devices, lat, metric, c):
    """Current scope value for one metric with an explicit state."""
    vals, states, stale_vals = [], [], []
    for d in devices:
        tele, ts = lat.get(d["device_id"], (None, None))
        st = point_state(d, tele, ts, metric, c["freshness_seconds"])
        states.append(st)
        v = pick(tele, metric) if tele else None
        if st in ("live", "simulated"):
            vals.append((v, st))
        elif st == "stale" and v is not None:
            stale_vals.append(v)
    unit, how = METRICS[metric][1], METRICS[metric][2]
    if vals:
        nums = [v for v, _ in vals]
        value = sum(nums) if how == "sum" else max(nums) if how == "max" else sum(nums) / len(nums)
        srcs = {s for _, s in vals}
        state = "simulated" if srcs == {"simulated"} else "live" if srcs == {"live"} else "mixed"
        return {"value": round(value, 2), "unit": unit, "state": state, "n": len(vals)}
    if stale_vals:
        return {"value": None, "unit": unit, "state": "stale", "n": 0}
    if any(s == "no_data" for s in states):
        return {"value": None, "unit": unit, "state": "no_data", "n": 0}
    return {"value": None, "unit": unit, "state": "not_configured", "n": 0}


def threshold_status(metric, value, c):
    """normal / approaching / warning / critical, or None when the metric has no thresholds."""
    th = c["thresholds"].get(metric)
    if value is None or not isinstance(th, dict):
        return None
    frac = c["thresholds"].get("approach_fraction", 0.9)
    if "crit" in th and value >= th["crit"]:
        return "critical"
    if "warn" in th and value >= th["warn"]:
        return "warning"
    if "low" in th and value < th["low"]:
        return "warning"
    if metric in ("co2", "pm25") and "warn" in th and value >= th["warn"] * frac:
        return "approaching"
    return "normal"


def resolve_period(rng, start, end):
    """Return (start_dt, end_dt, bucket_seconds, label)."""
    if start and end:
        s, e = parse_ts(start), parse_ts(end)
        if not s or not e or e <= s:
            raise ValueError("Invalid date range")
        span = (e - s).total_seconds()
        if span > 400 * 86400:
            raise ValueError("Date range too large (max 400 days)")
        bucket = 60 if span <= 3600 * 3 else 900 if span <= 2 * 86400 else 7200 if span <= 10 * 86400 else 21600 if span <= 60 * 86400 else 86400
        return s, e, bucket, "custom"
    rng = rng if rng in RANGES else "24h"
    span, bucket = RANGES[rng]
    e = now_utc()
    return e - timedelta(seconds=span), e, bucket, rng


def compare(cur, prev, unit=""):
    """Absolute and percentage difference; never divide by zero or invent a baseline."""
    if cur is None or prev is None:
        return {"available": False}
    diff = cur - prev
    pct = None if prev == 0 else diff / abs(prev) * 100
    return {"available": True, "previous": round(prev, 3), "diff": round(diff, 3), "pct": None if pct is None else round(pct, 1), "unit": unit}


def bucket_rows(conn, ids, start, end, bucket, metrics, with_max=()):
    """{device_id: {bucket_ts: {metric: avg, metric+'_max': max}}} using SQL aggregation only."""
    out = defaultdict(lambda: defaultdict(dict))
    if not ids:
        return out
    cols = []
    for m in metrics:
        cols.append(f"AVG({expr(m)})")
        if m in with_max:
            cols.append(f"MAX({expr(m)})")
    for i in range(0, len(ids), 400):
        chunk = ids[i:i + 400]
        sql = (f"SELECT device_id, (CAST(strftime('%s',timestamp) AS INTEGER)/{int(bucket)})*{int(bucket)} AS b, {','.join(cols)} "
               f"FROM historical_telemetry WHERE device_id IN ({','.join('?' * len(chunk))}) AND timestamp>=? AND timestamp<? "
               f"GROUP BY device_id, b ORDER BY b")
        for r in conn.execute(sql, [*chunk, fmt(start), fmt(end)]):
            j = 2
            for m in metrics:
                out[r[0]][r[1]][m] = r[j]
                j += 1
                if m in with_max:
                    out[r[0]][r[1]][m + "_max"] = r[j]
                    j += 1
    return out


def combine(rows, metric, how=None):
    """Merge per-device buckets into one scope series [[epoch, value], ...]; empty buckets are absent."""
    how = how or METRICS[metric][2]
    acc = defaultdict(list)
    for bks in rows.values():
        for b, vals in bks.items():
            v = vals.get(metric)
            if v is not None:
                acc[b].append(v)
    return [[b, round(sum(v) if how == "sum" else max(v) if how == "max" else sum(v) / len(v), 3)] for b, v in sorted(acc.items())]


def stats(series):
    vals = [v for _, v in series]
    if not vals:
        return {"min": None, "max": None, "avg": None, "n": 0}
    return {"min": round(min(vals), 2), "max": round(max(vals), 2), "avg": round(sum(vals) / len(vals), 2), "n": len(vals)}


# ----------------------------------------------------------------- energy
MAX_METER_STEP_KWH = 10000


def meter_hours(buckets, s_ep):
    """Hourly kWh from a cumulative meter. Positive successive differences only; a decrease is a
    reset (new baseline, nothing added); absurd jumps are discarded. Returns {bucket: kWh}."""
    hours, last = {}, None
    for b in sorted(buckets):
        m = buckets[b]
        if m is None:
            continue
        if last is not None and b >= s_ep:
            d = m - last
            if 0 <= d <= MAX_METER_STEP_KWH:
                hours[b] = d
        last = m
    return hours


def energy(conn, devices, start, end, c):
    """kWh per device per hour bucket. Meter deltas preferred, else hourly mean power x 1 h.
    Hours without readings are unknown, not zero."""
    ids = [d["device_id"] for d in devices]
    rows = bucket_rows(conn, ids, start - timedelta(hours=1), end, 3600, ["energy_kwh", "power_w"], with_max=("energy_kwh", "power_w"))
    s_ep = int(start.timestamp()) // 3600 * 3600
    per_dev, methods = {}, {}
    for dev, bks in rows.items():
        hours = meter_hours({b: v.get("energy_kwh_max") for b, v in bks.items()}, s_ep)
        if hours:
            methods[dev] = "meter"
        else:
            hours = {b: v["power_w"] / 1000.0 for b, v in bks.items() if v.get("power_w") is not None and b >= s_ep}
            if hours:
                methods[dev] = "integrated"
        if hours:
            per_dev[dev] = hours
    return per_dev, methods


def renewable_kwh(conn, devices, start, end):
    """Energy generated by renewable meters (cumulative meter deltas). None when no device has such a meter."""
    ids = [d["device_id"] for d in devices]
    rows = bucket_rows(conn, ids, start - timedelta(hours=1), end, 3600, ["solar_kwh"], with_max=("solar_kwh",))
    s_ep = int(start.timestamp()) // 3600 * 3600
    total, found = 0.0, False
    for bks in rows.values():
        hours = meter_hours({b: v.get("solar_kwh_max") for b, v in bks.items()}, s_ep)
        if hours:
            found = True
            total += sum(hours.values())
    return round(total, 3) if found else None


def energy_summary(conn, devices, start, end, c):
    per_dev, methods = energy(conn, devices, start, end, c)
    total = sum(sum(h.values()) for h in per_dev.values()) if per_dev else None
    hourly = defaultdict(float)
    for h in per_dev.values():
        for b, v in h.items():
            hourly[b] += v
    tz = tzinfo(c["timezone"])
    daily = defaultdict(float)
    for b, v in hourly.items():
        daily[datetime.fromtimestamp(b, tz).strftime("%Y-%m-%d")] += v
    cost, tr = None, c["tariff"]
    if total is not None and tr.get("rate_per_kwh") is not None:
        if tr.get("tariff_type") == "time_of_use" and tr.get("tou"):
            cost = 0.0
            for b, v in hourly.items():
                hh, rate = datetime.fromtimestamp(b, tz).hour, tr["rate_per_kwh"]
                for w in tr["tou"]:
                    a, z = int(w["from"][:2]), int(w["to"][:2])
                    if (a <= hh < z) if a < z else (hh >= a or hh < z):
                        rate = w["rate"]
                cost += v * rate
        else:
            cost = total * tr["rate_per_kwh"]
    return {"total_kwh": None if total is None else round(total, 3), "cost": None if cost is None else round(cost, 2),
            "per_device": {k: round(sum(v.values()), 3) for k, v in per_dev.items()}, "daily": dict(sorted(daily.items())),
            "methods": methods, "hourly": dict(hourly)}


def comfort_score(t, h, co2, pm25):
    """Documented method: start at 100; -5 per °C outside 20-26; -1 per %RH outside 30-60;
    -1 per 50 ppm CO2 above 800; -1 per µg/m³ PM2.5 above 12. Clamped 0-100; unavailable inputs are skipped."""
    if all(x is None for x in (t, h, co2, pm25)):
        return None
    s = 100.0
    if t is not None:
        s -= 5 * (max(0, 20 - t) + max(0, t - 26))
    if h is not None:
        s -= max(0, 30 - h) + max(0, h - 60)
    if co2 is not None:
        s -= max(0, co2 - 800) / 50
    if pm25 is not None:
        s -= max(0, pm25 - 12)
    return int(max(0, min(100, round(s))))


def alarms(conn, devices, start=None, end=None, severity=None, status=None, limit=100):
    ids = [d["device_id"] for d in devices]
    if not ids:
        return []
    by_dev = {d["device_id"]: d for d in devices}
    sql = (f"SELECT id, device_id, alarm_type, alarm_message, triggered_at, acknowledged, acknowledged_at, resolved, resolved_at, severity, "
           f"field_key, actual_value_json, threshold_value, operator, source, building, floor, room FROM alarm_history WHERE device_id IN ({','.join('?' * len(ids))})")
    args = list(ids)
    if start:
        sql += " AND triggered_at>=?"
        args.append(fmt(start))
    if end:
        sql += " AND triggered_at<?"
        args.append(fmt(end))
    if severity:
        sql += " AND severity=?"
        args.append(severity)
    if status == "active":
        sql += " AND resolved=0"
    elif status == "resolved":
        sql += " AND resolved=1"
    sql += " ORDER BY triggered_at DESC LIMIT ?"
    args.append(int(limit))
    res = []
    for r in conn.execute(sql, args):
        d = by_dev.get(r["device_id"], {})
        try:
            val = json.loads(r["actual_value_json"]) if r["actual_value_json"] else None
        except ValueError:
            val = r["actual_value_json"]
        res.append({"id": r["id"], "time": r["triggered_at"], "device_id": r["device_id"], "device": d.get("device_label") or r["device_id"], "event": r["alarm_message"], "type": r["alarm_type"],
                    "severity": r["severity"], "building": r["building"], "floor": r["floor"], "room": r["room"], "room_id": d.get("room_id"),
                    "mapped": d.get("room_number") is not None, "value": val, "field": r["field_key"], "threshold": r["threshold_value"],
                    "operator": r["operator"], "status": "resolved" if r["resolved"] else ("acknowledged" if r["acknowledged"] else "active"),
                    "acknowledged": bool(r["acknowledged"]), "resolved": bool(r["resolved"]), "resolved_at": r["resolved_at"],
                    "simulated": r["source"] == "simulated"})
    return res
