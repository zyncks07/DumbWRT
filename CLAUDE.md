# CLAUDE.md — openwrt-monitor

A guide for any Claude session working on this project. Read this end-to-end before touching code.

---

## 1. Project Overview

A web app that monitors WiFi clients across a fleet of OpenWrt routers (currently 8, target up to **30**) from a single low-power **Intel Atom mini-PC**. It SSHes into each router and calls `ubus iwinfo` (the same mechanism LuCI uses) to enumerate radios, interfaces (2.4 GHz and 5 GHz), and associated clients. Routers share **one** SSH key.

### Hard constraints (these drive every design decision)

- **Slow CPU.** Atom-class. Avoid thread fan-out, framework overhead, per-poll subprocess forks, and heavy client-side rendering.
- **Limited storage.** Database, logs, and history must be retention-bounded. Nothing grows unbounded.
- **Intranet-only deployment.** The UI must render with the public internet down. No CDN in the critical path.
- **Single shared SSH key** for all routers. Whole system relies on this simplification.
- **Up to 30 routers.** Anything that's O(N) per second per router on the CPU is a non-starter at N=30.

---

## 2. Current Architecture (v1 — what is running right now)

### File map (absolute paths — these are real, not aspirational)

| Purpose | Path |
|---|---|
| Project root (canonical) | `/home/bulik/apps/openwrt-monitor/` |
| Flask web app | `/home/bulik/apps/openwrt-monitor/flask_app.py` (port 5000) |
| SSH/ubus collector daemon | `/home/bulik/apps/openwrt-monitor/ubus_collector.py` |
| HTML templates | `/home/bulik/apps/openwrt-monitor/templates/{login,dashboard,config,logs}.html` |
| Static assets | `/home/bulik/apps/openwrt-monitor/static/` — `fontawesome/`, `theme.css`, `theme.js`, `copy-utils.js` |
| Systemd unit copies in repo | `/home/bulik/apps/openwrt-monitor/system/openwrt-{collector,dashboard}.service` |
| Runtime config | `/etc/openwrt-monitor/config.json` |
| SSH client config | `/etc/openwrt-monitor/ssh_config` (ControlMaster multiplex) |
| SQLite database | `/var/lib/openwrt-monitor/monitor.db` |
| DB backups | `/var/lib/openwrt-monitor/monitor.db.backup-*` |
| Collector log | `/var/log/openwrt-collector.log` (logrotate config installed; 5 MB × 5) |
| Live systemd units | `/etc/systemd/system/openwrt-collector.service`, `openwrt-dashboard.service` — now `ExecStart` from the project root |
| Unrelated voucher app | `/var/www/openwrt-monitor/wifi/` — separate Node/Express app on port 3000; **NOT part of the monitor**, left in place when the monitor moved |

### Tables in `monitor.db`

- `routers` — `ip` PK, `hostname`, `online`, `last_seen`, `first_seen`
- `interfaces` — one row per radio interface per router; SSID, BSSID, freq, channel, bandwidth, mode, encryption, num_clients, **noise** (dBm), **bitrate** (kbit/s, BSS max rate), **txpower** (dBm). The three bold columns were added 2026-05-31 via a migration block in `init_db()` (try/except ALTER TABLE — safe to run repeatedly; follow this pattern for all future column additions).
- `clients` — associated stations; MAC, signal, signal_avg, noise, rx/tx_rate, rx/tx_packets, rx/tx_bytes, connected_time, inactive, authorized, last_seen, first_seen
- `voucher_sessions` — active captive-portal sessions from pfSense, keyed by lowercased `mac`. `voucher_code` (= CP `username`), `authmethod`, `allow_time` (unix session start), `session_timeout` (s), `last_activity` (unix, pf-state derived), `last_seen`. **Full-replaced each collector cycle** (live snapshot, not history). Added 2026-07-27 — see §12.
- `trusted_macs` — captive-portal allowed / pass-through MAC list (devices that bypass the portal, no voucher). `mac` PK (lowercased), `descr` (admin label), `last_seen`. Full-replaced each cycle. Added 2026-07-27 — see §12.

**Important:** the `clients` table is destructive — every poll cycle does `DELETE FROM clients WHERE router_ip=? AND interface=?` and re-inserts. **No history is kept.** That's the single biggest cause of "rich data being discarded" that this project complains about.

`interfaces` and `clients` rows for interfaces no longer present in `iwinfo devices` are deleted per-poll (orphan cleanup added after a disabled wifi-iface produced phantom SSIDs in the UI). `wifi_iface_config` carries the full UCI wifi-iface set (active + disabled) so the dashboard can render a "Disabled" badge for configured-but-off SSIDs alongside the active ones.

### Data flow

```
OpenWrt router
   │ ssh + ubus call iwinfo {devices, info, assoclist}
   ▼
ubus_collector.py  (one poll thread + one ping thread per router)
   │ subprocess.run(['ssh', ...])  ← per-poll fork; the dominant CPU cost
   ▼
SQLite  /var/lib/openwrt-monitor/monitor.db
   ▲
   │ sqlite3 queries
   │
flask_app.py  (port 5000, session auth)
   │ JSON over HTTP
   ▼
templates/*.html  (inline CSS+JS, polls via setInterval)
```

### Auth

- Credentials and Flask `secret_key` live in `/etc/openwrt-monitor/auth.json` (mode 600, root-owned).
- Password is stored as an Argon2id hash (`argon2-cffi` / Debian `python3-argon2`).
- `auth.py` is the only module that touches the file. `verify()` for login, `change_password()` for rotation from the UI.
- Bootstrap or reset from the CLI with `sudo python3 scripts/init_auth.py`.
- `secret_key` is stable across restarts, so sessions survive a service restart.

