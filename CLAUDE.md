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
| Static assets | `/home/bulik/apps/openwrt-monitor/static/` (empty — everything is inlined in templates) |
| Systemd unit copies in repo | `/home/bulik/apps/openwrt-monitor/system/openwrt-{collector,dashboard}.service` |
| Runtime config | `/etc/openwrt-monitor/config.json` |
| SSH client config | `/etc/openwrt-monitor/ssh_config` (ControlMaster multiplex) |
| SQLite database | `/var/lib/openwrt-monitor/monitor.db` |
| DB backups | `/var/lib/openwrt-monitor/monitor.db.backup-*` |
| Collector log | `/var/log/openwrt-collector.log` (logrotate config installed; 5 MB × 5) |
| Live systemd units | `/etc/systemd/system/openwrt-collector.service`, `openwrt-dashboard.service` — now `ExecStart` from the project root |
| Unrelated voucher app | `/var/www/openwrt-monitor/wifi/` — separate Node/Express app on port 3000; **NOT part of the monitor**, left in place when the monitor moved |
| Stray old dashboard file | `/home/bulik/apps/openwrt-monitor/templatesclear` — old variant carried along by the rsync; safe to delete |

### Tables in `monitor.db`

- `routers` — `ip` PK, `hostname`, `online`, `last_seen`, `first_seen`
- `interfaces` — one row per radio interface per router; SSID, BSSID, freq, channel, bandwidth, mode, encryption, num_clients
- `clients` — associated stations; MAC, signal, signal_avg, noise, rx/tx_rate, rx/tx_packets, rx/tx_bytes, connected_time, inactive, authorized, last_seen, first_seen

**Important:** the `clients` table is destructive — every poll cycle does `DELETE FROM clients WHERE router_ip=? AND interface=?` and re-inserts. **No history is kept.** That's the single biggest cause of "rich data being discarded" that this project complains about.

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

## 3. Target Architecture (v2 — what we are building toward)

### Web layer: FastAPI on uvicorn

- Replaces Flask.
- Native async, WebSocket and SSE built in, Pydantic request/response models, auto OpenAPI.
- One process, one worker (this box has one CPU's worth of useful compute — don't fan out).
- Keep the same URL surface as v1 during migration so the frontend doesn't break mid-flight.

### Collector: asyncio + asyncssh

