"""Analytics dashboards: pages, JSON API, settings and PDF download endpoints."""
from __future__ import annotations

import json
import logging
from contextlib import closing
from datetime import timedelta

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates

from app.config import settings
from app.db.connection import get_db_connection
from app.services import analytics as A
from app.services import dashboard_data as D
from app.services.sensor_simulator import get_setting, set_setting, simulation_enabled

log = logging.getLogger(__name__)
router = APIRouter()
templates = Jinja2Templates(directory="templates")
templates.env.globals["preview_mode"] = settings.PREVIEW_MODE
PAGES = ("overview", "energy", "environment", "occupancy", "safety", "floor-plan", "reports", "settings")
REPORT_TYPES = ("overview", "energy", "environment", "occupancy", "safety")


def user_of(request: Request) -> dict:
    from app.main import get_current_user_from_request
    user = get_current_user_from_request(request)
    if not user:
        raise HTTPException(401, "Authentication required")
    if (user.get("role") or "").lower() not in ("admin", "client"):
        raise HTTPException(403, "Not permitted")
    return user


def admin_of(request: Request) -> dict:
    user = user_of(request)
    if (user.get("role") or "").lower() != "admin":
        raise HTTPException(403, "Administrator role required")
    if not (request.headers.get("content-type") or "").startswith("application/json"):
        raise HTTPException(415, "JSON body required")  # blocks cross-site form posts
    return user


def parse_filters(conn, user, q) -> dict:
    f = {}
    for k in ("building_id", "floor_id", "room_id"):
        v = q.get(k)
        if v not in (None, ""):
            try:
                f[k] = int(v)
            except ValueError:
                raise HTTPException(422, f"{k} must be an integer")
    if q.get("device_id"):
        f["device_id"] = str(q["device_id"])[:80]
    f["range"] = q.get("range") if q.get("range") in A.RANGES else "24h"
    for k in ("start", "end"):
        if q.get(k):
            if not A.parse_ts(q[k]):
                raise HTTPException(422, f"Invalid {k}")
            f[k] = q[k]
    if bool(f.get("start")) != bool(f.get("end")):
        raise HTTPException(422, "Both start and end are required for a custom period")
    if q.get("severity") in ("info", "warning", "critical"):
        f["severity"] = q["severity"]
    if q.get("status") in ("active", "resolved"):
        f["status"] = q["status"]
    floors, rooms = A.hierarchy(conn, user)
    if f.get("building_id") and f["building_id"] not in {x["building_id"] for x in floors}:
        raise HTTPException(404, "Building not found")
    if f.get("floor_id") and f["floor_id"] not in {x["floor_id"] for x in floors}:
        raise HTTPException(404, "Floor not found")
    if f.get("room_id") and f["room_id"] not in {x["room_id"] for x in rooms}:
        raise HTTPException(404, "Room not found")
    return f


def _json(request, fn):
    user = user_of(request)
    try:
        with closing(get_db_connection()) as conn:
            f = parse_filters(conn, user, request.query_params)
            key = (user["id"], request.url.path, tuple(sorted(f.items())))
            return A.cached(key, 2.0, lambda: fn(conn, user, f))
    except ValueError as e:
        raise HTTPException(422, str(e))


# ------------------------------------------------------------------ pages
@router.get("/dashboards", response_class=HTMLResponse, include_in_schema=False)
def dashboards_root(request: Request):
    return dashboards_page(request, "overview")


@router.get("/dashboards/{page}", response_class=HTMLResponse, include_in_schema=False)
def dashboards_page(request: Request, page: str):
    if page not in PAGES:
        raise HTTPException(404, "Unknown dashboard")
    user = user_of(request)
    return templates.TemplateResponse(request, "dashboards.html", {"page": page, "user": user, "is_admin": (user["role"] or "").lower() == "admin", "poll_ms": 4000})


# ------------------------------------------------------------------ data
@router.get("/api/dashboards/meta")
def meta(request: Request):
    user = user_of(request)
    with closing(get_db_connection()) as conn:
        floors, rooms = A.hierarchy(conn, user)
        devs = A.scope_devices(conn, user)
        mode = "preview" if settings.PREVIEW_MODE else "simulation" if settings.SIMULATION_ENABLED else "production"
        return {"floors": floors, "rooms": rooms, "settings": A.cfg(conn), "now": A.now_utc().isoformat(), "source": A.source_label(devs), "mode": mode,
                "simulator": {"allowed": mode != "production", "running": simulation_enabled(conn),
                              "interval": get_setting(conn, "sim_interval", settings.SIMULATION_INTERVAL_SECONDS)},
                "is_admin": (user["role"] or "").lower() == "admin", "user": {"name": user.get("name"), "role": user.get("role")}}


