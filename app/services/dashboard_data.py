"""Section payload builders. One implementation feeds both the live dashboards and the PDFs."""
from __future__ import annotations

import json
import statistics
from collections import defaultdict
from datetime import datetime, timedelta

from app.services import analytics as A

ALL_METRICS = ["temperature", "humidity", "co2", "pm25", "lux", "occupancy", "power_w", "water_lpm"]
SAFETY_CATS = [("smoke", "Smoke / fire"), ("leak", "Water leak"), ("gas", "Gas"), ("door_open", "Doors / windows")]


class Ctx:
    def __init__(self, conn, user, f):
        self.conn, self.user, self.f = conn, user, f
        self.c = A.cfg(conn)
        self.start, self.end, self.bucket, self.range = A.resolve_period(f.get("range"), f.get("start"), f.get("end"))
        self.span = self.end - self.start
        self.pstart = self.start - self.span
        self.devices = A.scope_devices(conn, user, f.get("building_id"), f.get("floor_id"), f.get("room_id"), f.get("device_id"))
        self.ids = [d["device_id"] for d in self.devices]
        self.lat = A.latest(conn, self.devices)
        self.by_id = {d["device_id"]: d for d in self.devices}

    def meta(self):
        ts = [t for _, t in self.lat.values() if t]
        return {"generated_at": A.now_utc().isoformat(), "range": self.range, "start": self.start.isoformat(), "end": self.end.isoformat(),
                "previous_start": self.pstart.isoformat(), "source": A.source_label(self.devices), "device_count": len(self.devices),
                "last_update": max(ts).isoformat() if ts else None, "freshness_seconds": self.c["freshness_seconds"],
                "timezone": self.c["timezone"], "labels": self.c["show_source_labels"], "filters": {k: v for k, v in self.f.items() if v is not None}}

    def rows(self, metrics, with_max=()):
        return A.bucket_rows(self.conn, self.ids, self.pstart, self.end, self.bucket, metrics, with_max)

    def split(self, rows, metric, how=None):
        s = int(self.start.timestamp())
        full = A.combine(rows, metric, how)
        return [p for p in full if p[0] >= s], [p for p in full if p[0] < s]

    def room_label(self, d):
        return f"{d['room_number']} {d['label']}" if d.get("label") else (d["room"] or d["device_id"])

    def online(self, devs, lat=None):
        lat = lat or self.lat
        return sum(1 for d in devs if A.is_fresh(lat.get(d["device_id"], (None, None))[1], self.c))


def _cmp(cur, prev, unit):
    def agg(s):
        return sum(v for _, v in s) / len(s) if s else None
    c = A.compare(agg(cur), agg(prev), unit)
    c["basis"] = "period average vs previous period average"
    return c


def _kpis(x, rows, names):
    out = {}
    for m in names:
        k = A.kpi(x.devices, x.lat, m, x.c)
        cur, prev = x.split(rows, m)
        k["comparison"] = _cmp(cur, prev, A.METRICS[m][1])
        k["status"] = A.threshold_status(m, k["value"], x.c)
        out[m] = k
    return out


def safety_state(devs, lat, c, open_alarm_sev=None):
    """normal / warning / critical / stale / unavailable (never 'normal' without a fresh safety reading)."""
    seen = fresh = 0
    worst = "normal"
    for d in devs:
        tele, ts = lat.get(d["device_id"], (None, None))
        if not tele:
            continue
        for m in ("smoke", "leak", "gas", "door_open"):
            if not A.has_key(tele, m):
                continue
            seen += 1
            if A.point_state(d, tele, ts, m, c["freshness_seconds"]) not in ("live", "simulated"):
                continue
            fresh += 1
            if A.pick(tele, m) == 1:
                if m == "door_open":
                    worst = "warning" if worst == "normal" else worst
                else:
                    worst = "critical"
    if open_alarm_sev == "critical":
        worst = "critical"
    elif open_alarm_sev == "warning" and worst == "normal":
        worst = "warning"
    return "unavailable" if seen == 0 else "stale" if fresh == 0 else worst


def safety_cards(x):
    cards = []
    for m, label in SAFETY_CATS:
        k = A.kpi(x.devices, x.lat, m, x.c)
        if k["state"] in ("not_configured", "no_data", "stale"):
            state = k["state"]
        else:
            state = ("warning" if m == "door_open" else "critical") if k["value"] else "normal"
        cards.append({"key": m, "label": label, "state": state, "source": k["state"]})
    stale = len(x.devices) - x.online(x.devices)
    cards.append({"key": "devices", "label": "Devices offline/stale", "state": "warning" if stale else "normal", "value": stale, "total": len(x.devices)})
    return cards


