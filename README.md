# Smart Building Management System

FastAPI-based smart-building platform for LoRaWAN provisioning, telemetry,
floor maps, alarms, gateways, client access and firmware tooling.

## Requirements

- Python **3.10+**
- pip

## Install and run

### Windows

```powershell
.\windows\install.ps1
.\windows\run.bat
```

### Linux

```bash
bash linux/install.sh
bash linux/run.sh
```

Direct entry point:

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## Configuration

### Dashboard preview

Run `python tools/preview_dashboard.py` (or `windows\preview.bat`) and open
`http://localhost:8001/admin`. Sign in with `admin@preview.local` and
`Preview123!`. The two client accounts are `client1@preview.local` and
`client2@preview.local`, with the same preview password.

The launcher creates `build/dashboard-preview.db`, separate from the configured
database. It includes two clients, sites, buildings, floors, rooms, devices,
gateways, sensor profiles, sensors, firmware modules, client access records,
maps, alarm recipients, alarms and audit events, plus sample telemetry history.
One device and gateway are online and the other pair is offline. Preview mode
simulates gateway health and refreshes local readings; TTN and ThingsBoard
connections and alert emails are disabled. Authentication and client scope
checks still apply. Re-running preserves changes made in the preview workspace.
Use `--seed-only` to prepare the sample database without starting a server,
or `--port 8002` to choose another local port.

### Analytics dashboards (`/dashboards/...`)

An 8-page analytics workspace (Overview, Energy, Environment, Occupancy, Safety,
Floor Plan, Reports, Settings) is available to signed-in admin and client users
from the **Dashboards** item in the main navigation. Client users only see the
sites/buildings/floors granted in `user_access`; settings, capacities, markers
and simulator scenarios are admin-only.

* **Run the demo:** `pip install -r requirements.txt`, then
  `python tools/preview_dashboard.py --port 8001` and open
  `http://localhost:8001/dashboards/overview` (login `admin@preview.local` /
  `Preview123!`). Preview mode starts the Python simulator automatically.
* **Simulator** (`app/services/sensor_simulator.py`): runs only when
  `PREVIEW_MODE=true` or `SIMULATION_ENABLED=true`; production default is off and
  nothing is fabricated. It is a single asyncio task guarded by a database lease
  (safe with several workers), writes to `historical_telemetry` and
  `device_latest_telemetry` only for devices whose `data_source` is `simulated`
  (or `preview`), and never touches real devices. `SIMULATION_INTERVAL_SECONDS`
  (default 5) sets the cadence. Everything it produces is labelled SIMULATED.
* **Labels and alarms:** `SHOW_DATA_SOURCE_LABELS=false` (or Settings > Data-source
  labels > Hide) removes every LIVE/SIMULATED/demo label from the dashboards and PDFs;
  the database still records provenance (`devices.data_source`, `alarm_history.source`).
  The generator drives values across the warning/critical limits configured in Settings
  (CO2 in teaching rooms, PM2.5/humidity in the canteen, HVAC-fault overheating), so
  alarms open, escalate, keep their peak reading and auto-resolve. Generated alarms
  never send e-mail.
* **Real sensors** use the unchanged TTN webhook / ingest path and are labelled LIVE.
  Data older than the freshness window (Settings, default 300 s) is STALE; a metric
  with no reading is NO DATA / NOT CONFIGURED, never zero and never "normal".
* **Migration** `012_dashboards.py` is additive (new `devices.data_source` column,
  `dashboard_settings`, `map_buildings`, `map_rooms`, two indexes) and is applied by
  the normal startup migration runner; no data is reset.
* **Maps:** `static/images/university_campus_map.jpg` and
  `university_floor_1_plan.png`. Room polygons in `app/data/map_config.json` were
  derived by colour segmentation (`python tools/extract_floor_polygons.py`, needs
  `pip install -r requirements-tools.txt`) and should be reviewed. Campus letters
  (A, B, C, D, G, N) are read from the drawing; mapping a letter to a real building
  is **unconfirmed** until set in Settings. Capacities are blank in production; the
  demo uses clearly labelled placeholder capacities.
* **Reports:** Overview, Energy, Environment, Occupancy and Safety PDFs
  (`/api/reports/<type>/pdf`, ReportLab) built from the same data as the dashboards,
  honouring the building/floor/room/period filters.
* **Tests:** `python -m pytest tests/test_dashboards.py`.