@router.get("/api/dashboards/{section}")
def section(request: Request, section: str):
    if section not in D.BUILDERS:
        raise HTTPException(404, "Unknown dashboard")
    return _json(request, lambda conn, user, f: D.build(conn, user, section, f))


@router.get("/api/campus/map")
def campus_map(request: Request):
    return _json(request, lambda conn, user, f: D.campus(D.Ctx(conn, user, f)))


@router.get("/api/rooms/{room_id}/telemetry")
def room_telemetry(request: Request, room_id: int):
    user = user_of(request)
    with closing(get_db_connection()) as conn:
        try:
            f = parse_filters(conn, user, {**dict(request.query_params), "room_id": str(room_id)})
            x = D.Ctx(conn, user, f)
        except ValueError as e:
            raise HTTPException(422, str(e))
        names = ["temperature", "humidity", "co2", "pm25", "lux", "occupancy", "power_w"]
        rows = A.bucket_rows(conn, x.ids, x.start, x.end, x.bucket, names)
        return {"meta": x.meta(), "room_id": room_id, "current": {m: A.kpi(x.devices, x.lat, m, x.c) for m in names},
                "series": {m: A.combine(rows, m) for m in names}, "alarms": A.alarms(conn, x.devices, limit=10),
                "devices": [{"device_id": d["device_id"], "label": d.get("device_label") or d["device_id"], "source": "simulated" if A.is_sim(d) else "live",
                             "last_update": (lambda t: t.isoformat() if t else None)(x.lat.get(d["device_id"], (None, None))[1])} for d in x.devices]}


# ------------------------------------------------------------------ reports
@router.get("/api/reports/{report_type}/pdf")
def report_pdf(request: Request, report_type: str):
    if report_type not in REPORT_TYPES:
        raise HTTPException(404, "Unsupported report type")
    user = user_of(request)
    from app.services.reports import render_report
    try:
        with closing(get_db_connection()) as conn:
            f = parse_filters(conn, user, request.query_params)
            data = D.build(conn, user, report_type, f)
            pdf = render_report(report_type, data, f, {"floors": A.hierarchy(conn, user)}, user)
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(422, str(e))
    except Exception:
        log.exception("PDF generation failed")
        raise HTTPException(500, "The report could not be generated")
    day = A.now_utc().strftime("%Y-%m-%d")
    return Response(pdf, media_type="application/pdf", headers={
        "Content-Disposition": f'attachment; filename="smart_building_{report_type}_report_{day}.pdf"', "Cache-Control": "no-store"})


# ------------------------------------------------------------------ settings (admin only)
def _num(v, lo, hi, name):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not (lo <= v <= hi):
        raise HTTPException(422, f"{name} must be a number between {lo} and {hi}")
    return v


def validate_setting(key, v):
    if key == "thresholds":
        if not isinstance(v, dict):
            raise HTTPException(422, "thresholds must be an object")
        out = {}
        for m, d in v.items():
            if m == "approach_fraction":
                out[m] = _num(d, 0.5, 1, m)
            elif m in ("temperature", "humidity", "co2", "pm25") and isinstance(d, dict):
                out[m] = {k: _num(x, -50, 10000, f"{m}.{k}") for k, x in d.items() if k in ("low", "warn", "crit") and x is not None}
                if "warn" in out[m] and "crit" in out[m] and out[m]["warn"] > out[m]["crit"]:
                    raise HTTPException(422, f"{m}: warn must not exceed crit")
            else:
                raise HTTPException(422, f"Unknown threshold '{m}'")
        return out
    if key == "tariff":
        if not isinstance(v, dict):
            raise HTTPException(422, "tariff must be an object")
        cur = str(v.get("currency", "EUR")).upper()
        if not (len(cur) == 3 and cur.isalpha()):
            raise HTTPException(422, "currency must be a 3-letter code")
        if v.get("tariff_type", "flat") not in ("flat", "time_of_use"):
            raise HTTPException(422, "tariff_type must be flat or time_of_use")
        rate, tou = v.get("rate_per_kwh"), []
        for w in v.get("tou") or []:
            try:
                int(w["from"][:2])
                int(w["to"][:2])
            except Exception:
                raise HTTPException(422, "tou windows need from/to as HH:MM")
            tou.append({"from": w["from"], "to": w["to"], "rate": _num(w["rate"], 0, 100, "tou rate")})
        return {"currency": cur, "tariff_type": v.get("tariff_type", "flat"), "rate_per_kwh": None if rate is None else _num(rate, 0, 100, "rate_per_kwh"), "tou": tou}
    if key == "freshness_seconds":
        return int(_num(v, 10, 86400, key))
    if key == "timezone":
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo(str(v))
        except Exception:
            raise HTTPException(422, "Unknown timezone")
        return str(v)
    if key == "heatmap":
        m, h = _num(v.get("medium"), 1, 99, "medium"), _num(v.get("high"), 2, 100, "high")
        if m >= h:
            raise HTTPException(422, "medium must be below high")
        return {"medium": m, "high": h}
    if key == "show_source_labels":
        if not isinstance(v, bool):
            raise HTTPException(422, "show_source_labels must be true/false")
        return v
    if key == "sim_enabled":
        if not isinstance(v, bool):
            raise HTTPException(422, "sim_enabled must be true/false")
        return v
    if key == "sim_interval":
        return float(_num(v, 1, 3600, key))
    raise HTTPException(422, f"Setting '{key}' cannot be changed here")