def floor_plan(x, floor_id=None):
    conn, c = x.conn, x.c
    floors, rooms = A.hierarchy(conn, x.user)
    floors_by = {f["floor_id"]: f for f in floors}
    floor_id = floor_id or x.f.get("floor_id")
    if floor_id is None:  # first floor that has a mapped plan
        mapped = [r["floor_id"] for r in rooms if r.get("room_number")]
        floor_id = mapped[0] if mapped else None
    if floor_id not in floors_by:
        return {"floor": None, "rooms": [], "unmapped_rooms": []}
    fl = floors_by[floor_id]
    maprows = {r["room_id"]: dict(r) for r in conn.execute("SELECT m.* FROM map_rooms m JOIN rooms r ON r.id=m.room_id WHERE r.floor_id=?", (floor_id,))}
    sub = A.scope_devices(conn, x.user, floor_id=floor_id)
    lat = A.latest(conn, sub)
    open_alarms = defaultdict(list)
    for a in A.alarms(conn, sub, status="active", limit=500):
        if a["room_id"]:
            open_alarms[a["room_id"]].append(a)
    heat = c["heatmap"]
    out, unmapped = [], []
    for r in rooms:
        if r["floor_id"] != floor_id:
            continue
        m = maprows.get(r["room_id"])
        if not m:
            unmapped.append({"room_id": r["room_id"], "name": r["room_name"]})
            continue
        devs = [d for d in sub if d["room_id"] == r["room_id"]]
        k = {mm: A.kpi(devs, lat, mm, c) for mm in ALL_METRICS}
        occ, cap = k["occupancy"]["value"], m["capacity"]
        util = round(occ / cap * 100, 1) if occ is not None and cap else None
        heatc = "unavailable" if occ is None else "unknown_capacity" if util is None else "high" if util >= heat["high"] else "medium" if util >= heat["medium"] else "low"
        sev = "critical" if any(a["severity"] == "critical" for a in open_alarms[r["room_id"]]) else "warning" if open_alarms[r["room_id"]] else None
        out.append({"room_id": r["room_id"], "room_number": m["room_number"], "label": m["label"], "name": r["room_name"], "room_type": m["room_type"],
                    "polygon": json.loads(m["polygon_json"]), "capacity": cap, "capacity_source": m["capacity_source"],
                    "metrics": {mm: {"value": v["value"], "unit": v["unit"], "state": v["state"], "status": A.threshold_status(mm, v["value"], c)} for mm, v in k.items()},
                    "utilization": util, "occupancy_exact": False, "heat": heatc, "safety": safety_state(devs, lat, c, sev),
                    "devices_total": len(devs), "devices_online": x.online(devs, lat), "active_alarms": len(open_alarms[r["room_id"]]),
                    "source": A.source_label(devs)})
    return {"floor": {"floor_id": floor_id, "name": fl["floor"], "building_id": fl["building_id"], "building": fl["building"],
                      "image": fl["image_path"], "width": fl["image_width"], "height": fl["image_height"]},
            "rooms": out, "unmapped_rooms": unmapped, "heat_cutoffs": heat}


