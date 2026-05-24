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
from datetime import datetime
from pathlib import Path
from typing import Optional

import asyncssh


CONFIG_PATH = Path("/etc/openwrt-monitor/config.json")
DB_PATH = Path("/var/lib/openwrt-monitor/monitor.db")
LOG_PATH = Path("/var/log/openwrt-collector.log")

CONNECT_TIMEOUT = 5
COMMAND_TIMEOUT = 10
BACKOFF_INITIAL = 2
BACKOFF_MAX = 60


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


def save_snapshot(router_ip: str, hostname: str, interfaces: list[dict]):
    """Persist one poll's results."""
    now = datetime.now().isoformat()
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO routers (ip, hostname, online, last_seen, first_seen)
            VALUES (?, ?, 1, ?, ?)
            ON CONFLICT(ip) DO UPDATE SET
                hostname=excluded.hostname,
                online=1,
                last_seen=excluded.last_seen
        """, (router_ip, hostname, now, now))

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
        cur.execute("""
            INSERT INTO routers (ip, hostname, online, last_seen, first_seen)
            VALUES (?, ?, 0, ?, ?)
            ON CONFLICT(ip) DO UPDATE SET online=0
        """, (router_ip, router_ip, now, now))
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


async def poll_once(conn: asyncssh.SSHClientConnection, router_ip: str):
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

    save_snapshot(router_ip, hostname, interfaces)
    return hostname, len(devices), total_clients


async def run_router(router_ip: str, config: dict, shutdown: asyncio.Event):
    ssh_key = config.get("ssh_key", "")
    ssh_user = config.get("ssh_user", "root")
    poll_interval = config.get("poll_interval", 10)
    if not ssh_key:
        logger.error(f"{router_ip}: no ssh_key configured, skipping")
        return

    backoff = BACKOFF_INITIAL
    conn: Optional[asyncssh.SSHClientConnection] = None

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
            host, n_dev, n_cli = await poll_once(conn, router_ip)
            logger.info(f"{router_ip} ({host}): {n_cli} clients across {n_dev} ifaces")
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


async def main():
    init_db()
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
