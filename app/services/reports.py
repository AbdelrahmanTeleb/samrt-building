"""PDF reports (ReportLab). Each report renders the SAME payload the dashboard shows."""
from __future__ import annotations

import contextvars
import io
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from reportlab.graphics.charts.barcharts import HorizontalBarChart, VerticalBarChart
from reportlab.graphics.charts.lineplots import LinePlot
from reportlab.graphics.shapes import Drawing, String
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

NAVY, BLUE, GREY = colors.HexColor("#0A1525"), colors.HexColor("#2F81F7"), colors.HexColor("#5B6B7F")
TITLES = {"overview": "Smart Building Overview Report", "energy": "Energy Report", "environment": "Environment Report",
          "occupancy": "Occupancy Report", "safety": "Safety Report"}
STATE_TXT = {"live": "LIVE", "simulated": "SIMULATED", "mixed": "MIXED (live + simulated)", "stale": "STALE", "no_data": "NO DATA",
             "not_configured": "NOT CONFIGURED", "none": "No devices in scope", "unavailable": "UNAVAILABLE"}
PROVENANCE = ("live", "simulated", "mixed")
# Per-render options (thread/request safe): whether provenance labels are printed, and the display timezone.
_OPT: contextvars.ContextVar = contextvars.ContextVar("report_opt", default=None)


def _opt():
    return _OPT.get() or {"labels": True, "tz": timezone.utc}


def st(state, default=None):
    """Printable state. With labels hidden, a reporting data source is shown simply as OK."""
    if not _opt()["labels"] and state in PROVENANCE:
        return "OK"
    return STATE_TXT.get(state, default if default is not None else str(state).upper())


def lt(iso, with_tz=False):
    """ISO/SQL timestamp (UTC) -> 'dd Mon yyyy HH:MM' in the configured timezone."""
    if not iso:
        return "-"
    try:
        d = datetime.fromisoformat(str(iso).replace("Z", "+00:00").replace(" ", "T"))
    except ValueError:
        return str(iso)[:16]
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    d = d.astimezone(_opt()["tz"])
    return d.strftime("%d %b %Y %H:%M") + (f" ({d.tzname()})" if with_tz else "")


ss = getSampleStyleSheet()
H1 = ParagraphStyle("h1", parent=ss["Title"], textColor=NAVY, fontSize=20, alignment=0, spaceAfter=4)
H2 = ParagraphStyle("h2", parent=ss["Heading2"], textColor=BLUE, fontSize=12, spaceBefore=10, spaceAfter=4)
P = ParagraphStyle("p", parent=ss["BodyText"], fontSize=9, leading=12)
SM = ParagraphStyle("sm", parent=P, fontSize=7.5, textColor=GREY)
CELL = ParagraphStyle("cell", parent=P, fontSize=7.5, leading=9, textColor=colors.black)


def esc(s):
    # The built-in PDF fonts have no subscript digits: CO₂ -> CO2.
    return str(s).replace("₂", "2").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def fv(v, unit="", nd=1):
    if v is None:
        return "n/a"
    if isinstance(v, float):
        v = round(v, nd)
    return f"{v:g} {unit}".strip() if isinstance(v, (int, float)) else f"{v} {unit}".strip()


def cmp_txt(c):
    if not c or not c.get("available"):
        return "no comparison available"
    pct = "baseline is zero" if c["pct"] is None else f"{c['pct']:+.1f}%"
    return f"{c['diff']:+g} {c.get('unit', '')} ({pct}) vs previous period".replace("  ", " ")