def campus(x):
    conn, c = x.conn, x.c
    floors, rooms = A.hierarchy(conn, x.user)
    b_ids = sorted({f["building_id"] for f in floors})
    if not b_ids:
        return {"image": None, "buildings": [], "unplaced": []}
    q = ",".join("?" * len(b_ids))
    binfo = {r["id"]: dict(r) for r in conn.execute(
        f"SELECT b.id, b.name, b.site_id, s.campus_image_path img, s.image_width w, s.image_height h, m.code, m.x, m.y, m.confirmed "
        f"FROM buildings b JOIN sites s ON s.id=b.site_id LEFT JOIN map_buildings m ON m.building_id=b.id WHERE b.id IN ({q}) ORDER BY b.id", b_ids)}
    # A campus image belongs to one site: the selected building's site, else the site with the most placed markers.
    by_site = defaultdict(list)
    for bid, b in binfo.items():
        by_site[b["site_id"]].append(bid)
    sel = x.f.get("building_id")
    if sel in binfo:
        site = binfo[sel]["site_id"]
    else:
        site = max(by_site, key=lambda sid: (sum(1 for i in by_site[sid] if binfo[i]["x"] is not None), len(by_site[sid])))
    b_ids = sorted(by_site[site])
    binfo = {i: binfo[i] for i in b_ids}
    devs = [d for d in A.scope_devices(conn, x.user) if d["site_id"] == site]
    lat = A.latest(conn, devs)
    per_dev, _ = A.energy(conn, devs, A.now_utc() - timedelta(hours=24), A.now_utc(), c)
    al = A.alarms(conn, devs, status="active", limit=1000)
    markers, unplaced = [], []
    for bid in b_ids:
        b = binfo[bid]
        bd = [d for d in devs if d["building_id"] == bid]
        bdev = {d["device_id"] for d in bd}
        ts = [lat[i][1] for i in bdev if i in lat and lat[i][1]]
        e = [sum(per_dev[i].values()) for i in bdev if i in per_dev]
        bfloors = {f["floor_id"] for f in floors if f["building_id"] == bid}
        online = x.online(bd, lat)
        item = {"building_id": bid, "name": b["name"], "code": b["code"], "floors": len(bfloors),
                "rooms": sum(1 for r in rooms if r["floor_id"] in bfloors), "devices_online": online, "devices_offline": len(bd) - online,
                "energy_kwh_24h": round(sum(e), 2) if e else None, "temperature": A.kpi(bd, lat, "temperature", c)["value"],
                "co2": A.kpi(bd, lat, "co2", c)["value"], "active_alarms": sum(1 for a in al if a["device_id"] in bdev),
                "last_update": max(ts).isoformat() if ts else None, "source": A.source_label(bd), "confirmed": bool(b["confirmed"]), "x": b["x"], "y": b["y"]}
        (markers if b["x"] is not None else unplaced).append(item)
    first = next(iter(binfo.values()))
    return {"image": first["img"], "width": first["w"], "height": first["h"], "site_id": site, "buildings": markers, "unplaced": unplaced}


def _system(x, name, on_metric, fault_metric, noun, missing):
    """HVAC / lighting row from controller telemetry. No controller reporting -> not configured; stale -> stale."""
    states, on, faults, n = [], 0, 0, 0
    for d in x.devices:
        tele, ts = x.lat.get(d["device_id"], (None, None))
        st = A.point_state(d, tele, ts, on_metric, x.c["freshness_seconds"])
        if st == "not_configured":
            continue
        states.append(st)
        if st in ("live", "simulated"):
            n += 1
            on += 1 if A.pick(tele, on_metric) else 0
            if fault_metric and A.pick(tele, fault_metric):
                faults += 1
    if not states:
        return {"name": name, "state": "not_configured", "note": missing}
    if not n:
        return {"name": name, "state": "stale" if "stale" in states else "no_data", "note": "No fresh controller data"}
    sim = any(A.is_sim(d) for d in x.devices)
    return {"name": name, "state": "warning" if faults else "normal",
            "note": f"{on}/{n} {noun}" + (f"; {faults} fault(s)" if faults else "") + (" (SIMULATED)" if sim and x.c["show_source_labels"] else "")}


def overview(x):
    rows = x.rows(ALL_METRICS)
    k = _kpis(x, rows, ALL_METRICS)
    es, ep = A.energy_summary(x.conn, x.devices, x.start, x.end, x.c), A.energy_summary(x.conn, x.devices, x.pstart, x.start, x.c)
    cards = safety_cards(x)
    worst = "critical" if any(c["state"] == "critical" for c in cards) else "warning" if any(c["state"] == "warning" for c in cards) else \
        "normal" if any(c["state"] == "normal" for c in cards) else "unavailable"
    trends = {m: x.split(rows, m)[0] for m in ("temperature", "humidity", "co2", "pm25", "occupancy", "power_w")}
    by_room = defaultdict(float)
    for did, v in es["per_device"].items():
        by_room[x.room_label(x.by_id[did])] += v
    card = {c["key"]: c for c in cards}
    water_cfg = k["water_lpm"]["state"] != "not_configured" or card["leak"]["state"] != "not_configured"
    online = x.online(x.devices)
    systems = [
        _system(x, "HVAC", "hvac_on", "hvac_fault", "units running", "No HVAC controller telemetry configured"),
        _system(x, "Lighting", "light_on", None, "rooms lit", "No lighting controller configured"),
        {"name": "Water system", "state": ("critical" if card["leak"]["state"] == "critical" else "normal") if water_cfg else "not_configured"},
        {"name": "Fire system", "state": card["smoke"]["state"]},
        {"name": "Security (doors)", "state": card["door_open"]["state"]},
        {"name": "Sensors online", "state": "normal" if x.devices and online == len(x.devices) else "warning" if online else "no_data", "note": f"{online}/{len(x.devices)} reporting"}]
    return {"meta": x.meta(), "kpis": k, "safety": {"state": worst, "cards": cards},
            "energy": {"total_kwh": es["total_kwh"], "comparison": A.compare(es["total_kwh"], ep["total_kwh"], "kWh"), "daily": es["daily"]},
            "trends": trends, "by_room_kwh": dict(sorted(((r, round(v, 2)) for r, v in by_room.items()), key=lambda i: -i[1])[:10]),
            "systems": systems, "alarms": A.alarms(x.conn, x.devices, limit=8), "campus": campus(x),
            "floor_plan": floor_plan(x), "bucket_seconds": x.bucket}


