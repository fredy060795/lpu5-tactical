#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
meshtastic_gateway_service.py - Standalone Meshtastic Gateway Service

This service runs parallel to api.py and handles:
- Hardware connection via serial port
- Real-time data import from Meshtastic devices
- Automatic sync to database (meshtastic_nodes_db.json, meshtastic_messages_db.json)
- WebSocket broadcast for live updates

Usage:
    python meshtastic_gateway_service.py --port COM7 --auto-sync

The service can be started/stopped via API endpoints or run standalone.
"""

import os
import sys
import json
import time
import logging
import argparse
import threading
from datetime import datetime
from typing import Optional, Dict, List

# Optional imports (graceful fallback)
try:
    import meshtastic.serial_interface
    MESHTASTIC_AVAILABLE = True
except ImportError:
    MESHTASTIC_AVAILABLE = False
    print("WARNING: meshtastic library not available")

try:
    from pubsub import pub
    PUBSUB_AVAILABLE = True
except ImportError:
    PUBSUB_AVAILABLE = False
    print("WARNING: pubsub library not available")

try:
    import serial.tools.list_ports
    SERIAL_AVAILABLE = True
except ImportError:
    SERIAL_AVAILABLE = False
    print("WARNING: pyserial not available")

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s - %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger('MeshtasticGateway')


class MeshtasticGatewayService:
    """
    Standalone service for Meshtastic hardware connection and data import.
    Runs independently from the main API server.
    """
    
    def __init__(self, port: str, base_path: str = None, broadcast_callback=None):
        self.port = port
        self.base_path = base_path or os.path.dirname(os.path.abspath(__file__))
        self.interface: Optional[object] = None
        self.running = False
        self.sync_thread: Optional[threading.Thread] = None
        self.broadcast_callback = broadcast_callback  # Optional callback for WebSocket broadcasts
        
        # Database paths
        self.nodes_db_path = os.path.join(self.base_path, "meshtastic_nodes_db.json")
        self.messages_db_path = os.path.join(self.base_path, "meshtastic_messages_db.json")
        
        # Statistics
        self.stats = {
            "connected": False,
            "port": port,
            "nodes_synced": 0,
            "messages_received": 0,
            "last_sync": None,
            "uptime_start": None
        }
        
        logger.info(f"MeshtasticGatewayService initialized for port: {port}")
    
    def _broadcast(self, event_type: str, data: Dict):
        """Send broadcast via callback if available"""
        if self.broadcast_callback:
            try:
                self.broadcast_callback(event_type, data)
            except Exception as e:
                logger.error(f"Broadcast callback error: {e}")
    
    def connect(self) -> bool:
        """Establish connection to Meshtastic hardware"""
        if not MESHTASTIC_AVAILABLE:
            logger.error("Meshtastic library not available - cannot connect")
            return False
        
        try:
            logger.info(f"Connecting to Meshtastic device on {self.port}...")
            self.interface = meshtastic.serial_interface.SerialInterface(self.port)
            
            # Subscribe to incoming packets
            if PUBSUB_AVAILABLE:
                pub.subscribe(self.on_receive_packet, "meshtastic.receive")
                logger.info("Subscribed to meshtastic.receive events")
            
            self.stats["connected"] = True
            self.stats["uptime_start"] = datetime.utcnow().isoformat()
            logger.info(f"✓ Connected to Meshtastic device on {self.port}")
            
            # Broadcast connection event
            self._broadcast("gateway_status", {
                "status": "connected",
                "port": self.port,
                "timestamp": datetime.utcnow().isoformat()
            })
            
            return True
            
        except Exception as e:
            logger.error(f"✗ Failed to connect to {self.port}: {e}")
            self.stats["connected"] = False
            return False
    
    def disconnect(self):
        """Close hardware connection"""
        if self.interface:
            try:
                if PUBSUB_AVAILABLE:
                    pub.unsubscribe(self.on_receive_packet, "meshtastic.receive")
                
                if hasattr(self.interface, 'close'):
                    self.interface.close()
                
                logger.info(f"Disconnected from {self.port}")
                
                # Broadcast disconnection event
                self._broadcast("gateway_status", {
                    "status": "disconnected",
                    "port": self.port,
                    "timestamp": datetime.utcnow().isoformat()
                })
                
            except Exception as e:
                logger.error(f"Error during disconnect: {e}")
            finally:
                self.interface = None
                self.stats["connected"] = False
    
    def on_receive_packet(self, packet, interface):
        """Handle incoming Meshtastic packet (pubsub callback)"""
        try:
            from_id = packet.get('fromId') or packet.get('from')
            
            if not from_id:
                return
            
            # Update node data
            node = self.interface.nodes.get(from_id)
            if node:
                self.process_node(node, force_update=True)
            
            # Process messages
            decoded = packet.get('decoded')
            if decoded and decoded.get('portnum') == 'TEXT_MESSAGE_APP':
                self.process_message(packet)
            
            self.stats["messages_received"] += 1
            
        except Exception as e:
            logger.error(f"Error processing packet: {e}")
    
    def process_node(self, node: Dict, force_update: bool = False):
        """Process and save node to database"""
        try:
            user = node.get('user', {})
            pos = node.get('position', {})
            
            # Extract identifiers
            raw_uid = user.get('id') or f"!{node.get('num'):08x}"
            uid = raw_uid.replace('!', 'ID-')
            
            # Extract name
            long_name = user.get('longName')
            short_name = user.get('shortName')
            callsign = long_name or short_name or uid
            
            # Extract GPS coordinates
            lat_i = pos.get('latitude_i')
            lon_i = pos.get('longitude_i')
            lat_f = pos.get('latitude')
            lon_f = pos.get('longitude')
            
            # Determine final coordinates (prioritize integer microdegrees)
            final_lat = 0.0
            final_lon = 0.0
            has_gps = False
            
            if lat_i and lon_i and lat_i != 0:
                final_lat = lat_i * 1e-7
                final_lon = lon_i * 1e-7
                has_gps = True
            elif lat_f and lon_f and lat_f != 0:
                final_lat = lat_f
                final_lon = lon_f
                has_gps = True
            
            # Build node record
            node_record = {
                "id": uid,
                "mesh_id": raw_uid,
                "name": callsign,
                "longName": long_name,
                "shortName": short_name,
                "lat": final_lat,
                "lng": final_lon,
                "has_gps": has_gps,
                "altitude": pos.get('altitude', 0),
                "battery": node.get('deviceMetrics', {}).get('batteryLevel'),
                "snr": node.get('snr'),
                "rssi": node.get('rssi'),
                "last_heard": node.get('lastHeard', int(time.time())),
                "hardware": user.get('hwModel', 'UNKNOWN'),
                "device": self.port,
                "imported_from": "gateway_service",
                "timestamp": datetime.utcnow().isoformat()
            }
            
            # Load existing nodes
            nodes_db = self.load_json(self.nodes_db_path, [])
            
            # Update or append
            existing = next((n for n in nodes_db if n.get('mesh_id') == raw_uid), None)
            if existing:
                existing.update(node_record)
            else:
                nodes_db.append(node_record)
            
            # Save to disk
            self.save_json(self.nodes_db_path, nodes_db)
            
            if has_gps and force_update:
                logger.info(f"LIVE UPDATE: {callsign} @ {final_lat:.5f}, {final_lon:.5f}")
                
                # Broadcast node update
                self._broadcast("gateway_node_update", {
                    "id": uid,
                    "name": callsign,
                    "lat": final_lat,
                    "lng": final_lon,
                    "has_gps": has_gps,
                    "timestamp": datetime.utcnow().isoformat()
                })
            
            self.stats["nodes_synced"] = len(nodes_db)
            self.stats["last_sync"] = datetime.utcnow().isoformat()
            
        except Exception as e:
            logger.error(f"Error processing node: {e}")
    
    def process_message(self, packet: Dict):
        """Process and save text message"""
        try:
            decoded = packet.get('decoded', {})
            text = decoded.get('text', '')
            
            if not text:
                return
            
            from_id = packet.get('fromId') or packet.get('from')
            to_id = packet.get('toId') or packet.get('to')
            
            # Get sender name
            sender_name = from_id
            if self.interface and from_id in self.interface.nodes:
                node = self.interface.nodes[from_id]
                user = node.get('user', {})
                sender_name = user.get('longName') or user.get('shortName') or from_id
            
            message_record = {
                "id": f"msg-{int(time.time() * 1000)}",
                "from": from_id,
                "to": to_id,
                "sender_name": sender_name,
                "text": text,
                "timestamp": datetime.utcnow().isoformat(),
                "packet_id": packet.get('id'),
                "channel": packet.get('channel', 0)
            }
            
            # Load existing messages
            messages_db = self.load_json(self.messages_db_path, [])
            
            # Append and limit to last 1000 messages
            messages_db.append(message_record)
            if len(messages_db) > 1000:
                messages_db = messages_db[-1000:]
            
            # Save to disk
            self.save_json(self.messages_db_path, messages_db)
            
            logger.info(f"MESSAGE: {sender_name}: {text[:50]}{'...' if len(text) > 50 else ''}")
            
            # Broadcast message event
            self._broadcast("gateway_message", {
                "direction": "incoming",
                "from": from_id,
                "sender_name": sender_name,
                "text": text,
                "timestamp": datetime.utcnow().isoformat()
            })
            
        except Exception as e:
            logger.error(f"Error processing message: {e}")
    
    def full_sync(self):
        """Perform full sync of all nodes"""
        if not self.interface or not hasattr(self.interface, 'nodes'):
            logger.warning("Cannot sync - no interface or nodes available")
            return
        
        try:
            nodes_list = sorted(
                self.interface.nodes.values(),
                key=lambda x: x.get('user', {}).get('longName', '')
            )
            
            logger.info(f"Starting full sync: {len(nodes_list)} nodes")
            
            for node in nodes_list:
                self.process_node(node, force_update=False)
            
            logger.info(f"✓ Full sync complete: {len(nodes_list)} nodes synced")
            
        except Exception as e:
            logger.error(f"Error during full sync: {e}")
    
    def sync_loop(self, interval: int = 300):
        """Continuous sync loop (runs in background thread)"""
        logger.info(f"Starting sync loop (interval: {interval}s)")
        
        while self.running:
            try:
                self.full_sync()
            except Exception as e:
                logger.error(f"Error in sync loop: {e}")
            
            # Sleep in small intervals for clean shutdown
            for _ in range(interval):
                if not self.running:
                    break
                time.sleep(1)
        
        logger.info("Sync loop stopped")
    
    def start(self, auto_sync: bool = True, sync_interval: int = 300):
        """Start the gateway service"""
        if not self.connect():
            logger.error("Failed to start - could not connect to hardware")
            return False
        
        self.running = True
        
        # Initial full sync
        time.sleep(2)  # Wait for device initialization
        self.full_sync()
        
        # Start background sync thread
        if auto_sync:
            self.sync_thread = threading.Thread(
                target=self.sync_loop,
                args=(sync_interval,)
                daemon=True,
                name="MeshtasticSyncThread"
            )
            self.sync_thread.start()
            logger.info("✓ Gateway service started with auto-sync enabled")
        else:
            logger.info("✓ Gateway service started (manual sync only)")
        
        return True
    
    def stop(self):
        """Stop the gateway service"""
        logger.info("Stopping gateway service...")
        self.running = False
        
        # Wait for sync thread to finish
        if self.sync_thread and self.sync_thread.is_alive():
            self.sync_thread.join(timeout=5)
        
        self.disconnect()
        logger.info("✓ Gateway service stopped")
    
    def get_status(self) -> Dict:
        """Get current service status"""
        return {
            "running": self.running,
            "connected": self.stats["connected"],
            "port": self.port,
            "nodes_synced": self.stats["nodes_synced"],
            "messages_received": self.stats["messages_received"],
            "last_sync": self.stats["last_sync"],
            "uptime_start": self.stats["uptime_start"]
        }
    
    # Helper methods for JSON file operations
    def load_json(self, path: str, default):
        """Load JSON file with fallback"""
        if not os.path.exists(path):
            return default
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error loading {path}: {e}")
            return default
    
    def save_json(self, path: str, data):
        """Save JSON file"""
        try:
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Error saving {path}: {e}")


def list_serial_ports() -> List[Dict]:
    """List available serial ports"""
    if not SERIAL_AVAILABLE:
        return []
    
    ports = []
    for port in serial.tools.list_ports.comports():
        ports.append({
            "device": port.device,
            "description": port.description,
            "hwid": port.hwid
        })
    return ports


def main():
    """Main entry point for standalone execution"""
    parser = argparse.ArgumentParser(description='Meshtastic Gateway Service')
    parser.add_argument('--port', type=str, help='Serial port (e.g., COM7, /dev/ttyUSB0)')
    parser.add_argument('--list-ports', action='store_true', help='List available serial ports')
    parser.add_argument('--auto-sync', action='store_true', default=True, help='Enable automatic sync (default: True)')
    parser.add_argument('--sync-interval', type=int, default=300, help='Sync interval in seconds (default: 300)')
    
    args = parser.parse_args()
    
    # List ports mode
    if args.list_ports:
        print("\n=== Available Serial Ports ===")
        ports = list_serial_ports()
        if not ports:
            print("No serial ports found")
        else:
            for i, port in enumerate(ports):
                print(f"[{i}] {port['device']} - {port['description']}")
        return
    
    # Require port for normal operation
    if not args.port:
        print("ERROR: --port is required")
        print("\nUsage:")
        print("  python meshtastic_gateway_service.py --port COM7")
        print("  python meshtastic_gateway_service.py --list-ports")
        sys.exit(1)
    
    # Start service
    print("\n" + "="*60)
    print("  MESHTASTIC GATEWAY SERVICE")
    print("="*60)
    print(f"  Port: {args.port}")
    print(f"  Auto-sync: {args.auto_sync}")
    print(f"  Sync interval: {args.sync_interval}s")
    print("="*60 + "\n")
    
    gateway = MeshtasticGatewayService(args.port)
    
    if not gateway.start(auto_sync=args.auto_sync, sync_interval=args.sync_interval):
        print("\n✗ Failed to start gateway service")
        sys.exit(1)
    
    print("\nGateway service running. Press Ctrl+C to stop.\n")
    
    # Keep running until interrupted
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n\nShutdown signal received...")
        gateway.stop()
        print("✓ Service stopped cleanly")


if __name__ == "__main__":
    main()