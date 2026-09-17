#!/usr/bin/env python3
"""OpenWrt Ubus Collector — asyncio + asyncssh.

One persistent SSH connection per router, kept open across polls.
Replaces the previous threaded subprocess-ssh-per-poll model, which
was the dominant CPU cost on Atom-class hardware.

Schema and config are unchanged from v1 — Flask reads the same
SQLite tables (routers, interfaces, clients).
"""

import asyncio
import json
import logging
import re
import signal
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import asyncssh

import backup
import pfsense
import retention


CONFIG_PATH = Path("/etc/openwrt-monitor/config.json")
DB_PATH = Path("/var/lib/openwrt-monitor/monitor.db")
LOG_PATH = Path("/var/log/openwrt-collector.log")

CONNECT_TIMEOUT = 5
COMMAND_TIMEOUT = 10
BACKOFF_INITIAL = 2
BACKOFF_MAX = 60
# Seconds between history snapshots. Decoupled from poll_interval
# so we can poll frequently for the live view without bloating the
# history tables. Override per-deploy via config['history_interval'].
HISTORY_INTERVAL_DEFAULT = 300
# Seconds between pfSense ARP-table pulls. pfSense's ARP cache itself
# updates much faster than this, so 30s is a fine UX/load compromise.
PFSENSE_ARP_INTERVAL_DEFAULT = 30
# Seconds between pfSense captive-portal voucher-session pulls.
PFSENSE_VOUCHER_INTERVAL_DEFAULT = 30
# Seconds between pfSense allowed/pass-through MAC pulls. Admin-managed list
# that changes rarely, so this polls much less often than voucher sessions.
PFSENSE_TRUSTED_INTERVAL_DEFAULT = 300

# ---- Uplink bandwidth / client-count sampling ----
# Byte counters and the associated-client count are read every poll and
# accumulated in memory; one row per router lands in bandwidth_history each
# bucket. 360s x 24h = 240 points — the same row count and the same SVG size
# as the original 120s x 8h, because the column is now half as wide and finer
# resolution than one point per pixel would only cost CPU.
BANDWIDTH_BUCKET_DEFAULT = 360          # config['bandwidth_interval']
BANDWIDTH_WINDOW_HOURS_DEFAULT = 24     # config['bandwidth_window_hours']
# How often the bridge/carrier/wireless topology probe is re-run to
# re-pick the uplink port. Cabling changes rarely; this is cheap insurance.
TOPO_REFRESH_SECONDS = 300
# ath79/ag71xx keeps its netdev byte counters in an unsigned long, which is
# 32-bit on mips32 — eth1/eth0 on the ArcherC7, E314Nv2 and UnifiACmesh wrap
# at 4 GiB. Deltas must be corrected for that.
COUNTER_32BIT = 1 << 32
# A delta implying more than this is a counter reset (reboot), not traffic.
# The fastest uplink in the fleet is 2.5 GbE.
MAX_PLAUSIBLE_BPS = 2_500_000_000


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler(LOG_PATH)],
)
# asyncssh logs every channel open/close at INFO — far too chatty
# for this use case where we open ~3 channels per router per poll.
logging.getLogger("asyncssh").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