def _renewable(x, total_kwh):
    gen = A.renewable_kwh(x.conn, x.devices, x.start, x.end)
    if gen is None:
        return {"value": None, "unit": "%", "state": "not_configured", "note": "No renewable-generation meter is configured"}
    if not total_kwh:
        return {"value": None, "unit": "%", "state": "no_data", "generated_kwh": gen, "note": "No consumption data to compare with"}
    return {"value": round(min(gen, total_kwh) / total_kwh * 100, 1), "unit": "%", "state": A.source_label(x.devices),
            "generated_kwh": gen, "note": f"{gen:.1f} kWh generated vs {total_kwh:.1f} kWh consumed"}


def energy_section(x):
    rows = x.rows(["power_w"], with_max=("power_w",))
    cur, prev = x.split(rows, "power_w")
    es, ep = A.energy_summary(x.conn, x.devices, x.start, x.end, x.c), A.energy_summary(x.conn, x.devices, x.pstart, x.start, x.c)
    peak = max((v for _, v in cur), default=None)
    by_room = defaultdict(float)
    for did, v in es["per_device"].items():
        by_room[x.room_label(x.by_id[did])] += v
    ranked = sorted(((k, round(v, 2)) for k, v in by_room.items()), key=lambda i: -i[1])
    tr = x.c["tariff"]
    abn = []
    if len(cur) >= 12:
        vals = [v for _, v in cur]
        mu, sd = statistics.mean(vals), statistics.pstdev(vals)
        abn = [[t, v] for t, v in cur if sd > 0 and v > mu + 2 * sd]
    return {"meta": x.meta(), "kpis": {
        "energy_kwh": {"value": es["total_kwh"], "unit": "kWh", "comparison": A.compare(es["total_kwh"], ep["total_kwh"], "kWh"),
                       "method": sorted(set(es["methods"].values())), "state": "no_data" if es["total_kwh"] is None else A.source_label(x.devices)},
        "cost": {"value": es["cost"], "currency": tr["currency"], "comparison": A.compare(es["cost"], ep["cost"], tr["currency"]),
                 "unavailable_reason": None if es["cost"] is not None else "No electricity tariff configured (Settings > Tariff)" if tr.get("rate_per_kwh") is None else "No energy data"},
        "peak_power": {"value": peak, "unit": "W", "comparison": A.compare(peak, max((v for _, v in prev), default=None), "W")},
        "current_power": A.kpi(x.devices, x.lat, "power_w", x.c),
        "renewable": _renewable(x, es["total_kwh"])},
        "power_series": cur, "power_stats": A.stats(cur), "previous_power_series": prev,
        "distribution": ranked[:8], "daily_kwh": es["daily"], "previous_daily_kwh": ep["daily"], "by_room": ranked[:10],
        "abnormal": abn, "tariff": {"currency": tr["currency"], "type": tr["tariff_type"], "configured": tr.get("rate_per_kwh") is not None},
        "bucket_seconds": x.bucket}


def environment(x):
    names = ["temperature", "humidity", "co2", "pm25", "lux"]
    rows = x.rows(names)
    k = _kpis(x, rows, names)
    comp = A.comfort_score(*(k[m]["value"] for m in ("temperature", "humidity", "co2", "pm25")))
    co2_rooms = []
    for d in x.devices:
        tele, ts = x.lat.get(d["device_id"], (None, None))
        st = A.point_state(d, tele, ts, "co2", x.c["freshness_seconds"])
        v = A.pick(tele, "co2") if tele else None
        ok = st in ("live", "simulated")
        co2_rooms.append({"room": x.room_label(d), "value": v if ok else None, "state": st, "status": A.threshold_status("co2", v, x.c) if ok else None})
    series = {m: x.split(rows, m)[0] for m in names}
    over, th = None, x.c["thresholds"]["co2"].get("warn")
    if series["co2"] and th:
        over = round(sum(1 for _, v in series["co2"] if v >= th) * x.bucket / 60)
    return {"meta": x.meta(), "kpis": k,
            "comfort": {"score": comp, "method": "100 - 5/°C outside 20-26 - 1/%RH outside 30-60 - 1 per 50 ppm CO2 above 800 - 1 per µg/m³ PM2.5 above 12"},
            "series": series, "stats": {m: A.stats(series[m]) for m in names}, "co2_by_room": co2_rooms,
            "minutes_co2_above_warning": over, "thresholds": x.c["thresholds"], "bucket_seconds": x.bucket}