def _audit(conn, user, action, target, details):
    try:
        conn.execute("INSERT INTO audit_log(actor, action, target_type, target_id, details, message) VALUES (?,?,?,?,?,?)",
                     (user.get("email") or "admin", action, "dashboard_settings", str(target), json.dumps(details, default=str)[:2000], f"{action}: {target}"))
    except Exception:
        log.exception("audit write failed")


@router.put("/api/dashboards/settings")
async def put_settings(request: Request):
    user = admin_of(request)
    body = await request.json()
    if not isinstance(body, dict) or not body:
        raise HTTPException(422, "Body must be a non-empty object")
    clean = {k: validate_setting(k, v) for k, v in body.items()}
    if ("sim_enabled" in clean or "sim_interval" in clean) and not (settings.PREVIEW_MODE or settings.SIMULATION_ENABLED):
        raise HTTPException(409, "The simulator is disabled in production mode; enable it through deployment configuration")
    with closing(get_db_connection()) as conn:
        for k, v in clean.items():
            set_setting(conn, k, v)
        _audit(conn, user, "update_dashboard_settings", ",".join(clean), clean)
        conn.commit()
        A._cache.clear()
        return {"ok": True, "settings": A.cfg(conn)}


@router.put("/api/dashboards/rooms/{room_id}/capacity")
async def put_capacity(request: Request, room_id: int):
    user = admin_of(request)
    body = await request.json()
    cap = body.get("capacity")
    if cap is not None:
        cap = int(_num(cap, 1, 100000, "capacity"))
    with closing(get_db_connection()) as conn:
        if not conn.execute("SELECT 1 FROM map_rooms WHERE room_id=?", (room_id,)).fetchone():
            raise HTTPException(404, "Room has no map entry")
        conn.execute("UPDATE map_rooms SET capacity=?, capacity_source='configured' WHERE room_id=?", (cap, room_id))
        _audit(conn, user, "set_room_capacity", room_id, {"capacity": cap})
        conn.commit()
        A._cache.clear()
    return {"ok": True}


@router.put("/api/dashboards/buildings/{building_id}/marker")
async def put_marker(request: Request, building_id: int):
    user = admin_of(request)
    b = await request.json()
    x, y = _num(b.get("x"), 0, 1, "x"), _num(b.get("y"), 0, 1, "y")
    with closing(get_db_connection()) as conn:
        if not conn.execute("SELECT 1 FROM buildings WHERE id=?", (building_id,)).fetchone():
            raise HTTPException(404, "Building not found")
        conn.execute("INSERT INTO map_buildings(building_id,code,x,y,confirmed) VALUES (?,?,?,?,?) ON CONFLICT(building_id) DO UPDATE SET "
                     "code=excluded.code, x=excluded.x, y=excluded.y, confirmed=excluded.confirmed",
                     (building_id, str(b.get("code") or "")[:8] or None, x, y, 1 if b.get("confirmed") else 0))
        _audit(conn, user, "set_building_marker", building_id, b)
        conn.commit()
        A._cache.clear()
    return {"ok": True}


@router.post("/api/dashboards/simulator/scenario")
async def scenario(request: Request):
    user = admin_of(request)
    if not (settings.PREVIEW_MODE or settings.SIMULATION_ENABLED):
        raise HTTPException(409, "Simulator is disabled")
    b = await request.json()
    name = b.get("name")
    if name not in ("safety_test", "leak_test", "none"):
        raise HTTPException(422, "Unknown scenario")
    minutes = int(_num(b.get("minutes", 3), 1, 30, "minutes"))
    until = (A.now_utc() + timedelta(minutes=minutes)).isoformat()
    with closing(get_db_connection()) as conn:
        set_setting(conn, "sim_scenario", None if name == "none" else {"name": name, "until": until})
        _audit(conn, user, "simulator_scenario", name, {"minutes": minutes})
        conn.commit()
    return {"ok": True, "scenario": name, "minutes": minutes}