def table(rows, widths=None, head=True):
    rows = [[c.replace("₂", "2") if isinstance(c, str) else c for c in r] for r in rows]
    t = Table(rows, colWidths=widths, repeatRows=1 if head else 0)
    st = [("FONTSIZE", (0, 0), (-1, -1), 8), ("GRID", (0, 0), (-1, -1), .25, colors.HexColor("#C9D3E0")), ("VALIGN", (0, 0), (-1, -1), "TOP"),
          ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F2F6FB")])]
    if head:
        st += [("BACKGROUND", (0, 0), (-1, 0), NAVY), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white), ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold")]
    t.setStyle(TableStyle(st))
    return t


def line_chart(series, title, unit, width=170 * mm, height=55 * mm):
    d = Drawing(width, height)
    d.add(String(0, height - 8, f"{title} ({unit})", fontSize=8, fillColor=NAVY))
    if len(series) < 2:
        d.add(String(10, height / 2, "Not enough data points in this period", fontSize=8, fillColor=GREY))
        return d
    lp = LinePlot()
    lp.x, lp.y, lp.width, lp.height = 38, 18, width - 50, height - 34
    lp.data = [[(t, v) for t, v in series]]
    lp.lines[0].strokeColor, lp.lines[0].strokeWidth = BLUE, 1.2
    lp.xValueAxis.valueSteps = [series[0][0], series[len(series) // 2][0], series[-1][0]]
    lp.xValueAxis.labelTextFormat = lambda t: datetime.fromtimestamp(t, timezone.utc).strftime("%d %b %H:%M")
    lp.xValueAxis.labels.fontSize = lp.yValueAxis.labels.fontSize = 6.5
    d.add(lp)
    return d


def bar_chart(items, title, unit, horizontal=True, width=170 * mm):
    items = [(k, v) for k, v in items if v is not None][:10]
    height = max(30 * mm, 8 * mm * len(items) + 12 * mm)
    d = Drawing(width, height)
    d.add(String(0, height - 8, f"{title} ({unit})", fontSize=8, fillColor=NAVY))
    if not items:
        d.add(String(10, height / 2, "No data", fontSize=8, fillColor=GREY))
        return d
    ch = HorizontalBarChart() if horizontal else VerticalBarChart()
    ch.x, ch.y, ch.width, ch.height = 70 if horizontal else 30, 8, width - (85 if horizontal else 40), height - 24
    seq = list(reversed(items)) if horizontal else items
    ch.data = [[v for _, v in seq]]
    ch.categoryAxis.categoryNames = [str(k)[:16] for k, _ in seq]
    ch.categoryAxis.labels.fontSize = ch.valueAxis.labels.fontSize = 6.5
    ch.bars[0].fillColor = BLUE
    ch.valueAxis.valueMin = 0
    d.add(ch)
    return d


def kpi_table(rows):
    return table([["Metric", "Value", "Status / comparison"]] + [[a, b, Paragraph(esc(c), SM)] for a, b, c in rows], [50 * mm, 38 * mm, 82 * mm])


def header_block(rtype, data, f, names, user):
    m = data["meta"]
    floors, _ = names["floors"]
    scope = []
    for k, key, lbl in (("building_id", "building", "Building"), ("floor_id", "floor", "Floor")):
        if f.get(k):
            r = next((x for x in floors if x[k] == f[k]), None)
            scope.append(f"{lbl}: {esc(r[key]) if r else f[k]}")
    if f.get("room_id"):
        scope.append(f"Room id: {f['room_id']}")
    out = [Paragraph(TITLES[rtype], H1),
           Paragraph(f"<b>Scope:</b> {', '.join(scope) or 'All buildings you are authorised to view'}", P),
           Paragraph(f"<b>Period:</b> {lt(m['start'])} to {lt(m['end'], True)} &nbsp; "
                     f"<b>Generated:</b> {lt(m['generated_at'])} &nbsp; <b>By:</b> {esc(user.get('name') or user.get('email') or '')}", P),
           Paragraph((f"<b>Data source:</b> {st(m['source'], m['source'])} &nbsp; " if _opt()["labels"] else "")
                     + f"<b>Devices in scope:</b> {m['device_count']}", P)]
    if _opt()["labels"] and m["source"] in ("simulated", "mixed"):
        out.append(Paragraph("<font color='#B45309'><b>Notice:</b> this report contains SIMULATED demonstration data, not physical sensor readings.</font>", P))
    if m["device_count"] == 0:
        out.append(Paragraph("<b>No devices are in scope for this selection; all values below are unavailable.</b>", P))
    return out


AL_UNIT = {"co2": "ppm", "temperature": "°C", "humidity": "%", "pm25": "µg/m³", "pm10": "µg/m³"}


def reading_txt(a):
    """'Detected' for switch-type sensors, otherwise value + unit and the limit that was crossed."""
    if a.get("operator") == "==" or a.get("field") in ("smoke", "leak", "door_open", "gas"):
        return "Open" if a.get("field") == "door_open" else "Detected"
    u = AL_UNIT.get(a.get("field"), "")
    val = fv(a["value"], u)
    if a.get("threshold") is None:
        return val
    return f"{val} ({'min.' if a.get('operator') == '<' else 'limit'} {fv(a['threshold'], u)})"


def alarm_rows(al, n=25):
    rows = [["Time", "Device", "Event", "Sev.", "Location", "Reading", "Status"]]
    for a in al[:n]:
        loc = " / ".join(x for x in (a["building"], a["floor"], a["room"]) if x)
        rows.append([Paragraph(lt(a["time"]), CELL), Paragraph(esc(a.get("device") or a["device_id"]), CELL), Paragraph(esc(a["event"] or a["type"]), CELL), (a["severity"] or "").capitalize(),
                     Paragraph(esc(loc) + ("" if a["mapped"] else " (not on plan)"), CELL), Paragraph(esc(reading_txt(a)), CELL), a["status"].capitalize()])
    return table(rows, [22 * mm, 27 * mm, 32 * mm, 15 * mm, 34 * mm, 32 * mm, 18 * mm]) if len(rows) > 1 else Paragraph("No alarms in this period.", P)


def _krow(name, k, extra=""):
    return [name, fv(k["value"], k["unit"]), f"{st(k['state'], k['state'])}; {cmp_txt(k['comparison'])}{extra}"]


def story_overview(d):
    k, e = d["kpis"], d["energy"]
    rows = [_krow(n, k[m]) for n, m in (("Temperature", "temperature"), ("Humidity", "humidity"), ("CO2", "co2"), ("Occupancy (estimate)", "occupancy"),
                                        ("Electrical power", "power_w"), ("Water flow", "water_lpm"), ("Light intensity", "lux"), ("PM2.5", "pm25"))]
    return [Paragraph("Executive summary", H2),
            Paragraph(f"Safety state: <b>{d['safety']['state'].upper()}</b>. Energy in the period: <b>{fv(e['total_kwh'], 'kWh')}</b> ({cmp_txt(e['comparison'])}). "
                      f"{len(d['alarms'])} recent alarm(s) listed below.", P), Paragraph("Key indicators", H2), kpi_table(rows),
            Paragraph("Trends", H2), line_chart(d["trends"]["temperature"], "Temperature", "°C"), line_chart(d["trends"]["power_w"], "Electrical power", "W"),
            bar_chart(list(d["by_room_kwh"].items()), "Energy by room", "kWh"), Paragraph("Building systems", H2),
            table([["System", "State", "Note"]] + [[x["name"], st(x["state"], x["state"].upper()), x.get("note", "")] for x in d["systems"]]),
            Paragraph("Recent alarms", H2), alarm_rows(d["alarms"])]


def story_energy(d):
    k, ps, cost = d["kpis"], d["power_stats"], d["kpis"]["cost"]
    rows = [["Energy consumed", fv(k["energy_kwh"]["value"], "kWh", 2), cmp_txt(k["energy_kwh"]["comparison"]) + f"; method: {', '.join(k['energy_kwh']['method']) or 'n/a'}"],
            ["Estimated cost", f"{cost['value']} {cost['currency']}" if cost["value"] is not None else "unavailable", cost["unavailable_reason"] or cmp_txt(cost["comparison"])],
            ["Peak power", fv(k["peak_power"]["value"], "W", 0), cmp_txt(k["peak_power"]["comparison"])],
            ["Average / min / max demand", f"{fv(ps['avg'], 'W', 0)} / {fv(ps['min'], 'W', 0)} / {fv(ps['max'], 'W', 0)}", f"{ps['n']} aggregated samples"],
            ["Renewable share", fv(k["renewable"]["value"], "%", 0) if k["renewable"]["value"] is not None else "n/a", k["renewable"]["note"]]]
    daily = list(d["daily_kwh"].items())
    return [Paragraph("Executive summary", H2),
            Paragraph("Energy (kWh) is distinguished from power (W): energy is metered or integrated from power over elapsed time; hours without readings are "
                      f"treated as unknown, never zero. Abnormal-demand samples (&gt; mean + 2 sd): <b>{len(d['abnormal'])}</b>.", P),
            kpi_table(rows), Paragraph("Charts", H2), line_chart(d["power_series"], "Power demand", "W"),
            bar_chart([(a[5:], v) for a, v in daily[-14:]], "Daily energy", "kWh", horizontal=False), bar_chart(d["by_room"], "Energy by room", "kWh"),
            Paragraph("Distribution by area", H2), table([["Area", "kWh"]] + [[a, f"{v:g}"] for a, v in d["distribution"]]) if d["distribution"] else Paragraph("No energy data.", P)]


def story_environment(d):
    k = d["kpis"]
    rows = [_krow(n, k[m], f"; threshold status: {k[m]['status'] or 'n/a'}") for n, m in (("Temperature", "temperature"), ("Humidity", "humidity"), ("CO2", "co2"), ("PM2.5", "pm25"), ("Light", "lux"))]
    st = d["stats"]
    srows = [["Metric", "Min", "Average", "Max"]] + [[m, st[m]["min"], st[m]["avg"], st[m]["max"]] for m in st]
    score, over = d["comfort"]["score"], d["minutes_co2_above_warning"]
    return [Paragraph("Executive summary", H2),
            Paragraph(f"Comfort score: <b>{score if score is not None else 'n/a'}</b> / 100. Method: {esc(d['comfort']['method'])}. "
                      f"Time with CO2 above the configured warning limit: <b>{over if over is not None else 'n/a'}</b> min.", P),
            kpi_table(rows), Paragraph("Statistics", H2), table(srows), Paragraph("Charts", H2),
            line_chart(d["series"]["temperature"], "Temperature", "°C"), line_chart(d["series"]["humidity"], "Humidity", "%"),
            bar_chart([(r["room"], r["value"]) for r in d["co2_by_room"]], "CO2 by room (latest)", "ppm"), line_chart(d["series"]["lux"], "Light intensity", "lux")]


def story_occupancy(d):
    k, cur = d["kpis"], d["kpis"]["current"]
    rows = [["Current occupancy", fv(cur["value"], "people", 0) + " (estimate)", f"{st(cur['state'], cur['state'])}; {cmp_txt(cur['comparison'])}"],
            ["Utilisation", fv(k["utilization"]["value"], "%"), "Needs configured room capacity" if k["utilization"]["value"] is None else "Rooms with a capacity only"],
            ["Observed peak period", k["peak_hours"]["value"] or "n/a", k["peak_hours"]["note"] or "From the hourly profile of this period"],
            ["Rooms in scope", k["rooms"]["value"], ""]]
    tr = [["Room", "Occupancy", "Capacity", "Utilisation", "State"]]
    for r in d["by_room"]:
        cap = "n/a" if r["capacity"] is None else f"{r['capacity']}" + (" (demo)" if r["capacity_source"] == "demo" and _opt()["labels"] else "")
        tr.append([Paragraph(esc(r["room"]), SM), fv(r["occupancy"], "", 0), cap, fv(r["utilization"], "%"), st(r["state"], r["state"])])
    reg = d["scatter"]["regression"]
    return [Paragraph("Executive summary", H2),
            Paragraph("Occupancy is estimated from presence sensors (no cameras). Utilisation is only calculated where a room capacity is configured."
                      + (f" Occupancy-power correlation r = {reg['r']}." if reg else " Not enough data for an occupancy-power correlation."), P),
            kpi_table(rows), Paragraph("Charts", H2), line_chart(d["series"], "Occupancy", "people"),
            bar_chart([(r["room"], r["occupancy"]) for r in d["by_room"]], "Occupancy by room", "people"),
            Paragraph("Rooms", H2), table(tr, [60 * mm, 25 * mm, 28 * mm, 25 * mm, 28 * mm])]


def story_safety(d):
    c = d["counts"]
    rows = [[x["label"], st(x["state"], x["state"].upper()), str(x.get("value", ""))] for x in d["cards"]]
    return [Paragraph("Executive summary", H2),
            Paragraph(f"{c['total']} alarm(s) in the period, {c['active']} active, {c['critical']} critical"
                      f"{', ' + str(c['simulated']) + ' simulated' if c['simulated'] and _opt()['labels'] else ''}. "
                      "A missing sensor is reported as NOT CONFIGURED / NO DATA, never as normal; acknowledgement does not imply resolution.", P),
            Paragraph("Safety systems", H2), table([["System", "State", "Detail"]] + rows, [70 * mm, 50 * mm, 50 * mm]),
            Paragraph("Alarms and threshold violations", H2), alarm_rows(d["alarms"], 60)]


STORIES = {"overview": story_overview, "energy": story_energy, "environment": story_environment, "occupancy": story_occupancy, "safety": story_safety}


def render_report(rtype, data, filters, names, user) -> bytes:
    buf, title = io.BytesIO(), TITLES[rtype]
    labels = data["meta"].get("labels", True) is not False
    try:
        tz = ZoneInfo(data["meta"].get("timezone") or "UTC")
    except Exception:
        tz = timezone.utc
    token = _OPT.set({"labels": labels, "tz": tz})
    src = st(data["meta"]["source"], data["meta"]["source"])

    def deco(canvas, doc):
        canvas.saveState()
        canvas.setFillColor(NAVY)
        canvas.rect(0, A4[1] - 10 * mm, A4[0], 10 * mm, stroke=0, fill=1)
        canvas.setFillColor(colors.white)
        canvas.setFont("Helvetica-Bold", 8)
        canvas.drawString(15 * mm, A4[1] - 6.5 * mm, "SMART BUILDING MANAGEMENT SYSTEM")
        canvas.drawRightString(A4[0] - 15 * mm, A4[1] - 6.5 * mm, title)
        canvas.setFillColor(GREY)
        canvas.setFont("Helvetica", 7.5)
        canvas.drawString(15 * mm, 8 * mm, f"Data source: {src}" if labels else f"Generated {lt(data['meta']['generated_at'], True)}")
        canvas.drawRightString(A4[0] - 15 * mm, 8 * mm, f"Page {doc.page}")
        canvas.restoreState()

    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm, topMargin=18 * mm, bottomMargin=16 * mm, title=title, author="Smart Building Management System")
    try:
        doc.build(header_block(rtype, data, filters, names, user) + STORIES[rtype](data) + [Spacer(1, 6)], onFirstPage=deco, onLaterPages=deco)
    finally:
        _OPT.reset(token)
    return buf.getvalue()
