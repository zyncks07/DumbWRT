"""Retention / vacuum helpers — shared by the collector and Flask.

The collector schedules a daily run via `retention_loop`. The UI's
"Run Now" button hits a Flask endpoint that calls `run_cleanup`
directly. Both write status to a single-row table so the UI can
display last-run / rows-deleted / outcome.
"""

import json
import logging
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path

DB_PATH = Path("/var/lib/openwrt-monitor/monitor.db")
CONFIG_PATH = Path("/etc/openwrt-monitor/config.json")

HISTORY_TABLES = (
    "client_history",
    "interface_history",
    "router_status_history",
    "system_metrics",
)

DEFAULT_RETENTION_DAYS = 30
DEFAULT_RAW_LOG_LINES = 500
CLEANUP_INTERVAL_SECONDS = 86400  # daily

logger = logging.getLogger(__name__)


def init_status_table():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS retention_status (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            last_run DATETIME,
            duration_ms INTEGER,
            rows_deleted INTEGER,
            ok INTEGER,
            message TEXT
        )
    """)
    conn.commit()
    conn.close()


def _read_config() -> dict:
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def get_retention_days() -> int:
    return int(_read_config().get("history_retention_days", DEFAULT_RETENTION_DAYS))


def run_cleanup(retention_days: int) -> dict:
    """Delete history rows older than `retention_days`, then VACUUM.

    Returns a status dict and also writes it to `retention_status`.
    """
    cutoff = (datetime.now() - timedelta(days=retention_days)).isoformat()
    start = time.monotonic()
    rows_deleted = 0
    error = None

    try:
        conn = sqlite3.connect(DB_PATH)
        try:
            cur = conn.cursor()
            for tbl in HISTORY_TABLES:
                cur.execute(f"DELETE FROM {tbl} WHERE ts < ?", (cutoff,))
                rows_deleted += cur.rowcount
            conn.commit()
            # VACUUM cannot run inside a transaction.
            cur.execute("VACUUM")
        finally:
            conn.close()
    except Exception as e:
        error = str(e)
        logger.error(f"retention: cleanup failed: {e}", exc_info=True)

    duration_ms = int((time.monotonic() - start) * 1000)
    msg = (
        error
        if error
        else f"Removed {rows_deleted} rows older than {retention_days}d "
             f"(VACUUM in {duration_ms} ms)"
    )
    result = {
        "ts": datetime.now().isoformat(),
        "duration_ms": duration_ms,
        "rows_deleted": rows_deleted,
        "ok": error is None,
        "message": msg,
    }
    _update_status(result)
    if error is None:
        logger.info(f"retention: {msg}")
    return result


def _update_status(result: dict):
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO retention_status
                (id, last_run, duration_ms, rows_deleted, ok, message)
            VALUES (1, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                last_run = excluded.last_run,
                duration_ms = excluded.duration_ms,
                rows_deleted = excluded.rows_deleted,
                ok = excluded.ok,
                message = excluded.message
        """, (
            result["ts"], result["duration_ms"], result["rows_deleted"],
            1 if result["ok"] else 0, result["message"],
        ))
        conn.commit()
    finally:
        conn.close()


def get_status() -> dict:
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("SELECT * FROM retention_status WHERE id = 1")
        row = cur.fetchone()
    finally:
        conn.close()
    if row is None:
        return {
            "last_run": None,
            "duration_ms": None,
            "rows_deleted": None,
            "ok": None,
            "message": "Never run",
        }
    return dict(row)


def get_history_size() -> dict:
    """Row count per history table + total DB size on disk."""
    out = {}
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.cursor()
        for tbl in HISTORY_TABLES:
            cur.execute(f"SELECT COUNT(*) FROM {tbl}")
            out[tbl] = cur.fetchone()[0]
    finally:
        conn.close()
    try:
        out["db_bytes"] = DB_PATH.stat().st_size
    except FileNotFoundError:
        out["db_bytes"] = 0
    return out
