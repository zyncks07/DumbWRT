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
    cursor.execute('SELECT r.*, COUNT(DISTINCT i.interface) as num_interfaces, SUM(i.num_clients) as total_clients FROM routers r LEFT JOIN interfaces i ON r.ip = i.router_ip GROUP BY r.ip')
    routers = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return jsonify({'success': True, 'routers': routers})

@app.route('/api/router/<router_ip>')
@login_required
def api_router_detail(router_ip):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM interfaces WHERE router_ip = ? ORDER BY interface', (router_ip,))
    interfaces = [dict(row) for row in cursor.fetchall()]
    for iface in interfaces:
        cursor.execute('SELECT * FROM clients WHERE router_ip = ? AND interface = ? ORDER BY signal DESC', (router_ip, iface['interface']))
        iface['clients'] = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return jsonify({'success': True, 'router_ip': router_ip, 'interfaces': interfaces})

@app.route('/api/clients')
@login_required
def api_clients():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT c.*, i.ssid, i.frequency FROM clients c LEFT JOIN interfaces i ON c.router_ip = i.router_ip AND c.interface = i.interface ORDER BY c.signal DESC')
    clients = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return jsonify({'success': True, 'clients': clients, 'total': len(clients)})

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
            
            config = {
                'ssh_key': data.get('ssh_key', ''),
                'ssh_user': data.get('ssh_user', 'root'),
                'poll_interval': int(data.get('poll_interval', 10)),
                'ping_interval': int(data.get('ping_interval', 10)),
                'routers': data.get('routers', [])
            }
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
        
        # Get last 100 lines from collector service
        result = subprocess.run(
            ['journalctl', '-u', 'openwrt-collector', '-n', '100', '--no-pager', '-o', 'short-iso'],
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
                devices_json = json.loads(result.stdout)
                devices = devices_json.get('devices', [])
                
                for device in devices:
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

if __name__ == '__main__':
    print("Starting OpenWrt Monitor Dashboard...")
    print(f"Template folder: {app.template_folder}")
    print(f"Templates exist: {os.path.exists(app.template_folder)}")
    if os.path.exists(app.template_folder):
        print(f"Template files: {os.listdir(app.template_folder)}")
    app.run(host='0.0.0.0', port=5000, debug=False)