def occupancy(x):
    rows = x.rows(["occupancy", "power_w"])
    cur, prev = x.split(rows, "occupancy")
    kp = A.kpi(x.devices, x.lat, "occupancy", x.c)
    kp["comparison"] = _cmp(cur, prev, "people")
    table, cap_sum, occ_sum = [], 0, 0
    for d in x.devices:
        tele, ts = x.lat.get(d["device_id"], (None, None))
        st = A.point_state(d, tele, ts, "occupancy", x.c["freshness_seconds"])
        v = A.pick(tele, "occupancy") if st in ("live", "simulated") else None
        cap = d["capacity"]
        util = round(v / cap * 100, 1) if v is not None and cap else None
        if v is not None and cap:
            cap_sum += cap
            occ_sum += v
        table.append({"room": x.room_label(d), "room_id": d["room_id"], "occupancy": v, "capacity": cap, "capacity_source": d["capacity_source"], "utilization": util, "state": st})
    tz = A.tzinfo(x.c["timezone"])
    prof = defaultdict(list)
    for t, v in cur:
        prof[datetime.fromtimestamp(t, tz).hour].append(v)
    hourly = {h: round(sum(v) / len(v), 1) for h, v in sorted(prof.items())}
    peak = None
    if len(hourly) >= 12:
        best = max(range(24), key=lambda h: sum(hourly.get((h + i) % 24, 0) for i in range(2)))
        peak = f"{best:02d}:00 - {(best + 2) % 24:02d}:00"
    pw = dict(x.split(rows, "power_w")[0])
    pts = [[o, round(pw[t] / 1000, 3)] for t, o in cur if t in pw]
    reg = None
    if len(pts) >= 10:
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        mx, my = statistics.mean(xs), statistics.mean(ys)
        sxx, syy = sum((a - mx) ** 2 for a in xs), sum((b - my) ** 2 for b in ys)
        if sxx > 0 and syy > 0:
            sxy = sum((a - mx) * (b - my) for a, b in pts)
            sl = sxy / sxx
            r = sxy / (sxx * syy) ** 0.5
            reg = {"slope": sl, "intercept": my - sl * mx, "r": round(r, 3), "show": abs(r) >= 0.3}
    _, rooms = A.hierarchy(x.conn, x.user)
    fl = x.f.get("floor_id")
    n_rooms = sum(1 for r in rooms if (fl is None or r["floor_id"] == fl) and (x.f.get("room_id") is None or r["room_id"] == x.f["room_id"]))
    return {"meta": x.meta(), "kpis": {"current": {**kp, "kind": "estimate"},
            "utilization": {"value": round(occ_sum / cap_sum * 100, 1) if cap_sum else None, "unit": "%", "state": "ok" if cap_sum else "capacity_not_configured"},
            "peak_hours": {"value": peak, "note": None if peak else "Not enough history to determine a peak period"}, "rooms": {"value": n_rooms}},
            "series": cur, "by_room": sorted(table, key=lambda r: -(r["occupancy"] if r["occupancy"] is not None else -1)), "hourly_profile": hourly,
            "scatter": {"points": pts, "regression": reg}, "floor_plan": floor_plan(x), "bucket_seconds": x.bucket}


def safety(x):
    al = A.alarms(x.conn, x.devices, x.start if x.f.get("range") or x.f.get("start") else None, x.end, x.f.get("severity"), x.f.get("status"), limit=200)
    counts = {"active": sum(1 for a in al if a["status"] == "active"), "total": len(al), "critical": sum(1 for a in al if a["severity"] == "critical"),
              "simulated": sum(1 for a in al if a["simulated"])}
    return {"meta": x.meta(), "cards": safety_cards(x), "alarms": al, "counts": counts, "floor_plan": floor_plan(x),
            "can_manage": (x.user.get("role") or "").lower() == "admin"}


BUILDERS = {"overview": overview, "energy": energy_section, "environment": environment, "occupancy": occupancy, "safety": safety,
            "floor-plan": lambda x: {"meta": x.meta(), "floor_plan": floor_plan(x)}}


def build(conn, user, section, f):
    return BUILDERS[section](Ctx(conn, user, f))