### Systemd

```
openwrt-collector.service  →  python3 /home/bulik/apps/openwrt-monitor/ubus_collector.py
openwrt-dashboard.service  →  python3 /home/bulik/apps/openwrt-monitor/flask_app.py
```

Both as `root`, `Restart=always`. Saving config in the UI calls `systemctl restart` on both. After v2 these collapse into a single `openwrt-monitor.service`.

---

## 3. Target Architecture (what we are building toward)

### Scope clarification

The original plan in this section called for a Flask → FastAPI migration plus a WebSocket live-update channel (old P1 #6 and #7). **Those are deferred / probably skipped.** The CPU bottleneck is in the collector, not the web layer. Flask reads SQLite cheaply and handles the dashboard fine even at 30 routers. FastAPI would be structural prettiness, not a real win, and the WebSocket is UX polish we can revisit only if browser polling actually becomes a problem.

### Collector: asyncio + asyncssh

- Single asyncio event loop replaces the previous 2N threads (poll + ping per router).
- One **persistent** `asyncssh.connect()` per router, reused across polls. No more `subprocess.run(['ssh', ...])` forking each call — that was the main CPU drain on Atom.
- Per-router task: connect → poll loop. If a poll fails, drop the connection, mark offline, reconnect with exponential backoff.
- Hostname fetch on each poll doubles as a liveness check, so the v1 separate ping thread is gone. Poll interval is still configurable in `config.json`.

### Web layer: keep Flask

- No FastAPI migration. Flask + SQLite is fine for a one-box, 30-router monitor.
- Browser polls `/api/*` on a `setInterval`. Acceptable until/unless it isn't.

### DB: stay on SQLite, tune it later if needed

- **WAL mode is now enabled** (set once via `PRAGMA journal_mode=WAL` in `ubus_collector.init_db()` — persistent in the DB header, so it covers Flask/retention/backup connections too). This was added in the 2026-05-30 bug-fix pass to cut `database is locked` errors under the many concurrent writers. Python's `sqlite3.connect` already defaults `timeout`/busy-wait to 5 s.
- Schema beyond v1's `routers`/`interfaces`/`clients` now includes the history tables (`*_history`, `system_metrics`), `arp_entries`, `wifi_iface_config`, and `retention_status` — see `init_db()`.

### Auth (already done — see §2)

- Argon2-hashed creds in `/etc/openwrt-monitor/auth.json`, stable `secret_key`, no in-source rewrite.

### Service units (already done)

- `openwrt-collector.service` and `openwrt-dashboard.service`, both `ExecStart` from the project tree, no `/usr/local/bin` copies.

---

## 4. Conventions & Gotchas (read before you edit anything)

- **Canonical project root is `/home/bulik/apps/openwrt-monitor/`** — moved here from `/var/www/openwrt-monitor/` so the tree is bulik-owned (no sudo or `safe.directory` friction for git or edits) and no longer mixed with the unrelated voucher app. Systemd units now `ExecStart` directly from this tree; there is no `/usr/local/bin/` copy of the Python files to keep in sync.
- **Git is initialized.** `git init` on the project root with `main` branch and an initial snapshot commit. `.gitignore` excludes the wifi voucher app, `.claude/` state, logs, `*.backup-*`, and secrets.
- **Font Awesome is vendored at `static/fontawesome/`.** Templates link `/static/fontawesome/css/all.min.css` — no public CDN in the critical path. If you need a new icon style or a different FA version, drop the woff2+ttf into `static/fontawesome/webfonts/` and update the CSS; do NOT add a CDN link back.
- **Auth dies hard if `auth.json` is missing.** `flask_app.py` calls `sys.exit(1)` at import time if `/etc/openwrt-monitor/auth.json` is absent or malformed. This is intentional — there is no fallback to hardcoded credentials. Bootstrap with `sudo python3 scripts/init_auth.py`.
- **SSH is handled by `asyncssh` now.** The collector keeps one persistent connection per router; channel multiplexing is native, no `ControlMaster` needed. `/etc/openwrt-monitor/ssh_config` is left in place only for ad-hoc CLI use; the collector no longer reads it.
- **`clients` table is destructive.** Don't try to query historical client data from it — there isn't any. History goes in the new `*_history` tables (§5 P2).
- **Collector is single-process asyncio.** One persistent `asyncssh` connection per router, kept open across polls. The v1 model (2N threads + per-poll `subprocess` fork) is gone — that was the dominant CPU cost on Atom.
- **One SSH key for everything.** Documented assumption. If that changes, half the code changes too.
- **Deploying a change today** = edit file → `systemctl restart openwrt-collector openwrt-dashboard`. After v2 → `systemctl restart openwrt-monitor`.
- **`/var/www/openwrt-monitor/wifi/` is a separate app.** Captive-portal voucher dispenser, port 3000 (Node/Express). It used to be nested inside this project when the monitor lived at `/var/www/openwrt-monitor/`; the monitor moved to `~/apps/openwrt-monitor/` in P0 #0 and the wifi app stayed put. It is gitignored. Don't touch it from this repo.
- **Saving config restarts services.** `POST /api/config` calls `systemctl restart openwrt-collector openwrt-dashboard`. Expect a brief gap in polling. In v2 (single process), prefer a SIGHUP-style live reload.
- **`POST /api/config` must merge, not rebuild.** It now loads the existing `config.json` and overwrites only the keys the Config form owns (`ssh_key`, `ssh_user`, `poll_interval`, `ping_interval`, `routers`). It used to rebuild the dict from scratch, which silently wiped keys owned by other pages (`history_retention_days`, `raw_log_lines`, `backup_keep_days` from Maintenance; `pfsense_*`, `history_interval`). If you add a new config key written elsewhere, this merge keeps it safe — don't reintroduce a from-scratch rebuild here.
- **Storage today:** ~15 MB DB, 1 backup (~96 MB aging out in 4 days), ~20 MB log. Steady-state target: ~75 MB total (18% of 420 MB budget). Retention defaults are now aggressive — see §11.

---

## 5. Prioritized Improvement Roadmap

Ordered by **impact / risk**. Don't reorder without good reason — earlier items unblock later ones.

### P0 — Foundation (do first; unblocks everything else)

0. **Relocate project tree to `/home/bulik/apps/openwrt-monitor/`** so it is bulik-owned and unmixed from the wifi voucher app. Update `template_folder`/`static_folder` in `flask_app.py` and `WorkingDirectory`/`ExecStart` in the systemd units. **Done.**
1. **`git init`** at the new project root, `main` branch, initial snapshot commit, `.gitignore`. **Done.**
2. **Move hardcoded credentials out of `flask_app.py`** into `/etc/openwrt-monitor/auth.json` with an Argon2 hash. Stop the in-place source rewrite. **Done.**
3. **Vendor Font Awesome + fonts into `/static/`.** Drop every CDN `<link>` from `templates/*.html`. **Done** — Font Awesome 6.4.0 Free at `static/fontawesome/{css,webfonts}/`. Only the woff2 + ttf for `solid-900`, `regular-400`, `brands-400` are vendored (skipped `v4compatibility` and `svg-with-js` since unused).
4. **logrotate config** for `/var/log/openwrt-collector.log` (5 MB × 5 files, `copytruncate`). **Done** — config in `system/openwrt-monitor.logrotate`, installed at `/etc/logrotate.d/openwrt-monitor`. `copytruncate` is required because `ubus_collector.py` uses `logging.FileHandler` which holds the FD open.

### P1 — Async collector (the actual unlock for 30 routers)

5. **Port the collector to asyncio + asyncssh.** One persistent connection per router. Kill `subprocess.run(['ssh', ...])`. **Done.**
6. ~~**Port Flask → FastAPI.**~~ **Deferred / probably skipped.** Flask is fine for a one-box monitor; the web layer isn't the bottleneck. Revisit only if there's a real reason to.
7. ~~**Add WebSocket `/ws/live`.**~~ **Deferred.** Browser polling on `setInterval` is acceptable. Revisit if it actually becomes a problem.

### P2 — Historize the discarded data

8. **New time-series tables.** **Done** — `client_history`, `interface_history`, `router_status_history`, `system_metrics` are created in `init_db()`; the collector appends rows at `history_interval` (default 60s, decoupled from `poll_interval`). `router_status_history` writes only on transition. `system_metrics` captures uptime + load1/5/15 + memory from `ubus call system info`. Retention is **not yet enforced** — that's P3 #12; until then the tables grow.
9. **Capture per-router system info** via `ubus call system info`. **Done as part of #8** (`load1`, `load5`, `load15`, `uptime`, `mem_total/free/used` in `system_metrics`). `ubus call network.device status` (per-iface byte counters, errors) still TODO if we want it.
10. **MAC → IP/hostname enrichment via pfSense REST API.** **Done** — `pfsense.py` fetches `GET /api/v2/diagnostics/arp_table` from the pfSense REST API package (auth: `X-API-Key` header). Collector's `arp_loop` task pulls every 30s by default and upserts into `arp_entries` (PK = lowercased MAC). Flask's `/api/clients` and `/api/router/<ip>` LEFT JOIN `clients` ↔ `arp_entries` on `lower(c.mac) = a.mac` and expose `arp_ip` + `arp_hostname` fields. Dashboard renders an IP / Device column next to MAC. Original plan was SSH-to-dhcpd.leases on the dumb-AP routers; that turned out to be wrong — DHCP runs on the pfSense firewall, not the APs. Hostname is best-effort (currently pfSense returns "?" for most clients since they don't send DHCP `client-hostname` and reverse-DNS isn't populating).
11. **New page: Clients (cross-router).** **Done** — `/clients` route, `templates/clients.html`. Shows one row per associated client across the whole fleet with hostname/IP (from `arp_entries`), MAC, router (hostname), SSID, band (2.4G/5G badge), signal (bar + dBm), RX/TX rate, RX/TX bytes, connected, inactive. Filter bar: free-text search + band dropdown + router dropdown. Every column header is click-sortable (asc/desc), default `signal desc`. Polls `/api/clients` every 10 s. `/api/clients` was extended to JOIN `routers` so `router_hostname` is available. Signal-trend sparkline is a future enhancement (would need `client_history` lookup per visible row — deferred).

### P3 — Operations & maintenance UI

12. **Logs Maintenance page** (explicitly requested). **Done** — `/maintenance` route, `templates/maintenance.html`, `retention.py` module shared by Flask and collector.
    - `history_retention_days` setting (default **30**, range 1–3650), persisted in `config.json`. Applied to every `*_history` table by `retention.run_cleanup()`.
    - `raw_log_lines` setting (default **500**, range 10–100000), persisted in `config.json`. Read per request by `/api/raw-logs` — no restart needed to change.
    - VACUUM runs as part of every cleanup (no separate schedule).
    - The collector schedules `retention.run_cleanup` daily via `retention_loop`. Manual "Run cleanup now" button hits `POST /api/maintenance/run-now`.
    - Status (last run, rows deleted, duration, ok/err) lives in the `retention_status` single-row table.
13. **Reachability strip per router** — last 24h online/offline timeline from `router_status_history`. **Done** — new `GET /api/reachability` returns compact `[start_off, end_off, online]` segments per router. Dashboard adds a "Last 24h" column to the dense table rendered as flex-box `.reach-strip` divs (green = online, red = offline). Fetched once on load (before first router render so strips aren't empty) and refreshed every 5 minutes; not joined into `/api/routers` to keep the 10 s poll lean.
14. **DB backup endpoint.** **Done** — `backup.py` uses SQLite's online backup API to take consistent snapshots while writers are active. Files land at `/var/lib/openwrt-monitor/monitor.db.backup-YYYYMMDD-HHMMSS`, matching the prior manual convention. Collector schedules `backup_loop` (daily, immediate on start) which creates a snapshot then prunes anything older than `backup_keep_days` (default 7). Flask endpoints: `GET /api/maintenance/backups` (list + retention), `POST /api/maintenance/backup/run`, `GET /api/maintenance/backup/download/<name>`, `DELETE /api/maintenance/backup/<name>`. UI lives in the Maintenance page (`Backups` card) with "Backup now", per-row Download/Delete, and a retention input. Restore is intentionally **not** wired through the UI — too dangerous with live writers; the card shows the `systemctl stop … && cp … && systemctl start …` CLI sequence instead.

### P4 — UI overhaul (info density + theme)

15. **Light/dark theme via CSS custom properties** on `<html data-theme="light|dark">`. Persist in `localStorage`. Default = `prefers-color-scheme`. Toggle in the header. **Done** — tokens in `static/theme.css`, toggle + FOUC handling in `static/theme.js` (plus a tiny inline pre-render script in each template's `<head>`). All templates' inline CSS now uses `var(--accent)` / `var(--fg)` / `var(--card-bg)` / etc. Toggle button (`#themeToggle`) is in every nav except login.html. A handful of subtle tints (rgba shadows on err/warn/ok buttons) remain hardcoded — fine in both modes, not worth chasing.
16. **Dense router grid.** **Done** — `dashboard.html` renders a CSS-grid table (`.router-table` / `.router-th` / `.router-tr`) at 36 px row height. **12 columns** (as of 2026-05-31): status dot, hostname, **2.4G Radio mini**, **5G Radio mini**, IP, total clients, 2.4G count, 5G count, load1, uptime, 24h reach strip, expand chevron. The two radio-mini cells (`.rm24`, `.rm5`) sit beside hostname and show a channel badge + colored noise dBm + colored rate (Mbps/Gbps) for quick cross-router comparison. Click row toggles `.expanded` on both the `.router-tr` and the sibling `.router-detail`. `/api/routers` returns `clients_24`, `clients_5`, `load1/5/15`, `uptime`, `mem_*`, plus `ch_24/noise_24/bitrate_24/ch_5/noise_5/bitrate_5` aggregated from the `interfaces` table. Smart rerender: full HTML rebuild only when router set changes; otherwise per-cell `setText` + `.innerHTML` for the radio-mini cells. Density toggle deferred.
17. **Sortable / filterable clients table.** **Done as part of P2 #11** — same `/clients` page covers it. Virtualization deferred; at current scale (~30 clients) the table renders fast. If client count grows beyond ~300, add row virtualization (windowing).
18. **Drop chart.js everywhere it appears.** If the monitor needs charts, use **uPlot** (~40 KB) or hand-rolled SVG sparklines. No more 250 KB chart libraries.

### P5 — Nice to have

19. **Alert rules.** Per-client or per-router. e.g. "signal < -75 for >5 min", "router offline > 2 min", "new MAC on guest SSID". Write to an `events` table; surface on dashboard.
20. **Prometheus `/metrics` endpoint.** So the box can later be scraped by a real TSDB without rebuilding the world.
21. **CSV export** for any table view.

---

## 6. UI / UX Design Principles (binding rules)

- **Information density first.** 30 routers visible without scrolling on 1080p. Collapsed router row ≈ 32–40 px tall.
- **No CDN in the critical path.** Vendor all fonts, icons, and JS into `/static/`. Dashboard must render fully offline.
- **No animation on data updates.** Atom CPU. No fading, no sliding rows, no morphing numbers. Status dots change color instantly.
- **CSS custom properties for theming.** All colors via `var(--bg-0)`, `var(--fg-0)`, `var(--accent)`, `var(--ok)`, `var(--warn)`, `var(--err)`. Theme switch toggles `data-theme` on `<html>`; no JS re-styling individual elements.
- **System theme honored by default.** `@media (prefers-color-scheme: light)` sets the default; user toggle overrides and persists in `localStorage`.
- **No frontend frameworks.** Plain JS + a thin fetch/WS helper. React or Vue would dominate Atom startup. If state grows, **Alpine.js** (~15 KB) is the only acceptable escalation.
- **DOM-diff via element keys, not full re-render.** Live updates patch existing rows in place. Never `innerHTML = ...` the grid.
- **WebSocket is the only live data source** post-P1. No `setInterval(fetch...)` in the dashboard.
- **Mobile is a non-goal.** This is an ops console. Don't spend layout budget on phones.
- **Density modes.** `compact` (default, 32 px rows) and `comfortable` (44 px). Toggle in settings.
- **Color semantics are fixed.** Green = online, amber = degraded (high inactive, low-signal floor), red = offline, gray = unknown. **Never color alone** — pair with text or icon.
- **One font, two weights.** Inter 400 / 600 (or system stack). JetBrains Mono only for MAC / IP / byte columns.

---

## 7. How to Work on This Project (orientation checklist for future Claude)

1. **Read this file first.** Then skim `templates/dashboard.html` to see what the UI currently does, and `ubus_collector.py` to see what data is being collected.
2. **See what's running:**
   ```
   systemctl status openwrt-collector openwrt-dashboard   # pre-v2
   systemctl status openwrt-monitor                       # post-v2
   ```
3. **Read live config:** `cat /etc/openwrt-monitor/config.json`.
4. **Dump DB schema before changing it:**
   ```
   sqlite3 /var/lib/openwrt-monitor/monitor.db .schema
   ```
   Backups live next to it (`monitor.db.backup-*`).
5. **Don't add data without thinking about retention.** Storage is the user's #2 pain point after CPU.
6. **Don't import a heavy npm or CDN library to "improve the UI".** The rules in §6 are binding.
7. **Don't commit secrets.** The hardcoded password in v1's `flask_app.py` must not survive into v2.
8. **Edits to `flask_app.py` and `ubus_collector.py` take effect on `systemctl restart`** — the systemd units `ExecStart` directly from this project tree, so there is no mirror step.
9. **After any code change:** `sudo systemctl restart openwrt-collector openwrt-dashboard` (or the unified unit post-v2). Then `journalctl -u <unit> -f` to confirm clean startup.

---

## 8. Bug-fix Pass — 2026-05-30 (state for the next agent)

A focused bug hunt. Four fixes landed; record here so they aren't re-broken or re-investigated.

### Fixed

1. **`POST /api/config` clobbered other pages' config keys (HIGH, data loss).** It rebuilt `config.json` from 5 keys, wiping `history_retention_days` / `raw_log_lines` / `backup_keep_days` (Maintenance page) and `pfsense_*` / `history_interval`. Now merges in place. See the §4 gotcha above. `flask_app.py` `api_config`.
2. **Client RX/TX rate wrong for slow stations (MED).** `parse_client` only divided kbit/s→Mbit/s when `rate > 1000`, so a ≤1 Mbit/s client displayed as "1000 Mbps". Now `round(rate/1000)` unconditionally. Verified live that iwinfo `assoclist.rate` is kbit/s. `ubus_collector.py:~223`.
3. **SQLite had no WAL (robustness).** Now `PRAGMA journal_mode=WAL` in `init_db()`. See §3 DB note.
4. **`/api/router-raw-data` interpolated the device name into a remote shell command (LOW/defensive).** Now whitelisted with `re.fullmatch(r'[A-Za-z0-9._-]+', device)` before use. `flask_app.py` `api_router_raw_data`.

> **DEPLOY STATE:** all four fixes committed in `5b9c23c` and services restarted. WAL is active (`PRAGMA journal_mode` returns `wal`).

### Not explored thoroughly (open territory for the next pass)

- **Templates' inline JS** (`dashboard.html`, `clients.html`, `config.html`, `maintenance.html`, `logs.html`): only skimmed for the band/signal/reachability logic. The dashboard's "smart rerender" (per-cell `setText` vs full rebuild) and the clients-table sort/filter paths were not audited for correctness or edge cases (empty data, NaN sorts, XSS via `innerHTML` with router/SSID/hostname strings).
- **pfSense enrichment is now active and verified** — credentials restored via the new Settings UI card (2026-06-01). All clients are showing IPs. The Settings card (`config.html`) lets the user set `pfsense_url` / `pfsense_api_key` without hand-editing JSON. `arp_loop` now logs a WARNING when unconfigured or when the fetch returns empty, so silent failure can't recur undetected.
- **Concurrency under load not tested.** WAL should help, but the daily `VACUUM` in `retention.run_cleanup` still takes an exclusive lock; behaviour at the 30-router target during VACUUM/backup is unverified.
- **`ping_interval` is a dead v1 key** — still written by the Config form and stored, but the asyncio collector never reads it (it only uses `poll_interval`). Harmless, but a candidate for removal.
- **`api_reachability` first-segment inference** (assumes the pre-window state was the opposite of the first transition) is plausible but not validated against real `router_status_history` data.

---

## 9. Feature Pass — 2026-05-31 (radio health stats)

Commit `da0c63e`. Three files changed, no regressions observed.

### What was added

**Collector (`ubus_collector.py`)**
- `init_db()` migration: three new `INTEGER` columns on `interfaces` — `noise`, `bitrate`, `txpower` — added via try/except `ALTER TABLE` so existing DBs upgrade on next collector restart without a manual migration step. **Use this same pattern for all future column additions.**
- `save_snapshot()`: the `interfaces` INSERT/UPDATE now stores `info.get('noise')`, `info.get('bitrate')`, `info.get('txpower')` from `ubus call iwinfo info` alongside the existing fields.

**API (`flask_app.py`)**
- `/api/routers`: aggregates six new per-band radio fields using conditional `MIN`/`MAX` over the `interfaces` JOIN — `ch_24`, `noise_24`, `bitrate_24`, `ch_5`, `noise_5`, `bitrate_5`. `MIN` on channel/noise (all SSIDs on the same physical radio report the same value so MIN/MAX are equivalent); `MAX` on bitrate.
- `/api/router/<ip>`: the SSID entry `dict` built in `api_router_detail` now explicitly passes `noise`, `bitrate`, `txpower` through to the response (the `entry.update()` call has a hardcoded key list — new fields must be added there manually, they do **not** flow through automatically from `SELECT *`).

**Dashboard (`templates/dashboard.html`)**
- **Router list row** (quick-glance): two new grid cells `.rm24` / `.rm5` positioned immediately after `.hostname`. Each renders `fmtRadioMini(ch, noise, kbps)` — a channel badge pill + colored noise dBm + colored rate short-form. Color thresholds: noise ≤ −90 → green, ≤ −80 → amber, else red; rate ≥ 300 Mbps → green, ≥ 54 Mbps → amber, else red. The grid is now **12 columns**: `28px minmax(140px,1.4fr) 112px 112px 110px 60px 60px 60px 64px 90px 110px 28px`.
- **Expanded interface card** (detail): `renderRadioMetrics(s, clients)` inserts a 5-tile strip between `interface-meta` and the clients table. Tiles: Channel (plain), Noise Floor (colored + 3 px bar), Avg Client SNR (computed from `clients` array as `avg(c.signal − c.noise)`, colored + bar), Radio Rate (colored + bar), TX Power (plain). Bars are hidden when value is null (e.g. no clients → no SNR bar).
- Color helper functions added: `noiseClass`, `snrClass`, `bitrateClass`, `fmtKbps`, `fmtKbpsShort`, `fmtRadioMini`, `renderRadioMetrics`. All reuse the existing `.m-ok` / `.m-warn` / `.m-err` CSS classes.
- Incremental update path uses `row.querySelector('.rm24/.rm5').innerHTML = fmtRadioMini(...)` — safe because all values are integers from the DB (no user-controlled strings).

### Deploy state

Committed `da0c63e`, pushed to `origin/main`. Services restarted. The new `interfaces` columns populate after the first collector poll post-restart; until then the radio cells show `—`.

### Open territory

- **`iwinfo info`.signal for an AP is always −1** (not applicable in Master mode) — it is intentionally not stored or displayed. If STA-mode monitoring is ever added, revisit.
- **Radio mini cells use `innerHTML`** in the incremental path (safe today, but note it as an exception to the usual `setText` discipline — don't extend this pattern to user-controlled strings).
- **Avg Client SNR disappears when there are no clients.** This is correct UX but means a newly-booted router shows `—` for SNR even though the radio is up. A future improvement could show the radio noise floor alone as a proxy.

---

## 10. Feature + Bug-fix Pass — 2026-06-01

Commit `91e06be`. Six files changed.

### One-click copy for all IPs and MACs (`static/copy-utils.js`)

Shared utility loaded by `dashboard.html` and `clients.html` (added before `theme.js`). All other templates are unaffected.

- **Pattern**: add `data-copy="<value>" data-copy-label="IP|MAC"` to any element. One delegated listener on `document` (capture phase) handles all clicks — no per-element `onclick` needed.
- **Cross-browser**: `navigator.clipboard.writeText` primary path; `execCommand('copy')` via hidden `<textarea>` fallback for HTTP / Android WebView / older Safari; `setSelectionRange(0, length)` makes the fallback work on iOS.
- **Capture phase + stopPropagation**: the listener fires before any bubble-phase `onclick` (e.g. the router row-expand `toggleRouter`), so clicking an IP cell copies without also toggling the row.
- **Toast**: fixed-position `#_ct` div, `var(--ok)` background (theme-aware), repositions to avoid viewport edges, fades after 1.5 s. `aria-live="polite"` for screen readers.
- **dashboard.html**: router `.ip` cells, expanded client MAC `<td>`, ARP IP within `ipCell`. Old `copyMAC()` removed.
- **clients.html**: identity column (IP/MAC), standalone MAC `<td>`.
- **`innerHTML` use in incremental path** is safe (values are DB integers/addresses) but is an exception to `setText` discipline — don't extend to user-controlled strings.

### pfSense Settings UI (`templates/config.html`)

New "pfSense Integration" card in `settings-grid`:
- `pfsense_url` text input, `pfsense_api_key` password input with show/hide toggle (`toggleKeyVisibility()`).
- `loadConfig()` populates fields from the existing `GET /api/config` response.
- `saveConfig()` includes `pfsense_url` and `pfsense_api_key` in the POST body.
- `flask_app.py api_config` merges these keys **only when present in POST data** (key-presence guard: `if _k in data: config[_k] = data[_k]`). This means an old form version that doesn't send the keys can never silently wipe them — the same root-cause protection as the 2026-05-30 config-save fix.

### Fix: silent pfSense ARP failure (`ubus_collector.py`)

`arp_loop` previously logged nothing when credentials were missing or when the fetch returned an empty list. A config-save bug on 2026-05-25 wiped `pfsense_url`/`pfsense_api_key` from config.json; the loop ran silently for 7 days with no indication. Now:
- **No credentials**: logs one `WARNING` (suppressed on subsequent cycles via `_warned_unconfigured` flag to avoid log spam).
- **Fetch returns empty list**: logs a `WARNING` every cycle (not suppressed — empty from a configured pfSense is always abnormal).

### Router ARP supplement (`ubus_collector.py`) — limited value on dumb APs

`parse_router_arp()` parses `ip neigh show` stdout; `save_router_arp()` does `INSERT OR IGNORE` into `arp_entries` (pfSense data always wins). Called from `poll_once()` over the existing SSH connection after iwinfo calls.

**Important caveat**: on dumb-AP (bridged) topologies the router's kernel ARP table only contains hosts the AP's own L3 stack has resolved (typically the gateway + a handful of management hosts). WiFi clients are forwarded at L2 — the AP never resolves their IPs. `ip neigh show` returns ~3 entries in this setup, making this supplement a no-op in practice. It is harmless and may help in non-bridged setups, but it is **not** the solution for bridged dumb-APs. See open territory below.

### Root cause of missing client IPs — post-mortem

The config-save bug (fixed 2026-05-30) wiped pfSense credentials from config.json on 2026-05-25 when the user saved Settings. `arp_loop` ran silently with no credentials for 7 days. The 7-day-old `arp_entries` rows were from the last successful pfSense poll. Restoring credentials via the new Settings UI resolved all missing IPs immediately.

Three specific MACs investigated on router "UniFiACMEsh":
- `B2:90:BD:99:46:1C` and `BA:13:1A:C7:7A:F4` — locally-administered bit set → randomized/private MACs from modern devices (iOS/Android/Windows MAC randomization). These now resolve once pfSense ARP is active.
- `A8:16:9D:EA:62:A8` — real Apple OUI, globally administered — resolved after ARP refresh.

### Deploy state

Committed `91e06be`, pushed to `origin/main`. Services restarted. All clients now show IPs. pfSense ARP logs `refreshed N entries` every 30 s.

### Open territory

- **pfSense DHCP lease table not yet used.** If a device has a static IP or its pfSense ARP entry expires between 30s poll cycles, it will briefly lose its IP in the UI. Fetching `GET /api/v2/services/dhcpv4/lease` alongside the ARP table would provide persistent IP→MAC mappings that survive ARP cache expiry. This is the next meaningful improvement for IP enrichment completeness.
- **Dumb-AP ARP gap**: the `ip neigh show` supplement does not help on bridged APs (see above). The only reliable supplemental source for these is pfSense DHCP leases.
- **Locally-administered (randomized) MACs**: devices using per-connection MAC rotation (iOS "rotate daily" option) will cycle through MACs each session. pfSense DHCP leases would still catch them since the lease is recorded at DHCP time. No fix is possible for devices that never send DHCP (static IP + randomized MAC).

---

## 11. Feature Pass — 2026-06-01 (database bloat control)

Commit `007f4e9`. Five files changed. Budget: 420 MB total (live DB + backups).

### Problem

DB was 97 MB with 30-day history at 60s interval; 20 backup files totalling 1.3 GB. Root causes:
1. `DEFAULT_RETENTION_DAYS = 30` — 30× more history than needed for a live-view-only system.
2. `HISTORY_INTERVAL_DEFAULT = 60` — 1-minute snapshots; 5× more rows than needed.
3. `DEFAULT_KEEP_DAYS = 7` — user wanted 4.
4. `arp_entries` had no cleanup path — grew unbounded with every MAC ever seen.
5. Every service restart triggered an immediate backup, creating same-day duplicates.

### Defaults changed

| Constant | File | Old | New |
|---|---|---|---|
| `DEFAULT_RETENTION_DAYS` | `retention.py` | 30 | **1** |
| `HISTORY_INTERVAL_DEFAULT` | `ubus_collector.py` | 60 | **300** |
| `DEFAULT_KEEP_DAYS` | `backup.py` | 7 | **4** |

### New behaviour

- **`retention.run_cleanup()`**: now also `DELETE FROM arp_entries WHERE last_seen < cutoff` after the history-table loop. `arp_entries` uses `last_seen` not `ts` — the column name matters.
- **`backup.run_backup(force=False)`**: skips if a backup was made within the last 23 h (prevents restart-triggered duplicates). The Flask "Backup now" endpoint calls `run_backup(force=True)` so manual requests always create a file.
- **`flask_app.py api_maintenance`**: GET returns `history_interval` in config block and `backup_bytes` in sizes block. POST validates and persists `history_interval` (30–3600 s). Collector re-reads config each cycle — no restart needed for retention/interval changes.
- **`maintenance.html`**: Storage card shows Total (DB + backups) vs 420 MB budget with %, amber at 85%. Snapshot interval input added to History retention card.

### Steady-state projections

| Scenario | Live DB | 4 backups | Total |
|---|---|---|---|
| 7 routers, ~150 clients | ~15 MB | ~60 MB | **~75 MB (18%)** |
| 30 routers, ~600 clients | ~30 MB | ~120 MB | **~150 MB (36%)** |

The pre-change 96 MB backup will be pruned 4 days after the cleanup run. Until then total is ~111 MB.

### Important: `history_interval` requires collector restart

Unlike `history_retention_days` (read by the daily retention loop), `history_interval` is read at collector startup via `config.get('history_interval', HISTORY_INTERVAL_DEFAULT)` in `run_router()`. Changing it in the Maintenance UI takes effect after `sudo systemctl restart openwrt-collector`.

### Open territory

- `arp_entries` cleanup uses the same `retention_days` window as history. Could make it a separate, longer window (e.g. 7 days) so rarely-seen devices don't lose their IP mapping overnight. Currently 1-day window matches history tables.
- Maintenance UI does not warn that `history_interval` needs a service restart. A note or badge would improve UX.

---

## 12. Feature Pass — 2026-07-27 (captive-portal voucher / last-activity / trusted-MAC enrichment)

Commit `09f13cb`. Five files. Surfaces pfSense captive-portal state per wifi client in a new **"Active Voucher"** column, on both the Clients page (`/clients`) and each router's expanded client table on the dashboard.

### Key architectural finding (verified against package source)

**The `pfSense-pkg-RESTAPI` v2 package has NO captive-portal or voucher endpoints.** There is no `/api/v2/.../voucher` analogous to `arp_table`. Confirmed: zero `CaptivePortal`/`Voucher` endpoint classes in the package. So voucher data cannot be pulled via a normal REST data endpoint.

**Transport used instead:** `POST /api/v2/diagnostics/command_prompt` (the REST command-exec endpoint = GUI Diagnostics > Command Prompt), reusing the existing `pfsense_url` + `pfsense_api_key`. Request `{"command": "<fixed php>"}`, response `data.output` (stdout) + `data.result_code`. **Requires the pfSense API key/user to hold the "Diagnostics: Command Prompt" privilege** — without it the endpoint 403s and the columns stay blank (loops log a warning, no crash). Commands are static and app-controlled → no injection surface. This is the pattern to reuse for **any** pfSense data the REST API doesn't expose directly.

### Data sources (all read-only on pfSense)

- **Voucher sessions** — per-zone captive-portal SQLite DBs `/var/db/captiveportal<zone>.db`, table `captiveportal`. Columns: `username` = voucher code, `allow_time` = unix session start, `session_timeout` = length (s), `mac` = join key, `authmethod`. Read read-only via php `SQLite3` (pfSense has php always; the `sqlite3` CLI is not guaranteed).
- **Last activity** — NOT a stored column. Derived live from the pf state table via pfSense's own `captiveportal_get_last_activity($ip)` (needs global `$cpzone` set + `require_once` of `config.inc` + `captiveportal.inc`). Zone recovered from the DB filename. This makes the voucher command heavier than a plain SQLite read (loads pfSense config/CP libs) but measured ~445 ms — lighter than the pre-existing ARP call.
- **Trusted / allowed MACs** — pfSense config `captiveportal/<zone>/passthrumac` (array of `{action, mac, descr}`). Needs `config.inc` only (lighter than the voucher command). These devices bypass the portal (no voucher) → shown as a green **TRUSTED** badge.

### Code map

- **`pfsense.py`** — `VOUCHER_CMD` / `TRUSTED_CMD` constants (fixed php snippets), `fetch_voucher_sessions()` / `fetch_trusted_macs()` (parse one-JSON-per-line stdout), `_post_sync()` (POST sibling of `_fetch_sync`).
- **`ubus_collector.py`** — `voucher_sessions` + `trusted_macs` tables (in `init_db()`); `last_activity` column added via the try/except ALTER-TABLE migration loop (the table shipped without it earlier the same day). `save_voucher_sessions()` / `save_trusted_macs()` **full-replace** each cycle (DELETE + re-INSERT → self-cleaning live snapshot, no retention/bloat, MAC lowercased for the JOIN). `voucher_loop` (`pfsense_voucher_interval`, default 30, **set to 60 in live config** to halve pfSense PHP spawns) and `trusted_loop` (`pfsense_trusted_interval`, default 300 — admin list changes rarely). Both registered in `main()`, both re-read config each cycle. **`pfsense_voucher_interval` / `pfsense_trusted_interval` are read at loop top each cycle**, so a config change takes effect next cycle without restart.
- **`flask_app.py`** — `/api/clients` and `/api/router/<ip>` both `LEFT JOIN voucher_sessions v` and `LEFT JOIN trusted_macs t` on `lower(c.mac)`, exposing `voucher_code`, `voucher_authmethod`, `voucher_start`, `voucher_last_activity`, `voucher_expiry` (= allow_time+session_timeout, computed server-side), plus `trusted` (`t.mac IS NOT NULL`) and `trusted_descr`.
- **`templates/clients.html` + `templates/dashboard.html`** — Active Voucher cell priority: **voucher** (code + activation + live "time left" + "active Ns ago" last-activity, all derived browser-side from the server timestamps) → **green TRUSTED badge** (+ admin descr) → **"—"**. `descr` is admin-entered free text so it is **HTML-escaped** (`esc()`/`vEsc()`) — the only client field that is; don't render it raw. Dashboard's `voucherCell()` mirrors the clients-page helpers.

### Same-pass cosmetic changes (also in `09f13cb`)

- **Per-band SSID card tint** (`dashboard.html`): `.band-24-card` (cyan) / `.band-5-card` (green) — a flat color layered over `var(--card-bg-3)` + an inset accent stripe, so 2.4 vs 5 GHz radios are distinguishable at a glance. Band from `s.frequency` (≥5000 = 5G, <3000 = 2.4G).
- **Router drop-down animation** (`dashboard.html`): `.router-detail` is now a CSS grid animating `grid-template-rows: 0fr → 1fr` (slides to exact content height, no JS), with content wrapped in `.router-detail-inner` (`overflow:hidden`). **Fires only on the user's expand click, not on the 10s poll** (the poll only swaps innerHTML while already at `1fr`), so it respects the §6 "no animation on data updates" rule. Honors `prefers-reduced-motion`.

### Resource impact (measured live, for reference)

- **This box (Atom):** one voucher cycle = ~0.8 ms CPU (parse + WAL write). Net-new collector CPU **< 0.01% of a core** — the collector's ~19% is entirely SSH-polling 8 routers every 10s (`poll_interval`), unrelated.
- **pfSense:** cost is PHP-spawn + `config.inc` parse (~250 ms + ~170 ms), not the query. Time-averaged net-new ≈ **~1.6% of one core** as sub-0.5 s bursts. Network ~15–20 MB/day. Biggest lever: `pfsense_voucher_interval` (raised to 60 → ~halves it). One inefficiency: `_post_sync`/`_fetch_sync` open a fresh TLS connection per call (no keep-alive) → ~3k handshakes/day.

### Open territory

- **MAC-join dependency:** all captive-portal enrichment joins on `lower(mac)` between the AP assoclist and pfSense. Holds for consistent MACs (incl. randomized ones within a session); a device whose CP `mac` is blank won't join. Same limitation as ARP enrichment.
- **TLS keep-alive:** connection-per-call is the one measured inefficiency; a pooled/persistent HTTPS client would cut pfSense handshake CPU if ever needed.
- **`authmethod` gate:** the UI treats a session as a voucher when `authmethod` contains "voucher" (case-insensitive) or is blank. Live data shows exactly `voucher`; revisit the gate if other CP auth methods (local/RADIUS) ever share the column.
- **DHCP leases still unused** (see §10) — remains the next IP-enrichment improvement, independent of this pass.
