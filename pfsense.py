"""pfSense REST API client (read-only).

Currently used by the collector to pull the ARP table so the UI can
surface IP (and, when pfSense populates it, hostname) next to the
raw MAC for every wifi client.

Authentication is via X-API-Key header against the
pfSense-pkg-RESTAPI v2 package. SSL verification is disabled to
match the typical self-signed cert on an internal pfSense — same
as `curl -k`.
"""

import asyncio
import json
import logging
import re
import ssl
from typing import Optional
from urllib.error import URLError
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE

REQUEST_TIMEOUT = 10

_EXPIRES_RE = re.compile(r"(\d+)\s*seconds?", re.IGNORECASE)

# Static shell command run on pfSense (via the REST command_prompt endpoint)
# to dump every active captive-portal session. There is no REST endpoint for
# captive-portal vouchers, but the session rows live in the per-zone SQLite DBs
# at /var/db/captiveportal<zone>.db (table `captiveportal`). php is always
# present on pfSense; the `sqlite3` CLI is not, so we use php's SQLite3 class.
#
# `last_activity` is NOT a stored column — pfSense derives it live from the pf
# state table. We reuse pfSense's own captiveportal_get_last_activity($ip),
# which needs the global $cpzone set to the row's zone and the captive-portal
# library loaded; hence the require_once of config.inc + captiveportal.inc. The
# zone name is recovered from the DB filename (captiveportal<zone>.db). It falls
# back to null when no activity is recorded (the UI then shows the login time).
#
# Each active session is emitted as one JSON object per line. DBs are opened
# read-only so a live/locked DB is never disturbed; @ suppresses per-file query
# errors so one bad zone DB can't abort the rest. This command is fixed and
# app-controlled (no user input), so there is no shell-injection surface.
VOUCHER_CMD = (
    "php -r '"
    'require_once("/etc/inc/config.inc");'
    'require_once("/etc/inc/captiveportal.inc");'
    "global $cpzone;"
    'foreach(glob("/var/db/captiveportal*.db") as $f){'
    '$cpzone=substr(basename($f,".db"),strlen("captiveportal"));'
    "$d=new SQLite3($f,SQLITE3_OPEN_READONLY);"
    '$r=@$d->query("SELECT allow_time,ip,mac,username,session_timeout,authmethod FROM captiveportal");'
    "while($r && $row=$r->fetchArray(SQLITE3_ASSOC)){"
    '$la=captiveportal_get_last_activity($row["ip"]);'
    "echo json_encode(array("
    '"mac"=>$row["mac"],"ip"=>$row["ip"],"username"=>$row["username"],'
    '"allow_time"=>$row["allow_time"],"session_timeout"=>$row["session_timeout"],'
    '"authmethod"=>$row["authmethod"],"last_activity"=>($la?$la:null)'
    ")).\"\\n\";"
    "}"
    "$d->close();"
    "}'"
)


# Static shell command to dump the captive-portal "pass-through MAC" list
# (Allowed MACs) for every zone. These devices bypass the portal — no voucher
# needed — so the UI flags them as TRUSTED. The list lives in pfSense config at
# captiveportal/<zone>/passthrumac (array of {action, mac, descr}); reading it
# only needs config.inc, not the heavier captiveportal.inc. Emits one JSON
# object per allowed MAC. Fixed, app-controlled command (no injection surface).
TRUSTED_CMD = (
    "php -r '"
    'require_once("/etc/inc/config.inc");'
    '$z=config_get_path("captiveportal",array());'
    "foreach($z as $zn=>$c){"
    '$pm=isset($c["passthrumac"])?$c["passthrumac"]:array();'
    "foreach($pm as $e){"
    'if(empty($e["mac"])) continue;'
    "echo json_encode(array("
    '"mac"=>$e["mac"],'
    '"descr"=>(isset($e["descr"])?$e["descr"]:""),'
    '"action"=>(isset($e["action"])?$e["action"]:"")'
    ")).\"\\n\";"
    "}"
    "}'"
)