- A single asyncio event loop replaces the current 2N threads (poll + ping per router).
- One **persistent** `asyncssh.connect()` per router, reused across polls. No more `subprocess.run(['ssh', ...])` — that fork is the main thing pinning the Atom.
- Per-router task scheduled at `poll_interval`; ping is a lightweight `connection.run('echo OK')` on the existing channel.
- Bounded concurrency with a semaphore (so a flapping router can't starve the loop).
- Exponential backoff on connect failures, marking the router offline in the DB.

### DB: stay on SQLite (right size for one box), but tune it

- WAL mode, `PRAGMA synchronous=NORMAL`, `PRAGMA mmap_size`, `PRAGMA cache_size` — set on connection open.
- New time-series tables (see roadmap §5 P2): `client_history`, `interface_history`, `router_status_history`, `system_metrics`.
- New `dhcp_leases` table, ingested from `/tmp/dhcp.leases` over SSH, JOINed on MAC so the UI shows IP + hostname instead of raw MAC.
- Retention bounded by the Logs Maintenance page (§5 P3).

### Live updates: WebSocket, not polling

- One channel `/ws/live` that pushes router status changes and client deltas.
- The dashboard subscribes once on load; remove every `setInterval(fetch...)` from the templates.

### Auth: move credentials out of source

- Argon2-hashed password in `/etc/openwrt-monitor/auth.json` (mode 600, root-only).
- Session-based via FastAPI middleware. Stable secret read from the same file (or `/etc/openwrt-monitor/secret`).
- Delete the in-place source-rewrite password change.

### Service unit

- One unified `openwrt-monitor.service` running `uvicorn` against the project tree.
- The collector runs as an asyncio task inside the same process. Single process keeps memory low; if isolation matters later, split into two units.

---

## 4. Conventions & Gotchas (read before you edit anything)

- **Canonical project root is `/home/bulik/apps/openwrt-monitor/`** — moved here from `/var/www/openwrt-monitor/` so the tree is bulik-owned (no sudo or `safe.directory` friction for git or edits) and no longer mixed with the unrelated voucher app. Systemd units now `ExecStart` directly from this tree; there is no `/usr/local/bin/` copy of the Python files to keep in sync.
- **Git is initialized.** `git init` on the project root with `main` branch and an initial snapshot commit. `.gitignore` excludes the wifi voucher app, `.claude/` state, logs, `*.backup-*`, and secrets.
- **Font Awesome is vendored at `static/fontawesome/`.** Templates link `/static/fontawesome/css/all.min.css` — no public CDN in the critical path. If you need a new icon style or a different FA version, drop the woff2+ttf into `static/fontawesome/webfonts/` and update the CSS; do NOT add a CDN link back.
- **Auth dies hard if `auth.json` is missing.** `flask_app.py` calls `sys.exit(1)` at import time if `/etc/openwrt-monitor/auth.json` is absent or malformed. This is intentional — there is no fallback to hardcoded credentials. Bootstrap with `sudo python3 scripts/init_auth.py`.
- **SSH multiplex is already set up.** `/etc/openwrt-monitor/ssh_config` has `ControlMaster auto`, `ControlPersist 10m`, `ServerAliveInterval 30`. Always pass `-F /etc/openwrt-monitor/ssh_config` if you open a raw `ssh` subprocess. (In v2, `asyncssh` replaces the need entirely.)
- **`clients` table is destructive.** Don't try to query historical client data from it — there isn't any. History goes in the new `*_history` tables (§5 P2).
- **Threads-per-router doesn't scale.** Today: 30 routers × 2 threads × per-poll `subprocess` fork is the dominant CPU cost. This is *the* reason for the asyncio migration, not aesthetics.
- **One SSH key for everything.** Documented assumption. If that changes, half the code changes too.
- **Deploying a change today** = edit file → `systemctl restart openwrt-collector openwrt-dashboard`. After v2 → `systemctl restart openwrt-monitor`.
- **`/var/www/openwrt-monitor/wifi/` is a separate app.** Captive-portal voucher dispenser, port 3000 (Node/Express). It used to be nested inside this project when the monitor lived at `/var/www/openwrt-monitor/`; the monitor moved to `~/apps/openwrt-monitor/` in P0 #0 and the wifi app stayed put. It is gitignored. Don't touch it from this repo.
- **`templatesclear` at the project root** is an old standalone dashboard file carried along by the move. Delete it once you're sure nothing references it.
- **Saving config restarts services.** `POST /api/config` calls `systemctl restart openwrt-collector openwrt-dashboard`. Expect a brief gap in polling. In v2 (single process), prefer a SIGHUP-style live reload.
- **Storage today:** ~31 MB DB, ~20 MB log. With history added and 30 routers, both will grow fast. Retention is non-optional.

---

## 5. Prioritized Improvement Roadmap

Ordered by **impact / risk**. Don't reorder without good reason — earlier items unblock later ones.

### P0 — Foundation (do first; unblocks everything else)

0. **Relocate project tree to `/home/bulik/apps/openwrt-monitor/`** so it is bulik-owned and unmixed from the wifi voucher app. Update `template_folder`/`static_folder` in `flask_app.py` and `WorkingDirectory`/`ExecStart` in the systemd units. **Done.**
1. **`git init`** at the new project root, `main` branch, initial snapshot commit, `.gitignore`. **Done.**
2. **Move hardcoded credentials out of `flask_app.py`** into `/etc/openwrt-monitor/auth.json` with an Argon2 hash. Stop the in-place source rewrite. **Done.**
3. **Vendor Font Awesome + fonts into `/static/`.** Drop every CDN `<link>` from `templates/*.html`. **Done** — Font Awesome 6.4.0 Free at `static/fontawesome/{css,webfonts}/`. Only the woff2 + ttf for `solid-900`, `regular-400`, `brands-400` are vendored (skipped `v4compatibility` and `svg-with-js` since unused).
4. **logrotate config** for `/var/log/openwrt-collector.log` (5 MB × 5 files, `copytruncate`). **Done** — config in `system/openwrt-monitor.logrotate`, installed at `/etc/logrotate.d/openwrt-monitor`. `copytruncate` is required because `ubus_collector.py` uses `logging.FileHandler` which holds the FD open.

### P1 — Async migration (the actual unlock for 30 routers)

5. **Port the collector to asyncio + asyncssh.** One persistent connection per router. Kill `subprocess.run(['ssh', ...])`.
6. **Port Flask → FastAPI.** Preserve URL paths so the frontend keeps working. Move auth to FastAPI session middleware.
7. **Add WebSocket `/ws/live`.** Server pushes router status changes and client deltas. Remove `setInterval(fetch...)` from `dashboard.html`.

### P2 — Historize the discarded data

8. **New time-series tables:**
   - `client_history` — `mac`, `router_ip`, `interface`, `signal`, `rx_rate`, `tx_rate`, `rx_bytes`, `tx_bytes`, `connected_time`, `ts` — index `(mac, ts)` and `(router_ip, ts)`.
   - `interface_history` — `router_ip`, `interface`, `num_clients`, `channel`, `ts`.
   - `router_status_history` — `router_ip`, `online`, `ts` (write only on transition).
   - `system_metrics` — `router_ip`, `uptime`, `load1`, `load5`, `load15`, `mem_used`, `mem_total`, `ts`.
9. **Capture per-router system info** via `ubus call system info` and `ubus call network.device status`. Currently dropped; trivially useful for an ops dashboard.
10. **DHCP lease ingestion.** Read `/tmp/dhcp.leases` over SSH every N polls, write to `dhcp_leases` (mac, ip, hostname, expires, router_ip). JOIN on MAC in the clients UI so rows show IP + hostname.
11. **New page: Clients (cross-router).** One row per known MAC: current AP, signal trend sparkline, total bytes, first/last seen. Filterable, sortable.

### P3 — Operations & maintenance UI

12. **Logs Maintenance page** (explicitly requested):
    - `history_retention_days` — default **30**, applied to every `*_history` table.
    - `raw_log_lines` — default 500, how many lines `/logs` shows from `journalctl`.
    - `vacuum_schedule` — default weekly. `DELETE FROM *_history WHERE ts < ?` then `VACUUM`.
    - Background async task; show next-run + last-run + last-vacuum-result in the UI.
13. **Reachability strip per router** — last 24h online/offline timeline from `router_status_history`.
14. **DB backup/restore endpoint.** The `monitor.db.backup-*` files in `/var/lib/openwrt-monitor/` show this is already partially done by hand. Wire it up: nightly snapshot, prune older than N days, manual "download backup" button.

### P4 — UI overhaul (info density + theme)

15. **Light/dark theme via CSS custom properties** on `<html data-theme="light|dark">`. Persist in `localStorage`. Default = `prefers-color-scheme`. Toggle in the header.
16. **Dense router grid.** Replace today's 1-card-per-router stack with a single-row layout: status dot + hostname + IP + total clients + 2.4 GHz / 5 GHz badges + load + uptime. Click to expand client list inline. **Target: 30 routers visible without scrolling on 1080p.**
17. **Sortable / filterable clients table.** By signal, by router, by SSID, by inactive time. Virtualize rows so 200+ clients render without jank on the Atom.
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
