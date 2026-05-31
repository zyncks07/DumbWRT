"""SQLite backup helpers — shared by the collector (scheduled) and Flask (on-demand).

Uses SQLite's online backup API so a snapshot is consistent even while
writers (collector, Flask) are active. Backups land in
/var/lib/openwrt-monitor/ alongside the live DB with a timestamped
suffix matching the existing manual-backup naming convention.
"""

import logging
import shutil
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path

DB_PATH = Path("/var/lib/openwrt-monitor/monitor.db")
BACKUP_DIR = DB_PATH.parent
BACKUP_PREFIX = "monitor.db.backup-"
DEFAULT_KEEP_DAYS = 4
BACKUP_INTERVAL_SECONDS = 86400  # daily

logger = logging.getLogger(__name__)


def _new_backup_path(now: datetime | None = None) -> Path:
    now = now or datetime.now()
    return BACKUP_DIR / f"{BACKUP_PREFIX}{now.strftime('%Y%m%d-%H%M%S')}"


def run_backup(force: bool = False) -> dict:
    """Take an online SQLite snapshot. Returns a status dict.

    When `force=False` (the default used by the automatic daily loop),
    skips if a backup was created within the last 23 hours — prevents
    multiple restarts in one day from accumulating duplicate backups.
    Pass `force=True` for user-initiated "Backup now" requests.
    """
    if not force:
        recent = list_backups()
        if recent:
            age_s = (datetime.now() - datetime.fromisoformat(recent[0]['mtime'])).total_seconds()
            if age_s < 82800:  # 23 h
                msg = f"Skipped — {recent[0]['name']} is only {int(age_s / 3600)}h old"
                logger.info(f"backup: {msg}")
                return {'ts': datetime.now().isoformat(), 'name': None,
                        'bytes': 0, 'duration_ms': 0, 'ok': True, 'message': msg}
    target = _new_backup_path()
    start = time.monotonic()
    error: str | None = None
    bytes_written = 0
    try:
        with sqlite3.connect(str(DB_PATH)) as src, sqlite3.connect(str(target)) as dst:
            src.backup(dst)
        bytes_written = target.stat().st_size
    except Exception as e:
        error = str(e)
        try:
            target.unlink(missing_ok=True)
        except Exception:
            pass
        logger.error(f"backup failed: {e}", exc_info=True)
    duration_ms = int((time.monotonic() - start) * 1000)
    if error is None:
        logger.info(f"backup: wrote {target.name} ({bytes_written} bytes, {duration_ms} ms)")
    return {
        "ts": datetime.now().isoformat(),
        "name": target.name if error is None else None,
        "bytes": bytes_written,
        "duration_ms": duration_ms,
        "ok": error is None,
        "message": error or f"Backup {target.name} ({bytes_written // 1024} KB in {duration_ms} ms)",
    }


def list_backups() -> list[dict]:
    out: list[dict] = []
    for p in sorted(BACKUP_DIR.glob(BACKUP_PREFIX + "*"), reverse=True):
        try:
            st = p.stat()
        except FileNotFoundError:
            continue
        out.append({
            "name": p.name,
            "bytes": st.st_size,
            "mtime": datetime.fromtimestamp(st.st_mtime).isoformat(),
        })
    return out


def prune_backups(keep_days: int) -> int:
    """Delete backups whose mtime is older than `keep_days` days.
    Returns the number removed."""
    cutoff = datetime.now() - timedelta(days=max(1, keep_days))
    removed = 0
    for p in BACKUP_DIR.glob(BACKUP_PREFIX + "*"):
        try:
            if datetime.fromtimestamp(p.stat().st_mtime) < cutoff:
                p.unlink()
                removed += 1
        except FileNotFoundError:
            pass
        except OSError as e:
            logger.warning(f"prune: could not remove {p}: {e}")
    if removed:
        logger.info(f"backup prune: removed {removed} file(s) older than {keep_days}d")
    return removed


def safe_backup_path(name: str) -> Path:
    """Resolve a user-provided filename to a real backup file.
    Refuses anything that doesn't look like a backup or attempts traversal."""
    if not name.startswith(BACKUP_PREFIX):
        raise ValueError("not a backup filename")
    if "/" in name or "\\" in name or ".." in name:
        raise ValueError("invalid filename")
    p = BACKUP_DIR / name
    if not p.is_file():
        raise FileNotFoundError(name)
    return p