def _fetch_sync(url: str, api_key: str) -> Optional[dict]:
    req = Request(url, headers={"X-API-Key": api_key, "Accept": "application/json"})
    try:
        with urlopen(req, timeout=REQUEST_TIMEOUT, context=_SSL_CTX) as r:
            return json.loads(r.read())
    except URLError as e:
        logger.warning(f"pfSense fetch failed: {e}")
        return None
    except json.JSONDecodeError as e:
        logger.warning(f"pfSense returned non-JSON: {e}")
        return None


def _post_sync(url: str, api_key: str, payload: dict) -> Optional[dict]:
    body = json.dumps(payload).encode()
    req = Request(
        url,
        data=body,
        method="POST",
        headers={
            "X-API-Key": api_key,
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )
    try:
        with urlopen(req, timeout=REQUEST_TIMEOUT, context=_SSL_CTX) as r:
            return json.loads(r.read())
    except URLError as e:
        logger.warning(f"pfSense POST failed: {e}")
        return None
    except json.JSONDecodeError as e:
        logger.warning(f"pfSense returned non-JSON: {e}")
        return None


async def fetch_arp_table(base_url: str, api_key: str) -> list[dict]:
    """GET /api/v2/diagnostics/arp_table; returns the `data` list or []."""
    if not base_url or not api_key:
        return []
    url = base_url.rstrip("/") + "/api/v2/diagnostics/arp_table"
    body = await asyncio.to_thread(_fetch_sync, url, api_key)
    if not body or body.get("code") != 200:
        return []
    return body.get("data") or []


async def fetch_voucher_sessions(base_url: str, api_key: str) -> list[dict]:
    """Return active captive-portal sessions from pfSense.

    There is no REST endpoint for captive-portal / voucher data, so we run
    VOUCHER_CMD through POST /api/v2/diagnostics/command_prompt (the GUI's
    Diagnostics > Command Prompt) and parse its stdout. Requires the API
    key/user to hold the "Diagnostics: Command Prompt" privilege; without
    it the endpoint returns non-200 and we yield [].

    Each returned dict has: mac, ip, username (voucher code), allow_time
    (unix session start), session_timeout (seconds), authmethod.
    """
    if not base_url or not api_key:
        return []
    url = base_url.rstrip("/") + "/api/v2/diagnostics/command_prompt"
    body = await asyncio.to_thread(_post_sync, url, api_key, {"command": VOUCHER_CMD})
    if not body or body.get("code") != 200:
        return []
    output = (body.get("data") or {}).get("output") or ""
    sessions = []
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            sessions.append(json.loads(line))
        except json.JSONDecodeError:
            # Tolerate any stray non-JSON line (warnings, php notices).
            continue
    return sessions


async def fetch_trusted_macs(base_url: str, api_key: str) -> list[dict]:
    """Return the captive-portal pass-through (Allowed) MAC list.

    Same command_prompt transport as fetch_voucher_sessions. Each dict has
    mac, descr, action. Empty on any non-200 (e.g. missing Command Prompt
    privilege). Changes rarely (admin-managed), so the collector polls this
    on a slow interval.
    """
    if not base_url or not api_key:
        return []
    url = base_url.rstrip("/") + "/api/v2/diagnostics/command_prompt"
    body = await asyncio.to_thread(_post_sync, url, api_key, {"command": TRUSTED_CMD})
    if not body or body.get("code") != 200:
        return []
    output = (body.get("data") or {}).get("output") or ""
    macs = []
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            macs.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return macs


def parse_expires_seconds(s: str) -> Optional[int]:
    """Convert pfSense's 'Expires in N seconds' string to int. None for
    static/permanent entries or unparseable values."""
    if not s:
        return None
    m = _EXPIRES_RE.search(s)
    return int(m.group(1)) if m else None