Copy `.env.example` to `.env` and edit it for your environment.

The active names include `DB_FILE`, TTN settings, `TB_USERNAME`, `TB_PASSWORD`,
`ALERT_EMAIL_FROM`, `ALERT_EMAIL_TO`, `ADMIN_EMAIL`, `ADMIN_PASSWORD`,
`COOKIE_SECURE`, and `LOCAL_TEST_MODE`.

Two settings gate real security behaviour:

- **The development TTN is local.** Host-side tools use
  `http://localhost:1885`; Docker Compose routes the application container to
  the same stack through `http://host.docker.internal:1885`.
- **`TTN_WEBHOOK_SECRET` is required.** `/ttn-webhook` returns 503 to every
  request while it is empty. Set the same value in the TTN webhook's
  `X-Webhook-Secret` header.
- **`CORS_ORIGINS`** is a comma-separated list of front-end origins. Empty
  means same-origin only; there is no wildcard.

Set `COOKIE_SECURE=true` for any deployment served over HTTPS.

## Portals

- Admin: `/admin`
- Client: `/client-portal`
- API docs: `/docs`
- Readiness check: `/healthz`

Provisioning and provisioning-option endpoints require an authenticated admin
session because provisioning responses contain LoRaWAN root credentials. TTN
uplinks use the separate `/ttn-webhook` endpoint and authenticate with
`X-Webhook-Secret`.

Device placement is floor-based: every device must be assigned to a valid
floor (and may optionally be assigned to a room). Site-only and building-only
device placement is rejected.

To change the sensor profile of an existing device, open **Devices**
(`/device-capabilities-manager`), choose its **Sensor profile**, and click
**Change profile**. Select a profile matching the device's firmware; for the
ESP32-CAM meter-reader node use `ESPCAM_METER_V1`. This updates the device's
profile versions, capabilities, and TTN decoder while preserving its identity,
credentials, placement, and flashed firmware. Only enabled, active profiles
are offered. If TTN synchronization fails, the assignment remains saved and
**Retry decoder sync** completes it once the network server is available.

## LoRaWAN discovery and commissioning

Every board uses the configured `JOIN_EUI` and `LORAWAN_APP_KEY`. Its unique
DevEUI is derived deterministically from the TTN application ID and ESP32 MAC
using the same transformation in
`firmware/lilygo_cayenne_lpp_node/lilygo_cayenne_lpp_node.ino`. The
application namespace prevents the same board from colliding with an older TTN
application registration.

Generate the shared firmware credential header after configuring `.env`:

```powershell
python tools/generate_lorawan_credentials.py
```

Register a batch in TTN before field deployment without creating backend rows:

```powershell
python tools/register_ttn_devices.py --csv device_inventory.csv
```

The CSV requires `chip_mac` and may include `device_id`. Use `--dry-run` to
inspect the derived identities. The first accepted TTN webhook then creates the
backend device automatically with `is_placed = 0`. Telemetry and radio metadata
are stored, but alarms remain suppressed until an administrator drags the node
onto a floor and saves its position. Room assignment is optional.

Run the field acceptance check with one command:

```powershell
python tools/hardware_acceptance.py --device-id node-aabbccddeeff --timeout 300
```

Add `--require-commissioned` when the test must also verify floor placement.

For a controlled migration of an older database, first run reconciliation and
TTN synchronization in dry-run mode, then apply each operation:

```powershell
python tools/reconcile_legacy_database.py --source backups/retired-databases-20260912/devices.db --target smarthome.db
python tools/reconcile_legacy_database.py --source backups/retired-databases-20260912/devices.db --target smarthome.db --apply
python tools/sync_ttn_credentials.py
python tools/sync_ttn_credentials.py --apply
```

Both apply commands create timestamped database backups. TTN synchronization
never prints the shared AppKey and restores the previous TTN identity if a
replacement registration fails.

Device connection states are `never_connected`, `online`, `stale`, and
`offline`. `DEVICE_STALE_AFTER_SECONDS` and `DEVICE_OFFLINE_AFTER_SECONDS`
control the boundaries.

## Database

SQLite is the default. Startup bootstraps the current compatibility schema and
then runs tracked non-destructive migrations in `app/db/migrations/`.

The project is migrating incrementally to the canonical hierarchy:

`Client -> Site -> Building -> Floor -> Room -> Device`

Legacy location columns/tables are intentionally retained until all routes use
relational joins and migration tests pass against a copy of real data.