def load_config() -> dict:
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except FileNotFoundError:
        logger.error(f"Config not found: {CONFIG_PATH}")
        return {}
    except json.JSONDecodeError as e:
        logger.error(f"Bad JSON in {CONFIG_PATH}: {e}")
        return {}


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    # WAL is a persistent property of the DB file, so setting it once here
    # applies to every connection (collector tasks, Flask, retention,
    # backup) and survives restarts. Lets readers and a writer proceed
    # concurrently, which avoids most "database is locked" errors now that
    # several asyncio tasks plus Flask all write/read the same file.
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS routers (
            ip TEXT PRIMARY KEY,
            hostname TEXT,
            online INTEGER DEFAULT 0,
            last_seen DATETIME,
            first_seen DATETIME
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS interfaces (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            router_ip TEXT,
            interface TEXT,
            ssid TEXT, bssid TEXT,
            frequency INTEGER, channel INTEGER, bandwidth INTEGER,
            mode TEXT, encryption TEXT,
            num_clients INTEGER DEFAULT 0,
            last_updated DATETIME,
            UNIQUE(router_ip, interface)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS clients (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            router_ip TEXT, interface TEXT, mac TEXT,
            signal INTEGER, signal_avg INTEGER, noise INTEGER,
            rx_rate INTEGER, tx_rate INTEGER,
            rx_packets INTEGER, tx_packets INTEGER,
            rx_bytes INTEGER, tx_bytes INTEGER,
            connected_time INTEGER, inactive INTEGER, authorized INTEGER,
            last_seen DATETIME, first_seen DATETIME,
            UNIQUE(router_ip, interface, mac)
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_clients_router ON clients(router_ip)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_clients_mac ON clients(mac)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_interfaces_router ON interfaces(router_ip)")

    # ---- History tables (P2 #8) ----
    # Append-only; bounded by retention task (P3, not yet wired up).
    cur.execute("""
        CREATE TABLE IF NOT EXISTS client_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts DATETIME NOT NULL,
            router_ip TEXT NOT NULL,
            interface TEXT NOT NULL,
            mac TEXT NOT NULL,
            signal INTEGER,
            rx_rate INTEGER, tx_rate INTEGER,
            rx_bytes INTEGER, tx_bytes INTEGER,
            connected_time INTEGER
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_client_history_ts ON client_history(ts)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_client_history_mac_ts ON client_history(mac, ts)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_client_history_router_ts ON client_history(router_ip, ts)")

    cur.execute("""
        CREATE TABLE IF NOT EXISTS interface_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts DATETIME NOT NULL,
            router_ip TEXT NOT NULL,
            interface TEXT NOT NULL,
            num_clients INTEGER,
            channel INTEGER,
            frequency INTEGER
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_iface_history_ts ON interface_history(ts)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_iface_history_router_ts ON interface_history(router_ip, ts)")

    cur.execute("""
        CREATE TABLE IF NOT EXISTS router_status_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts DATETIME NOT NULL,
            router_ip TEXT NOT NULL,
            online INTEGER NOT NULL
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_status_history_router_ts ON router_status_history(router_ip, ts)")

    cur.execute("""
        CREATE TABLE IF NOT EXISTS system_metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts DATETIME NOT NULL,
            router_ip TEXT NOT NULL,
            uptime INTEGER,
            load1 REAL, load5 REAL, load15 REAL,
            mem_total INTEGER, mem_free INTEGER, mem_used INTEGER
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_sysmetrics_router_ts ON system_metrics(router_ip, ts)")

    # ---- Uplink bandwidth + client-count history ----
    # One row per router per bucket, holding the bytes that crossed the AP's
    # uplink port group during that bucket, and the mean number of associated
    # clients over it. `ts` is a unix INTEGER (bucket start, wall-clock
    # aligned so every router shares bucket boundaries) —
    # deliberately NOT the ISO string the other *_history tables use, because
    # /api/bandwidth buckets it arithmetically. retention.py handles it
    # separately for exactly that reason.
    # `span` is the number of seconds actually measured inside the bucket; it
    # is < the bucket length when the router was offline for part of it, and
    # is what the API divides by to get bit/s.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS bandwidth_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts INTEGER NOT NULL,
            router_ip TEXT NOT NULL,
            rx_bytes INTEGER NOT NULL,
            tx_bytes INTEGER NOT NULL,
            span INTEGER NOT NULL,
            clients INTEGER,
            clients_24 INTEGER,
            clients_5 INTEGER
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_bw_router_ts ON bandwidth_history(router_ip, ts)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_bw_ts ON bandwidth_history(ts)")

    # ---- per-SSID associated-client counts (fleet graph, §16) ----
    # One row per (bucket, router, SSID), carrying the mean number of clients
    # on that SSID over the polls that landed in the bucket. Deliberately a
    # separate table rather than more columns on bandwidth_history: that table
    # is one row per router per bucket, and an SSID set is a variable-width
    # dimension that would need either a column per SSID (renaming an SSID
    # would need a migration) or a JSON blob.
    #
    # It rides bandwidth_history's bucket boundaries, window and
    # self-cleaning-on-write retention, so the dashboard can index both with
    # the same arithmetic and neither waits on the daily retention pass.
    #
    # Stored per-router even though the graph is fleet-wide, because that is
    # where the data is produced — each router task owns its own accumulator
    # and flushes independently. The API sums across routers per bucket.
    # Zero-client SSIDs are stored too: a configured-but-empty SSID is a real
    # zero, not a gap, and dropping those rows would make an all-quiet bucket
    # indistinguishable from an offline one.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ssid_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts INTEGER NOT NULL,
            router_ip TEXT NOT NULL,
            ssid TEXT NOT NULL,
            clients INTEGER NOT NULL
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_ssid_hist_router_ts ON ssid_history(router_ip, ts)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_ssid_hist_ts ON ssid_history(ts)")

    # ---- pfSense ARP cache (P2 #10) ----
    # MAC is canonicalised to lowercase so JOINs against clients.mac
    # (which is whatever case the AP reported) work via lower().
    cur.execute("""
        CREATE TABLE IF NOT EXISTS arp_entries (
            mac TEXT PRIMARY KEY,
            ip TEXT,
            hostname TEXT,
            interface TEXT,
            last_seen DATETIME NOT NULL,
            expires_seconds INTEGER
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_arp_ip ON arp_entries(ip)")

    # ---- pfSense captive-portal voucher sessions ----
    # One row per currently-active captive-portal session, keyed by the
    # client's (lowercased) MAC so /api/clients can LEFT JOIN clients.mac.
    # This is a live snapshot: voucher_loop full-replaces the table each
    # cycle, so no history/retention path is needed. voucher_code is the
    # captive-portal `username` (the voucher for voucher-auth sessions);
    # allow_time is the unix session start, session_timeout its length in s.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS voucher_sessions (
            mac TEXT PRIMARY KEY,
            ip TEXT,
            voucher_code TEXT,
            authmethod TEXT,
            allow_time INTEGER,
            session_timeout INTEGER,
            last_activity INTEGER,
            last_seen DATETIME NOT NULL
        )
    """)

    # ---- pfSense captive-portal allowed / pass-through MACs ----
    # Devices the admin whitelisted so they bypass the portal (no voucher).
    # Keyed by lowercased MAC for the /api/clients JOIN; full-replaced each
    # trusted_loop cycle. descr is the admin's friendly label.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS trusted_macs (
            mac TEXT PRIMARY KEY,
            descr TEXT,
            last_seen DATETIME NOT NULL
        )
    """)

    # ---- Configured wifi-iface sections from UCI (active or disabled) ----
    # Lets the UI surface configured-but-off SSIDs with a "disabled" badge
    # instead of just omitting them. Section is the UCI section name and
    # is unique per router.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS wifi_iface_config (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            router_ip TEXT NOT NULL,
            section TEXT NOT NULL,
            ssid TEXT,
            radio TEXT,
            disabled INTEGER NOT NULL DEFAULT 0,
            last_updated DATETIME NOT NULL,
            UNIQUE(router_ip, section)
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_wic_router ON wifi_iface_config(router_ip)")

    # Migration: add radio health columns (noise floor, BSS bitrate, TX power).
    # SQLite doesn't support IF NOT EXISTS on ALTER TABLE, so catch the duplicate error.
    for _ddl in [
        "ALTER TABLE interfaces ADD COLUMN noise INTEGER",
        "ALTER TABLE interfaces ADD COLUMN bitrate INTEGER",
        "ALTER TABLE interfaces ADD COLUMN txpower INTEGER",
        # last_activity added after voucher_sessions shipped without it.
        "ALTER TABLE voucher_sessions ADD COLUMN last_activity INTEGER",
        # Live uplink throughput, refreshed every poll so /api/routers (10s)
        # can show a current number next to the 24h sparklines.
        "ALTER TABLE routers ADD COLUMN bw_in_bps INTEGER",
        "ALTER TABLE routers ADD COLUMN bw_out_bps INTEGER",
        "ALTER TABLE routers ADD COLUMN bw_uplink TEXT",
        "ALTER TABLE routers ADD COLUMN bw_updated INTEGER",
        # How the uplink was chosen: gateway | override | traffic | none.
        "ALTER TABLE routers ADD COLUMN bw_uplink_src TEXT",
        # Mean associated-client count per bucket, added after
        # bandwidth_history shipped. NULL on pre-migration rows, which the
        # dashboard renders as a gap; it fills in within one window.
        "ALTER TABLE bandwidth_history ADD COLUMN clients INTEGER",
        # Per-band split of that mean, so the client sparkline can show the
        # 2.4G/5G distribution as two overlaid series. NULL on rows written
        # before this column existed; the dashboard draws those buckets as a
        # bare total line, and they fill in within one window.
        "ALTER TABLE bandwidth_history ADD COLUMN clients_24 INTEGER",
        "ALTER TABLE bandwidth_history ADD COLUMN clients_5 INTEGER",
    ]:
        try:
            cur.execute(_ddl)
        except sqlite3.OperationalError:
            pass  # column already exists on existing DBs

    conn.commit()
    conn.close()


def parse_bandwidth(htmode: str) -> Optional[int]:
    """Channel width in MHz from iwinfo's `htmode` string.

    Takes the width off the end of the token instead of matching each mode
    name, so every generation's prefix works: HT (n), VHT (ac), HE (ax),
    EHT (be). The old prefix list missed HE20/HE40 entirely — "HE20" does
    not contain "HT20" — which is what OpenWrt 25.12 reports on the WiFi 6
    APs, leaving their bandwidth NULL.
    """
    if not htmode:
        return None
    mode = htmode.upper().strip()
    if mode == "NOHT":
        return 20
    if "80+80" in mode:
        return 160  # two non-contiguous 80 MHz segments
    m = re.search(r"(\d+)$", mode)
    if not m:
        return None
    width = int(m.group(1))
    return width if width in (20, 40, 80, 160, 320) else None


def parse_router_arp(output: str) -> dict:
    """Parse `ip neigh show` stdout into {lowercase_mac: ip}.

    Skips entries without a resolved MAC (no 'lladdr') and entries in
    terminal states (FAILED, INCOMPLETE). STALE/REACHABLE/DELAY/PROBE
    are all valid — the client was seen recently enough to have a mapping.
    """
    result = {}
    for line in output.splitlines():
        parts = line.split()
        if "lladdr" not in parts:
            continue
        state = parts[-1]
        if state in ("FAILED", "INCOMPLETE"):
            continue
        try:
            ip = parts[0]
            mac = parts[parts.index("lladdr") + 1].lower()
        except (ValueError, IndexError):
            continue
        if mac and ip:
            result[mac] = ip
    return result


def parse_netdev(output: str) -> dict:
    """Parse `cat /proc/net/dev` stdout into {iface: (rx_bytes, tx_bytes)}.

    Layout is two header lines then one line per interface:
        eth1: <rx_bytes> <rx_packets> ... x8 | <tx_bytes> <tx_packets> ...
    The name may butt straight up against the colon and the first number,
    so split on the first ':' rather than on whitespace.
    """
    result = {}
    for line in output.splitlines():
        if ":" not in line:
            continue  # the two header lines
        name, _, rest = line.partition(":")
        name = name.strip()
        fields = rest.split()
        if not name or len(fields) < 9:
            continue
        try:
            result[name] = (int(fields[0]), int(fields[8]))
        except ValueError:
            continue
    return result


def parse_topology(brif: str, carrier: str, phy: str) -> list[str]:
    """Work out which interfaces are wired bridge ports that are link-up.

    Inputs are the raw stdout of, respectively:
        ls -d /sys/class/net/*/brif/*      -> bridge membership
        grep -H . /sys/class/net/*/carrier -> link state (path:value)
        ls -d /sys/class/net/*/phy80211    -> which netdevs are wireless

    Everything a bridge holds that is not a wireless netdev is a wired port;
    carrier filters out the empty LAN sockets. Returns the wired, link-up
    member names (e.g. ['lan1', 'lan3'] or ['eth1.9', 'eth1.11', 'eth1.12']).
    """
    wireless = set()
    for line in phy.splitlines():
        parts = line.strip().strip("/").split("/")
        # /sys/class/net/<name>/phy80211
        if len(parts) >= 2 and parts[-1] == "phy80211":
            wireless.add(parts[-2])

    carriers = {}
    for line in carrier.splitlines():
        path, _, value = line.partition(":")
        parts = path.strip().strip("/").split("/")
        if len(parts) >= 2 and parts[-1] == "carrier":
            carriers[parts[-2]] = value.strip()

    members = []
    for line in brif.splitlines():
        parts = line.strip().strip("/").split("/")
        # /sys/class/net/<bridge>/brif/<member>
        if len(parts) < 2 or parts[-2] != "brif":
            continue
        name = parts[-1]
        if name in wireless or name in members:
            continue
        if carriers.get(name) != "1":
            continue
        members.append(name)
    return members


def parse_gateway(output: str) -> tuple[Optional[str], Optional[str]]:
    """Parse the `@@GW@@` line — "<gateway ip> <gateway mac>"."""
    for line in output.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            return parts[0], parts[1].lower()
        if len(parts) == 1:
            return parts[0], None  # default route, but the MAC isn't resolved
    return None, None


def parse_port_numbers(output: str) -> dict:
    """Parse `grep -H . /sys/class/net/*/brif/*/port_no` into
    {(bridge, port_no): member}.

    The sysfs value is HEX ("0x1" ... "0xa") while `brctl showmacs` prints the
    same port number in DECIMAL, so this converts to int for the join.
    """
    result = {}
    for line in output.splitlines():
        path, _, value = line.partition(":")
        parts = path.strip().strip("/").split("/")
        # /sys/class/net/<bridge>/brif/<member>/port_no
        if len(parts) < 4 or parts[-1] != "port_no" or parts[-3] != "brif":
            continue
        try:
            result[(parts[-4], int(value.strip(), 16))] = parts[-2]
        except ValueError:
            continue
    return result


def parse_fdb(output: str) -> list[tuple]:
    """Parse the bridge-prefixed `brctl showmacs` lines into
    [(bridge, port_no, mac, is_local, age)].

    Columns after our sed prefix: bridge, port no, mac addr, is local?, ageing.
    """
    result = []
    for line in output.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        try:
            port = int(parts[1])
            age = float(parts[4]) if len(parts) > 4 else 0.0
        except ValueError:
            continue  # the "port no  mac addr ..." header, if it ever matches
        result.append((parts[0], port, parts[2].lower(), parts[3] == "yes", age))
    return result


def gateway_ports(fdb: list[tuple], portno: dict,
                  gw_mac: Optional[str]) -> list[str]:
    """Bridge member names behind which the default gateway's MAC is learned.

    Freshest first (smallest ageing timer), so that after a re-cable the new
    port wins over the old entry still ageing out of the FDB.
    """
    if not gw_mac:
        return []
    hits = [
        (age, portno[(bridge, port)])
        for bridge, port, mac, is_local, age in fdb
        # is_local rows are the bridge's own port MACs, never a path to anything.
        if mac == gw_mac and not is_local and (bridge, port) in portno
    ]
    out = []
    for _age, member in sorted(hits):
        if member not in out:
            out.append(member)
    return out


def pick_uplink(wired_up: list[str], counters: dict,
                override: Optional[str] = None,
                gw_ports: Optional[list[str]] = None
                ) -> tuple[Optional[str], list[str], str]:
    """Choose the AP's uplink port group from its wired bridge ports.

    Members are grouped by physical parent (`eth1.9`/`eth1.11`/`eth1.12` all
    belong to `eth1`) because the ArcherC7 bridges VLAN sub-interfaces rather
    than the port itself. On the APs that have a second wired port in use, that
    port is a daisy-chained downstream device whose traffic also crosses the
    uplink — counting both would double it, so only one group is ever chosen.

    Three steps, in order of how much we trust them:

    1. `override` — the parent name from config['bandwidth_uplink'].
    2. `gw_ports` — the bridge ports the default gateway's MAC is learned on.
       This is the real definition of "uplink": the port facing pfSense, i.e.
       the internet path. It is derived, not inferred, and is correct on a
       brand-new AP's first poll because it needs no traffic history.
    3. Lifetime rx+tx — the original heuristic. Only reached when the gateway
       isn't resolved yet (AP still booting) or sits behind a wireless port
       (mesh backhaul), since a wireless member is never in `wired_up`.

    Returns (group_name, [member ifaces], source).
    """
    groups: dict[str, list[str]] = {}
    for name in wired_up:
        groups.setdefault(name.split(".")[0], []).append(name)
    if not groups:
        return None, [], "none"
    if override and override in groups:
        return override, groups[override], "override"

    for member in gw_ports or []:
        parent = member.split(".")[0]
        if parent in groups:
            # Every wired member of that physical port counts, not just the one
            # the gateway happened to be visible on: on the ArcherC7 the
            # gateway shows up on eth1.9/eth1.11 but eth1.12 is the same cable.
            return parent, groups[parent], "gateway"

    def score(members: list[str]) -> int:
        return sum(sum(counters.get(m, (0, 0))) for m in members)

    best = max(groups.items(), key=lambda kv: (score(kv[1]), kv[0]))
    return best[0], best[1], "traffic"


def counter_delta(prev: int, cur: int, elapsed: float) -> Optional[int]:
    """Bytes transferred between two counter readings, or None if unusable.

    Handles the 32-bit wrap on ath79 (see COUNTER_32BIT). A wrap inside one
    10s poll can only mean the counter just crossed 2^32, so the corrected
    delta is small; a reboot instead yields a ~4 GiB "delta", which the
    plausibility cap rejects.
    """
    d = cur - prev
    if d < 0:
        if prev < COUNTER_32BIT and cur < COUNTER_32BIT:
            d += COUNTER_32BIT
        else:
            return None
    if elapsed > 0 and (d * 8.0 / elapsed) > MAX_PLAUSIBLE_BPS:
        return None
    return d


def parse_client(c: dict) -> dict:
    rx = c.get("rx") or {}
    tx = c.get("tx") or {}
    # iwinfo reports rates in kbit/s; store Mbit/s for the UI. Divide
    # unconditionally — the old `if rate > 1000` guard left stations at
    # <= 1 Mbit/s (legacy/distant clients on basic rates) unconverted,
    # so a 1 Mbit/s client showed up as "1000 Mbps".
    rx_rate = round((rx.get("rate", 0) or 0) / 1000)
    tx_rate = round((tx.get("rate", 0) or 0) / 1000)
    return {
        "mac": c.get("mac"),
        "signal": c.get("signal", 0),
        "signal_avg": c.get("signal_avg", c.get("signal", 0)),
        "noise": c.get("noise", -95),
        "rx_rate": rx_rate, "tx_rate": tx_rate,
        "rx_packets": rx.get("packets", 0), "tx_packets": tx.get("packets", 0),
        "rx_bytes": rx.get("bytes", 0), "tx_bytes": tx.get("bytes", 0),
        "connected_time": c.get("connected_time", 0),
        "inactive": c.get("inactive", 0),
        "authorized": 1 if c.get("authorized", True) else 0,
    }


def _check_transition(cur, router_ip: str, new_online: int, now: str):
    """Append a router_status_history row only if online state actually changed."""
    cur.execute("SELECT online FROM routers WHERE ip=?", (router_ip,))
    row = cur.fetchone()
    prev = row[0] if row else None
    if prev != new_online:
        cur.execute(
            "INSERT INTO router_status_history (ts, router_ip, online) VALUES (?, ?, ?)",
            (now, router_ip, new_online),
        )


def save_snapshot(router_ip: str, hostname: str, interfaces: list[dict],
                  wifi_config: Optional[list[dict]] = None):
    """Persist one poll's results.

    `wifi_config` is the parsed UCI wireless config (list of wifi-iface
    sections including disabled ones). Pass None to leave that table
    untouched for this poll (e.g. the UCI fetch failed)."""
    now = datetime.now().isoformat()
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.cursor()
        _check_transition(cur, router_ip, 1, now)
        cur.execute("""
            INSERT INTO routers (ip, hostname, online, last_seen, first_seen)
            VALUES (?, ?, 1, ?, ?)
            ON CONFLICT(ip) DO UPDATE SET
                hostname=excluded.hostname,
                online=1,
                last_seen=excluded.last_seen
        """, (router_ip, hostname, now, now))

        # Drop interfaces (and their clients) that no longer exist on the
        # router — e.g. a wifi-iface UCI section was disabled and its
        # phy*-apN device is gone from `iwinfo devices`. Without this,
        # the old rows linger forever and the UI shows phantom SSIDs.
        active = [iface["device"] for iface in interfaces]
        if active:
            placeholders = ",".join("?" * len(active))
            cur.execute(
                f"DELETE FROM clients "
                f"WHERE router_ip = ? AND interface NOT IN ({placeholders})",
                (router_ip, *active),
            )
            cur.execute(
                f"DELETE FROM interfaces "
                f"WHERE router_ip = ? AND interface NOT IN ({placeholders})",
                (router_ip, *active),
            )
        else:
            # No active interfaces at all: clean both tables for this router.
            cur.execute("DELETE FROM clients WHERE router_ip = ?", (router_ip,))
            cur.execute("DELETE FROM interfaces WHERE router_ip = ?", (router_ip,))

        # UCI wifi-iface set — only touch the table if we got a real
        # response (None means the uci.get call failed; don't nuke).
        if wifi_config is not None:
            sections = [w["section"] for w in wifi_config]
            if sections:
                ph = ",".join("?" * len(sections))
                cur.execute(
                    f"DELETE FROM wifi_iface_config "
                    f"WHERE router_ip = ? AND section NOT IN ({ph})",
                    (router_ip, *sections),
                )
            else:
                cur.execute(
                    "DELETE FROM wifi_iface_config WHERE router_ip = ?",
                    (router_ip,),
                )
            for w in wifi_config:
                cur.execute("""
                    INSERT INTO wifi_iface_config
                        (router_ip, section, ssid, radio, disabled, last_updated)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(router_ip, section) DO UPDATE SET
                        ssid = excluded.ssid,
                        radio = excluded.radio,
                        disabled = excluded.disabled,
                        last_updated = excluded.last_updated
                """, (router_ip, w["section"], w["ssid"], w["radio"],
                      w["disabled"], now))

        for iface in interfaces:
            info = iface["info"]
            clients = iface["clients"]
            cur.execute("""
                INSERT INTO interfaces
                    (router_ip, interface, ssid, bssid, frequency, channel,
                     bandwidth, mode, encryption, num_clients,
                     noise, bitrate, txpower, last_updated)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(router_ip, interface) DO UPDATE SET
                    ssid=excluded.ssid, bssid=excluded.bssid,
                    frequency=excluded.frequency, channel=excluded.channel,
                    bandwidth=excluded.bandwidth, mode=excluded.mode,
                    encryption=excluded.encryption,
                    num_clients=excluded.num_clients,
                    noise=excluded.noise, bitrate=excluded.bitrate,
                    txpower=excluded.txpower,
                    last_updated=excluded.last_updated
            """, (
                router_ip, iface["device"],
                info.get("ssid", ""), info.get("bssid", ""),
                info.get("frequency"), info.get("channel"),
                parse_bandwidth(info.get("htmode", "")),
                info.get("mode", ""),
                (info.get("encryption") or {}).get("description", "Open"),
                len(clients),
                info.get("noise"),
                info.get("bitrate"),
                info.get("txpower"),
                now,
            ))

            cur.execute(
                "SELECT mac, first_seen FROM clients WHERE router_ip=? AND interface=?",
                (router_ip, iface["device"]),
            )
            existing = {row[0]: row[1] for row in cur.fetchall()}

            cur.execute(
                "DELETE FROM clients WHERE router_ip=? AND interface=?",
                (router_ip, iface["device"]),
            )

            for c in clients:
                if not c["mac"]:
                    continue
                cur.execute("""
                    INSERT INTO clients
                        (router_ip, interface, mac, signal, signal_avg, noise,
                         rx_rate, tx_rate, rx_packets, tx_packets,
                         rx_bytes, tx_bytes, connected_time, inactive,
                         authorized, last_seen, first_seen)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    router_ip, iface["device"], c["mac"],
                    c["signal"], c["signal_avg"], c["noise"],
                    c["rx_rate"], c["tx_rate"],
                    c["rx_packets"], c["tx_packets"],
                    c["rx_bytes"], c["tx_bytes"],
                    c["connected_time"], c["inactive"],
                    c["authorized"], now, existing.get(c["mac"], now),
                ))
        conn.commit()
    finally:
        conn.close()


def mark_offline(router_ip: str):
    now = datetime.now().isoformat()
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.cursor()
        _check_transition(cur, router_ip, 0, now)
        cur.execute("""
            INSERT INTO routers (ip, hostname, online, last_seen, first_seen)
            VALUES (?, ?, 0, ?, ?)
            ON CONFLICT(ip) DO UPDATE SET online=0
        """, (router_ip, router_ip, now, now))
        # Clear the live throughput so the dashboard shows "—" rather than
        # the last rate seen before the AP dropped.
        cur.execute(
            "UPDATE routers SET bw_in_bps=NULL, bw_out_bps=NULL WHERE ip=?",
            (router_ip,),
        )
        conn.commit()
    finally:
        conn.close()


def save_live_bandwidth(router_ip: str, uplink: Optional[str], src: str,
                        in_bps: Optional[int], out_bps: Optional[int]):
    """Store the instantaneous uplink rate on the routers row.

    Kept on `routers` (rather than read back off bandwidth_history) so the
    dashboard's existing 10s /api/routers poll carries a live number without
    a second query — the sparkline data only lands once per bucket.
    """
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            "UPDATE routers SET bw_in_bps=?, bw_out_bps=?, bw_uplink=?, "
            "bw_uplink_src=?, bw_updated=? WHERE ip=?",
            (in_bps, out_bps, uplink, src, int(time.time()), router_ip),
        )
        conn.commit()
    finally:
        conn.close()


def save_bandwidth_bucket(router_ip: str, ts: int, rx: int, tx: int,
                          span: int, clients: Optional[int],
                          clients_24: Optional[int], clients_5: Optional[int],
                          window_hours: int):
    """Append one closed bucket and drop anything outside the window.

    Self-cleaning like voucher_sessions (§12): the table is pinned to the
    display window on every write, so it never waits on the daily retention
    pass and never grows past ~(window / bucket) rows per router.

    `span` is 0 when no usable byte delta landed in the bucket (no uplink
    picked, or a counter reset). The row is still written for the sake of
    `clients`, and the API treats span<=0 as "no bandwidth sample" — the two
    series are independent gaps.

    `clients_24` / `clients_5` are the same mean split by band, drawn as two
    overlaid series. They are rounded independently of `clients`, so their
    sum can differ from it by 1 — harmless, since the two are never summed
    for display.
    """
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO bandwidth_history "
            "(ts, router_ip, rx_bytes, tx_bytes, span, clients, clients_24, clients_5) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (ts, router_ip, rx, tx, span, clients, clients_24, clients_5),
        )
        cur.execute(
            "DELETE FROM bandwidth_history WHERE router_ip=? AND ts < ?",
            (router_ip, ts - window_hours * 3600),
        )
        conn.commit()
    finally:
        conn.close()


def save_ssid_bucket(router_ip: str, ts: int, counts: dict[str, int],
                     window_hours: int):
    """Append this router's per-SSID client means for one closed bucket.

    Self-cleaning on write, on the same window as save_bandwidth_bucket, so
    the table stays pinned at ~(window / bucket) x (SSIDs on this router)
    rows per router and never waits on the daily retention pass.

    Every SSID the router had an interface for is written, zeros included —
    see the table comment in init_db().
    """
    if not counts:
        return
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.cursor()
        cur.executemany(
            "INSERT INTO ssid_history (ts, router_ip, ssid, clients) "
            "VALUES (?, ?, ?, ?)",
            [(ts, router_ip, ssid, n) for ssid, n in counts.items()],
        )
        cur.execute(
            "DELETE FROM ssid_history WHERE router_ip=? AND ts < ?",
            (router_ip, ts - window_hours * 3600),
        )
        conn.commit()
    finally:
        conn.close()



def save_history(router_ip: str, interfaces: list[dict],
                 system_info: Optional[dict], now: str):
    """Append one snapshot's worth of rows to the history tables."""
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.cursor()
        for iface in interfaces:
            info = iface["info"]
            clients = iface["clients"]
            cur.execute(
                "INSERT INTO interface_history "
                "(ts, router_ip, interface, num_clients, channel, frequency) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (now, router_ip, iface["device"], len(clients),
                 info.get("channel"), info.get("frequency")),
            )
            for c in clients:
                if not c["mac"]:
                    continue
                cur.execute(
                    "INSERT INTO client_history "
                    "(ts, router_ip, interface, mac, signal, rx_rate, tx_rate, "
                    " rx_bytes, tx_bytes, connected_time) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (now, router_ip, iface["device"], c["mac"],
                     c["signal"], c["rx_rate"], c["tx_rate"],
                     c["rx_bytes"], c["tx_bytes"], c["connected_time"]),
                )
        if system_info:
            cur.execute(
                "INSERT INTO system_metrics "
                "(ts, router_ip, uptime, load1, load5, load15, "
                " mem_total, mem_free, mem_used) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (now, router_ip,
                 system_info.get("uptime"),
                 system_info.get("load1"), system_info.get("load5"),
                 system_info.get("load15"),
                 system_info.get("mem_total"),
                 system_info.get("mem_free"),
                 system_info.get("mem_used")),
            )
        conn.commit()
    finally:
        conn.close()


async def ubus_call(conn: asyncssh.SSHClientConnection,
                    namespace: str, method: str,
                    params: Optional[dict] = None) -> Optional[dict]:
    cmd = f"ubus call {namespace} {method}"
    if params:
        payload = json.dumps(params).replace("'", "'\\''")
        cmd += f" '{payload}'"
    try:
        result = await asyncio.wait_for(
            conn.run(cmd, check=False), timeout=COMMAND_TIMEOUT
        )
    except asyncio.TimeoutError:
        return None
    if result.exit_status != 0:
        return None
    out = (result.stdout or "").strip()
    if not out:
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        logger.debug(f"bad JSON from {namespace}.{method}")
        return None


async def fetch_wifi_config(conn: asyncssh.SSHClientConnection) -> Optional[list[dict]]:
    """`ubus call uci get '{"config":"wireless"}'` → list of wifi-iface
    sections including disabled ones. Returns None on failure so callers
    can distinguish "fetch failed" from "no wifi-iface configured"."""
    result = await ubus_call(conn, "uci", "get", {"config": "wireless"})
    if not result:
        return None
    values = result.get("values") or {}
    out: list[dict] = []
    for section_name, section in values.items():
        if not isinstance(section, dict):
            continue
        if section.get(".type") != "wifi-iface":
            continue
        out.append({
            "section": section.get(".name", section_name),
            "ssid": section.get("ssid", "") or "",
            "radio": section.get("device", "") or "",
            "disabled": 1 if str(section.get("disabled", "0")) == "1" else 0,
        })
    return out


async def fetch_system_info(conn: asyncssh.SSHClientConnection) -> Optional[dict]:
    """`ubus call system info` → load avg, uptime, memory."""
    result = await ubus_call(conn, "system", "info")
    if not result:
        return None
    load = result.get("load") or [0, 0, 0]
    mem = result.get("memory") or {}
    total = mem.get("total", 0)
    free = mem.get("free", 0)
    # OpenWrt reports the 3 load averages as fixed-point: raw / 65536.
    return {
        "uptime": result.get("uptime", 0),
        "load1":  load[0] / 65536.0 if len(load) > 0 else 0.0,
        "load5":  load[1] / 65536.0 if len(load) > 1 else 0.0,
        "load15": load[2] / 65536.0 if len(load) > 2 else 0.0,
        "mem_total": total,
        "mem_free": free,
        "mem_used": total - free,
    }


# Byte counters ride along with the ARP probe that poll_once already runs,
# so sampling bandwidth costs no extra SSH round trip. The topology suffix
# is appended only when the uplink needs re-picking (TOPO_REFRESH_SECONDS).
NETDEV_CMD = "ip neigh show; echo '@@NETDEV@@'; cat /proc/net/dev"
TOPO_SUFFIX = (
    "; echo '@@BRIF@@'; ls -d /sys/class/net/*/brif/* 2>/dev/null"
    "; echo '@@CARRIER@@'; grep -H . /sys/class/net/*/carrier 2>/dev/null"
    "; echo '@@PHY@@'; ls -d /sys/class/net/*/phy80211 2>/dev/null"
    # Gateway path: which bridge port has the default gateway's MAC behind it.
    # busybox `ip neigh show <addr>` IGNORES the address filter and dumps the
    # whole table, so the address match has to be done here in awk.
    "; echo '@@GW@@'"
    "; gwip=$(ip route show default 2>/dev/null"
    " | sed -n 's/^default via \\([^ ]*\\).*/\\1/p' | head -1)"
    "; gwmac=$(ip neigh show 2>/dev/null"
    " | awk -v g=\"$gwip\" '$1==g {for(i=1;i<=NF;i++) if($i==\"lladdr\") print $(i+1)}'"
    " | head -1)"
    "; echo \"$gwip $gwmac\""
    "; echo '@@PORTNO@@'; grep -H . /sys/class/net/*/brif/*/port_no 2>/dev/null"
    # Filter the FDB to the gateway MAC on the router: a busy bridge holds ~45
    # entries and we only ever care about one of them.
    "; echo '@@FDB@@'"
    "; [ -n \"$gwmac\" ] && for b in /sys/class/net/*/bridge; do"
    " bn=${b%/bridge}; bn=${bn##*/};"
    " brctl showmacs \"$bn\" 2>/dev/null | grep -i \"$gwmac\" | sed \"s/^/$bn /\";"
    " done"
)


def _split_sections(text: str) -> dict:
    """Split the combined probe stdout on its @@MARKER@@ lines."""
    sections = {"NEIGH": []}
    current = "NEIGH"
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("@@") and stripped.endswith("@@"):
            current = stripped.strip("@")
            sections[current] = []
            continue
        sections.setdefault(current, []).append(line)
    return {k: "\n".join(v) for k, v in sections.items()}


async def poll_once(conn: asyncssh.SSHClientConnection, router_ip: str,
                    fetch_system: bool = False, fetch_topology: bool = False):
    hostname = router_ip
    try:
        r = await asyncio.wait_for(
            conn.run("uci get system.@system[0].hostname", check=False),
            timeout=COMMAND_TIMEOUT,
        )
        if r.exit_status == 0 and r.stdout:
            hostname = r.stdout.strip()
    except (asyncio.TimeoutError, asyncssh.Error):
        pass

    devices_resp = await ubus_call(conn, "iwinfo", "devices")
    devices = (devices_resp or {}).get("devices", [])

    interfaces = []
    total_clients = 0
    # Per-band split, for the client sparkline. Band boundaries match
    # the SQL convention used everywhere else (<3000 = 2.4G, >=5000 = 5G); an
    # interface in neither band counts toward the total only.
    clients_24 = clients_5 = 0
    # Per-SSID counts for the fleet graph (§16), summed over this router's
    # bands: one SSID normally exists as both a 2.4G and a 5G interface, and
    # the graph tracks the network, not the radio. Seeded at 0 for every SSID
    # present so a configured-but-empty SSID records a real zero.
    ssid_counts: dict[str, int] = {}
    for dev in devices:
        info = await ubus_call(conn, "iwinfo", "info", {"device": dev}) or {}
        assoc = await ubus_call(conn, "iwinfo", "assoclist", {"device": dev}) or {}
        clients = [parse_client(c) for c in assoc.get("results", [])]
        interfaces.append({"device": dev, "info": info, "clients": clients})
        total_clients += len(clients)
        ssid = info.get("ssid")
        if ssid:
            ssid_counts[ssid] = ssid_counts.get(ssid, 0) + len(clients)
        freq = info.get("frequency")
        if not freq:
            pass  # interface down / frequency unknown: total only
        elif freq < 3000:
            clients_24 += len(clients)
        elif freq >= 5000:
            clients_5 += len(clients)

    system_info = await fetch_system_info(conn) if fetch_system else None
    wifi_config = await fetch_wifi_config(conn)

    save_snapshot(router_ip, hostname, interfaces, wifi_config)

    # One exec covering two jobs:
    #  - arp_entries supplement from the router's own kernel ARP table. The
    #    AP sees ARP from every associated client before pfSense does, so
    #    this catches static-IP devices and ARP-expired pfSense entries.
    #    INSERT OR IGNORE means pfSense data always takes precedence.
    #  - /proc/net/dev byte counters for the uplink bandwidth graphs, plus
    #    the bridge/carrier/wireless topology when a refresh is due.
    counters = {}
    topology = None
    try:
        cmd = NETDEV_CMD + (TOPO_SUFFIX if fetch_topology else "")
        probe = await asyncio.wait_for(
            conn.run(cmd, check=False), timeout=COMMAND_TIMEOUT
        )
        # Deliberately not gated on exit_status: this is a chain, so the
        # status is only the last command's. A router with no bridge makes
        # the topology `ls` fail, which must not discard the ARP table and
        # counters that already came back fine.
        if probe.stdout:
            sections = _split_sections(probe.stdout)
            if sections.get("NEIGH"):
                save_router_arp(router_ip, parse_router_arp(sections["NEIGH"]))
            if sections.get("NETDEV"):
                counters = parse_netdev(sections["NETDEV"])
            if fetch_topology and "BRIF" in sections:
                gw_ip, gw_mac = parse_gateway(sections.get("GW", ""))
                topology = {
                    "wired_up": parse_topology(
                        sections.get("BRIF", ""),
                        sections.get("CARRIER", ""),
                        sections.get("PHY", ""),
                    ),
                    "gw_ip": gw_ip,
                    "gw_ports": gateway_ports(
                        parse_fdb(sections.get("FDB", "")),
                        parse_port_numbers(sections.get("PORTNO", "")),
                        gw_mac,
                    ),
                }
    except (asyncio.TimeoutError, asyncssh.Error):
        pass

    return (hostname, interfaces, system_info, len(devices), total_clients,
            clients_24, clients_5, ssid_counts, counters, topology)


async def run_router(router_ip: str, config: dict, shutdown: asyncio.Event):
    ssh_key = config.get("ssh_key", "")
    ssh_user = config.get("ssh_user", "root")
    poll_interval = config.get("poll_interval", 10)
    history_interval = config.get("history_interval", HISTORY_INTERVAL_DEFAULT)
    if not ssh_key:
        logger.error(f"{router_ip}: no ssh_key configured, skipping")
        return

    backoff = BACKOFF_INITIAL
    conn: Optional[asyncssh.SSHClientConnection] = None
    # Force a history write on the very first successful poll.
    last_history_mono = -float("inf")

    # ---- bandwidth + client-count accumulator (task-local: one per router) ----
    bw_bucket = max(30, int(config.get("bandwidth_interval",
                                       BANDWIDTH_BUCKET_DEFAULT)))
    bw_window = max(1, int(config.get("bandwidth_window_hours",
                                      BANDWIDTH_WINDOW_HOURS_DEFAULT)))
    uplink_group: Optional[str] = None
    uplink_ifaces: list[str] = []
    uplink_src = "none"
    prev_counters: Optional[dict] = None
    prev_mono = 0.0
    bucket_start: Optional[int] = None
    acc_rx = acc_tx = 0
    acc_span = 0.0
    # Client count is a gauge, so the bucket carries its mean over the polls
    # that landed in it rather than a single instantaneous reading. The two
    # per-band sums ride the same divisor (acc_cli_n).
    acc_cli_sum = acc_cli_n = 0
    acc_cli24_sum = acc_cli5_sum = 0
    # Per-SSID sums, on the same divisor again. Keyed by SSID rather than by
    # interface: one SSID is normally two interfaces (2.4G + 5G) and the
    # fleet graph tracks the network, not the radio.
    acc_ssid_sum: dict[str, int] = {}
    # Force a topology probe on the first poll after every (re)connect.
    last_topo_mono = -float("inf")

    while not shutdown.is_set():
        if conn is None:
            # A reconnect may mean the AP rebooted; never delta across the gap.
            prev_counters = None
            last_topo_mono = -float("inf")
            try:
                conn = await asyncssh.connect(
                    router_ip,
                    username=ssh_user,
                    client_keys=[ssh_key],
                    known_hosts=None,
                    keepalive_interval=30,
                    keepalive_count_max=3,
                    connect_timeout=CONNECT_TIMEOUT,
                )
                logger.info(f"{router_ip}: connected")
                backoff = BACKOFF_INITIAL
            except (OSError, asyncssh.Error, asyncio.TimeoutError) as e:
                logger.warning(f"{router_ip}: connect failed ({e}); retry in {backoff}s")
                mark_offline(router_ip)
                try:
                    await asyncio.wait_for(shutdown.wait(), timeout=backoff)
                    return
                except asyncio.TimeoutError:
                    pass
                backoff = min(backoff * 2, BACKOFF_MAX)
                continue

        try:
            now_mono = time.monotonic()
            write_history = (now_mono - last_history_mono) >= history_interval
            refresh_topo = (now_mono - last_topo_mono) >= TOPO_REFRESH_SECONDS
            (host, interfaces, system_info, n_dev, n_cli, n_cli24, n_cli5,
             ssid_counts, counters, topology) = await poll_once(
                conn, router_ip,
                fetch_system=write_history, fetch_topology=refresh_topo,
            )
            logger.info(f"{router_ip} ({host}): {n_cli} clients across {n_dev} ifaces")
            if write_history:
                save_history(router_ip, interfaces, system_info,
                             datetime.now().isoformat())
                last_history_mono = now_mono

            if topology is not None:
                last_topo_mono = now_mono
                # Re-read the tunables on the same slow cadence, so changing
                # them in config.json takes effect without a restart.
                cfg = load_config()
                bw_bucket = max(30, int(cfg.get("bandwidth_interval",
                                                BANDWIDTH_BUCKET_DEFAULT)))
                bw_window = max(1, int(cfg.get("bandwidth_window_hours",
                                               BANDWIDTH_WINDOW_HOURS_DEFAULT)))
                override = (cfg.get("bandwidth_uplink") or {}).get(router_ip)
                group, ifaces, src = pick_uplink(
                    topology["wired_up"], counters, override,
                    topology["gw_ports"],
                )
                if (group, src) != (uplink_group, uplink_src):
                    via = {
                        "gateway": f"via gateway {topology['gw_ip']}",
                        "override": "via config override",
                        "traffic": "via busiest-port fallback"
                                   " (gateway not on a wired port)",
                        "none": "no wired bridge port",
                    }[src]
                    logger.info(
                        f"{router_ip}: uplink={group or 'none'}"
                        f" ({'+'.join(ifaces) if ifaces else '-'}) {via}"
                    )
                uplink_group, uplink_ifaces, uplink_src = group, ifaces, src

            # Buckets are wall-clock aligned so every router shares
            # boundaries and /api/bandwidth can index them directly.
            # Rollover runs on every successful poll, not only when a byte
            # delta was usable: the client-count series has to keep advancing
            # on an AP whose uplink counters don't (no wired bridge port,
            # counter reset, first poll after a reconnect).
            bucket = int(time.time()) // bw_bucket * bw_bucket
            if bucket_start is None:
                bucket_start = bucket
            elif bucket != bucket_start:
                if acc_span > 0 or acc_cli_n:
                    save_bandwidth_bucket(
                        router_ip, bucket_start, acc_rx, acc_tx,
                        int(round(acc_span)),
                        round(acc_cli_sum / acc_cli_n) if acc_cli_n else None,
                        round(acc_cli24_sum / acc_cli_n) if acc_cli_n else None,
                        round(acc_cli5_sum / acc_cli_n) if acc_cli_n else None,
                        bw_window,
                    )
                if acc_cli_n:
                    save_ssid_bucket(
                        router_ip, bucket_start,
                        {ssid: round(total / acc_cli_n)
                         for ssid, total in acc_ssid_sum.items()},
                        bw_window,
                    )
                bucket_start = bucket
                acc_rx = acc_tx = 0
                acc_span = 0.0
                acc_cli_sum = acc_cli_n = 0
                acc_cli24_sum = acc_cli5_sum = 0
                acc_ssid_sum = {}
            acc_cli_sum += n_cli
            acc_cli24_sum += n_cli24
            acc_cli5_sum += n_cli5
            for _ssid, _n in ssid_counts.items():
                acc_ssid_sum[_ssid] = acc_ssid_sum.get(_ssid, 0) + _n
            acc_cli_n += 1

            if counters:
                if prev_counters is not None and uplink_ifaces:
                    elapsed = now_mono - prev_mono
                    d_rx, d_tx, usable = 0, 0, elapsed > 0
                    for name in uplink_ifaces:
                        prev = prev_counters.get(name)
                        curr = counters.get(name)
                        if prev is None or curr is None:
                            usable = False
                            break
                        drx = counter_delta(prev[0], curr[0], elapsed)
                        dtx = counter_delta(prev[1], curr[1], elapsed)
                        if drx is None or dtx is None:
                            usable = False  # wrap-uncorrectable: counter reset
                            break
                        d_rx += drx
                        d_tx += dtx
                    if usable:
                        save_live_bandwidth(
                            router_ip, uplink_group, uplink_src,
                            int(d_rx * 8 / elapsed), int(d_tx * 8 / elapsed),
                        )
                        # Credited to whichever bucket was open when the
                        # sample was taken (rolled over just above).
                        acc_rx += d_rx
                        acc_tx += d_tx
                        acc_span += elapsed
                prev_counters = counters
                prev_mono = now_mono
        except (asyncssh.Error, OSError, asyncio.TimeoutError) as e:
            logger.warning(f"{router_ip}: poll failed ({e}); dropping connection")
            try:
                conn.close()
            except Exception:
                pass
            conn = None
            mark_offline(router_ip)
            continue
        except Exception as e:
            logger.error(f"{router_ip}: unexpected error: {e}", exc_info=True)

        try:
            await asyncio.wait_for(shutdown.wait(), timeout=poll_interval)
            break
        except asyncio.TimeoutError:
            pass

    if conn is not None:
        conn.close()
    logger.info(f"{router_ip}: stopped")


def save_arp_entries(entries: list[dict]):
    """Upsert one ARP snapshot from pfSense. MAC is the PK (lowercased)."""
    now = datetime.now().isoformat()
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.cursor()
        for e in entries:
            mac = (e.get("mac_address") or "").lower()
            if not mac:
                continue
            hostname = e.get("hostname") or ""
            # pfSense returns '?' when there is no known hostname.
            if hostname == "?":
                hostname = ""
            cur.execute("""
                INSERT INTO arp_entries
                    (mac, ip, hostname, interface, last_seen, expires_seconds)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(mac) DO UPDATE SET
                    ip = excluded.ip,
                    hostname = excluded.hostname,
                    interface = excluded.interface,
                    last_seen = excluded.last_seen,
                    expires_seconds = excluded.expires_seconds
            """, (
                mac,
                e.get("ip_address", "") or "",
                hostname,
                e.get("interface", "") or "",
                now,
                pfsense.parse_expires_seconds(e.get("expires", "")),
            ))
        conn.commit()
    finally:
        conn.close()


def save_router_arp(router_ip: str, arp_map: dict):
    """Insert router ARP entries into arp_entries using INSERT OR IGNORE.

    Router ARP acts as a secondary source: it only creates rows that
    pfSense hasn't populated. When pfSense's arp_loop runs it does a
    full ON CONFLICT DO UPDATE, overwriting these rows with the richer
    pfSense data (verified IP + hostname). This layering means:
      - static-IP / pfSense-invisible clients  → router fills the gap
      - DHCP clients pfSense knows             → pfSense wins
    """
    if not arp_map:
        return
    now = datetime.now().isoformat()
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.cursor()
        for mac, ip in arp_map.items():
            cur.execute(
                "INSERT OR IGNORE INTO arp_entries "
                "(mac, ip, hostname, interface, last_seen) "
                "VALUES (?, ?, '', ?, ?)",
                (mac, ip, router_ip, now),
            )
        conn.commit()
    finally:
        conn.close()


async def arp_loop(shutdown: asyncio.Event):
    """Pull pfSense ARP table on a fixed interval. Re-reads config each
    cycle so a UI change to pfsense_url / pfsense_api_key takes effect
    on the next loop iteration without restarting."""
    _warned_unconfigured = False
    while not shutdown.is_set():
        cfg = load_config()
        url = cfg.get("pfsense_url", "")
        key = cfg.get("pfsense_api_key", "")
        interval = int(cfg.get("pfsense_arp_interval", PFSENSE_ARP_INTERVAL_DEFAULT))
        try:
            if url and key:
                _warned_unconfigured = False
                entries = await pfsense.fetch_arp_table(url, key)
                if entries:
                    save_arp_entries(entries)
                    logger.info(f"pfSense ARP: refreshed {len(entries)} entries")
                else:
                    logger.warning(
                        "pfSense ARP: fetch returned no entries — verify pfsense_url, "
                        "pfsense_api_key, and that the pfSense REST API package is installed"
                    )
            else:
                interval = max(interval, 300)
                if not _warned_unconfigured:
                    logger.warning(
                        "pfSense ARP enrichment not configured — "
                        "set pfsense_url and pfsense_api_key via Settings or config.json; "
                        "client IP addresses will not be populated until then"
                    )
                    _warned_unconfigured = True
        except Exception as e:
            logger.error(f"arp_loop error: {e}", exc_info=True)
        try:
            await asyncio.wait_for(shutdown.wait(), timeout=interval)
            break
        except asyncio.TimeoutError:
            pass


def save_voucher_sessions(sessions: list[dict]):
    """Full-replace the voucher_sessions table with the current active set.

    Captive-portal sessions are a live snapshot (a device is either on an
    active session or it isn't), so we DELETE + re-INSERT each cycle. This
    is self-cleaning — expired/logged-out sessions vanish automatically —
    and keeps the table tiny (one row per connected client), which matters
    for the storage budget. MAC is lowercased to match the /api/clients
    JOIN (lower(clients.mac) = voucher_sessions.mac).
    """
    now = datetime.now().isoformat()
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM voucher_sessions")
        for s in sessions:
            mac = (s.get("mac") or "").lower()
            if not mac:
                continue

            def _int(v):
                try:
                    return int(v)
                except (TypeError, ValueError):
                    return None

            cur.execute("""
                INSERT INTO voucher_sessions
                    (mac, ip, voucher_code, authmethod, allow_time, session_timeout, last_activity, last_seen)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(mac) DO UPDATE SET
                    ip = excluded.ip,
                    voucher_code = excluded.voucher_code,
                    authmethod = excluded.authmethod,
                    allow_time = excluded.allow_time,
                    session_timeout = excluded.session_timeout,
                    last_activity = excluded.last_activity,
                    last_seen = excluded.last_seen
            """, (
                mac,
                s.get("ip", "") or "",
                s.get("username", "") or "",
                s.get("authmethod", "") or "",
                _int(s.get("allow_time")),
                _int(s.get("session_timeout")),
                _int(s.get("last_activity")),
                now,
            ))
        conn.commit()
    finally:
        conn.close()


async def voucher_loop(shutdown: asyncio.Event):
    """Pull pfSense captive-portal voucher sessions on a fixed interval.

    Re-reads config each cycle (like arp_loop) so pfsense_url/pfsense_api_key
    changes take effect without a restart. Uses the REST command_prompt
    endpoint — the REST API has no captive-portal/voucher endpoint — so the
    API key must hold the Command Prompt privilege for this to return data.
    """
    _warned_unconfigured = False
    while not shutdown.is_set():
        cfg = load_config()
        url = cfg.get("pfsense_url", "")
        key = cfg.get("pfsense_api_key", "")
        interval = int(cfg.get("pfsense_voucher_interval", PFSENSE_VOUCHER_INTERVAL_DEFAULT))
        try:
            if url and key:
                _warned_unconfigured = False
                sessions = await pfsense.fetch_voucher_sessions(url, key)
                save_voucher_sessions(sessions)
                logger.info(f"pfSense voucher sessions: {len(sessions)} active")
            else:
                interval = max(interval, 300)
                if not _warned_unconfigured:
                    logger.warning(
                        "pfSense voucher enrichment not configured — "
                        "set pfsense_url and pfsense_api_key via Settings or config.json"
                    )
                    _warned_unconfigured = True
        except Exception as e:
            logger.error(f"voucher_loop error: {e}", exc_info=True)
        try:
            await asyncio.wait_for(shutdown.wait(), timeout=interval)
            break
        except asyncio.TimeoutError:
            pass


def save_trusted_macs(macs: list[dict]):
    """Full-replace the trusted_macs table with the pass-through MAC list.

    Like voucher_sessions, this is a snapshot mirror of pfSense state, so
    DELETE + re-INSERT keeps it in sync (removals disappear automatically).
    MAC lowercased to match the /api/clients JOIN.
    """
    now = datetime.now().isoformat()
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM trusted_macs")
        for m in macs:
            mac = (m.get("mac") or "").lower()
            if not mac:
                continue
            cur.execute("""
                INSERT INTO trusted_macs (mac, descr, last_seen)
                VALUES (?, ?, ?)
                ON CONFLICT(mac) DO UPDATE SET
                    descr = excluded.descr,
                    last_seen = excluded.last_seen
            """, (mac, m.get("descr", "") or "", now))
        conn.commit()
    finally:
        conn.close()


async def trusted_loop(shutdown: asyncio.Event):
    """Pull the pfSense captive-portal allowed/pass-through MAC list on a slow
    interval. Same command_prompt transport and config-reread pattern as
    voucher_loop; the list changes rarely so the default interval is long."""
    _warned_unconfigured = False
    while not shutdown.is_set():
        cfg = load_config()
        url = cfg.get("pfsense_url", "")
        key = cfg.get("pfsense_api_key", "")
        interval = int(cfg.get("pfsense_trusted_interval", PFSENSE_TRUSTED_INTERVAL_DEFAULT))
        try:
            if url and key:
                _warned_unconfigured = False
                macs = await pfsense.fetch_trusted_macs(url, key)
                save_trusted_macs(macs)
                logger.info(f"pfSense trusted MACs: {len(macs)} allowed")
            else:
                interval = max(interval, 300)
                if not _warned_unconfigured:
                    logger.warning(
                        "pfSense trusted-MAC enrichment not configured — "
                        "set pfsense_url and pfsense_api_key via Settings or config.json"
                    )
                    _warned_unconfigured = True
        except Exception as e:
            logger.error(f"trusted_loop error: {e}", exc_info=True)
        try:
            await asyncio.wait_for(shutdown.wait(), timeout=interval)
            break
        except asyncio.TimeoutError:
            pass


async def backup_loop(shutdown: asyncio.Event):
    """Daily SQLite snapshot + prune. Re-reads config each cycle so
    `backup_keep_days` changes take effect on the next run."""
    while not shutdown.is_set():
        try:
            cfg = load_config()
            keep = int(cfg.get("backup_keep_days", backup.DEFAULT_KEEP_DAYS))
            await asyncio.to_thread(backup.run_backup)
            await asyncio.to_thread(backup.prune_backups, keep)
        except Exception as e:
            logger.error(f"backup loop: {e}", exc_info=True)
        try:
            await asyncio.wait_for(
                shutdown.wait(), timeout=backup.BACKUP_INTERVAL_SECONDS
            )
            break
        except asyncio.TimeoutError:
            pass


async def retention_loop(shutdown: asyncio.Event):
    """Daily history cleanup. Re-reads config each cycle for live changes."""
    while not shutdown.is_set():
        try:
            days = retention.get_retention_days()
            # SQLite work goes off-thread so the event loop isn't blocked
            # during the VACUUM.
            await asyncio.to_thread(retention.run_cleanup, days)
        except Exception as e:
            logger.error(f"retention loop error: {e}", exc_info=True)
        try:
            await asyncio.wait_for(
                shutdown.wait(), timeout=retention.CLEANUP_INTERVAL_SECONDS
            )
            break
        except asyncio.TimeoutError:
            pass


async def main():
    init_db()
    retention.init_status_table()
    config = load_config()
    routers = config.get("routers", [])
    if not routers:
        logger.error("No routers configured")
        return

    logger.info(f"Starting async collector for {len(routers)} routers")

    shutdown = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, shutdown.set)

    tasks = [
        asyncio.create_task(run_router(ip, config, shutdown), name=f"poll-{ip}")
        for ip in routers
    ]
    tasks.append(asyncio.create_task(retention_loop(shutdown), name="retention"))
    tasks.append(asyncio.create_task(arp_loop(shutdown), name="pfsense-arp"))
    tasks.append(asyncio.create_task(voucher_loop(shutdown), name="pfsense-voucher"))
    tasks.append(asyncio.create_task(trusted_loop(shutdown), name="pfsense-trusted"))
    tasks.append(asyncio.create_task(backup_loop(shutdown), name="backup"))
    await shutdown.wait()
    logger.info("Shutdown signaled; cancelling tasks")
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    logger.info("Collector stopped")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
