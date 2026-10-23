#!/usr/bin/env python3
"""OpenWrt Monitor - Flask Web Application"""

from flask import Flask, render_template, jsonify, request, redirect, url_for, session
import sqlite3
import json
import os
import secrets
import time
import logging
import sys
from datetime import datetime, timedelta
from functools import wraps
from collections import defaultdict
import threading

import auth
import backup
import retention

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__,
            template_folder='/home/bulik/apps/openwrt-monitor/templates',
            static_folder='/home/bulik/apps/openwrt-monitor/static')

app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=8)
app.config['SESSION_COOKIE_SAMESITE']    = 'Lax'
app.config['SESSION_COOKIE_HTTPONLY']    = True

try:
    app.secret_key = auth.get_secret_key()
except (FileNotFoundError, KeyError):
    logger.error(
        "Auth not configured. Run: sudo python3 "
        "/home/bulik/apps/openwrt-monitor/scripts/init_auth.py"
    )
    sys.exit(1)

DB_PATH = '/var/lib/openwrt-monitor/monitor.db'
CONFIG_PATH = '/etc/openwrt-monitor/config.json'

# --- Login security ---------------------------------------------------------
_login_attempts: dict = defaultdict(list)
_login_lock = threading.Lock()
LOGIN_MAX    = 5
LOGIN_WINDOW = 300  # seconds (5-minute window)


def _get_client_ip() -> str:
    """Return normalized IPv4 string; strips ::ffff: prefix from dual-stack sockets."""
    ip = request.remote_addr or ''
    if ip.startswith('::ffff:'):
        ip = ip[7:]
    return ip


def login_required(f):
    """Decorator to require login"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'logged_in' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function


@app.before_request
def check_ip_whitelist():
    """Block requests from IPs not in the allowed list. /static/ is always exempt."""
    if request.path.startswith('/static/'):
        return
    allowed = load_config().get('allowed_ips', [])
    if not allowed:
        return  # empty list = allow-all (failsafe — never locks the admin out)
    if _get_client_ip() not in allowed:
        return render_template('403.html', client_ip=_get_client_ip()), 403

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def load_config():
    try:
        with open(CONFIG_PATH, 'r') as f:
            return json.load(f)
    except:
        return {'routers': [], 'ssh_key': '', 'poll_interval': 10}

def save_config(config):
    # Write-then-rename so a concurrent load_config() never sees a truncated
    # file — it would fall back to the defaults above (ssh_key '') and the
    # next save would persist that.
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    tmp = CONFIG_PATH + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(config, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, CONFIG_PATH)

@app.route('/')
def index():
    """Redirect to login or dashboard"""
    if 'logged_in' in session:
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))

@app.route('/login')
def login():
    """Login page"""
    if 'logged_in' in session:
        return redirect(url_for('dashboard'))
    return render_template('login.html')

@app.route('/api/login', methods=['POST'])
def api_login():
    """Handle login with per-IP rate limiting."""
    data = request.json or {}
    username = data.get('username', '')
    password = data.get('password', '')
    ip = _get_client_ip()
    now = time.time()

    with _login_lock:
        _login_attempts[ip] = [t for t in _login_attempts[ip] if now - t < LOGIN_WINDOW]
        if len(_login_attempts[ip]) >= LOGIN_MAX:
            retry_after = int(LOGIN_WINDOW - (now - _login_attempts[ip][0]))
            logger.warning("Rate-limited login from %s (%d attempts in %ds window)", ip, LOGIN_MAX, LOGIN_WINDOW)
            return jsonify({'success': False, 'error': 'too_many_attempts',
                            'retry_after': retry_after}), 429
        _login_attempts[ip].append(now)

    if auth.verify(username, password):
        with _login_lock:
            _login_attempts.pop(ip, None)
        logger.info("Successful login for '%s' from %s", username, ip)
        session['logged_in'] = True
        session.permanent = True
        return jsonify({'success': True})
    else:
        logger.warning("Failed login attempt for user '%s' from %s", username, ip)
        return jsonify({'success': False, 'error': 'Invalid credentials'}), 401

@app.route('/logout')
def logout():
    """Logout"""
    session.pop('logged_in', None)
    return redirect(url_for('login'))


@app.route('/api/client-ip')
@login_required
def api_client_ip():
    """Return the caller's normalized IP — used by Settings UI to show current IP."""
    return jsonify({'success': True, 'ip': _get_client_ip()})

@app.route('/dashboard')
@login_required
def dashboard():
    """Main dashboard (protected)"""
    return render_template('dashboard.html')

@app.route('/config')
@login_required
def config_page():
    """Configuration page (protected)"""
    return render_template('config.html')

