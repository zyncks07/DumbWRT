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
HISTORY_INTERVAL_DEFAULT = 60
# Seconds between pfSense ARP-table pulls. pfSense's ARP cache itself
# updates much faster than this, so 30s is a fine UX/load compromise.
PFSENSE_ARP_INTERVAL_DEFAULT = 30


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

    conn.commit()
    conn.close()


def parse_bandwidth(htmode: str) -> Optional[int]:
    if not htmode:
        return None
    if "HT20" in htmode or "NOHT" in htmode:
        return 20
    if "HT40" in htmode or "VHT40" in htmode:
        return 40
    if "VHT80" in htmode or "HE80" in htmode:
        return 80
    if "VHT160" in htmode or "HE160" in htmode:
        return 160
    return None


def parse_client(c: dict) -> dict:
    rx = c.get("rx") or {}
    tx = c.get("tx") or {}
    rx_rate = rx.get("rate", 0) or 0
    tx_rate = tx.get("rate", 0) or 0
    if rx_rate > 1000:
        rx_rate //= 1000
    if tx_rate > 1000:
        tx_rate //= 1000
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


def save_snapshot(router_ip: str, hostname: str, interfaces: list[dict]):
    """Persist one poll's results."""
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

        for iface in interfaces:
            info = iface["info"]
            clients = iface["clients"]
            cur.execute("""
                INSERT INTO interfaces
                    (router_ip, interface, ssid, bssid, frequency, channel,
                     bandwidth, mode, encryption, num_clients, last_updated)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(router_ip, interface) DO UPDATE SET
                    ssid=excluded.ssid, bssid=excluded.bssid,
                    frequency=excluded.frequency, channel=excluded.channel,
                    bandwidth=excluded.bandwidth, mode=excluded.mode,
                    encryption=excluded.encryption,
                    num_clients=excluded.num_clients,
                    last_updated=excluded.last_updated
            """, (
                router_ip, iface["device"],
                info.get("ssid", ""), info.get("bssid", ""),
                info.get("frequency"), info.get("channel"),
                parse_bandwidth(info.get("htmode", "")),
                info.get("mode", ""),
                (info.get("encryption") or {}).get("description", "Open"),
                len(clients), now,
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


async def poll_once(conn: asyncssh.SSHClientConnection, router_ip: str,
                    fetch_system: bool = False):
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
    for dev in devices:
        info = await ubus_call(conn, "iwinfo", "info", {"device": dev}) or {}
        assoc = await ubus_call(conn, "iwinfo", "assoclist", {"device": dev}) or {}
        clients = [parse_client(c) for c in assoc.get("results", [])]
        interfaces.append({"device": dev, "info": info, "clients": clients})
        total_clients += len(clients)

    system_info = await fetch_system_info(conn) if fetch_system else None

    save_snapshot(router_ip, hostname, interfaces)
    return hostname, interfaces, system_info, len(devices), total_clients


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

    while not shutdown.is_set():
        if conn is None:
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
            host, interfaces, system_info, n_dev, n_cli = await poll_once(
                conn, router_ip, fetch_system=write_history
            )
            logger.info(f"{router_ip} ({host}): {n_cli} clients across {n_dev} ifaces")
            if write_history:
                save_history(router_ip, interfaces, system_info,
                             datetime.now().isoformat())
                last_history_mono = now_mono
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


async def arp_loop(shutdown: asyncio.Event):
    """Pull pfSense ARP table on a fixed interval. Re-reads config each
    cycle so a UI change to pfsense_url / pfsense_api_key takes effect
    on the next loop iteration without restarting."""
    while not shutdown.is_set():
        cfg = load_config()
        url = cfg.get("pfsense_url", "")
        key = cfg.get("pfsense_api_key", "")
        interval = int(cfg.get("pfsense_arp_interval", PFSENSE_ARP_INTERVAL_DEFAULT))
        try:
            if url and key:
                entries = await pfsense.fetch_arp_table(url, key)
                if entries:
                    save_arp_entries(entries)
                    logger.info(f"pfSense ARP: refreshed {len(entries)} entries")
            else:
                # Not configured — sleep longer between checks.
                interval = max(interval, 300)
        except Exception as e:
            logger.error(f"arp_loop error: {e}", exc_info=True)
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
