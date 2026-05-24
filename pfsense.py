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


async def fetch_arp_table(base_url: str, api_key: str) -> list[dict]:
    """GET /api/v2/diagnostics/arp_table; returns the `data` list or []."""
    if not base_url or not api_key:
        return []
    url = base_url.rstrip("/") + "/api/v2/diagnostics/arp_table"
    body = await asyncio.to_thread(_fetch_sync, url, api_key)
    if not body or body.get("code") != 200:
        return []
    return body.get("data") or []


def parse_expires_seconds(s: str) -> Optional[int]:
    """Convert pfSense's 'Expires in N seconds' string to int. None for
    static/permanent entries or unparseable values."""
    if not s:
        return None
    m = _EXPIRES_RE.search(s)
    return int(m.group(1)) if m else None
