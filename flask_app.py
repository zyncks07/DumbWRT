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
from datetime import datetime
from functools import wraps

import auth
import backup
import retention

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__,
            template_folder='/home/bulik/apps/openwrt-monitor/templates',
            static_folder='/home/bulik/apps/openwrt-monitor/static')

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

def login_required(f):
    """Decorator to require login"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'logged_in' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

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
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, 'w') as f:
        json.dump(config, f, indent=2)

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
    """Handle login"""
    data = request.json
    username = data.get('username')
    password = data.get('password')
    
    if auth.verify(username, password):
        session['logged_in'] = True
        session.permanent = True
        return jsonify({'success': True})
    else:
        return jsonify({'success': False, 'error': 'Invalid credentials'}), 401

@app.route('/logout')
def logout():
    """Logout"""
    session.pop('logged_in', None)
    return redirect(url_for('login'))

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

@app.route('/api/router/<router_ip>')
@login_required
def api_router_detail(router_ip):
    import re

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM interfaces WHERE router_ip = ? ORDER BY interface', (router_ip,))
    interfaces = [dict(row) for row in cursor.fetchall()]
    for iface in interfaces:
        cursor.execute("""
            SELECT c.*, a.ip AS arp_ip, a.hostname AS arp_hostname
            FROM clients c
            LEFT JOIN arp_entries a ON lower(c.mac) = a.mac
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
    cursor.execute("""
        SELECT c.*,
               i.ssid, i.frequency,
               r.hostname AS router_hostname,
               a.ip       AS arp_ip,
               a.hostname AS arp_hostname
        FROM clients c
        LEFT JOIN interfaces i
          ON c.router_ip = i.router_ip AND c.interface = i.interface
        LEFT JOIN routers r
          ON c.router_ip = r.ip
        LEFT JOIN arp_entries a
          ON lower(c.mac) = a.mac
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
    conn.close()
    return jsonify({'success': True, 'routers': routers_stats, 'clients_total': clients_total, 'total_interfaces': total_interfaces, 'clients_by_band': clients_by_band})

@app.route('/api/config', methods=['GET', 'POST'])
@login_required
def api_config():
    if request.method == 'GET':
        config = load_config()
        return jsonify({'success': True, 'config': config})
    else:
        try:
            data = request.json
            old_config = load_config()
            old_routers = set(old_config.get('routers', []))
            new_routers = set(data.get('routers', []))
            
            # Find routers that were removed
            removed_routers = old_routers - new_routers
            
            # Merge into the existing config; only touch the keys this form
            # owns. Rebuilding from scratch here used to wipe keys written by
            # other pages (history_retention_days, raw_log_lines,
            # backup_keep_days from Maintenance; pfsense_* / history_interval),
            # silently reverting retention/backup/pfSense settings on save.
            config = old_config
            config['ssh_key'] = data.get('ssh_key', '')
            config['ssh_user'] = data.get('ssh_user', 'root')
            config['poll_interval'] = int(data.get('poll_interval', 10))
            config['ping_interval'] = int(data.get('ping_interval', 10))
            config['routers'] = data.get('routers', [])
            # pfSense integration — only update when the form sends these keys
            # so that a POST from an older page version can't silently wipe them.
            for _k in ('pfsense_url', 'pfsense_api_key'):
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

@app.route('/maintenance')
@login_required
def maintenance_page():
    return render_template('maintenance.html')


@app.route('/api/maintenance', methods=['GET', 'POST'])
@login_required
def api_maintenance():
    config = load_config()
    if request.method == 'GET':
        return jsonify({
            'success': True,
            'config': {
                'history_retention_days': int(config.get(
                    'history_retention_days', retention.DEFAULT_RETENTION_DAYS)),
                'raw_log_lines': int(config.get(
                    'raw_log_lines', retention.DEFAULT_RAW_LOG_LINES)),
            },
            'status': retention.get_status(),
            'sizes': retention.get_history_size(),
        })

    # POST: update only the maintenance keys; preserve everything else.
    data = request.json or {}
    try:
        days = int(data.get('history_retention_days', config.get(
            'history_retention_days', retention.DEFAULT_RETENTION_DAYS)))
        lines = int(data.get('raw_log_lines', config.get(
            'raw_log_lines', retention.DEFAULT_RAW_LOG_LINES)))
        bdays = int(data.get('backup_keep_days', config.get(
            'backup_keep_days', backup.DEFAULT_KEEP_DAYS)))
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': 'days and lines must be integers'}), 400
    if days < 1 or days > 3650:
        return jsonify({'success': False, 'error': 'history_retention_days out of range (1–3650)'}), 400
    if lines < 10 or lines > 100000:
        return jsonify({'success': False, 'error': 'raw_log_lines out of range (10–100000)'}), 400
    if bdays < 1 or bdays > 3650:
        return jsonify({'success': False, 'error': 'backup_keep_days out of range (1–3650)'}), 400

    config['history_retention_days'] = days
    config['raw_log_lines'] = lines
    config['backup_keep_days'] = bdays
    save_config(config)
    # No service restart needed — collector re-reads the value each cycle,
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
    result = backup.run_backup()
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
