"""Analytics dashboards: data-source flag, map metadata, settings. Non-destructive."""


def _cols(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def up(conn):
    # 'live' keeps every existing device exactly as it was; only rows explicitly
    # marked 'simulated' may ever be written by the demo simulator.
    if "data_source" not in _cols(conn, "devices"):
        conn.execute("ALTER TABLE devices ADD COLUMN data_source TEXT NOT NULL DEFAULT 'live'")
    conn.execute("""CREATE TABLE IF NOT EXISTS dashboard_settings (
        key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS map_buildings (
        building_id INTEGER PRIMARY KEY REFERENCES buildings(id) ON DELETE CASCADE,
        code TEXT, x REAL NOT NULL, y REAL NOT NULL, confirmed INTEGER NOT NULL DEFAULT 0)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS map_rooms (
        room_id INTEGER PRIMARY KEY REFERENCES rooms(id) ON DELETE CASCADE,
        room_number TEXT, label TEXT, room_type TEXT, capacity INTEGER,
        capacity_source TEXT, polygon_json TEXT NOT NULL, geometry_source TEXT)""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_hist_device_ts ON historical_telemetry(device_id, timestamp)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_devices_data_source ON devices(data_source)")