## Deployment (Docker)

```bash
cp .env.example .env   # then edit .env for your environment
docker compose up --build
```

This builds the image, starts the `sbms-app` container, and publishes it on
`http://localhost:8000`. The SQLite database file persists across container
restarts and rebuilds in the `sbms-data` named Docker volume (mounted at
`/app/data` inside the container); uploaded files persist in the `./uploads`
bind mount. The container runs as an unprivileged, non-root user (uid 1000).

### Editing templates and static files

`templates/` and `static/` are baked into the image by `COPY`, so a plain
`docker compose up -d` would need `--build` before an edit shows up.
`docker-compose.override.yml` mounts both read-only for local development, and
Compose merges it automatically:

```bash
docker compose up -d
```

Edits then appear on the next request -- no rebuild, no restart (Jinja2
auto-reloads templates, and StaticFiles reads from disk per request). Hard-refresh
the browser (Ctrl+Shift+R) if a page looks stale; a service worker is registered.

Deploy **without** the override so the image stays self-contained:

```bash
docker compose -f docker-compose.yml up -d --build
```

Python code changes still need a rebuild either way.

### Networks that intercept TLS

The build fetches packages from PyPI, so on a network with a TLS-inspecting
proxy it needs that proxy's root certificate or the build fails with
`CERTIFICATE_VERIFY_FAILED`. Export the root certificate (DER format) to
`.docker-local-ca.cer` and add this to `.env`:

```
SBMS_LOCAL_CA_FILE=.docker-local-ca.cer
```

The certificate is installed into the image trust store (so outbound HTTPS to
TTN also works at runtime) and is never committed. By default compose points at
the empty `.docker-local-ca.cer.example` placeholder, which the build reads as
"no corporate CA" -- so an ordinary network needs no setup at all.

The `sbms-data` named volume is initialized with the correct ownership
automatically (Docker copies it from the image on first mount). The
`./uploads` bind mount is different: its permissions come from the *host*
directory, not the image. On a Linux host, if the container ever fails to
write to `/app/uploads` with a permission error, run
`sudo chown -R 1000:1000 uploads` (or `chmod 777 uploads`) on the host once;
Docker Desktop on Windows/macOS does not need this.

Required environment variables (set in `.env`, loaded via `env_file` in
`docker-compose.yml`):

- `TTN_WEBHOOK_SECRET`
- `ADMIN_EMAIL` / `ADMIN_PASSWORD`
- `DB_FILE` (already set to `/app/data/smarthome.db` by `docker-compose.yml`)
- `COOKIE_SECURE=true` for any deployment served over HTTPS

For this local Docker setup, configure the TTN webhook with base URL
`http://host.docker.internal:8000`, uplink path `/ttn-webhook`, and the
`X-Webhook-Secret` header. A public URL is not required while both services run
on this Docker Desktop host.

> **Telemetry ingestion is dead until you set `TTN_WEBHOOK_SECRET`.**
> `.env.example` ships this blank on purpose. While it is empty, `/ttn-webhook`
> fails closed and returns `503` to every request, so **no LoRaWAN telemetry
> is accepted at all**. Before going live: set `TTN_WEBHOOK_SECRET` in your
> `.env` to a real random value, and configure the **exact same value** in the
> TTN application's webhook integration, in the `X-Webhook-Secret` header.

On a PaaS that injects its own port (Render/Railway/Fly/etc.), the container
honors `PORT` — pass `-e PORT=<port>` (or the platform's equivalent) and the
process binds to it instead of the default `8000`.

## Verification

`pytest` and `ruff` are dev-only tools and are not in `requirements.txt` (the
production image only installs runtime dependencies). Install them first:

```bash
pip install -r requirements-dev.txt
```

```bash
python -m compileall -q app tests tools
pytest -q
ruff check app tests tools/register_ttn_devices.py tools/hardware_acceptance.py tools/generate_lorawan_credentials.py tools/configure_local_ttn.py tools/reconcile_legacy_database.py tools/sync_ttn_credentials.py
docker compose config --quiet
```

These checks and a Docker image build also run in `.github/workflows/ci.yml`.

On a managed network that intercepts TLS, export the organization root
certificate to `.docker-local-ca.cer` and build without weakening verification:

```powershell
docker build --secret id=sbms_local_ca,src=.docker-local-ca.cer -t smart-building-management-system:test .
```
