#!/usr/bin/env python3
"""
OpenWrt Ubus Collector - SSH-based Real-time Monitoring
Uses iwinfo via ubus - the exact same method LuCI uses for wireless client monitoring
"""

import subprocess
import json
import time
import threading
import sqlite3
from datetime import datetime
from typing import Dict, List, Optional
import logging
from pathlib import Path
import os
import signal
import sys

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('/var/log/openwrt-collector.log')
    ]
)
logger = logging.getLogger(__name__)

# SQLite adapter for datetime
def adapt_datetime(dt):
    return dt.isoformat()

sqlite3.register_adapter(datetime, adapt_datetime)


class UbusCollector:
    """Collects WiFi client data from OpenWrt routers via SSH/ubus - LuCI compatible"""
    
    def __init__(self):
        self.config_path = '/etc/openwrt-monitor/config.json'
        self.db_path = '/var/lib/openwrt-monitor/monitor.db'
        self.ssh_config = '/etc/openwrt-monitor/ssh_config'
        
        # Runtime state
        self.running = False
        self.poll_threads = {}
        self.ping_threads = {}
        
        # Load configuration
        self.config = self.load_config()
        
        # Initialize database
        self.init_database()
        
        # Register signal handlers for graceful shutdown
        signal.signal(signal.SIGTERM, self.signal_handler)
        signal.signal(signal.SIGINT, self.signal_handler)
        
        logger.info("UbusCollector initialized")
    
    def signal_handler(self, signum, frame):
        """Handle shutdown signals"""
        logger.info(f"Received signal {signum}, shutting down...")
        self.stop()
        sys.exit(0)
    
    def load_config(self) -> Dict:
        """Load configuration from JSON file"""
        try:
            with open(self.config_path, 'r') as f:
                config = json.load(f)
                logger.info(f"Loaded config: {len(config.get('routers', []))} routers")
                return config
        except FileNotFoundError:
            logger.error(f"Config file not found: {self.config_path}")
            return {
                'routers': [],
                'ssh_key': '',
                'ssh_user': 'root',
                'poll_interval': 10,
                'ping_interval': 10
            }
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON in config: {e}")
            return {'routers': [], 'ssh_key': '', 'ssh_user': 'root', 'poll_interval': 10, 'ping_interval': 10}
    
    def init_database(self):
        """Initialize SQLite database with required tables"""
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # Routers table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS routers (
                ip TEXT PRIMARY KEY,
                hostname TEXT,
                online INTEGER DEFAULT 0,
                last_seen DATETIME,
                first_seen DATETIME
            )
        ''')
        
        # Interfaces table (one per SSID/radio)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS interfaces (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                router_ip TEXT,
                interface TEXT,
                ssid TEXT,
                bssid TEXT,
                frequency INTEGER,
                channel INTEGER,
                bandwidth INTEGER,
                mode TEXT,
                encryption TEXT,
                num_clients INTEGER DEFAULT 0,
                last_updated DATETIME,
                UNIQUE(router_ip, interface)
            )
        ''')
        
        # Clients table (associated stations)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS clients (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                router_ip TEXT,
                interface TEXT,
                mac TEXT,
                signal INTEGER,
                signal_avg INTEGER,
                noise INTEGER,
                rx_rate INTEGER,
                tx_rate INTEGER,
                rx_packets INTEGER,
                tx_packets INTEGER,
                rx_bytes INTEGER,
                tx_bytes INTEGER,
                connected_time INTEGER,
                inactive INTEGER,
                authorized INTEGER,
                last_seen DATETIME,
                first_seen DATETIME,
                UNIQUE(router_ip, interface, mac)
            )
        ''')
        
        # Create indexes for performance
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_clients_router ON clients(router_ip)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_clients_mac ON clients(mac)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_interfaces_router ON interfaces(router_ip)')
        
        conn.commit()
        conn.close()
        logger.info("Database initialized")
    
    def ssh_command(self, router_ip: str, command: str) -> Optional[str]:
        """Execute SSH command on router using multiplexed connection"""
        ssh_key = self.config.get('ssh_key', '')
        ssh_user = self.config.get('ssh_user', 'root')
        
        if not ssh_key:
            logger.error("No SSH key configured")
            return None
        
        ssh_cmd = [
            'ssh',
            '-F', self.ssh_config,
            '-i', ssh_key,
            '-o', 'ConnectTimeout=5',
            '-o', 'BatchMode=yes',
            f'{ssh_user}@{router_ip}',
            command
        ]
        
        try:
            result = subprocess.run(
                ssh_cmd,
                capture_output=True,
                text=True,
                timeout=10
            )
            
            if result.returncode == 0:
                return result.stdout.strip()
            else:
                if result.stderr:
                    logger.debug(f"SSH error on {router_ip}: {result.stderr.strip()}")
                return None
                
        except subprocess.TimeoutExpired:
            logger.warning(f"SSH timeout on {router_ip}")
            return None
        except Exception as e:
            logger.error(f"SSH exception on {router_ip}: {e}")
            return None
    
    def ubus_call(self, router_ip: str, namespace: str, method: str, params: Optional[Dict] = None) -> Optional[Dict]:
        """Call ubus method via SSH - returns parsed JSON"""
        command = f"ubus call {namespace} {method}"
        if params:
            # Properly escape JSON for shell
            params_json = json.dumps(params).replace("'", "'\\''")
            command += f" '{params_json}'"
        
        result = self.ssh_command(router_ip, command)
        
        if result:
            try:
                return json.loads(result)
            except json.JSONDecodeError as e:
                logger.error(f"JSON decode failed on {router_ip} {namespace}.{method}: {e}")
                logger.debug(f"Raw output: {result[:200]}")
                return None
        return None
    
    def get_wireless_devices(self, router_ip: str) -> List[str]:
        """
        Get list of wireless interfaces from router - EXACTLY like LuCI does it
        Returns interface names like: ['phy0-ap0', 'phy0-ap1', 'phy1-ap0']
        """
        result = self.ubus_call(router_ip, 'iwinfo', 'devices')
        
        if result and 'devices' in result:
            devices = result['devices']
            logger.debug(f"{router_ip}: Found {len(devices)} wireless devices: {devices}")
            return devices
        
        logger.warning(f"{router_ip}: No wireless devices found")
        return []
    
    def get_interface_info(self, router_ip: str, device: str) -> Optional[Dict]:
        """
        Get interface information - EXACTLY like LuCI does it
        Returns SSID, channel, frequency, encryption, etc.
        """
        info = self.ubus_call(router_ip, 'iwinfo', 'info', {'device': device})
        
        if not info:
            return None
        
        # Parse bandwidth from htmode (e.g., "HT20", "VHT80", "HE160")
        htmode = info.get('htmode', '')
        bandwidth = None
        if 'HT20' in htmode or 'NOHT' in htmode:
            bandwidth = 20
        elif 'HT40' in htmode or 'VHT40' in htmode:
            bandwidth = 40
        elif 'VHT80' in htmode or 'HE80' in htmode:
            bandwidth = 80
        elif 'VHT160' in htmode or 'HE160' in htmode:
            bandwidth = 160
        
        return {
            'ssid': info.get('ssid', ''),
            'bssid': info.get('bssid', ''),
            'mode': info.get('mode', ''),
            'channel': info.get('channel'),
            'frequency': info.get('frequency'),
            'bandwidth': bandwidth,
            'encryption': info.get('encryption', {}).get('description', 'Open'),
            'txpower': info.get('txpower')
        }
    
    def get_associated_clients(self, router_ip: str, device: str) -> List[Dict]:
        """
        Get associated clients - EXACTLY like LuCI does it
        Returns detailed client information with signal, rates, etc.
        """
        result = self.ubus_call(router_ip, 'iwinfo', 'assoclist', {'device': device})
        
        if not result or 'results' not in result:
            return []
        
        clients = []
        for client in result['results']:
            mac = client.get('mac')
            if not mac:
                continue
            
            # Extract RX/TX rates (handle different formats)
            rx_info = client.get('rx', {})
            tx_info = client.get('tx', {})
            
            rx_rate = rx_info.get('rate', 0) if isinstance(rx_info, dict) else 0
            tx_rate = tx_info.get('rate', 0) if isinstance(tx_info, dict) else 0
            
            # Convert from kbps to Mbps if needed (rates are usually in kbps)
            if rx_rate and rx_rate > 1000:
                rx_rate = rx_rate // 1000
            if tx_rate and tx_rate > 1000:
                tx_rate = tx_rate // 1000
            
            clients.append({
                'mac': mac,
                'signal': client.get('signal', 0),
                'signal_avg': client.get('signal_avg', client.get('signal', 0)),
                'noise': client.get('noise', -95),
                'rx_rate': rx_rate,
                'tx_rate': tx_rate,
                'rx_packets': rx_info.get('packets', 0) if isinstance(rx_info, dict) else 0,
                'tx_packets': tx_info.get('packets', 0) if isinstance(tx_info, dict) else 0,
                'rx_bytes': rx_info.get('bytes', 0) if isinstance(rx_info, dict) else 0,
                'tx_bytes': tx_info.get('bytes', 0) if isinstance(tx_info, dict) else 0,
                'connected_time': client.get('connected_time', 0),
                'inactive': client.get('inactive', 0),
                'authorized': 1 if client.get('authorized', True) else 0
            })
        
        return clients
    
    def save_interface_data(self, router_ip: str, device: str, info: Dict, num_clients: int):
        """Save interface information to database"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('''
            INSERT OR REPLACE INTO interfaces
            (router_ip, interface, ssid, bssid, frequency, channel, bandwidth, mode, encryption, num_clients, last_updated)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            router_ip,
            device,
            info.get('ssid', ''),
            info.get('bssid', ''),
            info.get('frequency'),
            info.get('channel'),
            info.get('bandwidth'),
            info.get('mode', ''),
            info.get('encryption', 'Open'),
            num_clients,
            datetime.now()
        ))
        
        conn.commit()
        conn.close()
    
    def save_client_data(self, router_ip: str, device: str, clients: List[Dict]):
        """Save client data to database"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        now = datetime.now()
        
        # Get existing clients for this interface to preserve first_seen
        cursor.execute('''
            SELECT mac, first_seen FROM clients 
            WHERE router_ip = ? AND interface = ?
        ''', (router_ip, device))
        
        existing = {row[0]: row[1] for row in cursor.fetchall()}
        
        # Clear old clients for this interface
        cursor.execute('DELETE FROM clients WHERE router_ip = ? AND interface = ?', (router_ip, device))
        
        # Insert current clients
        for client in clients:
            mac = client['mac']
            first_seen = existing.get(mac, now)
            
            cursor.execute('''
                INSERT INTO clients
                (router_ip, interface, mac, signal, signal_avg, noise, rx_rate, tx_rate,
                 rx_packets, tx_packets, rx_bytes, tx_bytes, connected_time, inactive,
                 authorized, last_seen, first_seen)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                router_ip, device, mac,
                client['signal'], client['signal_avg'], client['noise'],
                client['rx_rate'], client['tx_rate'],
                client['rx_packets'], client['tx_packets'],
                client['rx_bytes'], client['tx_bytes'],
                client['connected_time'], client['inactive'],
                client['authorized'], now, first_seen
            ))
        
        conn.commit()
        conn.close()
    
    def poll_router(self, router_ip: str):
        """Poll a single router for wireless client data - main collection loop"""
        poll_interval = self.config.get('poll_interval', 10)
        
        logger.info(f"Started polling {router_ip} every {poll_interval}s")
        
        while self.running:
            try:
                # Get all wireless devices (interfaces)
                devices = self.get_wireless_devices(router_ip)
                
                if not devices:
                    logger.warning(f"{router_ip}: No wireless devices found, retrying...")
                    time.sleep(poll_interval)
                    continue
                
                total_clients = 0
                
                # Poll each wireless interface
                for device in devices:
                    # Get interface information
                    info = self.get_interface_info(router_ip, device)
                    if not info:
                        logger.warning(f"{router_ip}/{device}: Failed to get interface info")
                        continue
                    
                    # Get associated clients
                    clients = self.get_associated_clients(router_ip, device)
                    
                    # Save to database
                    self.save_interface_data(router_ip, device, info, len(clients))
                    self.save_client_data(router_ip, device, clients)
                    
                    total_clients += len(clients)
                    
                    if clients:
                        logger.info(f"{router_ip}/{device} ({info.get('ssid', 'N/A')}): {len(clients)} clients")
                
                logger.debug(f"{router_ip}: Total {total_clients} clients across {len(devices)} interfaces")
                
            except Exception as e:
                logger.error(f"Error polling {router_ip}: {e}", exc_info=True)
            
            time.sleep(poll_interval)
        
        logger.info(f"Stopped polling {router_ip}")
    
    def ping_router(self, router_ip: str):
        """Monitor router online status"""
        ping_interval = self.config.get('ping_interval', 10)
        
        logger.info(f"Started ping monitoring {router_ip} every {ping_interval}s")
        
        while self.running:
            is_online = False
            hostname = router_ip
            
            try:
                # Try simple echo command first (faster than uci)
                result = self.ssh_command(router_ip, 'echo "OK"')
                
                if result and result.strip() == 'OK':
                    is_online = True
                    # Get hostname if online
                    hostname_result = self.ssh_command(router_ip, 'uci get system.@system[0].hostname')
                    if hostname_result:
                        hostname = hostname_result.strip()
                
            except Exception as e:
                logger.debug(f"{router_ip}: SSH check failed - {e}")
                is_online = False
            
            # Update database
            try:
                conn = sqlite3.connect(self.db_path)
                cursor = conn.cursor()
                
                if is_online:
                    cursor.execute('''
                        INSERT OR REPLACE INTO routers (ip, hostname, online, last_seen, first_seen)
                        VALUES (?, ?, 1, ?, COALESCE((SELECT first_seen FROM routers WHERE ip = ?), ?))
                    ''', (router_ip, hostname, datetime.now(), router_ip, datetime.now()))
                    logger.debug(f"{router_ip} ({hostname}): Online")
                else:
                    # Check if router exists first
                    cursor.execute('SELECT ip FROM routers WHERE ip = ?', (router_ip,))
                    if cursor.fetchone():
                        cursor.execute('UPDATE routers SET online = 0 WHERE ip = ?', (router_ip,))
                        logger.warning(f"{router_ip}: Marked offline")
                    else:
                        # Insert as offline
                        cursor.execute('''
                            INSERT INTO routers (ip, hostname, online, last_seen, first_seen)
                            VALUES (?, ?, 0, ?, ?)
                        ''', (router_ip, router_ip, datetime.now(), datetime.now()))
                        logger.warning(f"{router_ip}: Added as offline")
                
                conn.commit()
                conn.close()
                
            except Exception as e:
                logger.error(f"Failed to update router status for {router_ip}: {e}")
            
            time.sleep(ping_interval)
        
        logger.info(f"Stopped ping monitoring {router_ip}")
    
    def cleanup_old_data(self):
        """Cleanup old data periodically (runs every hour)"""
        while self.running:
            try:
                time.sleep(3600)  # Run every hour
                
                conn = sqlite3.connect(self.db_path)
                cursor = conn.cursor()
                
                # Mark routers offline if not seen in 5 minutes
                cursor.execute('''
                    UPDATE routers 
                    SET online = 0 
                    WHERE datetime(last_seen) < datetime('now', '-5 minutes')
                ''')
                
                affected = cursor.rowcount
                if affected > 0:
                    logger.info(f"Marked {affected} routers as offline")
                
                conn.commit()
                conn.close()
                
            except Exception as e:
                logger.error(f"Error during cleanup: {e}")
    
    def run(self):
        """Start the collector"""
        routers = self.config.get('routers', [])
        
        if not routers:
            logger.error("No routers configured! Please edit /etc/openwrt-monitor/config.json")
            return
        
        logger.info(f"Starting OpenWrt Monitor for {len(routers)} routers")
        self.running = True
        
        # Start cleanup thread
        cleanup_thread = threading.Thread(target=self.cleanup_old_data, daemon=True)
        cleanup_thread.start()
        
        # Start polling and ping threads for each router
        for router_ip in routers:
            # Polling thread (collects client data)
            poll_thread = threading.Thread(
                target=self.poll_router,
                args=(router_ip,),
                daemon=True,
                name=f"poll-{router_ip}"
            )
            poll_thread.start()
            self.poll_threads[router_ip] = poll_thread
            
            # Ping thread (monitors online status)
            ping_thread = threading.Thread(
                target=self.ping_router,
                args=(router_ip,),
                daemon=True,
                name=f"ping-{router_ip}"
            )
            ping_thread.start()
            self.ping_threads[router_ip] = ping_thread
        
        logger.info("All threads started, collector is running...")
        
        # Keep main thread alive
        try:
            while self.running:
                time.sleep(1)
        except KeyboardInterrupt:
            logger.info("Received keyboard interrupt")
            self.stop()
    
    def stop(self):
        """Stop the collector gracefully"""
        logger.info("Stopping collector...")
        self.running = False
        
        # Wait for threads to finish (with timeout)
        for thread in list(self.poll_threads.values()) + list(self.ping_threads.values()):
            thread.join(timeout=5)
        
        logger.info("Collector stopped")


if __name__ == '__main__':
    collector = UbusCollector()
    collector.run()