@app.route('/api/routers')
@login_required
def api_routers():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT
            r.*,
            COUNT(DISTINCT i.interface) AS num_interfaces,
            COALESCE(SUM(i.num_clients), 0) AS total_clients,
            COALESCE(SUM(CASE WHEN i.frequency < 3000 THEN i.num_clients ELSE 0 END), 0) AS clients_24,
            COALESCE(SUM(CASE WHEN i.frequency >= 5000 THEN i.num_clients ELSE 0 END), 0) AS clients_5,
            sm.uptime, sm.load1, sm.load5, sm.load15,
            sm.mem_total, sm.mem_used,
            MIN(CASE WHEN i.frequency < 3000 THEN i.channel END) AS ch_24,
            MIN(CASE WHEN i.frequency < 3000 THEN i.noise   END) AS noise_24,
            MAX(CASE WHEN i.frequency < 3000 THEN i.bitrate END) AS bitrate_24,
            MIN(CASE WHEN i.frequency >= 5000 THEN i.channel END) AS ch_5,
            MIN(CASE WHEN i.frequency >= 5000 THEN i.noise   END) AS noise_5,
            MAX(CASE WHEN i.frequency >= 5000 THEN i.bitrate END) AS bitrate_5
        FROM routers r
        LEFT JOIN interfaces i ON r.ip = i.router_ip
        LEFT JOIN (
            SELECT router_ip, uptime, load1, load5, load15, mem_total, mem_used
            FROM system_metrics
            WHERE id IN (SELECT MAX(id) FROM system_metrics GROUP BY router_ip)
        ) sm ON r.ip = sm.router_ip
        GROUP BY r.ip
        ORDER BY r.ip
    """)
    routers = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return jsonify({'success': True, 'routers': routers})

# Client IP / device name, resolved from pfSense ARP (alias a) and DHCP leases
# (alias d) — both queries that list clients must join both. IP: ARP is the
# live truth; a lease fills in when ARP has nothing, but only an active or
# static one (an expired lease's IP may since have gone to another device).
# Name: the admin's static-mapping label, then ARP's (DNS) hostname, then the
# hostname the device sent in its DHCP request. Kept as arp_ip/arp_hostname
# so the templates read them unchanged. The DHCP hostname is device-chosen
# text — the templates must escape it.
CLIENT_IP_SQL = ("COALESCE(NULLIF(a.ip, ''), "
                 "CASE WHEN d.status IN ('active', 'static') THEN NULLIF(d.ip, '') END)")
CLIENT_NAME_SQL = ("COALESCE(NULLIF(d.descr, ''), NULLIF(a.hostname, ''), "
                   "NULLIF(d.hostname, ''))")


@app.route('/api/router/<router_ip>')
@login_required
def api_router_detail(router_ip):
    import re

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM interfaces WHERE router_ip = ? ORDER BY interface', (router_ip,))
    interfaces = [dict(row) for row in cursor.fetchall()]
    for iface in interfaces:
        cursor.execute(f"""
            SELECT c.*, {CLIENT_IP_SQL} AS arp_ip, {CLIENT_NAME_SQL} AS arp_hostname,
                   v.voucher_code AS voucher_code,
                   v.authmethod   AS voucher_authmethod,
                   v.allow_time   AS voucher_start,
                   v.last_activity AS voucher_last_activity,
                   (v.allow_time + v.session_timeout) AS voucher_expiry,
                   t.descr AS trusted_descr,
                   (t.mac IS NOT NULL) AS trusted
            FROM clients c
            LEFT JOIN arp_entries a ON lower(c.mac) = a.mac
            LEFT JOIN dhcp_leases d ON lower(c.mac) = d.mac
            LEFT JOIN voucher_sessions v ON lower(c.mac) = v.mac
            LEFT JOIN trusted_macs t ON lower(c.mac) = t.mac
            WHERE c.router_ip = ? AND c.interface = ?
            ORDER BY c.signal DESC
        """, (router_ip, iface['interface']))
        iface['clients'] = [dict(row) for row in cursor.fetchall()]

    cursor.execute("""
        SELECT section, ssid, radio, disabled
        FROM wifi_iface_config
        WHERE router_ip = ?
        ORDER BY radio, disabled, ssid
    """, (router_ip,))
    configured = [dict(row) for row in cursor.fetchall()]
    conn.close()

    # Merge: one entry per configured SSID, joined to its live iwinfo
    # data when the SSID is currently broadcasting. Match by (radio, ssid):
    # interface 'phy0-ap*' belongs to 'radio0', 'phy1-ap*' to 'radio1', etc.
    phy_re = re.compile(r"phy(\d+)")
    active_by_key = {}
    for iface in interfaces:
        m = phy_re.match(iface.get('interface') or '')
        if not m:
            continue
        key = ('radio' + m.group(1), iface.get('ssid') or '')
        active_by_key[key] = iface

    ssids = []
    used = set()
    for c in configured:
        key = (c.get('radio') or '', c.get('ssid') or '')
        match = active_by_key.get(key)
        entry = {
            'section': c['section'],
            'ssid': c['ssid'],
            'radio': c['radio'],
            'disabled': bool(c['disabled']),
            'active': bool(match) and not bool(c['disabled']),
        }
        if match:
            used.add(id(match))
            entry.update({
                'interface': match.get('interface'),
                'frequency': match.get('frequency'),
                'channel': match.get('channel'),
                'bandwidth': match.get('bandwidth'),
                'mode': match.get('mode'),
                'encryption': match.get('encryption'),
                'num_clients': match.get('num_clients', 0),
                'clients': match.get('clients', []),
                'noise': match.get('noise'),
                'bitrate': match.get('bitrate'),
                'txpower': match.get('txpower'),
            })
        ssids.append(entry)

    # Anything broadcasting that wasn't in UCI (shouldn't normally happen
    # but guard against it). Tagged active without a section.
    for iface in interfaces:
        if id(iface) in used:
            continue
        ssids.append({
            'section': None,
            'ssid': iface.get('ssid'),
            'radio': None,
            'disabled': False,
            'active': True,
            'interface': iface.get('interface'),
            'frequency': iface.get('frequency'),
            'channel': iface.get('channel'),
            'bandwidth': iface.get('bandwidth'),
            'mode': iface.get('mode'),
            'encryption': iface.get('encryption'),
            'num_clients': iface.get('num_clients', 0),
            'clients': iface.get('clients', []),
            'noise': iface.get('noise'),
            'bitrate': iface.get('bitrate'),
            'txpower': iface.get('txpower'),
        })

    return jsonify({
        'success': True,
        'router_ip': router_ip,
        'interfaces': interfaces,
        'ssids': ssids,
    })

@app.route('/api/clients')
@login_required
def api_clients():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(f"""
        SELECT c.*,
               i.ssid, i.frequency, i.channel, i.bandwidth,
               r.hostname AS router_hostname,
               {CLIENT_IP_SQL}   AS arp_ip,
               {CLIENT_NAME_SQL} AS arp_hostname,
               v.voucher_code AS voucher_code,
               v.authmethod   AS voucher_authmethod,
               v.allow_time   AS voucher_start,
               v.last_activity AS voucher_last_activity,
               (v.allow_time + v.session_timeout) AS voucher_expiry,
               t.descr AS trusted_descr,
               (t.mac IS NOT NULL) AS trusted
        FROM clients c
        LEFT JOIN interfaces i
          ON c.router_ip = i.router_ip AND c.interface = i.interface
        LEFT JOIN routers r
          ON c.router_ip = r.ip
        LEFT JOIN arp_entries a
          ON lower(c.mac) = a.mac
        LEFT JOIN dhcp_leases d
          ON lower(c.mac) = d.mac
        LEFT JOIN voucher_sessions v
          ON lower(c.mac) = v.mac
        LEFT JOIN trusted_macs t
          ON lower(c.mac) = t.mac
        ORDER BY c.signal DESC
    """)
    clients = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return jsonify({'success': True, 'clients': clients, 'total': len(clients)})


@app.route('/clients')
@login_required
def clients_page():
    return render_template('clients.html')


@app.route('/api/reachability')
@login_required
def api_reachability():
    """Per-router online/offline timeline for the last 24h.

    Returns offsets in seconds from window_start (compact for the wire).
    Each segment: [start_offset, end_offset, online_flag].
    """
    from datetime import timedelta

    now = datetime.now()
    window_start = now - timedelta(hours=24)
    window_seconds = int((now - window_start).total_seconds())

    conn = get_db()
    cur = conn.cursor()

    cur.execute("SELECT ip, online FROM routers")
    routers_rows = cur.fetchall()

    out = {}
    for row in routers_rows:
        ip = row['ip']
        cur.execute(
            """
            SELECT ts, online FROM router_status_history
             WHERE router_ip = ? AND ts >= ?
             ORDER BY ts ASC
            """,
            (ip, window_start.isoformat()),
        )
        transitions = cur.fetchall()

        segments = []
        if not transitions:
            # No state change in the window — assume current state held throughout.
            segments.append([0, window_seconds, int(row['online'])])
        else:
            # State BEFORE the first transition was the opposite of what the
            # transition set. This is the safe inference without a second query.
            first_state = 1 - int(transitions[0]['online'])
            try:
                first_offset = int(
                    (datetime.fromisoformat(transitions[0]['ts']) - window_start).total_seconds()
                )
            except (TypeError, ValueError):
                first_offset = 0
            if first_offset > 0:
                segments.append([0, first_offset, first_state])

            n = len(transitions)
            for i in range(n):
                try:
                    t_off = int(
                        (datetime.fromisoformat(transitions[i]['ts']) - window_start).total_seconds()
                    )
                except (TypeError, ValueError):
                    continue
                if i + 1 < n:
                    try:
                        next_off = int(
                            (datetime.fromisoformat(transitions[i + 1]['ts']) - window_start).total_seconds()
                        )
                    except (TypeError, ValueError):
                        next_off = window_seconds
                else:
                    next_off = window_seconds
                if next_off > t_off:
                    segments.append([t_off, next_off, int(transitions[i]['online'])])

        out[ip] = segments

    conn.close()
    return jsonify({
        'success': True,
        'window_start': window_start.isoformat(),
        'window_seconds': window_seconds,
        'routers': out,
    })

BANDWIDTH_BUCKET_DEFAULT = 360
BANDWIDTH_WINDOW_HOURS_DEFAULT = 24
# Overlaid series stay readable up to a handful; past that the technique
# needs a different form (§15 open territory). The fleet runs three SSIDs.
SSID_SERIES_MAX = 4
# The fleet SSID graph is a full-width card, so it resolves 3x finer than the
# per-router sparkline columns. Its own key, not bandwidth_interval — see the
# collector's SSID_BUCKET_DEFAULT.
SSID_BUCKET_DEFAULT = 120
# The SSID card's range picker (§17). The fleet series is kept for a month;
# a range is drawn at about one point per SSID_PX_PER_POINT pixels of plot,
# on the smallest step from SSID_STEPS that fits — so 24 h on a 1830 px
# plot is the full 120s resolution (720 points) and 30 days is hourly (720).
# Steps are whole minutes/hours so buckets fall on local clock boundaries,
# and every step >= SSID_ROLLUP_SECONDS is a multiple of it so those ranges
# can read the 10-minute tier instead of 5x as many 120s rows.
SSID_KEEP_DAYS_DEFAULT = 31
SSID_ROLLUP_SECONDS = 600          # must match the collector's constant
SSID_STEPS = (120, 180, 240, 300, 360, 480, 600, 1200, 1800, 2400, 3600,
              7200, 10800, 21600)
SSID_PX_PER_POINT = 2.4
SSID_MIN_SPAN = 3600


@app.route('/api/bandwidth')
@login_required
def api_bandwidth():
    """Per-router uplink throughput and client count for the last
    `bandwidth_window_hours`.

    Returns five fixed-length arrays per router (`in`/`out` in bit/s,
    `clients` as a count plus its `clients_24`/`clients_5` band split),
    indexed by bucket so the client can render a sparkline without any
    timestamp maths. `null` means no sample for that bucket (router offline)
    and renders as a gap rather than a zero. The bandwidth and client series
    gap independently: a bucket with no usable byte delta still carries its
    client count. The band split gaps independently again — rows written
    before those columns existed have the total but no split, and the
    dashboard draws the bare total for exactly those buckets.
    """
    config = load_config()
    interval = max(30, int(config.get('bandwidth_interval',
                                      BANDWIDTH_BUCKET_DEFAULT)))
    hours = max(1, int(config.get('bandwidth_window_hours',
                                  BANDWIDTH_WINDOW_HOURS_DEFAULT)))
    buckets = max(1, (hours * 3600) // interval)

    # Align t0 to a bucket boundary — the collector aligns its buckets to
    # wall-clock too, so indices line up exactly across all routers.
    now_bucket = int(time.time()) // interval * interval
    t0 = now_bucket - (buckets - 1) * interval

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT ip, bw_uplink, bw_uplink_src FROM routers ORDER BY ip")
    out = {
        row['ip']: {
            'uplink': row['bw_uplink'],
            # How the uplink was chosen (gateway | override | traffic | none),
            # surfaced so a mis-detection is visible from the dashboard.
            'src': row['bw_uplink_src'],
            'in': [None] * buckets,
            'out': [None] * buckets,
            'clients': [None] * buckets,
            'clients_24': [None] * buckets,
            'clients_5': [None] * buckets,
            'peak': 0,
            # y-scale for the client graph: the peak of what the dashboard
            # actually draws. The two bands are overlaid, not stacked, so
            # that is the larger of the two — plus the total on buckets
            # predating the split columns, which are drawn as a bare total
            # line. Scaling to a total that nothing draws would waste half
            # the height.
            'peak_clients': 0,
            # The real peak total, for the tooltip only.
            'peak_total': 0,
        }
        for row in cur.fetchall()
    }

    cur.execute(
        "SELECT router_ip, ts, rx_bytes, tx_bytes, span, clients, "
        "clients_24, clients_5 "
        "FROM bandwidth_history WHERE ts >= ? ORDER BY ts ASC",
        (t0,),
    )
    for row in cur.fetchall():
        entry = out.get(row['router_ip'])
        if entry is None:
            continue
        idx = (row['ts'] - t0) // interval
        if not (0 <= idx < buckets):
            continue
        span = row['span'] or 0
        if span > 0:
            in_bps = int(row['rx_bytes'] * 8 / span)
            out_bps = int(row['tx_bytes'] * 8 / span)
            entry['in'][idx] = in_bps
            entry['out'][idx] = out_bps
            entry['peak'] = max(entry['peak'], in_bps, out_bps)
        # NULL on rows written before the `clients` column existed, and on
        # buckets where no poll succeeded — both render as a gap.
        if row['clients'] is not None:
            entry['clients'][idx] = row['clients']
            entry['peak_total'] = max(entry['peak_total'], row['clients'])
        c24, c5 = row['clients_24'], row['clients_5']
        if c24 is not None and c5 is not None:
            entry['clients_24'][idx] = c24
            entry['clients_5'][idx] = c5
            entry['peak_clients'] = max(entry['peak_clients'], c24, c5)
        elif row['clients'] is not None:
            # No split for this bucket, so the total is what gets drawn.
            entry['peak_clients'] = max(entry['peak_clients'], row['clients'])
    conn.close()

    return jsonify({
        'success': True,
        'window_hours': hours,
        'interval': interval,
        'buckets': buckets,
        't0': t0,
        'routers': out,
    })


@app.route('/api/ssid-clients')
@login_required
def api_ssid_clients():
    """Fleet-wide associated-client count per SSID, for any range inside the
    retained month (§17).

    Query: either `span` (seconds, a live range ending now — the presets), or
    `start` + `end` (unix seconds — the calendar; an `end` in the future is
    clamped to now and the range is live). `px` is the plot width, which
    sets the point budget. No parameters = the last 24 h, as before §17.

    Returns one fixed-length array per SSID indexed by bucket, `null` for
    buckets with no row (renders as a gap, not a zero). Each value is the
    mean of the 120s fleet sums inside that bucket; when the step is coarser
    than the stored interval, `max` carries the busiest 120s value per bucket
    too. Zero-client SSIDs are stored as real zeros by the collector, so a gap
    means no router reported at all, not a quiet network.

    `refresh` is the number of seconds until the next fetch is worth making —
    computed here, on the server clock, so a live range is re-read 30 s after
    a bucket boundary (every router has flushed by then) rather than at an
    arbitrary browser-timed offset that could catch a half-summed bucket. It
    is null for a range that has ended: its data can no longer change.
    """
    config = load_config()
    interval = max(30, int(config.get('ssid_interval', SSID_BUCKET_DEFAULT)))
    keep_days = max(1, int(config.get('ssid_history_days',
                                      SSID_KEEP_DAYS_DEFAULT)))
    now = int(time.time())
    floor_ts = now - keep_days * 86400

    try:
        px = int(request.args.get('px', 1830))
        if 'start' in request.args or 'end' in request.args:
            start = int(request.args['start'])
            end = int(request.args.get('end', now))
        else:
            end = now
            start = now - int(request.args.get('span', 86400))
    except (KeyError, ValueError):
        return jsonify({'success': False, 'error': 'bad range'}), 400
    px = max(200, min(px, 4000))
    end = min(end, now)
    start = max(start, floor_ts)
    if end - start < SSID_MIN_SPAN:
        start = max(floor_ts, end - SSID_MIN_SPAN)
        end = start + SSID_MIN_SPAN
    span = end - start
    live = end >= now - interval

    # Smallest step that keeps the point count inside the pixel budget. Only
    # multiples of the stored interval qualify: anything else would bin a
    # varying number of rows per bucket and draw a beat pattern.
    target = max(60, int(px / SSID_PX_PER_POINT))
    steps = [interval] + [st for st in SSID_STEPS
                          if st > interval and st % interval == 0]
    step = next((st for st in steps if -(-span // st) <= target), steps[-1])
    use_rollup = step % SSID_ROLLUP_SECONDS == 0

    # Aligned to the step, not to `start`: the bucket grid then stays put as a
    # live window slides, instead of re-binning and shimmering every refresh.
    t0 = start // step * step
    buckets = max(1, -(-(end - t0) // step))
    t_end = t0 + buckets * step

    conn = get_db()
    cur = conn.cursor()
    if use_rollup:
        cur.execute(
            "SELECT (ts - ?) / ? AS i, ssid, SUM(sum) * 1.0 / SUM(n) AS v, "
            "MAX(max) AS mx FROM ssid_fleet_rollup "
            "WHERE ts >= ? AND ts < ? GROUP BY i, ssid",
            (t0, step, t0, t_end),
        )
    else:
        cur.execute(
            "SELECT (ts - ?) / ? AS i, ssid, AVG(clients) AS v, "
            "MAX(clients) AS mx FROM ssid_fleet_history "
            "WHERE ts >= ? AND ts < ? GROUP BY i, ssid",
            (t0, step, t0, t_end),
        )
    rows = cur.fetchall()
    oldest = cur.execute(
        "SELECT ts FROM ssid_fleet_history ORDER BY ts LIMIT 1").fetchone()
    conn.close()

    binned = step > interval
    series, peaks = {}, {}
    totals = [0.0] * buckets
    for row in rows:
        idx = row['i']
        if not (0 <= idx < buckets):
            continue
        name = row['ssid']
        pts = series.get(name)
        if pts is None:
            pts = series[name] = [None] * buckets
            peaks[name] = [None] * buckets
        v = round(row['v'], 1)
        pts[idx] = int(v) if v == int(v) else v
        peaks[name][idx] = row['mx']
        totals[idx] += v

    # Ranked by peak so the cap keeps the networks that matter; the dashboard
    # then sorts by name for a stable colour assignment (§16).
    ranked = sorted(
        series.items(),
        key=lambda kv: max((v for v in kv[1] if v is not None), default=0),
        reverse=True,
    )[:SSID_SERIES_MAX]
    # Peak of what is actually DRAWN: the series are overlaid, not stacked, so
    # that is the largest single SSID. `peak_total` is the real fleet total,
    # for the tooltip and the axis note only.
    peak = max((v for _, pts in ranked for v in pts if v is not None), default=0)

    refresh = None
    if live:
        period = max(interval, min(step, SSID_ROLLUP_SECONDS))
        refresh = (now // period + 1) * period + 30 - now

    return jsonify({
        'success': True,
        'interval': interval,
        'step': step,
        'tier': 'rollup' if use_rollup else 'full',
        'buckets': buckets,
        't0': t0,
        'start': start,
        'end': end,
        'live': live,
        'refresh': refresh,
        'keep_days': keep_days,
        'oldest': oldest['ts'] if oldest else None,
        'names': [name for name, _ in ranked],
        'series': dict(ranked),
        'max': {name: peaks[name] for name, _ in ranked} if binned else None,
        'peak': peak,
        'peak_total': round(max(totals), 1) if totals else 0,
    })


@app.route('/api/stats')
@login_required
def api_stats():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT COUNT(*) as total, SUM(online) as online FROM routers')
    routers_stats = dict(cursor.fetchone())
    cursor.execute('SELECT COUNT(*) as total FROM clients')
    clients_total = cursor.fetchone()['total']
    cursor.execute('SELECT COUNT(*) as total FROM interfaces')
    total_interfaces = cursor.fetchone()['total']
    cursor.execute("SELECT CASE WHEN i.frequency < 3000 THEN '2.4GHz' WHEN i.frequency >= 5000 THEN '5GHz' ELSE 'Other' END as band, COUNT(*) as count FROM clients c LEFT JOIN interfaces i ON c.router_ip = i.router_ip AND c.interface = i.interface GROUP BY band")
    clients_by_band = {row['band']: row['count'] for row in cursor.fetchall()}
    # Live per-SSID totals for the fleet SSID graph's legend (§16). The graph
    # itself is a 24 h history on the 6-minute bucket, so its last point is up
    # to a bucket old; the legend number comes from here instead, on the same
    # 10 s cadence as every other pill in the strip. Same shape as the
    # per-router rows: a live number beside a slower graph.
    cursor.execute(
        "SELECT i.ssid AS ssid, COUNT(*) AS count FROM clients c "
        "JOIN interfaces i ON c.router_ip = i.router_ip "
        "AND c.interface = i.interface "
        "WHERE i.ssid IS NOT NULL AND i.ssid != '' GROUP BY i.ssid"
    )
    clients_by_ssid = {row['ssid']: row['count'] for row in cursor.fetchall()}
    conn.close()
    return jsonify({'success': True, 'routers': routers_stats, 'clients_total': clients_total, 'total_interfaces': total_interfaces, 'clients_by_band': clients_by_band, 'clients_by_ssid': clients_by_ssid})

@app.route('/api/config', methods=['GET', 'POST'])
@login_required
def api_config():
    if request.method == 'GET':
        config = load_config()
        return jsonify({'success': True, 'config': config})
    else:
        try:
            data = request.json or {}
            config = load_config()

            # Merge into the existing config, and only for keys actually
            # present in the POST. Defaulting absent keys used to wipe state:
            # the Settings page's "Save IP Whitelist" button posted only
            # {allowed_ips}, which reset ssh_key to '' and routers to [] —
            # and the empty router list then deleted every router from the DB
            # below (2026-10-16).
            if data.get('ssh_key'):
                # The path field is read-only in the UI; an empty value is
                # never a deliberate "clear the key", so it never overwrites.
                config['ssh_key'] = data['ssh_key']
            if 'ssh_user' in data:
                config['ssh_user'] = data['ssh_user'] or 'root'
            if 'poll_interval' in data:
                config['poll_interval'] = int(data['poll_interval'] or 10)
            if 'ping_interval' in data:
                config['ping_interval'] = int(data['ping_interval'] or 10)
            removed_routers = set()
            if data.get('routers'):
                removed_routers = set(config.get('routers', [])) - set(data['routers'])
                config['routers'] = data['routers']
            for _k in ('pfsense_url', 'pfsense_api_key', 'allowed_ips'):
                if _k in data:
                    config[_k] = data[_k]
            save_config(config)
            
            # Clean up database for removed routers
            if removed_routers:
                conn = sqlite3.connect(DB_PATH)
                cursor = conn.cursor()
                for router_ip in removed_routers:
                    logger.info(f"Removing router {router_ip} from database")
                    cursor.execute('DELETE FROM routers WHERE ip = ?', (router_ip,))
                    cursor.execute('DELETE FROM interfaces WHERE router_ip = ?', (router_ip,))
                    cursor.execute('DELETE FROM clients WHERE router_ip = ?', (router_ip,))
                conn.commit()
                conn.close()
            
            password_change = data.get('password_change')
            if password_change:
                ok, msg = auth.change_password(
                    password_change.get('current'),
                    password_change.get('new'),
                )
                if not ok:
                    return jsonify({'success': False, 'error': msg}), 401
                logger.info("Admin password changed successfully")
                session.clear()
            
            # Restart both services to apply changes
            import subprocess
            subprocess.Popen(['systemctl', 'restart', 'openwrt-collector'], 
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(0.5)
            subprocess.Popen(['systemctl', 'restart', 'openwrt-dashboard'], 
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            
            return jsonify({'success': True})
        except Exception as e:
            logger.error(f"Error saving config: {e}")
            return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/ssh-public-key')
@login_required
def api_ssh_public_key():
    """Get the current SSH public key"""
    try:
        config = load_config()
        ssh_key_path = config.get('ssh_key', '')
        
        if not ssh_key_path:
            return jsonify({'success': False, 'error': 'No SSH key configured'})
        
        # Try to read the public key
        public_key_path = ssh_key_path + '.pub'
        
        try:
            with open(public_key_path, 'r') as f:
                public_key = f.read().strip()
            return jsonify({'success': True, 'public_key': public_key})
        except FileNotFoundError:
            return jsonify({'success': False, 'error': 'Public key file not found'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/generate-ssh-key', methods=['POST'])
@login_required
def api_generate_ssh_key():
    """Generate a new SSH key pair"""
    try:
        import subprocess
        import pwd
        import grp
        
        # Get current user from config or default to bulik
        ssh_user = os.environ.get('SUDO_USER', 'bulik')
        user_home = os.path.expanduser(f'~{ssh_user}')
        ssh_dir = os.path.join(user_home, '.ssh')
        
        # Create .ssh directory if it doesn't exist
        os.makedirs(ssh_dir, mode=0o700, exist_ok=True)
        
        # Generate key with timestamp
        timestamp = time.strftime('%Y%m%d_%H%M%S')
        key_name = f'id_ed25519_openwrt_{timestamp}'
        private_key_path = os.path.join(ssh_dir, key_name)
        public_key_path = private_key_path + '.pub'
        
        # Generate ED25519 key (modern, secure, no passphrase)
        subprocess.run([
            'ssh-keygen',
            '-t', 'ed25519',
            '-f', private_key_path,
            '-N', '',  # No passphrase
            '-C', f'openwrt-monitor-{timestamp}'
        ], check=True, capture_output=True)
        
        # Set proper ownership
        try:
            uid = pwd.getpwnam(ssh_user).pw_uid
            gid = grp.getgrnam(ssh_user).gr_gid
            os.chown(private_key_path, uid, gid)
            os.chown(public_key_path, uid, gid)
            os.chmod(private_key_path, 0o600)
            os.chmod(public_key_path, 0o644)
        except:
            pass  # If running as root or permission issues, continue anyway
        
        # Read the new public key
        with open(public_key_path, 'r') as f:
            public_key = f.read().strip()
        
        # Update config with new key path
        config = load_config()
        config['ssh_key'] = private_key_path
        save_config(config)
        
        return jsonify({
            'success': True,
            'public_key': public_key,
            'private_key_path': private_key_path,
            'message': 'New SSH key generated successfully'
        })
        
    except subprocess.CalledProcessError as e:
        return jsonify({'success': False, 'error': f'Failed to generate key: {e.stderr.decode()}'}), 500
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

SSH_KEY_MAX_BYTES = 16384  # a 4096-bit RSA key is ~3.3 KB; anything larger isn't a key


def _test_key_on_routers(key_path, routers, user):
    """Try a no-op SSH login with exactly this key on every router, in
    parallel. Mirrors the collector: identity = this key only, host keys not
    checked (collector uses known_hosts=None). Returns {ip: (ok, message)}."""
    import subprocess
    procs = {}
    for ip in routers:
        procs[ip] = subprocess.Popen(
            ['ssh', '-i', key_path,
             '-o', 'IdentitiesOnly=yes', '-o', 'BatchMode=yes',
             '-o', 'ConnectTimeout=5',
             '-o', 'StrictHostKeyChecking=no', '-o', 'UserKnownHostsFile=/dev/null',
             '-o', 'LogLevel=ERROR',
             f'{user}@{ip}', 'true'],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, text=True)
    results = {}
    deadline = time.time() + 10
    for ip, p in procs.items():
        try:
            _, err = p.communicate(timeout=max(0.1, deadline - time.time()))
            msg = (err or '').strip().splitlines()
            results[ip] = (p.returncode == 0, 'ok' if p.returncode == 0 else (msg[-1] if msg else f'exit {p.returncode}'))
        except subprocess.TimeoutExpired:
            p.kill()
            p.communicate()
            results[ip] = (False, 'timed out')
    return results


@app.route('/api/ssh-key/install', methods=['POST'])
@login_required
def api_ssh_key_install():
    """Install an existing private key (pasted or uploaded) as the fleet key.

    The key is validated with ssh-keygen, tested against every configured
    router, and only then made active. If no router accepts it the request is
    refused unless `force` is set, so a wrong paste can't take the whole fleet
    offline. Activating restarts the collector, which reads ssh_key at start.
    """
    import subprocess
    import tempfile
    import pwd
    import shutil

    data = request.json or {}
    text = data.get('private_key') or ''
    force = bool(data.get('force'))

    if len(text.encode()) > SSH_KEY_MAX_BYTES:
        return jsonify({'success': False, 'error': 'File is too large to be an SSH private key'}), 400
    # Windows editors / uploads bring CRLF, which OpenSSH-format keys reject.
    text = text.replace('\r\n', '\n').replace('\r', '\n').strip() + '\n'
    if 'PuTTY-User-Key-File' in text:
        return jsonify({'success': False, 'error':
            'This is a PuTTY .ppk key. Open it in PuTTYgen and use '
            'Conversions → Export OpenSSH key, then upload that file.'}), 400
    if 'PRIVATE KEY-----' not in text:
        return jsonify({'success': False, 'error':
            'Not a private key. Paste the private key file '
            '(begins with -----BEGIN ... PRIVATE KEY-----), not the .pub.'}), 400

    config = load_config()
    routers = config.get('routers', [])
    ssh_user = config.get('ssh_user', 'root')

    workdir = tempfile.mkdtemp(prefix='owm-key-')  # mode 0700
    try:
        tmp_key = os.path.join(workdir, 'key')
        fd = os.open(tmp_key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as f:
            f.write(text)

        # -P '' makes an encrypted key fail instead of prompting; the
        # collector has no way to supply a passphrase either.
        r = subprocess.run(['ssh-keygen', '-y', '-P', '', '-f', tmp_key],
                           capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            err = (r.stderr or '').strip()
            if 'passphrase' in err.lower() or 'incorrect' in err.lower():
                err = 'Key is passphrase-protected. Remove the passphrase first (ssh-keygen -p -f <key>).'
            return jsonify({'success': False, 'error': f'Invalid private key: {err or "unrecognised format"}'}), 400
        public_key = r.stdout.strip()
        key_type, key_blob = public_key.split()[:2]

        results = _test_key_on_routers(tmp_key, routers, ssh_user) if routers else {}
        accepted = sum(1 for ok, _ in results.values() if ok)
        report = [{'ip': ip, 'ok': ok, 'message': msg} for ip, (ok, msg) in results.items()]
        if routers and accepted == 0 and not force:
            return jsonify({'success': False, 'needs_confirm': True, 'results': report,
                            'error': 'No router accepted this key.'}), 409

        # Store next to the generated keys (same dir, owner and mode), so the
        # files are visible where the operator already looks for them. If this
        # exact key is already on disk, point at that file instead of copying.
        ssh_owner = os.environ.get('SUDO_USER', 'bulik')
        ssh_dir = os.path.join(os.path.expanduser(f'~{ssh_owner}'), '.ssh')
        os.makedirs(ssh_dir, mode=0o700, exist_ok=True)
        key_path = None
        for name in sorted(os.listdir(ssh_dir)):
            if not name.endswith('.pub'):
                continue
            try:
                with open(os.path.join(ssh_dir, name)) as f:
                    fields = f.read().split()
            except OSError:
                continue
            priv = os.path.join(ssh_dir, name[:-4])
            if fields[:2] == [key_type, key_blob] and os.path.isfile(priv):
                key_path = priv
                break
        reused = key_path is not None
        if not reused:
            short = {'ssh-ed25519': 'ed25519', 'ssh-rsa': 'rsa'}.get(key_type, 'ecdsa' if key_type.startswith('ecdsa') else 'key')
            key_path = os.path.join(ssh_dir, f"id_{short}_openwrt_{time.strftime('%Y%m%d_%H%M%S')}")
            shutil.move(tmp_key, key_path)
            os.chmod(key_path, 0o600)
            with open(key_path + '.pub', 'w') as f:
                f.write(f'{key_type} {key_blob} openwrt-monitor-uploaded\n')
            os.chmod(key_path + '.pub', 0o644)
            try:
                pw = pwd.getpwnam(ssh_owner)
                os.chown(key_path, pw.pw_uid, pw.pw_gid)
                os.chown(key_path + '.pub', pw.pw_uid, pw.pw_gid)
            except (KeyError, OSError):
                pass

        config = load_config()
        config['ssh_key'] = key_path
        save_config(config)
        logger.info("SSH key set to %s by %s (%d/%d routers accepted)",
                    key_path, _get_client_ip(), accepted, len(routers))
        subprocess.Popen(['systemctl', 'restart', 'openwrt-collector'],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        return jsonify({'success': True, 'private_key_path': key_path, 'public_key': public_key,
                        'reused_existing_file': reused, 'results': report,
                        'accepted': accepted, 'total': len(routers)})
    except Exception as e:
        logger.error(f"Error installing SSH key: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

@app.route('/logs')
@login_required
def logs_page():
    """Raw logs viewer page (protected)"""
    return render_template('logs.html')

@app.route('/api/raw-logs')
@login_required
def api_raw_logs():
    """Get raw collector logs and router responses"""
    try:
        import subprocess

        n_lines = str(load_config().get('raw_log_lines', retention.DEFAULT_RAW_LOG_LINES))
        result = subprocess.run(
            ['journalctl', '-u', 'openwrt-collector', '-n', n_lines, '--no-pager', '-o', 'short-iso'],
            capture_output=True,
            text=True,
            timeout=5
        )
        
        logs = []
        if result.returncode == 0:
            for line in result.stdout.strip().split('\n'):
                if line.strip():
                    logs.append(line)
        
        return jsonify({
            'success': True,
            'logs': logs,
            'timestamp': datetime.now().isoformat()
        })
    except Exception as e:
        logger.error(f"Error fetching logs: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/router-raw-data/<router_ip>')
@login_required
def api_router_raw_data(router_ip):
    """Get raw ubus data from a specific router"""
    try:
        config = load_config()
        ssh_key = config.get('ssh_key', '')
        ssh_user = config.get('ssh_user', 'root')
        
        if not ssh_key:
            return jsonify({'success': False, 'error': 'No SSH key configured'})
        
        import subprocess
        
        # Get iwinfo devices
        cmd_devices = [
            'ssh', '-i', ssh_key,
            '-o', 'ConnectTimeout=5',
            '-o', 'BatchMode=yes',
            f'{ssh_user}@{router_ip}',
            'ubus call iwinfo devices'
        ]
        
        result = subprocess.run(cmd_devices, capture_output=True, text=True, timeout=10)
        
        raw_data = {
            'router_ip': router_ip,
            'timestamp': datetime.now().isoformat(),
            'devices_raw': result.stdout if result.returncode == 0 else f"Error: {result.stderr}",
            'interfaces': []
        }
        
        # If we got devices, get info for each
        if result.returncode == 0:
            try:
                import re as _re
                devices_json = json.loads(result.stdout)
                devices = devices_json.get('devices', [])

                for device in devices:
                    # Device names come from the router but still flow into a
                    # remote shell command string below; whitelist them to
                    # remove the command-injection surface. Real names look
                    # like 'phy0-ap0' / 'wlan0'.
                    if not _re.fullmatch(r'[A-Za-z0-9._-]+', device or ''):
                        continue
                    # Get interface info
                    cmd_info = [
                        'ssh', '-i', ssh_key,
                        '-o', 'ConnectTimeout=5',
                        '-o', 'BatchMode=yes',
                        f'{ssh_user}@{router_ip}',
                        f"ubus call iwinfo info '{{\"device\":\"{device}\"}}'"
                    ]
                    
                    result_info = subprocess.run(cmd_info, capture_output=True, text=True, timeout=10)
                    
                    # Get clients
                    cmd_clients = [
                        'ssh', '-i', ssh_key,
                        '-o', 'ConnectTimeout=5',
                        '-o', 'BatchMode=yes',
                        f'{ssh_user}@{router_ip}',
                        f"ubus call iwinfo assoclist '{{\"device\":\"{device}\"}}'"
                    ]
                    
                    result_clients = subprocess.run(cmd_clients, capture_output=True, text=True, timeout=10)
                    
                    raw_data['interfaces'].append({
                        'device': device,
                        'info_raw': result_info.stdout if result_info.returncode == 0 else f"Error: {result_info.stderr}",
                        'clients_raw': result_clients.stdout if result_clients.returncode == 0 else f"Error: {result_clients.stderr}"
                    })
            except:
                pass
        
        return jsonify({'success': True, 'data': raw_data})
        
    except Exception as e:
        logger.error(f"Error fetching raw data from {router_ip}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/security')
@login_required
def security_page():
    return render_template('security.html')


@app.route('/api/security/whitelist', methods=['GET', 'POST'])
@login_required
def api_security_whitelist():
    config = load_config()
    if request.method == 'GET':
        return jsonify({'success': True, 'allowed_ips': config.get('allowed_ips', [])})
    data = request.json or {}
    ips = data.get('allowed_ips', config.get('allowed_ips', []))
    if not isinstance(ips, list):
        return jsonify({'success': False, 'error': 'allowed_ips must be a list'}), 400
    config['allowed_ips'] = list(dict.fromkeys(str(ip).strip() for ip in ips if str(ip).strip()))
    save_config(config)
    logger.info("IP whitelist updated by %s: %s", _get_client_ip(), config['allowed_ips'])
    return jsonify({'success': True})


@app.route('/api/security/login-events')
@login_required
def api_security_login_events():
    import subprocess, re as _re
    try:
        result = subprocess.run(
            ['journalctl', '-u', 'openwrt-dashboard', '-n', '500',
             '--no-pager', '-o', 'short-iso'],
            capture_output=True, text=True, timeout=5
        )
        events = []
        for line in result.stdout.split('\n'):
            if not line.strip():
                continue
            lower = line.lower()
            if 'failed login attempt' in lower:
                kind = 'failed'
                m = _re.search(r"for user '([^']+)' from (\S+)", line)
                user = m.group(1) if m else '?'
                src  = m.group(2) if m else '?'
            elif 'successful login' in lower:
                kind = 'success'
                m = _re.search(r"for '([^']+)' from (\S+)", line)
                user = m.group(1) if m else '?'
                src  = m.group(2) if m else '?'
            elif 'rate-limited login' in lower:
                kind = 'ratelimit'
                m = _re.search(r"login from (\S+)", line)
                user = '—'
                src  = m.group(1) if m else '?'
            else:
                continue
            ts = line.split(' ', 1)[0] if ' ' in line else ''
            events.append({'ts': ts, 'kind': kind, 'user': user, 'ip': src})
        return jsonify({'success': True, 'events': list(reversed(events))[:100]})
    except Exception as e:
        logger.error("Error fetching login events: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/maintenance')
@login_required
def maintenance_page():
    return render_template('maintenance.html')


@app.route('/api/maintenance', methods=['GET', 'POST'])
@login_required
def api_maintenance():
    config = load_config()
    _HISTORY_INTERVAL_DEFAULT = 300
    if request.method == 'GET':
        sizes = retention.get_history_size()
        sizes['backup_bytes'] = sum(b['bytes'] for b in backup.list_backups())
        return jsonify({
            'success': True,
            'config': {
                'history_retention_days': int(config.get(
                    'history_retention_days', retention.DEFAULT_RETENTION_DAYS)),
                'raw_log_lines': int(config.get(
                    'raw_log_lines', retention.DEFAULT_RAW_LOG_LINES)),
                'history_interval': int(config.get(
                    'history_interval', _HISTORY_INTERVAL_DEFAULT)),
            },
            'status': retention.get_status(),
            'sizes': sizes,
        })

    # POST: update only the maintenance keys; preserve everything else.
    data = request.json or {}
    try:
        days     = int(data.get('history_retention_days', config.get(
                       'history_retention_days', retention.DEFAULT_RETENTION_DAYS)))
        lines    = int(data.get('raw_log_lines', config.get(
                       'raw_log_lines', retention.DEFAULT_RAW_LOG_LINES)))
        bdays    = int(data.get('backup_keep_days', config.get(
                       'backup_keep_days', backup.DEFAULT_KEEP_DAYS)))
        interval = int(data.get('history_interval', config.get('history_interval', 300)))
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': 'all values must be integers'}), 400
    if days < 1 or days > 3650:
        return jsonify({'success': False, 'error': 'history_retention_days out of range (1–3650)'}), 400
    if lines < 10 or lines > 100000:
        return jsonify({'success': False, 'error': 'raw_log_lines out of range (10–100000)'}), 400
    if bdays < 1 or bdays > 3650:
        return jsonify({'success': False, 'error': 'backup_keep_days out of range (1–3650)'}), 400
    if interval < 30 or interval > 3600:
        return jsonify({'success': False, 'error': 'history_interval out of range (30–3600)'}), 400

    config['history_retention_days'] = days
    config['raw_log_lines'] = lines
    config['backup_keep_days'] = bdays
    config['history_interval'] = interval
    save_config(config)
    # No service restart needed — collector re-reads config each cycle,
    # Flask reads raw_log_lines per request.
    return jsonify({'success': True})


@app.route('/api/maintenance/run-now', methods=['POST'])
@login_required
def api_maintenance_run_now():
    days = int(load_config().get(
        'history_retention_days', retention.DEFAULT_RETENTION_DAYS))
    try:
        result = retention.run_cleanup(days)
    except Exception as e:
        logger.error(f"manual cleanup failed: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500
    return jsonify({'success': True, 'result': result})


@app.route('/api/maintenance/backups')
@login_required
def api_backups_list():
    return jsonify({
        'success': True,
        'backups': backup.list_backups(),
        'keep_days': int(load_config().get(
            'backup_keep_days', backup.DEFAULT_KEEP_DAYS)),
    })


@app.route('/api/maintenance/backup/run', methods=['POST'])
@login_required
def api_backup_run():
    result = backup.run_backup(force=True)  # user explicitly requested — always create
    if not result['ok']:
        return jsonify({'success': False, 'error': result['message']}), 500
    # Apply current retention policy after creating a new one.
    try:
        keep = int(load_config().get(
            'backup_keep_days', backup.DEFAULT_KEEP_DAYS))
        backup.prune_backups(keep)
    except Exception as e:
        logger.warning(f"post-backup prune failed: {e}")
    return jsonify({'success': True, 'result': result})


@app.route('/api/maintenance/backup/download/<path:name>')
@login_required
def api_backup_download(name):
    try:
        p = backup.safe_backup_path(name)
    except (ValueError, FileNotFoundError):
        return jsonify({'success': False, 'error': 'invalid backup name'}), 400
    from flask import send_file
    return send_file(
        str(p), as_attachment=True, download_name=name,
        mimetype='application/octet-stream',
    )


@app.route('/api/maintenance/backup/<path:name>', methods=['DELETE'])
@login_required
def api_backup_delete(name):
    try:
        p = backup.safe_backup_path(name)
    except (ValueError, FileNotFoundError):
        return jsonify({'success': False, 'error': 'invalid backup name'}), 400
    try:
        p.unlink()
    except OSError as e:
        return jsonify({'success': False, 'error': str(e)}), 500
    return jsonify({'success': True})


if __name__ == '__main__':
    print("Starting OpenWrt Monitor Dashboard...")
    print(f"Template folder: {app.template_folder}")
    print(f"Templates exist: {os.path.exists(app.template_folder)}")
    if os.path.exists(app.template_folder):
        print(f"Template files: {os.listdir(app.template_folder)}")
    app.run(host='0.0.0.0', port=5000, debug=False)
