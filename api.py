#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
api.py - LPU5 Tactical Tracker API (full replacement)

This is a complete, standalone API implementation intended to replace the existing api.py.
It preserves all data in JSON DB files in the same directory (no automatic deletion).
Features:
 - Users, sessions, groups, missions, map_markers, meshtastic_nodes/messages CRUD
 - meshtastic preview/import/ingest with robust SerialInterface handling
 - ensures missing GPS -> lat/lng = 0.0 during import as requested
 - /api/scan_ports (pyserial) to enumerate COM ports
 - /api/me to return authenticated user info (Authorization: Bearer <token>)
 - QR endpoints:
     * /api/qr/create, /api/qr/list, /api/qr/{token}, /api/qr/{token}/png
     * public redirect /qr/{token} with allowed_ips, uses, expiry checks
 - registration QR endpoints (/api/registration_qr) for user registration flow
 - background thread to sync meshtastic_nodes_db.json -> map_markers_db.json periodically
 - defensive handling if optional dependencies (meshtastic, pyserial, qrcode) missing
 - Logging + audit log to JSON
Notes:
 - Replacing this file will not modify JSON DB files. Still: back up your DB folder before replacing in production.
 - If pyserial/meshtastic/qrcode are missing server-side they will be attempted to be used if available; otherwise fallback behavior used.
"""
from contextlib import asynccontextmanager
from fastapi import FastAPI, Body, HTTPException, Request, Path, Header, WebSocket, WebSocketDisconnect, UploadFile, File, Form
from fastapi.responses import FileResponse, JSONResponse, Response, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from datetime import datetime, timedelta, timezone
import os
import json
import re
import uuid
import hashlib
import jwt
import base64
from typing import Optional, Any, Dict, List
import logging
import random
import threading
import time
import socket
import ssl
import asyncio
import sys
import xml.sax.saxutils as _sax_utils

# Fix Windows asyncio ProactorEventLoop issue that causes
# "Exception in callback _ProactorBasePipeTransport._call_connection_lost"
# on shutdown. Use SelectorEventLoop on Windows instead.
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# -------------------------
# Logging setup - MUST come first before any code that uses logger
# -------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("lpu5-api")

# Global event loop reference for thread-safe broadcasts
_MAIN_EVENT_LOOP = None

# Database imports
from database import Base, SessionLocal, engine, get_db
from models import User, Unit, MapMarker, Mission, MeshtasticNode, AutonomousRule, Geofence, ChatMessage, ChatChannel, AuditLog, Drawing, Overlay, APISession, UserGroup, QRCode, PendingRegistration
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified
from fastapi import Depends

# Ensure all tables exist (creates any missing tables like chat_channels, chat_messages)
Base.metadata.create_all(bind=engine)

# Migrate existing tables: add missing columns that create_all() won't add to existing tables
from sqlalchemy import text as sa_text, inspect as sa_inspect
_inspector = sa_inspect(engine)
if "chat_messages" in _inspector.get_table_names():
    _existing_cols = {c["name"] for c in _inspector.get_columns("chat_messages")}
    with engine.begin() as _conn:
        if "delivered_to" not in _existing_cols:
            _conn.execute(sa_text("ALTER TABLE chat_messages ADD COLUMN delivered_to JSON"))
        if "read_by" not in _existing_cols:
            _conn.execute(sa_text("ALTER TABLE chat_messages ADD COLUMN read_by JSON"))

# Migrate users table: add unit_id and chat_channels columns if missing
if "units" in _inspector.get_table_names() and "users" in _inspector.get_table_names():
    _user_cols = {c["name"] for c in _inspector.get_columns("users")}
    with engine.begin() as _conn:
        if "unit_id" not in _user_cols:
            # SQLite does not enforce FK constraints by default; omit REFERENCES for compat
            _conn.execute(sa_text("ALTER TABLE users ADD COLUMN unit_id VARCHAR"))
        if "chat_channels" not in _user_cols:
            _conn.execute(sa_text("ALTER TABLE users ADD COLUMN chat_channels JSON"))

# Import new autonomous modules
try:
    from cot_protocol import CoTEvent, CoTProtocolHandler
    from geofencing import GeofencingManager, GeoFence, haversine_distance
    from autonomous_engine import AutonomousEngine, Rule
    from websocket_manager import ConnectionManager, WebSocketEventHandler, Channels
    AUTONOMOUS_MODULES_AVAILABLE = True
except ImportError as e:
    logger.warning(f"Autonomous modules not available: {e}")
    AUTONOMOUS_MODULES_AVAILABLE = False

# Optional features
try:
    import qrcode
except Exception:
    qrcode = None  # type: ignore

# Optional meshtastic import (if installed on the server)
try:
    import meshtastic  # type: ignore
except Exception:
    meshtastic = None

# Optional pyserial list_ports
try:
    from serial.tools import list_ports as serial_list_ports  # type: ignore
except Exception:
    serial_list_ports = None  # type: ignore

# Import gateway service
try:
    from meshtastic_gateway_service import MeshtasticGatewayService, list_serial_ports as gateway_list_ports
    GATEWAY_SERVICE_AVAILABLE = True
except Exception as e:
    logger.warning(f"Gateway service not available: {e}")
    MeshtasticGatewayService = None
    gateway_list_ports = None
    GATEWAY_SERVICE_AVAILABLE = False

# Import CoT listener service
try:
    from cot_listener_service import CoTListenerService
    COT_LISTENER_AVAILABLE = True
except Exception as e:  # pragma: no cover
    logger.warning("CoT listener service not available: %s", e)
    CoTListenerService = None
    COT_LISTENER_AVAILABLE = False

# RBAC permissions system has been removed - all users have full access
# Keeping basic authentication (verify_token, get_current_user) for user identity
PERMISSIONS_AVAILABLE = False
PermissionManager = None
logger.info("Permissions system DISABLED - all users have full access")

# Import Data Server Manager for separate process data distribution
try:
    from data_server_manager import DataServerManager
    DATA_SERVER_AVAILABLE = True
except ImportError as e:
    logger.warning(f"Data server manager not available: {e}")
    DATA_SERVER_AVAILABLE = False
    DataServerManager = None

# Helper function to detect local network IP
def get_local_ip():
    """
    Detects the local network IP address.
    Prioritizes physical network adapters over virtual ones.
    Specifically prioritizes 192.168.8.x WLAN subnet for mobile device access.
    Returns tuple: (primary_ip, all_detected_ips)
    """
    detected_ips = []
    
    # Method 1: Socket-based detection (most reliable for default route)
    try:
        # Create a socket connection to detect the local IP
        # Connect to a public IP (doesn't actually send data)
        # Port 80 is used as a common outbound port
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.1)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
        # Verify it's not localhost
        if local_ip and local_ip != "127.0.0.1":
            detected_ips.append(local_ip)
    except Exception as e:
        logger.warning(f"Primary IP detection failed: {e}")
    
    # Method 2: Hostname-based detection
    try:
        hostname = socket.gethostname()
        # getaddrinfo returns all addresses for the hostname
        addr_info = socket.getaddrinfo(hostname, None, socket.AF_INET)
        for info in addr_info:
            ip = info[4][0]
            if ip and ip != "127.0.0.1" and ip not in detected_ips:
                detected_ips.append(ip)
    except Exception as e:
        logger.warning(f"Hostname multi-address detection failed: {e}")
    
    # Method 3: Fallback - simple hostname resolution
    try:
        hostname = socket.gethostname()
        local_ip = socket.gethostbyname(hostname)
        if local_ip and local_ip != "127.0.0.1" and local_ip not in detected_ips:
            detected_ips.append(local_ip)
    except Exception as e:
        logger.warning(f"Hostname IP detection failed: {e}")
    
    # Filter and prioritize IPs with specific preference for 192.168.8.x WLAN subnet
    primary_ip = "127.0.0.1"
    
    if detected_ips:
        # HIGHEST PRIORITY: 192.168.8.x subnet (WLAN for mobile device access)
        wlan_ips = [ip for ip in detected_ips if ip.startswith("192.168.8.")]
        if wlan_ips:
            primary_ip = wlan_ips[0]
            return primary_ip, detected_ips
        
        # SECOND PRIORITY: Other 192.168.x.x addresses (common home/office networks)
        preferred = [ip for ip in detected_ips if ip.startswith("192.168.")]
        if preferred:
            primary_ip = preferred[0]
            return primary_ip, detected_ips
        
        # THIRD PRIORITY: 10.x.x.x (another common private range)
        preferred = [ip for ip in detected_ips if ip.startswith("10.")]
        if preferred:
            primary_ip = preferred[0]
            return primary_ip, detected_ips
        
        # FOURTH PRIORITY: 172.16-31.x.x
        preferred = []
        for ip in detected_ips:
            parts = ip.split('.')
            if len(parts) == 4 and parts[0] == "172":
                try:
                    second_octet = int(parts[1])
                    if 16 <= second_octet <= 31:
                        preferred.append(ip)
                except (ValueError, IndexError):
                    pass
        if preferred:
            primary_ip = preferred[0]
            return primary_ip, detected_ips
        
        # Return first detected IP as last resort
        primary_ip = detected_ips[0]
        return primary_ip, detected_ips
    
    # Last resort: return localhost
    logger.warning("Could not detect local network IP, falling back to 127.0.0.1")
    return primary_ip, []

# JWT settings (development use only)
JWT_SECRET = "LPU5-TACTICAL-SECRET-KEY-2024"
JWT_ALGORITHM = "HS256"
JWT_EXPIRATION_HOURS = 24

# Sync constants
MAX_STORED_MESSAGES = 1000  # Maximum messages to keep in database
MAX_RETURNED_MESSAGES = 100  # Maximum messages to return in sync/download

@asynccontextmanager
async def lifespan(application):
    # ---- Startup logic ----
    global _MAIN_EVENT_LOOP
    try:
        _MAIN_EVENT_LOOP = asyncio.get_running_loop()
        logger.info(f"Captured main event loop: {_MAIN_EVENT_LOOP}")
    except RuntimeError:
        try:
            _MAIN_EVENT_LOOP = asyncio.get_event_loop()
            logger.info(f"Captured event loop: {_MAIN_EVENT_LOOP}")
        except Exception as e:
            logger.warning(f"Could not capture event loop: {e}")

    ensure_db_files()
    ensure_default_admin()
    ensure_default_unit()

    # Start data server process if available
    if DATA_SERVER_AVAILABLE and data_server_manager:
        try:
            logger.info("Starting data distribution server...")
            if data_server_manager.start(timeout=15):
                logger.info("✅ Data server started successfully")
                status = data_server_manager.get_status()
                if status:
                    logger.info(f"   Data server status: {status.get('status')}")
                    logger.info(f"   WebSocket: ws://127.0.0.1:8102/ws")
            else:
                logger.warning("⚠️  Failed to start data server, falling back to direct WebSocket")
        except Exception as e:
            logger.error(f"Error starting data server: {e}")

    # Start background sync thread if enabled
    try:
        cfg = load_json("config") or {}
        enabled = cfg.get("meshtastic_auto_sync", True)
        interval = int(cfg.get("meshtastic_sync_interval_seconds", 30))
    except Exception:
        enabled = True
        interval = 30
    global _MESHTASTIC_SYNC_THREAD
    if enabled and (_MESHTASTIC_SYNC_THREAD is None or not _MESHTASTIC_SYNC_THREAD.is_alive()):
        _MESHTASTIC_SYNC_STOP_EVENT.clear()
        _MESHTASTIC_SYNC_THREAD = threading.Thread(target=_meshtastic_sync_worker, args=(interval,), daemon=True, name="meshtastic-sync")
        _MESHTASTIC_SYNC_THREAD.start()

    # Start periodic marker broadcast thread for real-time sync
    try:
        cfg = load_json("config") or {}
        broadcast_enabled = cfg.get("marker_broadcast_enabled", True)
        broadcast_interval = int(cfg.get("marker_broadcast_interval_seconds", 60))
    except Exception:
        broadcast_enabled = True
        broadcast_interval = 60
    global _MARKER_BROADCAST_THREAD
    if broadcast_enabled and (_MARKER_BROADCAST_THREAD is None or not _MARKER_BROADCAST_THREAD.is_alive()):
        _MARKER_BROADCAST_STOP_EVENT.clear()
        _MARKER_BROADCAST_THREAD = threading.Thread(target=_marker_broadcast_worker, args=(broadcast_interval,), daemon=True, name="marker-broadcast")
        _MARKER_BROADCAST_THREAD.start()
        logger.info("✅ Marker broadcast worker started (interval=%ss)", broadcast_interval)

    # Start CoT listener service if enabled in config
    try:
        cfg = load_json("config") or {}
        # Default to True so WinTAK/ATAK can reach LPU5 on first run without manual configuration.
        # Set cot_listener_enabled=false in config.json to disable.
        cot_listener_enabled = cfg.get("cot_listener_enabled", True)
    except Exception:
        cot_listener_enabled = True
    if cot_listener_enabled and COT_LISTENER_AVAILABLE:
        try:
            if _start_cot_listener():
                logger.info("✅ CoT listener service started")
            else:
                logger.warning("⚠️  Failed to start CoT listener service")
        except Exception as e:
            logger.error("Error starting CoT listener service: %s", e)

    # Auto-start TAK receiver thread and forward existing data if TAK integration is enabled
    try:
        tak_cfg = _get_tak_config()
        if tak_cfg.get("tak_forward_enabled") and tak_cfg.get("tak_server_host"):
            if tak_cfg.get("tak_connection_type", "udp") in ("tcp", "ssl"):
                if _start_tak_receiver_thread():
                    logger.info("✅ TAK receiver thread started")
                else:
                    logger.warning("⚠️  Failed to start TAK receiver thread")
            # Forward all existing LPU5 data to TAK server in a background thread
            # (5-second delay lets the server finish initialization before sending)
            def _delayed_tak_forward():
                time.sleep(5)
                _forward_all_lpu5_data_to_tak()

            threading.Thread(
                target=_delayed_tak_forward,
                daemon=True,
                name="tak-initial-forward",
            ).start()
            logger.info("✅ TAK initial data forward scheduled")
            # Start periodic TAK sync thread
            global _TAK_PERIODIC_SYNC_THREAD
            if _TAK_PERIODIC_SYNC_THREAD is None or not _TAK_PERIODIC_SYNC_THREAD.is_alive():
                _TAK_PERIODIC_SYNC_STOP_EVENT.clear()
                _TAK_PERIODIC_SYNC_THREAD = threading.Thread(
                    target=_tak_periodic_sync_worker,
                    daemon=True,
                    name="tak-periodic-sync",
                )
                _TAK_PERIODIC_SYNC_THREAD.start()
                logger.info("✅ TAK periodic sync worker started (interval=60s)")
    except Exception as e:
        logger.error("Error starting TAK receiver thread: %s", e)

    logger.info("Startup complete. DB files ensured.")

    yield

    # ---- Shutdown logic ----
    # Stop TAK receiver thread
    try:
        _stop_tak_receiver_thread()
    except Exception as e:
        logger.error("Error stopping TAK receiver thread: %s", e)

    # Stop CoT listener service
    try:
        _stop_cot_listener()
    except Exception as e:
        logger.error("Error stopping CoT listener service: %s", e)

    # Stop gateway service
    global _gateway_service
    if _gateway_service:
        try:
            logger.info("Stopping gateway service...")
            _gateway_service.stop()
            _gateway_service = None
            logger.info("✅ Gateway service stopped")
        except Exception as e:
            logger.error(f"Error stopping gateway service: {e}")

    # Stop meshtastic sync thread
    try:
        _MESHTASTIC_SYNC_STOP_EVENT.set()
    except Exception:
        pass

    # Stop marker broadcast thread
    try:
        _MARKER_BROADCAST_STOP_EVENT.set()
    except Exception:
        pass

    # Stop TAK periodic sync thread
    try:
        _TAK_PERIODIC_SYNC_STOP_EVENT.set()
    except Exception:
        pass

    # Stop data server process
    if DATA_SERVER_AVAILABLE and data_server_manager:
        try:
            logger.info("Stopping data distribution server...")
            if data_server_manager.stop():
                logger.info("✅ Data server stopped successfully")
            else:
                logger.warning("⚠️  Failed to stop data server gracefully")
        except Exception as e:
            logger.error(f"Error stopping data server: {e}")

app = FastAPI(title="LPU5 Tactical Tracker API", version="2.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Base path - define BEFORE using it
base_path = os.path.dirname(os.path.abspath(__file__))
logger.info(f"LPU5 API initialized. Base path: {base_path}")

# Initialize autonomous systems (if available)
websocket_manager = None
websocket_event_handler = None
geofencing_manager = None
autonomous_engine = None
data_server_manager = None

if AUTONOMOUS_MODULES_AVAILABLE:
    try:
        websocket_manager = ConnectionManager()
        websocket_event_handler = WebSocketEventHandler(websocket_manager)
        # Managers are now DB-backed and don't strictly need a JSON path
        geofencing_manager = GeofencingManager()
        autonomous_engine = AutonomousEngine()
        autonomous_engine.start_scheduler()
        logger.info("Autonomous systems initialized with SQLAlchemy backend")
    except Exception as e:
        logger.error(f"Failed to initialize autonomous systems: {e}")
        AUTONOMOUS_MODULES_AVAILABLE = False

# Initialize data server manager for separate process data distribution
if DATA_SERVER_AVAILABLE:
    try:
        data_server_manager = DataServerManager(
            data_server_port=8102,
            data_server_host="127.0.0.1"
        )
        logger.info("Data server manager initialized")
    except Exception as e:
        logger.error(f"Failed to initialize data server manager: {e}")
        DATA_SERVER_AVAILABLE = False

# Mount static directories
app.mount("/static", StaticFiles(directory=base_path), name="static")
assets_dir = os.path.join(base_path, "assets")
if os.path.isdir(assets_dir):
    app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")
    logger.info("Static mounted: /static and /assets")
else:
    logger.info("Static mounted: /static (no assets/ directory)")

# Uploads directory for mission attachments
uploads_dir = os.path.join(base_path, "uploads")
os.makedirs(uploads_dir, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=uploads_dir), name="uploads")
logger.info("Static mounted: /uploads")

# Legacy JSON DB file mapping - ARCHIVED (replaced by SQLAlchemy/SQLite)
# Some files remain for backward compatibility or migration reference
DB_PATHS: Dict[str, str] = {
    "config": os.path.join(base_path, "config.json"),
    "qr_codes": os.path.join(base_path, "qr_codes_db.json"),
    "pending_registrations": os.path.join(base_path, "pending_registrations_db.json"),
    "meshtastic_nodes": os.path.join(base_path, "meshtastic_nodes_db.json"),
    "map_markers": os.path.join(base_path, "map_markers_db.json"),
    "meshtastic_messages": os.path.join(base_path, "meshtastic_messages_db.json"),
}

DEFAULT_DB_CONTENTS: Dict[str, Any] = {
    "config": {},
    "qr_codes": [],
    "pending_registrations": [],
    "meshtastic_nodes": [],
    "map_markers": [],
    "meshtastic_messages": [],
}

# -------------------------
# JSON DB helpers
# -------------------------
def load_json(key: str) -> Any:
    path = DB_PATHS.get(key)
    if not path or not os.path.exists(path):
        return [] if key != "config" else {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError:
        logger.warning("Invalid JSON in %s at %s", key, path)
        return [] if key != "config" else {}
    except Exception:
        logger.exception("Error loading JSON for %s", key)
        return [] if key != "config" else {}

def save_json(key: str, data: Any) -> None:
    path = DB_PATHS.get(key)
    if not path:
        return
    dirpath = os.path.dirname(path)
    if dirpath and not os.path.exists(dirpath):
        os.makedirs(dirpath, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)
    logger.info("Saved %s -> %s", key, path)

# -------------------------
# Utility
# -------------------------
def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()

def verify_password(password: str, password_hash: str) -> bool:
    return hash_password(password) == password_hash

def generate_token(user_id: str, username: str) -> str:
    payload = {
        "user_id": user_id,
        "username": username,
        "exp": datetime.now(timezone.utc) + timedelta(hours=JWT_EXPIRATION_HOURS),
        "iat": datetime.now(timezone.utc)
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)

def verify_token(token: str) -> Optional[Dict]:
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except Exception:
        return None

def log_audit(action: str, user_id: str, details: Dict) -> None:
    """Log an audit event to the database"""
    db = SessionLocal()
    try:
        log_entry = AuditLog(
            event_type=action,
            user=user_id,
            details=json.dumps(details),
            timestamp=datetime.now(timezone.utc)
        )
        db.add(log_entry)
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to log audit: {e}")
    finally:
        db.close()

# -------------------------
# WebSocket broadcast helpers
# -------------------------
def broadcast_websocket_update(channel: str, event_type: str, data: Dict) -> None:
    """
    Broadcast an update to all WebSocket clients subscribed to a channel.
    Uses the separate data server process for data distribution.
    Falls back to direct WebSocket if data server is unavailable.
    
    Args:
        channel: WebSocket channel name (e.g., 'markers', 'drawings', 'overlays')
        event_type: Event type identifier (e.g., 'marker_created', 'drawing_updated')
        data: Event data dictionary to broadcast
    """
    # Try to broadcast via data server (best-effort, non-blocking)
    if DATA_SERVER_AVAILABLE and data_server_manager and data_server_manager.is_running():
        try:
            data_server_manager.broadcast(channel, event_type, data)
        except Exception as e:
            logger.warning(f"Failed to broadcast via data server: {e}")
    
    # Always broadcast via direct WebSocket (clients connect to main API server)
    if not AUTONOMOUS_MODULES_AVAILABLE or not websocket_manager:
        logger.debug("WebSocket manager not available")
        return
    
    try:
        message = {
            "type": event_type,
            "channel": channel,
            "data": data,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
        # Schedule the broadcast in the event loop
        import asyncio
        try:
            # Try to get the running loop (Python 3.10+)
            loop = asyncio.get_running_loop()
            asyncio.create_task(websocket_manager.publish_to_channel(channel, message))
            logger.debug(f"Created task for broadcast to {channel}")
        except RuntimeError:
            # No running loop - we're being called from a thread
            # Use the saved main event loop reference
            if _MAIN_EVENT_LOOP and _MAIN_EVENT_LOOP.is_running():
                asyncio.run_coroutine_threadsafe(
                    websocket_manager.publish_to_channel(channel, message),
                    _MAIN_EVENT_LOOP
                )
                logger.debug(f"Scheduled thread-safe broadcast to {channel}")
            else:
                logger.warning(f"Main event loop not available or not running for broadcast to {channel}")
    except Exception as e:
        logger.warning(f"Failed to broadcast to {channel}: {e}")

# -------------------------
# Sessions helpers
# -------------------------
def save_session(db: Session, session_obj: Dict) -> None:
    """
    Save session with simplified single-IP tracking per user in DB.
    Removes old sessions for the same user to avoid VPN + WLAN conflicts.
    """
    username = session_obj.get("username")
    if username:
        db.query(APISession).filter(APISession.username == username).delete()
    
    # Convert ISO strings to datetime objects
    created_at = datetime.fromisoformat(session_obj["created_at"]) if isinstance(session_obj.get("created_at"), str) else datetime.now(timezone.utc)
    expires_at = datetime.fromisoformat(session_obj["expires_at"]) if isinstance(session_obj.get("expires_at"), str) else (datetime.now(timezone.utc) + timedelta(hours=24))
    last_seen = datetime.fromisoformat(session_obj["last_seen"]) if isinstance(session_obj.get("last_seen"), str) else datetime.now(timezone.utc)

    new_session = APISession(
        id=session_obj.get("id"),
        token=session_obj.get("token"),
        user_id=session_obj.get("user_id"),
        username=username,
        created_at=created_at,
        expires_at=expires_at,
        ip=session_obj.get("ip"),
        last_seen=last_seen,
        data={"language": session_obj.get("language", "de")}
    )
    db.add(new_session)
    db.commit()
    logger.info("Saved session for %s in DB", username)

def remove_session_by_token(token: str) -> None:
    with SessionLocal() as db:
        db.query(APISession).filter(APISession.token == token).delete()
        db.commit()

def update_session_language(user_id: str, language: str) -> None:
    """
    Update language in all active sessions for a user in DB.
    """
    if not user_id or not language:
        logger.warning("update_session_language called with empty user_id or language")
        return
    
    with SessionLocal() as db:
        sessions = db.query(APISession).filter(APISession.user_id == user_id).all()
        for s in sessions:
            if not s.data:
                s.data = {}
            # We use a copy since SQLAlchemy JSON types need assignment to track changes usually
            d = dict(s.data)
            d["language"] = language
            s.data = d
        db.commit()
        logger.info(f"Updated language to '{language}' for {len(sessions)} session(s) of user {user_id} in DB")


def list_active_sessions() -> List[Dict]:
    with SessionLocal() as db:
        now = datetime.now(timezone.utc)
        sessions = db.query(APISession).filter(APISession.expires_at > now).all()
        return [
            {
                "id": s.id,
                "token": s.token,
                "user_id": s.user_id,
                "username": s.username,
                "created_at": s.created_at.isoformat(),
                "expires_at": s.expires_at.isoformat(),
                "ip": s.ip,
                "last_seen": s.last_seen.isoformat(),
                "language": (s.data or {}).get("language", "de")
            } for s in sessions
        ]

# -------------------------
# HTML serving & PAGE_MAP
# -------------------------
def get_html_response(filename: str):
    path = os.path.join(base_path, filename)
    if os.path.exists(path) and os.path.isfile(path):
        return FileResponse(path, media_type="text/html")
    raise HTTPException(status_code=404, detail=f"File {filename} not found")

PAGE_MAP: Dict[str, str] = {
    "/": "landing.html",
    "/landing": "landing.html",
    "/landing.html": "landing.html",
    "/index": "index.html",
    "/index.html": "index.html",
    "/dashboard": "index.html",
    "/mission": "mission.html",
    "/mission.html": "mission.html",
    "/missions": "mission.html",
    "/statistics": "statistics.html",
    "/statistics.html": "statistics.html",
    "/admin": "admin.html",
    "/admin.html": "admin.html",
    "/admin_map": "admin_map.html",
    "/admin_map.html": "admin_map.html",
    "/meshtastic": "meshtastic.html",
    "/meshtastic.html": "meshtastic.html",
    "/import_nodes": "import_nodes.html",
    "/import_nodes.html": "import_nodes.html",
    "/overview": "overview.html",
    "/overview.html": "overview.html",
    "/register": "register.html",
    "/register.html": "register.html",
}

for route, filename in PAGE_MAP.items():
    def make_handler(fname: str):
        def handler():
            return get_html_response(fname)
        return handler
    app.add_api_route(route, make_handler(filename), methods=["GET"], include_in_schema=False)

# -------------------------
# Debug endpoints
# -------------------------
@app.get("/_ls", include_in_schema=False)
def list_html_files():
    try:
        files = sorted([f for f in os.listdir(base_path) if f.lower().endswith(".html")])
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"base_path": base_path, "html_files": files}

@app.get("/_dbcheck", include_in_schema=False)
def db_check():
    results = {}
    for name, path in DB_PATHS.items():
        info = {"path": path, "exists": os.path.exists(path)}
        try:
            info["readable"] = os.access(path, os.R_OK) if os.path.exists(path) else False
            if os.path.exists(path):
                info["writable"] = os.access(path, os.W_OK)
            else:
                d = os.path.dirname(path) or base_path
                info["dir_writable"] = os.access(d, os.W_OK)
        except Exception as ex:
            info["error"] = str(ex)
        results[name] = info
    return {"base_path": base_path, "db_files": results}

@app.get("/logo.png")
def logo():
    path = os.path.join(base_path, "logo.png")
    if os.path.exists(path) and os.path.isfile(path):
        return FileResponse(path, media_type="image/png")
    transparent_png_b64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR4nGNgYAAAAAMAASsJTYQAAAAASUVORK5CYII="
    img = base64.b64decode(transparent_png_b64)
    return Response(content=img, media_type="image/png")

# -------------------------
# Startup tasks
# -------------------------
def ensure_db_files():
    for key, path in DB_PATHS.items():
        try:
            dirpath = os.path.dirname(path)
            if dirpath and not os.path.exists(dirpath):
                os.makedirs(dirpath, exist_ok=True)
            if not os.path.exists(path):
                default_val = DEFAULT_DB_CONTENTS.get(key, [] if key != "config" else {})
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(default_val, f, indent=4, ensure_ascii=False)
        except Exception as e:
            logger.exception("Error ensuring DB file %s: %s", key, e)

def ensure_default_admin():
    db = SessionLocal()
    try:
        # Check if any admin exists
        admin_exists = db.query(User).filter(User.role == "admin", User.is_active == True).first()
        if not admin_exists:
            # Check if default administrator exists
            admin_user = db.query(User).filter(User.username == "administrator").first()
            if admin_user:
                admin_user.role = "admin"
                admin_user.group_id = "admins"
                admin_user.is_active = True
                db.commit()
                logger.warning("Re-activated administrator user as admin in database")
            else:
                # Create default admin
                default_user = User(
                    id=str(uuid.uuid4()),
                    username="administrator",
                    email="",
                    password_hash=hash_password("password"),
                    role="admin",
                    group_id="admins",
                    is_active=True,
                    created_at=datetime.now(timezone.utc)
                )
                db.add(default_user)
                db.commit()
                logger.warning("Default admin created in database: username='administrator' password='password' - change immediately!")
        
        # Create test users for each role if they don't exist
        test_users = [
            {"username": "operator_user", "password": "operator123", "role": "operator", "group_id": "operators", "email": "operator@lpu5.test"},
            {"username": "normal_user", "password": "user123", "role": "user", "group_id": "users", "email": "user@lpu5.test"},
            {"username": "guest_user", "password": "guest123", "role": "guest", "group_id": "guests", "email": "guest@lpu5.test"}
        ]
        
        for tu in test_users:
            if not db.query(User).filter(User.username == tu["username"]).first():
                new_user = User(
                    id=str(uuid.uuid4()),
                    username=tu["username"],
                    email=tu["email"],
                    password_hash=hash_password(tu["password"]),
                    role=tu["role"],
                    group_id=tu["group_id"],
                    is_active=True,
                    created_at=datetime.now(timezone.utc)
                )
                db.add(new_user)
                db.commit()
                logger.info(f"Created test user: {tu['username']} with role {tu['role']} in database")
    except Exception as e:
        db.rollback()
        logger.error(f"Error in ensure_default_admin: {e}")
    finally:
        db.close()


def ensure_default_unit():
    """Ensure a default 'General' unit exists in the database."""
    db = SessionLocal()
    try:
        general = db.query(Unit).filter(Unit.name == "General").first()
        if not general:
            db.add(Unit(id=str(uuid.uuid4()), name="General", description="Default unit"))
            db.commit()
            logger.info("Created default 'General' unit")
    except Exception as e:
        db.rollback()
        logger.error(f"Error in ensure_default_unit: {e}")
    finally:
        db.close()


# -------------------------
# TAK Server forwarding helpers
# -------------------------

_TAK_SOCKET_TIMEOUT = 5  # seconds for TAK server socket operations
_TAK_PING_RESPONSE_TIMEOUT = 3  # seconds to wait for a server ping-ack response
_TAK_RECV_BUFFER = 4096  # bytes to read from server response




def _get_tak_config() -> dict:
    """Return TAK server config from config.json with safe defaults.

    Reads root-level snake_case keys first (written by PUT /api/tak/config).
    Falls back to the nested 'network' section (camelCase) written by network.html
    via POST /api/config when the root-level keys have not been set explicitly.
    """
    cfg = load_json("config") or {}
    net = cfg.get("network") if isinstance(cfg.get("network"), dict) else {}

    # tak_forward_enabled / enableTakIntegration
    tak_enabled = cfg.get("tak_forward_enabled")
    if tak_enabled is None:
        tak_enabled = net.get("enableTakIntegration", False)

    # tak_server_host / takServerUrl
    tak_host = cfg.get("tak_server_host") or net.get("takServerUrl", "")

    # tak_server_port / takServerPort
    try:
        tak_port = int(cfg.get("tak_server_port") or net.get("takServerPort") or 8089)
    except (TypeError, ValueError):
        tak_port = 8089

    # tak_connection_type / takConnectionType  (udp | tcp | ssl)
    tak_type = cfg.get("tak_connection_type") or net.get("takConnectionType", "udp")

    # tak_username / tak_password — credentials for TAK server authentication
    tak_username = cfg.get("tak_username", "")
    tak_password = cfg.get("tak_password", "")

    # tak_client_cert_path / tak_client_key_path — client certificate for mutual TLS (mTLS).
    # Required when the TAK server issues TLSV13_ALERT_CERTIFICATE_REQUIRED.
    tak_client_cert_path = cfg.get("tak_client_cert_path", "")
    tak_client_key_path  = cfg.get("tak_client_key_path", "")

    raw_host = str(tak_host).strip() if tak_host else ""
    if raw_host:
        raw_host = re.sub(r'^https?://', '', raw_host).rstrip('/')

    return {
        "tak_forward_enabled":      bool(tak_enabled),
        "tak_server_host":          raw_host,
        "tak_server_port":          tak_port,
        "tak_connection_type":      tak_type,
        "tak_username":             str(tak_username).strip() if tak_username else "",
        "tak_password":             str(tak_password) if tak_password else "",
        "tak_client_cert_path":     str(tak_client_cert_path).strip() if tak_client_cert_path else "",
        "tak_client_key_path":      str(tak_client_key_path).strip() if tak_client_key_path else "",
    }


def _build_tak_auth_xml(username: str, password: str) -> bytes:
    """Build a TAK server XML authentication packet from the given credentials."""
    _attr_extras = {'"': "&quot;"}
    u = _sax_utils.escape(username, _attr_extras)
    p = _sax_utils.escape(password, _attr_extras)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<auth><cot username="{u}" password="{p}"/></auth>'
    ).encode("utf-8")


def _build_tak_ssl_context(tak_cfg: dict):
    """
    Build an SSL context for outbound TAK server connections.

    TAK servers commonly use self-signed certificates; server certificate
    verification is intentionally disabled to allow field-deployed connections.
    When the TAK server requires mutual TLS (client certificate), the paths
    configured via ``tak_client_cert_path`` and ``tak_client_key_path`` are
    loaded into the context.  This resolves the
    ``TLSV13_ALERT_CERTIFICATE_REQUIRED`` error raised by servers that enforce
    mTLS (e.g. ascl-atak.duckdns.org:8089).
    """
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2

    cert_path = tak_cfg.get("tak_client_cert_path", "")
    key_path  = tak_cfg.get("tak_client_key_path", "")
    if cert_path and key_path:
        try:
            ctx.load_cert_chain(certfile=cert_path, keyfile=key_path)
            logger.debug("TAK mTLS: loaded client cert %s", cert_path)
        except Exception as _cert_err:
            logger.warning(
                "TAK mTLS: could not load client cert %s / key %s: %s",
                cert_path, key_path, _cert_err,
            )
    elif cert_path or key_path:
        missing = "tak_client_key_path" if cert_path else "tak_client_cert_path"
        present = "tak_client_cert_path" if cert_path else "tak_client_key_path"
        logger.warning(
            "TAK mTLS: both paths required but only %s is set; %s is missing – skipping client cert.",
            present, missing,
        )
    return ctx


def _build_cot_ping_xml() -> str:
    """Build a minimal CoT t-x-c-t ping XML string for server connectivity testing."""
    from datetime import timedelta
    now = datetime.now(timezone.utc)
    stale = now + timedelta(seconds=30)
    fmt = lambda dt: dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    uid = f"LPU5-PING-{uuid.uuid4().hex[:8].upper()}"
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<event version="2.0" uid="{uid}" type="t-x-c-t" how="m-g"'
        f' time="{fmt(now)}" start="{fmt(now)}" stale="{fmt(stale)}">'
        '<point lat="0.0" lon="0.0" hae="0.0" ce="9999999.0" le="9999999.0"/>'
        '<detail/></event>'
    )


# Fixed UID used for LPU5's SA beacon so the TAK server recognises the gateway
# as a persistent entity across reconnects.
_LPU5_COT_UID = "LPU5-GW"


def _build_lpu5_sa_xml() -> str:
    """Build a CoT SA (Situational Awareness) beacon that identifies LPU5 to the TAK server.

    Sending this event immediately after connecting (and optionally after auth)
    announces LPU5 as a named entity on the TAK network.  Without this
    announcement many TAK server implementations (including ATAK in server mode)
    treat the sender as anonymous and route subsequent CoT events only to
    specific users rather than broadcasting to all connected clients
    (including WinTAK users).
    """
    from datetime import timedelta
    now = datetime.now(timezone.utc)
    stale = now + timedelta(minutes=5)
    fmt = lambda dt: dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<event version="2.0" uid="{_LPU5_COT_UID}" type="a-f-G-U-C" how="m-g"'
        f' time="{fmt(now)}" start="{fmt(now)}" stale="{fmt(stale)}">'
        '<point lat="0.0" lon="0.0" hae="0.0" ce="9999999.0" le="9999999.0"/>'
        '<detail>'
        f'<contact callsign="{_LPU5_COT_UID}"/>'
        '<__group name="Cyan" role="Team Member"/>'
        '</detail>'
        '</event>'
    )


def forward_cot_to_tak(cot_xml: str) -> bool:
    """
    Forward a CoT XML string to the configured ATAK/TAK server.

    Supports UDP (legacy port 4242), TCP (standard port 8087), and SSL/TLS
    (secure port 8089) based on the tak_connection_type configuration.
    When tak_username and tak_password are configured, an XML auth packet is
    sent before the CoT payload on TCP and SSL connections.
    Returns True on success, False if forwarding is disabled or an error occurs.
    """
    try:
        tak_cfg = _get_tak_config()
        if not tak_cfg["tak_forward_enabled"] or not tak_cfg["tak_server_host"]:
            return False

        host = tak_cfg["tak_server_host"]
        port = tak_cfg["tak_server_port"]
        conn_type = tak_cfg.get("tak_connection_type", "udp")
        data = cot_xml.encode("utf-8")
        username = tak_cfg.get("tak_username", "")
        password = tak_cfg.get("tak_password", "")
        if bool(username) != bool(password):
            logger.warning("TAK server has only partial credentials configured (username=%s, password_set=%s); authentication will be skipped", bool(username), bool(password))
        auth_data = _build_tak_auth_xml(username, password) if (username and password) else None

        # For TCP/SSL prefer the persistent receiver socket: it is already
        # authenticated and announced via SA beacon, so the TAK server
        # attributes the CoT to a known entity and broadcasts it to ALL
        # connected clients (including WinTAK) rather than treating it as an
        # anonymous one-shot packet that may only reach specific users.
        if conn_type in ("tcp", "ssl"):
            # Obtain the socket reference under its own lock, then release
            # before acquiring _TAK_SEND_LOCK to avoid nested-lock deadlocks.
            with _TAK_SOCKET_LOCK:
                persistent_sock = _TAK_SOCKET
            if persistent_sock is not None:
                with _TAK_SEND_LOCK:
                    try:
                        persistent_sock.sendall(data)
                        logger.info("Forwarded CoT to TAK server %s:%s (%s, %d bytes)", host, port, conn_type, len(data))
                        with _TAK_RECEIVER_STATS_LOCK:
                            _TAK_RECEIVER_STATS["packets_sent"] += 1
                        return True
                    except Exception as _send_err:
                        logger.warning(
                            "CoT send via persistent TAK socket failed: %s; opening new connection",
                            _send_err,
                        )

        if conn_type == "tcp":
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                sock.settimeout(_TAK_SOCKET_TIMEOUT)
                sock.connect((host, port))
                if auth_data:
                    sock.sendall(auth_data)
                sock.sendall(data)
            except socket.timeout:
                logger.warning("CoT TCP forward to TAK server %s:%s timed out", host, port)
                return False
            except (socket.gaierror, ConnectionRefusedError, OSError) as e:
                logger.warning("CoT TCP forward to TAK server %s:%s failed: %s", host, port, e)
                return False
            finally:
                sock.close()
        elif conn_type == "ssl":
            ctx = _build_tak_ssl_context(tak_cfg)
            raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            raw.settimeout(_TAK_SOCKET_TIMEOUT)
            sock = ctx.wrap_socket(raw, server_hostname=host)
            try:
                sock.connect((host, port))
                if auth_data:
                    sock.sendall(auth_data)
                sock.sendall(data)
            except socket.timeout:
                logger.warning("CoT SSL forward to TAK server %s:%s timed out", host, port)
                return False
            except (socket.gaierror, ConnectionRefusedError, ssl.SSLError, OSError) as e:
                if isinstance(e, ssl.SSLError) and "CERTIFICATE_REQUIRED" in str(e).upper():
                    logger.warning(
                        "CoT SSL forward to %s:%s failed – the server requires a client certificate "
                        "(TLSV13_ALERT_CERTIFICATE_REQUIRED). "
                        "For WinTAK/ATAK running on the same machine use connection type 'tcp' with "
                        "port 8087 (no certificate needed). "
                        "For remote SSL servers configure tak_client_cert_path / tak_client_key_path "
                        "in the TAK Server settings.",
                        host, port,
                    )
                else:
                    logger.warning("CoT SSL forward to TAK server %s:%s failed: %s", host, port, e)
                return False
            finally:
                sock.close()
        else:
            # UDP (default/legacy TAK protocol on port 4242)
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                sock.settimeout(_TAK_SOCKET_TIMEOUT)
                sock.sendto(data, (host, port))
            except socket.timeout:
                logger.warning("CoT UDP forward to TAK server %s:%s timed out", host, port)
                return False
            except (socket.gaierror, OSError) as e:
                logger.warning("CoT UDP forward DNS/address error for %s:%s: %s", host, port, e)
                return False
            finally:
                sock.close()

        logger.info("Forwarded CoT to TAK server %s:%s (%s, %d bytes)", host, port, conn_type, len(data))
        with _TAK_RECEIVER_STATS_LOCK:
            _TAK_RECEIVER_STATS["packets_sent"] += 1
        return True
    except Exception as e:
        logger.warning("Failed to forward CoT to TAK server: %s", e)
        return False


def _forward_cot_multicast(cot_xml: str) -> bool:
    """
    Send a CoT XML string to the SA Multicast group via the active CoT listener service.

    This is used to push LPU5 marker updates to WinTAK/ATAK on the same LAN or
    the same Windows machine using the standard SA Multicast address 239.2.3.1:6969.
    Returns True on success, False when multicast is disabled or not available.
    """
    with _cot_listener_lock:
        svc = _cot_listener_service
    if svc is None or not svc.multicast_enabled:
        return False
    return svc.send_multicast(cot_xml)



_TAK_RECEIVER_THREAD: Optional[threading.Thread] = None
_TAK_RECEIVER_STOP = threading.Event()
_TAK_SOCKET = None
_TAK_SOCKET_LOCK = threading.Lock()
_TAK_SEND_LOCK = threading.Lock()  # serialises concurrent writes to _TAK_SOCKET
_TAK_RECEIVER_STATS: Dict[str, Any] = {
    "connected": False,
    "packets_sent": 0,
    "packets_received": 0,
    "parse_errors": 0,
    "last_error": None,
    "connected_since": None,
}
_TAK_RECEIVER_STATS_LOCK = threading.Lock()


def _process_incoming_cot(cot_xml: str) -> None:
    """Parse an incoming CoT XML event, upsert a MapMarker, and broadcast to WebSocket clients."""
    import xml.etree.ElementTree as _ET
    try:
        root = _ET.fromstring(cot_xml)
        if root.tag != "event":
            return

        uid = root.get("uid")
        if not uid:
            return
        event_type = root.get("type", "")

        # Only process unit/marker types; skip ping-acks and other system types
        relevant_prefixes = ("a-f", "a-h", "a-n", "a-u", "b-m-p")
        if not any(event_type.startswith(p) for p in relevant_prefixes):
            return

        point = root.find("point")
        if point is None:
            return
        try:
            lat = float(point.get("lat", 0))
            lng = float(point.get("lon", 0))
        except (TypeError, ValueError):
            return

        # Basic coordinate validation
        if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lng <= 180.0):
            return

        # Extract callsign from detail/contact
        detail = root.find("detail")
        callsign = uid
        if detail is not None:
            contact = detail.find("contact")
            if contact is not None:
                callsign = contact.get("callsign") or callsign

        # Map CoT type to LPU5 internal type
        if AUTONOMOUS_MODULES_AVAILABLE:
            lpu5_type = CoTProtocolHandler.cot_type_to_lpu5(event_type)
        else:
            if event_type.startswith("a-f"):
                lpu5_type = "friendly"
            elif event_type.startswith("a-h"):
                lpu5_type = "hostile"
            elif event_type.startswith("a-n"):
                lpu5_type = "neutral"
            else:
                lpu5_type = "unknown"

        # Upsert MapMarker
        db = SessionLocal()
        try:
            marker = db.query(MapMarker).filter(MapMarker.id == uid).first()
            if marker:
                marker.lat = lat
                marker.lng = lng
                marker.name = callsign
                marker.type = lpu5_type
                new_data = dict(marker.data) if marker.data else {}
                new_data["cot_type"] = event_type
                marker.data = new_data
                flag_modified(marker, "data")
            else:
                if uid.startswith("mesh-"):
                    # ATAK is echoing back a Meshtastic node we forwarded; skip
                    # creating a duplicate marker to avoid double rendering.
                    return
                marker = MapMarker(
                    id=uid,
                    name=callsign,
                    lat=lat,
                    lng=lng,
                    type=lpu5_type,
                    created_by="tak_server",
                    data={"cot_type": event_type},
                )
                db.add(marker)
            db.commit()

            # Broadcast to WebSocket clients
            broadcast_websocket_update("markers", "tak_unit_update", {
                "id": uid,
                "name": callsign,
                "callsign": callsign,
                "lat": lat,
                "lng": lng,
                "type": lpu5_type,
                "cot_type": event_type,
                "created_by": "tak_server",
            })
            logger.info("TAK event received: %s (%s) @ %.6f, %.6f", callsign, event_type, lat, lng)
        finally:
            db.close()

        with _TAK_RECEIVER_STATS_LOCK:
            _TAK_RECEIVER_STATS["packets_received"] += 1

    except Exception as e:
        logger.debug("Failed to parse incoming CoT: %s", e)
        with _TAK_RECEIVER_STATS_LOCK:
            _TAK_RECEIVER_STATS["parse_errors"] += 1


def _tak_receiver_loop() -> None:
    """Background thread that maintains a persistent TAK server connection and receives CoT events."""
    backoff_delays = [5, 10, 30, 60]
    attempt = 0
    while not _TAK_RECEIVER_STOP.is_set():
        try:
            tak_cfg = _get_tak_config()
            if not tak_cfg["tak_forward_enabled"] or not tak_cfg["tak_server_host"]:
                for _ in range(10):
                    if _TAK_RECEIVER_STOP.is_set():
                        return
                    time.sleep(1)
                continue

            host = tak_cfg["tak_server_host"]
            port = tak_cfg["tak_server_port"]
            conn_type = tak_cfg.get("tak_connection_type", "ssl")

            if conn_type not in ("tcp", "ssl"):
                # UDP is send-only; no persistent receiver possible
                for _ in range(10):
                    if _TAK_RECEIVER_STOP.is_set():
                        return
                    time.sleep(1)
                continue

            sock = None
            try:
                if conn_type == "ssl":
                    ctx = _build_tak_ssl_context(tak_cfg)
                    raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    raw.settimeout(_TAK_SOCKET_TIMEOUT)
                    sock = ctx.wrap_socket(raw, server_hostname=host)
                else:
                    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    sock.settimeout(_TAK_SOCKET_TIMEOUT)

                sock.connect((host, port))
                sock.settimeout(30.0)  # Blocking recv with timeout for clean shutdown

                with _TAK_RECEIVER_STATS_LOCK:
                    _TAK_RECEIVER_STATS["connected"] = True
                    _TAK_RECEIVER_STATS["connected_since"] = datetime.now(timezone.utc).isoformat()
                    _TAK_RECEIVER_STATS["last_error"] = None

                logger.info("TAK receiver connected to %s:%s (%s)", host, port, conn_type.upper())
                attempt = 0  # Reset backoff on successful connection

                # Send auth if configured
                username = tak_cfg.get("tak_username", "")
                password = tak_cfg.get("tak_password", "")
                if username and password:
                    auth_data = _build_tak_auth_xml(username, password)
                    sock.sendall(auth_data)

                # Announce LPU5 as a named SA entity so the TAK server knows our
                # identity and broadcasts subsequent CoT events to ALL connected
                # clients (including WinTAK) rather than routing as anonymous.
                sock.sendall(_build_lpu5_sa_xml().encode("utf-8"))
                logger.info("TAK: sent SA beacon as %s", _LPU5_COT_UID)

                # Expose the socket for forward_cot_to_tak only after auth and
                # SA are sent, eliminating a race with concurrent sends.
                with _TAK_SOCKET_LOCK:
                    global _TAK_SOCKET
                    _TAK_SOCKET = sock

                # Receive loop
                buf = b""
                while not _TAK_RECEIVER_STOP.is_set():
                    try:
                        chunk = sock.recv(_TAK_RECV_BUFFER)
                        if not chunk:
                            logger.warning("TAK server closed connection")
                            break
                        buf += chunk
                        # Process all complete CoT events in the buffer.
                        # Search for both the opening <event and closing </event>
                        # so partial or out-of-band data before the tag is discarded.
                        while b"</event>" in buf:
                            start = buf.find(b"<event")
                            end = buf.find(b"</event>")
                            if start == -1 or start > end:
                                # Discard leading garbage up through this </event>
                                buf = buf[end + 8:]
                                continue
                            packet = buf[start:end + 8]
                            buf = buf[end + 8:]
                            _process_incoming_cot(packet.decode("utf-8", errors="ignore"))
                    except socket.timeout:
                        continue  # No data yet; check stop event and retry
                    except (OSError, Exception) as recv_err:
                        if not _TAK_RECEIVER_STOP.is_set():
                            logger.error("TAK receiver read error: %s", recv_err)
                        break

            except (socket.timeout, socket.gaierror, ConnectionRefusedError, ssl.SSLError, OSError) as conn_err:
                if isinstance(conn_err, ssl.SSLError) and "CERTIFICATE_REQUIRED" in str(conn_err).upper():
                    logger.warning(
                        "TAK receiver connection to %s:%s failed – the server requires a client "
                        "certificate (TLSV13_ALERT_CERTIFICATE_REQUIRED). "
                        "For WinTAK/ATAK on the same machine switch to connection type 'tcp' with "
                        "port 8087 (no certificate needed). "
                        "For remote SSL servers configure tak_client_cert_path / tak_client_key_path.",
                        host, port,
                    )
                else:
                    logger.warning("TAK receiver connection to %s:%s failed: %s", host, port, conn_err)
                with _TAK_RECEIVER_STATS_LOCK:
                    _TAK_RECEIVER_STATS["last_error"] = str(conn_err)

        except Exception as outer_err:
            logger.warning("TAK receiver unexpected error: %s", outer_err)
            with _TAK_RECEIVER_STATS_LOCK:
                _TAK_RECEIVER_STATS["last_error"] = str(outer_err)

        finally:
            with _TAK_SOCKET_LOCK:
                if _TAK_SOCKET is not None:
                    try:
                        _TAK_SOCKET.close()
                    except Exception:
                        pass
                    _TAK_SOCKET = None
            with _TAK_RECEIVER_STATS_LOCK:
                _TAK_RECEIVER_STATS["connected"] = False
                _TAK_RECEIVER_STATS["connected_since"] = None

        if _TAK_RECEIVER_STOP.is_set():
            break

        delay = backoff_delays[min(attempt, len(backoff_delays) - 1)]
        attempt += 1
        logger.info("TAK receiver reconnecting in %ss (attempt %s)", delay, attempt)
        for _ in range(delay):
            if _TAK_RECEIVER_STOP.is_set():
                break
            time.sleep(1)

    logger.info("TAK receiver thread stopped")


def _start_tak_receiver_thread() -> bool:
    """Start the TAK receiver background thread. Returns True if started or already running."""
    global _TAK_RECEIVER_THREAD
    if _TAK_RECEIVER_THREAD is not None and _TAK_RECEIVER_THREAD.is_alive():
        logger.debug("TAK receiver thread already running")
        return True
    _TAK_RECEIVER_STOP.clear()
    _TAK_RECEIVER_THREAD = threading.Thread(
        target=_tak_receiver_loop,
        daemon=True,
        name="tak-receiver",
    )
    _TAK_RECEIVER_THREAD.start()
    logger.info("TAK receiver thread started")
    return True


def _stop_tak_receiver_thread() -> None:
    """Stop the TAK receiver background thread gracefully."""
    global _TAK_RECEIVER_THREAD
    _TAK_RECEIVER_STOP.set()
    with _TAK_SOCKET_LOCK:
        if _TAK_SOCKET is not None:
            try:
                _TAK_SOCKET.close()
            except Exception:
                pass
    if _TAK_RECEIVER_THREAD is not None and _TAK_RECEIVER_THREAD.is_alive():
        _TAK_RECEIVER_THREAD.join(timeout=5)
    _TAK_RECEIVER_THREAD = None
    logger.info("TAK receiver thread stopped")


def _is_tak_connected() -> bool:
    """Return True if the TAK receiver has an active socket connection."""
    with _TAK_RECEIVER_STATS_LOCK:
        return bool(_TAK_RECEIVER_STATS.get("connected"))


def _get_tak_connection_stats() -> dict:
    """Return a copy of the TAK receiver statistics dict."""
    with _TAK_RECEIVER_STATS_LOCK:
        return dict(_TAK_RECEIVER_STATS)


def _forward_all_lpu5_data_to_tak() -> dict:
    """
    Forward all existing LPU5 map markers to the configured TAK server.

    Called on startup when TAK integration is enabled to ensure the TAK server
    receives the current state of all markers.  Skips markers that cannot be
    converted to a valid CoT event (e.g. incomplete coordinate data).

    Returns a dict with counts: forwarded, skipped, failed.
    """
    if not AUTONOMOUS_MODULES_AVAILABLE:
        logger.debug("_forward_all_lpu5_data_to_tak: skipped — autonomous modules not available")
        return {"forwarded": 0, "skipped": 0, "failed": 0}
    tak_cfg = _get_tak_config()
    if not tak_cfg["tak_forward_enabled"] or not tak_cfg["tak_server_host"]:
        return {"forwarded": 0, "skipped": 0, "failed": 0}

    forwarded = 0
    skipped = 0
    failed = 0

    db = SessionLocal()
    try:
        markers = db.query(MapMarker).filter(MapMarker.created_by != "tak_server").all()
        for marker in markers:
            try:
                marker_dict = {
                    "id": marker.id,
                    "name": marker.name,
                    "lat": marker.lat,
                    "lng": marker.lng,
                    "type": marker.type,
                    "created_by": marker.created_by,
                }
                if isinstance(marker.data, dict):
                    for k, v in marker.data.items():
                        if k not in marker_dict:
                            marker_dict[k] = v
                cot_event = CoTProtocolHandler.marker_to_cot(marker_dict)
                if cot_event:
                    if forward_cot_to_tak(cot_event.to_xml()):
                        forwarded += 1
                    else:
                        failed += 1
                else:
                    skipped += 1
            except Exception as _fwd_err:
                logger.debug("Failed to forward marker %s to TAK: %s", marker.id, _fwd_err)
                failed += 1
    finally:
        db.close()

    logger.info(
        "_forward_all_lpu5_data_to_tak: forwarded=%d skipped=%d failed=%d",
        forwarded, skipped, failed,
    )
    return {"forwarded": forwarded, "skipped": skipped, "failed": failed}

# -------------------------
# Background meshtastic -> markers sync
# -------------------------
_MESHTASTIC_SYNC_THREAD = None
_MESHTASTIC_SYNC_STOP_EVENT = threading.Event()
# created_by values used by meshtastic code paths — used to filter meshtastic markers from general endpoints
_MESHTASTIC_CREATED_BY = {"import_meshtastic", "meshtastic_sync", "ingest_node"}

def _forward_meshtastic_node_to_tak(node_id: str, name: str, lat: float, lng: float) -> bool:
    """
    Convert a single Meshtastic node position to a CoT friendly-unit event and
    forward it to the configured TAK server.  Returns True on successful forward.
    Nodes without GPS are forwarded at coordinates (0.0, 0.0) so TAK still
    receives them and can distribute the node identity globally.
    Silently skips only when TAK forwarding is disabled.
    """
    if not AUTONOMOUS_MODULES_AVAILABLE:
        return False
    try:
        marker_dict = {
            "id": f"mesh-{node_id}",
            "name": name,
            "callsign": name,
            "lat": lat,
            "lng": lng,
            "type": "friendly",
            "meshtastic_node": True,
            "node_id": node_id,
            "source": "meshtastic",
        }
        cot_event = CoTProtocolHandler.marker_to_cot(marker_dict)
        if cot_event:
            return forward_cot_to_tak(cot_event.to_xml())
    except Exception as _fwd_err:
        logger.debug("TAK forward for Meshtastic node %s failed: %s", node_id, _fwd_err)
    return False


def sync_meshtastic_nodes_to_map_markers_once():
    """
    One-shot sync: reads meshtastic_nodes from DB and upserts markers into map_markers table.
    Nodes without valid lat/lng will be assigned 0.0, 0.0 per requirement.
    """
    db = SessionLocal()
    try:
        nodes = db.query(MeshtasticNode).all()
        # Index existing markers created by meshtastic sync
        existing_markers = db.query(MapMarker).filter(MapMarker.created_by.in_(["import_meshtastic", "meshtastic_sync"])).all()
        
        by_unit = {str(m.data.get("unit_id") if isinstance(m.data, dict) else ""): m for m in existing_markers if m.data}
        
        created = 0
        updated = 0

        for n in nodes:
            mesh = n.id
            name = n.long_name or n.short_name or mesh or "node"
            lat = n.lat if n.lat is not None else 0.0
            lng = n.lng if n.lng is not None else 0.0

            # find existing
            marker = by_unit.get(str(mesh))
            
            if marker:
                # update
                marker.lat = float(lat)
                marker.lng = float(lng)
                marker.name = f"{n.hardware_model or ''} = {name}"
                marker.type = "node"  # Ensure type is "node" not "friendly"
                marker_data = marker.data if isinstance(marker.data, dict) else {}
                marker_data["updated_at"] = datetime.now(timezone.utc).isoformat()
                marker.data = marker_data
                updated += 1
            else:
                new_marker = MapMarker(
                    id=str(uuid.uuid4()),
                    lat=float(lat),
                    lng=float(lng),
                    name=f"{n.hardware_model or ''} = {name}",
                    type="node",
                    created_by="import_meshtastic",
                    created_at=datetime.now(timezone.utc),
                    data={"unit_id": mesh}
                )
                db.add(new_marker)
                created += 1

        db.commit()
        logger.info("sync_meshtastic_nodes_to_map_markers_once completed: created=%d updated=%d", created, updated)

        # Forward nodes with valid GPS to the TAK server so TAK can distribute them globally.
        forwarded = sum(
            1 for n in nodes
            if _forward_meshtastic_node_to_tak(
                str(n.id),
                n.long_name or n.short_name or str(n.id) or "node",
                n.lat if n.lat is not None else 0.0,
                n.lng if n.lng is not None else 0.0,
            )
        )
        if forwarded:
            logger.info("Meshtastic sync: forwarded %d/%d nodes to TAK server", forwarded, len(nodes))

        return {"status": "success", "created": created, "updated": updated}
    except Exception as e:
        db.rollback()
        logger.exception("sync error: %s", e)
        return {"status": "error", "message": str(e)}
    finally:
        db.close()

def _meshtastic_sync_worker(interval_seconds: int = 30):
    logger.info("Meshtastic sync worker started (interval=%s s)", interval_seconds)
    while not _MESHTASTIC_SYNC_STOP_EVENT.is_set():
        try:
            sync_meshtastic_nodes_to_map_markers_once()
        except Exception as e:
            logger.exception("Error in meshtastic sync worker: %s", e)
        for _ in range(max(1, int(interval_seconds))):
            if _MESHTASTIC_SYNC_STOP_EVENT.is_set():
                break
            time.sleep(1)
    logger.info("Meshtastic sync worker stopped")

# Periodic marker broadcast for real-time sync
_MARKER_BROADCAST_THREAD = None
_MARKER_BROADCAST_STOP_EVENT = threading.Event()

def _marker_broadcast_worker(interval_seconds: int = 60):
    """
    Periodic marker broadcast worker for real-time sync.
    Broadcasts all map markers and overlays periodically to ensure all clients stay synchronized.
    """
    logger.info("Marker broadcast worker started (interval=%s s)", interval_seconds)
    while not _MARKER_BROADCAST_STOP_EVENT.is_set():
        db = SessionLocal()
        try:
            # Broadcast all map markers
            markers = db.query(MapMarker).all()
            marker_list = [
                {
                    "id": m.id, "lat": m.lat, "lng": m.lng, "name": m.name, 
                    "type": m.type, "color": m.color, "icon": m.icon, 
                    "created_by": m.created_by, "data": m.data,
                    "timestamp": m.created_at.isoformat() if m.created_at else datetime.now(timezone.utc).isoformat()
                } for m in markers
            ]
            if marker_list:
                broadcast_websocket_update("markers", "markers_sync", {"markers": marker_list, "sync_type": "periodic"})
                logger.debug("Broadcasted %s markers for periodic sync", len(marker_list))
            
            # Broadcast all overlays for sync
            overlays = db.query(Overlay).all()
            overlay_list = [
                {
                    "id": o.id, "name": o.name, "data": o.data,
                    "created_by": o.created_by,
                    "timestamp": o.created_at.isoformat() if o.created_at else datetime.now(timezone.utc).isoformat()
                } for o in overlays
            ]
            if overlay_list:
                broadcast_websocket_update("overlays", "overlays_sync", {"overlays": overlay_list, "sync_type": "periodic"})
                logger.debug("Broadcasted %s overlays for periodic sync", len(overlay_list))
                
        except Exception as e:
            logger.exception("Error in marker broadcast worker: %s", e)
        finally:
            db.close()
        
        # Sleep in small intervals to allow for clean shutdown
        for _ in range(max(1, interval_seconds)):
            if _MARKER_BROADCAST_STOP_EVENT.is_set():
                break
            time.sleep(1)
    logger.info("Marker broadcast worker stopped")


# Periodic TAK sync worker
_TAK_PERIODIC_SYNC_THREAD = None
_TAK_PERIODIC_SYNC_STOP_EVENT = threading.Event()

def _tak_periodic_sync_worker(interval_seconds: int = 60):
    """
    Periodic worker that forwards all LPU5 map markers to the TAK server every
    *interval_seconds* seconds.  Runs as a background daemon thread while the
    application is alive.
    """
    logger.info("TAK periodic sync worker started (interval=%s s)", interval_seconds)
    while not _TAK_PERIODIC_SYNC_STOP_EVENT.is_set():
        for _ in range(max(1, interval_seconds)):
            if _TAK_PERIODIC_SYNC_STOP_EVENT.is_set():
                break
            time.sleep(1)
        if _TAK_PERIODIC_SYNC_STOP_EVENT.is_set():
            break
        try:
            _forward_all_lpu5_data_to_tak()
        except Exception as _sync_err:
            logger.warning("TAK periodic sync error: %s", _sync_err)
    logger.info("TAK periodic sync worker stopped")


# -------------------------
# Authentication endpoints
# -------------------------
@app.post("/api/login_user")
async def login_user(data: dict = Body(...), request: Request = None, db: Session = Depends(get_db)):
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    if not username or not password:
        raise HTTPException(status_code=400, detail="Username and password required")
    
    user = db.query(User).filter(User.username == username).first()
    if not user or not verify_password(password, user.password_hash):
        log_audit("login_failed", "system", {"username": username})
        raise HTTPException(status_code=401, detail="Invalid credentials")
    
    if not user.is_active:
        raise HTTPException(status_code=403, detail="User account is inactive")
    
    # Update last login in data JSON
    current_data = user.data if user.data else {}
    current_data["last_login"] = datetime.now(timezone.utc).isoformat()
    user.data = current_data
    db.commit()
    
    log_audit("login_success", user.id, {"username": username})

    token = generate_token(user.id, user.username)
    expires_at = (datetime.now(timezone.utc) + timedelta(hours=JWT_EXPIRATION_HOURS)).isoformat()
    client_ip = request.client.host if request and request.client else ""
    session_obj = {
        "id": str(uuid.uuid4()),
        "token": token,
        "user_id": user.id,
        "username": user.username,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "expires_at": expires_at,
        "ip": client_ip,
        "ips": [client_ip],
        "last_ip": client_ip,
        "last_seen": datetime.now(timezone.utc).isoformat(),
        "language": (user.data or {}).get("language", "de")
    }
    save_session(db, session_obj)

    user_info = {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "role": user.role,
        "group_id": user.group_id,
        "unit": user.unit,
        "device": user.device,
        "rank": user.rank,
        "fullname": user.fullname,
        "callsign": user.callsign,
        "is_active": user.is_active,
        "data": user.data
    }

    return {"status": "success", "user": user_info, "token": token, "expires_at": expires_at}

@app.post("/api/logout")
async def logout(data: dict = Body(...)):
    token = data.get("token")
    if not token:
        raise HTTPException(status_code=400, detail="token required")
    remove_session_by_token(token)
    log_audit("logout", "system", {"token": token})
    return {"status": "success", "message": "Logged out"}

# New: /api/me endpoint - returns user info based on Authorization header token
@app.get("/api/me")
def api_me(authorization: Optional[str] = Header(None), db: Session = Depends(get_db)):
    """
    Returns user info for current token.
    Accepts Authorization: Bearer <token> header.
    """
    token = None
    if authorization:
        if authorization.lower().startswith("bearer "):
            token = authorization[7:].strip()
        else:
            token = authorization.strip()
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    
    payload = verify_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid token")
    
    user = db.query(User).filter(User.id == payload.get("user_id")).first()
    if not user:
        user = db.query(User).filter(User.username == payload.get("username")).first()
    
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    unit_name = user.unit
    if user.unit_id:
        unit_obj = db.query(Unit).filter(Unit.id == user.unit_id).first()
        unit_name = unit_obj.name if unit_obj else user.unit

    return {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "role": user.role,
        "group_id": user.group_id,
        "unit": unit_name,
        "unit_id": user.unit_id,
        "device": user.device,
        "rank": user.rank,
        "fullname": user.fullname,
        "callsign": user.callsign,
        "is_active": user.is_active,
        "data": user.data
    }

# -------------------------
# Permission check endpoints
# -------------------------
@app.post("/api/permissions/check")
async def check_permission(data: dict = Body(...), authorization: Optional[str] = Header(None)):
    """
    Check if current user has a specific permission.
    Body: { "permission": "users.create" }
    
    NOTE: Permission system disabled - all authenticated users have all permissions.
    """
    # Permission system removed - all users have full access
    return {"has_permission": True, "permission": data.get("permission", "")}

@app.get("/api/permissions/user")
async def get_user_permissions(authorization: Optional[str] = Header(None), db: Session = Depends(get_db)):
    """
    Get all permissions for the current user.
    """
    token = None
    if authorization:
        if authorization.lower().startswith("bearer "):
            token = authorization[7:].strip()
        else:
            token = authorization.strip()
            
    if not token:
        return {"permissions": ["*"], "role": "admin", "username": "unknown"}
        
    payload = verify_token(token)
    if not payload:
        return {"permissions": ["*"], "role": "admin", "username": "unknown"}
        
    user = db.query(User).filter(User.username == payload.get("username")).first()
    
    return {
        "permissions": ["*"],  # All permissions granted
        "role": user.role if user else "admin",
        "username": user.username if user else "unknown"
    }

@app.get("/api/permissions/list")
async def list_all_permissions(authorization: Optional[str] = Header(None)):
    """
    List all available permissions.
    
    NOTE: Permission system disabled - all authenticated users have all permissions.
    """
    # Permission system removed - return empty list as permissions no longer enforced
    return {"permissions": ["*"]}

@app.get("/api/roles")
async def get_roles():
    """
    Get available roles and their hierarchy.
    
    NOTE: Permission system disabled - returning default roles for backward compatibility.
    """
    return {
        "roles": [
            {"name": "admin", "level": 4, "description": "Full system access"},
            {"name": "operator", "level": 3, "description": "Mission and marker management"},
            {"name": "user", "level": 2, "description": "Standard user with self-update"},
            {"name": "guest", "level": 1, "description": "Read-only access to public data"}
        ]
    }

# -------------------------
# Units endpoints (CRUD)
# -------------------------
@app.get("/api/units")
async def list_units(db: Session = Depends(get_db)):
    units = db.query(Unit).order_by(Unit.name).all()
    return [{"id": u.id, "name": u.name, "description": u.description} for u in units]

@app.post("/api/units")
async def create_unit(data: dict = Body(...), authorization: Optional[str] = Header(None), db: Session = Depends(get_db)):
    name = (data.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Unit name required")
    if db.query(Unit).filter(Unit.name == name).first():
        raise HTTPException(status_code=400, detail="Unit name already exists")
    unit = Unit(id=str(uuid.uuid4()), name=name, description=data.get("description", ""))
    db.add(unit)
    db.commit()
    db.refresh(unit)
    log_audit("create_unit", "system", {"unit_id": unit.id, "name": name})
    return {"id": unit.id, "name": unit.name, "description": unit.description}

@app.delete("/api/units/{unit_id}")
async def delete_unit(unit_id: str, authorization: Optional[str] = Header(None), db: Session = Depends(get_db)):
    unit = db.query(Unit).filter((Unit.id == unit_id) | (Unit.name == unit_id)).first()
    if not unit:
        raise HTTPException(status_code=404, detail="Unit not found")
    if unit.name == "General":
        raise HTTPException(status_code=400, detail="Cannot delete the default 'General' unit")
    # Move affected users to "General"
    general = db.query(Unit).filter(Unit.name == "General").first()
    if general:
        db.query(User).filter(User.unit_id == unit.id).update({"unit_id": general.id, "unit": "General"})
    db.delete(unit)
    db.commit()
    log_audit("delete_unit", "system", {"unit_id": unit_id})
    return {"status": "success", "message": f"Unit deleted; affected users moved to General"}

# -------------------------
# Users endpoints (create/update/list/delete)
# -------------------------
@app.get("/api/users")
async def get_users(db: Session = Depends(get_db)):
    users = db.query(User).all()
    # Build unit_id -> name map for efficient lookup
    unit_map = {u.id: u.name for u in db.query(Unit).all()}
    return [
        {
            "id": u.id,
            "username": u.username,
            "email": u.email,
            "role": u.role,
            "group_id": u.group_id,
            "unit": unit_map.get(u.unit_id) or u.unit,
            "unit_id": u.unit_id,
            "device": u.device,
            "rank": u.rank,
            "fullname": u.fullname,
            "callsign": u.callsign,
            "is_active": u.is_active,
            "chat_channels": u.chat_channels if u.chat_channels else ["all"],
            "data": u.data
        } for u in users
    ]

@app.post("/api/users/create")
async def create_user_with_password(data: dict = Body(...), authorization: Optional[str] = Header(None), db: Session = Depends(get_db)):
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    group_id = data.get("group_id", "users")
    role = data.get("role", "user").lower()
    
    if not username:
        raise HTTPException(status_code=400, detail="Username required")
    if not password:
        raise HTTPException(status_code=400, detail="Password required")
    if len(password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters")
    
    if db.query(User).filter(User.username == username).first():
        raise HTTPException(status_code=400, detail="Username already exists")
    
    new_user = User(
        id=str(uuid.uuid4()),
        username=username,
        email=data.get("email", ""),
        password_hash=hash_password(password),
        role=role,
        group_id=group_id,
        is_active=True,
        created_at=datetime.now(timezone.utc),
        unit=data.get("unit"),
        device=data.get("device"),
        rank=data.get("rank"),
        fullname=data.get("fullname"),
        callsign=data.get("callsign"),
        data=data.get("data", {})
    )
    db.add(new_user)
    db.commit()
    db.refresh(new_user)
    
    log_audit("create_user", "system", {"user_id": new_user.id, "role": role})
    return {"status": "success", "user": {k: v for k, v in new_user.__dict__.items() if k != "password_hash" and not k.startswith("_")}}

@app.post("/api/users")
async def create_user_legacy(data: dict = Body(...), db: Session = Depends(get_db)):
    username = (data.get("username") or "").strip()
    email = data.get("email", "")
    group_id = data.get("group_id")
    provided_password = data.get("password", None)
    
    if not username:
        raise HTTPException(status_code=400, detail="Username required")
    
    if db.query(User).filter(User.username == username).first():
        raise HTTPException(status_code=400, detail="Username already exists")
    
    temp_password = None
    if provided_password:
        password_hash = hash_password(provided_password)
    else:
        temp_password = str(uuid.uuid4())[:12]
        password_hash = hash_password(temp_password)
    
    new_user = User(
        id=str(uuid.uuid4()),
        username=username,
        email=email,
        password_hash=password_hash,
        group_id=group_id or "users",
        role="user",
        is_active=True,
        created_at=datetime.now(timezone.utc)
    )
    db.add(new_user)
    db.commit()
    db.refresh(new_user)
    
    log_audit("create_user", "system", {"user_id": new_user.id})
    user_info = {k: v for k, v in new_user.__dict__.items() if k != "password_hash" and not k.startswith("_")}
    resp = {"status": "success", "user": user_info}
    if temp_password:
        resp["temp_password"] = temp_password
    return resp

@app.get("/api/users/{user_id}")
async def get_user(user_id: str, db: Session = Depends(get_db)):
    user = db.query(User).filter((User.id == user_id) | (User.username == user_id)).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    result = {k: v for k, v in user.__dict__.items() if k != "password_hash" and not k.startswith("_")}
    # Include resolved unit name
    if user.unit_id:
        unit_obj = db.query(Unit).filter(Unit.id == user.unit_id).first()
        result["unit"] = unit_obj.name if unit_obj else user.unit
    else:
        result["unit"] = user.unit
    # Ensure chat_channels defaults to ["all"]
    if not result.get("chat_channels"):
        result["chat_channels"] = ["all"]
    return result

@app.put("/api/users/{user_id}")
async def update_user(user_id: str, data: dict = Body(...), authorization: Optional[str] = Header(None), db: Session = Depends(get_db)):
    user = db.query(User).filter((User.id == user_id) | (User.username == user_id)).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    current_user_id = "system"
    try:
        payload = verify_token(authorization)
        if payload:
            current_user_id = payload.get("user_id") or payload.get("username") or "system"
    except Exception:
        pass
    
    if "role" in data:
        new_role = data["role"].lower()
        old_role = user.role
        user.role = new_role
        log_audit("role_changed", current_user_id, {"user_id": user_id, "old_role": old_role, "new_role": new_role})
    
    updatable_fields = ["email", "group_id", "is_active", "unit", "device", "rank", "fullname", "callsign"]
    for field in updatable_fields:
        if field in data:
            if field == "active": # Legacy field name
                 user.is_active = data[field]
            elif field == "unit":
                # Resolve unit name to unit_id
                unit_name = data[field]
                if unit_name:
                    unit_obj = db.query(Unit).filter(Unit.name == unit_name).first()
                    if unit_obj:
                        user.unit_id = unit_obj.id
                        user.unit = unit_obj.name
                    else:
                        # Store as plain string for backward compat
                        user.unit = unit_name
                else:
                    user.unit = unit_name
            else:
                 setattr(user, field, data[field])

    # Handle chat_channels update
    if "chat_channels" in data:
        channels = data["chat_channels"]
        if isinstance(channels, list):
            # Always ensure "all" is included
            if "all" not in channels:
                channels = ["all"] + channels
            old_channels = set(user.chat_channels or ["all"])
            new_channels = set(channels)
            user.chat_channels = channels

            # Sync channel.members for added/removed channels
            added_channels = new_channels - old_channels - {"all"}
            removed_channels = old_channels - new_channels - {"all"}
            affected = added_channels | removed_channels
            if affected:
                for ch in db.query(ChatChannel).filter(ChatChannel.id.in_(affected)).all():
                    members = set(ch.members or [])
                    if ch.id in added_channels:
                        members.add(user.username)
                    else:
                        members.discard(user.username)
                    ch.members = list(members)
    
    # Check for legacy 'active' field
    if "active" in data:
        user.is_active = data["active"]
    
    # Handle 'data' field merges
    if "data" in data and isinstance(data["data"], dict):
        current_extra = user.data or {}
        current_extra.update(data["data"])
        user.data = current_extra

    if "password" in data and data.get("password"):
        user.password_hash = hash_password(data.get("password"))
    
    db.commit()
    db.refresh(user)
    
    # Special handling for language in data
    if "language" in data:
        user_extra = user.data or {}
        user_extra["language"] = data["language"].strip().lower()
        user.data = user_extra
        db.commit()
        update_session_language(user.id, user_extra["language"])
    
    log_audit("update_user", current_user_id, {"user_id": user.id})
    return {"status": "success", "user": {k: v for k, v in user.__dict__.items() if k != "password_hash" and not k.startswith("_")}}

@app.put("/api/users/{user_id}/change-password")
async def change_user_password_admin(user_id: str, data: dict = Body(...), db: Session = Depends(get_db)):
    new_password = data.get("new_password", "").strip()
    if not new_password or len(new_password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters")
    
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    user.password_hash = hash_password(new_password)
    db.commit()
    
    log_audit("admin_change_password", "system", {"user_id": user_id})
    return {"status": "success", "message": "Password changed"}

@app.delete("/api/users/{user_id}")
async def delete_user(user_id: str, authorization: Optional[str] = Header(None), db: Session = Depends(get_db)):
    user_to_delete = db.query(User).filter((User.id == user_id) | (User.username == user_id)).first()
    if not user_to_delete:
        raise HTTPException(status_code=404, detail="User not found")
    
    current_user_id = "system"
    try:
        payload = verify_token(authorization)
        if payload:
            current_user_id = payload.get("user_id") or payload.get("username") or "system"
    except:
        pass

    db.delete(user_to_delete)
    db.commit()
    
    log_audit("delete_user", current_user_id, {"user_id": user_id})
    return {"status": "success", "message": "User deleted"}

# -------------------------
# Pending registration endpoints
# -------------------------
@app.post("/api/register_user")
async def register_user(data: dict = Body(...), db: Session = Depends(get_db)):
    username = (data.get("username") or "").strip()
    password = data.get("password")
    unit = data.get("unit") or data.get("device") or None
    callsign = data.get("callsign") or None
    qr_token = data.get("qr_token")
    if not username or not password:
        raise HTTPException(status_code=400, detail="Required fields missing (username, password)")
    
    # Resolve unit: accept unit name from registration form; fall back to "General"
    unit_name = unit or "General"
    unit_obj = db.query(Unit).filter(Unit.name == unit_name).first()
    if not unit_obj:
        # Fall back to General if provided name is unknown
        unit_obj = db.query(Unit).filter(Unit.name == "General").first()
        unit_name = unit_obj.name if unit_obj else unit_name
    
    if qr_token:
        qr = db.query(QRCode).filter(QRCode.token == qr_token).first()
        if not qr:
            raise HTTPException(status_code=400, detail="Invalid QR token")
        if qr.max_uses > 0 and qr.uses >= qr.max_uses:
            raise HTTPException(status_code=400, detail="QR token max uses exceeded")
        if qr.expires_at and datetime.now(timezone.utc) > qr.expires_at:
            raise HTTPException(status_code=400, detail="QR token expired")
        
        qr.uses += 1
        db.commit()

    # Check if user already exists
    if db.query(User).filter(User.username == username).first():
        raise HTTPException(status_code=400, detail="Username already exists")
        
    # Check if registration already pending
    if db.query(PendingRegistration).filter(PendingRegistration.username == username).first():
        raise HTTPException(status_code=400, detail="Registration already pending")

    registration = PendingRegistration(
        id=str(uuid.uuid4()),
        token=str(uuid.uuid4()),
        username=username,
        password_hash=hash_password(password),
        email=data.get("email", ""),
        fullname=data.get("fullname", ""),
        callsign=callsign,
        data={
            "unit": unit_name,
            "unit_id": unit_obj.id if unit_obj else None,
            "device": data.get("device") or unit,
            "rank": data.get("rank", "Operator"),
            "qr_token": qr_token,
            "status": "PENDING"
        }
    )
    db.add(registration)
    db.commit()
    
    log_audit("user_registration", "system", {"username": username})
    return {"status": "success", "message": "Registration pending approval"}

@app.get("/api/pending_registrations")
def get_pending_registrations(db: Session = Depends(get_db)):
    pending = db.query(PendingRegistration).all()
    out = []
    for p in pending:
        d = dict(p.data or {})
        d.update({
            "id": p.id,
            "username": p.username,
            "email": p.email,
            "fullname": p.fullname,
            "callsign": p.callsign,
            "created_at": p.created_at.isoformat()
        })
        out.append(d)
    return out

@app.post("/api/approve_registration")
async def approve_registration(data: dict = Body(...), db: Session = Depends(get_db)):
    username = (data.get("username") or "").strip()
    if not username:
        raise HTTPException(status_code=400, detail="username required")
    
    reg = db.query(PendingRegistration).filter(PendingRegistration.username == username).first()
    if not reg:
        raise HTTPException(status_code=404, detail="Registration not found")
    
    reg_data = reg.data or {}
    # Resolve unit_id
    unit_name = reg_data.get("unit")
    unit_id = reg_data.get("unit_id")
    if unit_name and not unit_id:
        unit_obj = db.query(Unit).filter(Unit.name == unit_name).first()
        unit_id = unit_obj.id if unit_obj else None
    # Fall back to General if still no unit
    if not unit_id:
        general = db.query(Unit).filter(Unit.name == "General").first()
        unit_id = general.id if general else None
        unit_name = "General" if general else unit_name
    new_user = User(
        id=str(uuid.uuid4()),
        username=reg.username,
        password_hash=reg.password_hash,
        unit=unit_name,
        unit_id=unit_id,
        device=reg_data.get("device"),
        callsign=reg.callsign,
        rank=reg_data.get("rank", "Operator"),
        email=reg.email,
        fullname=reg.fullname,
        group_id="users",
        role="user",
        is_active=True,
        created_at=datetime.now(timezone.utc),
        data={"legacy_id": reg.id}
    )
    db.add(new_user)
    db.delete(reg)
    db.commit()
    db.refresh(new_user)
    
    log_audit("approve_registration", "system", {"username": username, "user_id": new_user.id})
    return {"status": "success", "message": "User approved and created", "user_id": new_user.id}

@app.post("/api/reject_registration")
async def reject_registration(data: dict = Body(...), db: Session = Depends(get_db)):
    username = (data.get("username") or "").strip()
    if not username:
        raise HTTPException(status_code=400, detail="username required")
    
    reg = db.query(PendingRegistration).filter(PendingRegistration.username == username).first()
    if reg:
        db.delete(reg)
        db.commit()
        
    log_audit("reject_registration", "system", {"username": username})
    return {"status": "success", "message": "Registration rejected"}

# -------------------------
# Sessions endpoints
# -------------------------
@app.get("/api/sessions")
def api_list_sessions():
    return list_active_sessions()

# -------------------------
# Groups, QR, Missions, Map Markers, Meshtastic
# -------------------------
@app.get("/api/groups")
async def get_groups(db: Session = Depends(get_db)):
    groups = db.query(UserGroup).all()
    return [
        {
            "id": g.id,
            "name": g.name,
            "description": g.description,
            "created_at": g.created_at.isoformat(),
            "data": g.data
        } for g in groups
    ]

@app.post("/api/groups")
async def create_group(data: dict = Body(...), authorization: Optional[str] = Header(None), db: Session = Depends(get_db)):
    # Get current user for audit logging
    username = "system"
    if authorization:
        token = authorization[7:].strip() if authorization.lower().startswith("bearer ") else authorization.strip()
        payload = verify_token(token)
        if payload:
            username = payload.get("username", "unknown")
    
    name = data.get("name")
    description = data.get("description", "")
    
    if not name:
        raise HTTPException(status_code=400, detail="Group name required")
    
    # Check if group already exists
    existing = db.query(UserGroup).filter(UserGroup.name == name).first()
    if existing:
        raise HTTPException(status_code=400, detail="Group name already exists")

    new_group = UserGroup(
        id=str(uuid.uuid4()),
        name=name,
        description=description,
        data={
            "permissions": data.get("permissions", []),
            "default_role": data.get("default_role", "user"),
            "created_by": username
        }
    )
    db.add(new_group)
    db.commit()
    
    log_audit("create_group", username, {"group_id": new_group.id})
    return {
        "status": "success", 
        "group": {
            "id": new_group.id,
            "name": new_group.name,
            "description": new_group.description,
            "created_at": new_group.created_at.isoformat()
        }
    }

@app.delete("/api/groups/{group_id}")
async def delete_group(group_id: str, db: Session = Depends(get_db)):
    group = db.query(UserGroup).filter(UserGroup.id == group_id).first()
    if not group:
         raise HTTPException(status_code=404, detail="Group not found")
         
    db.delete(group)
    db.commit()
    log_audit("delete_group", "system", {"group_id": group_id})
    return {"status": "success"}

# Status update endpoint with history tracking
@app.post("/api/status/{unit_id}/{new_status}")
def update_unit_status(unit_id: str = Path(...), new_status: str = Path(...), authorization: Optional[str] = Header(None), db: Session = Depends(get_db)):
    """Update unit tactical status and maintain history in database"""
    user = db.query(User).filter((User.id == unit_id) | (User.username == unit_id) | (User.device == unit_id)).first()
    
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    # Update status with history in the 'data' JSON field
    ts = datetime.now(timezone.utc).isoformat()
    current_data = user.data if user.data else {}
    
    if "history" not in current_data or not isinstance(current_data["history"], list):
        current_data["history"] = []
    
    current_data["history"].append({
        "status": new_status,
        "timestamp": ts
    })
    
    user.data = current_data
    # In certain legacy logic, status might be a top-level field in JSON users
    # We'll keep it in 'data' for now, but we could also add a column if needed.
    # For now, let's also update the marker associated with this user.
    db.commit()
    
    # Also update map_marker if exists
    unit_identifier = user.device or user.callsign or user.username
    markers = db.query(MapMarker).filter(MapMarker.created_by == "import_meshtastic").all()
    marker = None
    for m in markers:
        if isinstance(m.data, dict) and str(m.data.get("unit_id", "")) == str(unit_identifier):
            marker = m
            break
    if marker:
        marker_data = marker.data if marker.data else {}
        marker_data["status"] = new_status
        marker_data["timestamp"] = ts
        marker.data = marker_data
        db.commit()
        
        # Broadcast update
        broadcast_websocket_update("markers", "marker_updated", {
            "id": marker.id,
            "status": new_status,
            "unit_id": unit_identifier
        })
    
    log_audit("status_update", user.id, {"status": new_status, "unit_id": unit_id})
    
    # Broadcast status_update to all WebSocket clients (for real-time sync between index.html and overview.html)
    broadcast_websocket_update("status", "status_update", {
        "status": new_status,
        "username": user.username,
        "unit_id": unit_id,
        "timestamp": ts
    })
    
    return {"status": "success", "user_id": user.id, "new_status": new_status}

# Missions: list, create
@app.get("/api/missions")
def get_missions():
    # missions.read is available to all roles (guest+)
    db = SessionLocal()
    try:
        missions = db.query(Mission).all()
        # Convert to list for response, handling mapping from DB to legacy format
        return [
            {
                "id": m.id,
                "objective": m.name or (m.data.get("objective") if m.data else None),
                "description": m.description,
                "status": m.status,
                "created_at": m.created_at.isoformat() if m.created_at else None,
                "data": m.data,
                # Flatten some fields from data for legacy frontend if needed
                "location": (m.data.get("location") if m.data else None),
                "date": (m.data.get("date") if m.data else None),
                "involved_units": (m.data.get("involved_units") if m.data else [])
            } for m in missions
        ]
    finally:
        db.close()

@app.post("/api/add_mission")
def add_mission(data: dict = Body(...), authorization: Optional[str] = Header(None)):
    # Permission system removed - all authenticated users can create missions
    db = SessionLocal()
    try:
        current_username = "system"
        try:
            payload = verify_token(authorization)
            if payload and payload.get("username"):
                current_username = payload.get("username")
        except: pass

        # Snapshot units from database instead of JSON
        users = db.query(User).all()
        involved_units = []
        for u in users:
            # We don't have a status field in User model yet? Let's check.
            # From models.py: User(id, username, password_hash, role, is_active, created_at)
            # Legacy used: name, device, status, timestamp
            involved_units.append({
                "name": u.username,
                "role": u.role,
                "is_active": u.is_active
            })
        
        # Merge all data into the JSON field
        extra_data = dict(data)
        extra_data["involved_units"] = involved_units
        extra_data["created_by"] = current_username

        new_mission = Mission(
            name=data.get("objective") or data.get("name") or "New Mission",
            description=data.get("description") or data.get("objective"),
            status="ONGOING",
            data=extra_data
        )
        db.add(new_mission)
        db.commit()
        db.refresh(new_mission)

        log_audit("create_mission", current_username, {"mission_id": new_mission.id})
        
        return {
            "status": "success", 
            "mission": {
                "id": new_mission.id,
                "objective": new_mission.name,
                "status": new_mission.status,
                "involved_units": involved_units
            }
        }
    except Exception as e:
        db.rollback()
        logger.error(f"Error adding mission: {e}")
        raise HTTPException(status_code=500, detail="Failed to add mission")
    finally:
        db.close()

@app.get("/api/mission_details/{mission_id}")
def api_mission_details(mission_id: str = Path(...)):
    db = SessionLocal()
    try:
        mission = db.query(Mission).filter(Mission.id == mission_id).first()
        if not mission:
            raise HTTPException(status_code=404, detail="Mission not found")
        
        # Map back to legacy format for frontend
        m_dict = {
            "id": mission.id,
            "objective": mission.name or (mission.data.get("objective") if mission.data else None),
            "description": mission.description,
            "status": mission.status,
            "created_at": mission.created_at.isoformat() if mission.created_at else None,
            "data": mission.data
        }
        if mission.data:
            m_dict.update(mission.data)
            
        return m_dict
    finally:
        db.close()

@app.post("/api/mission_complete/{mission_id}/{result}")
def api_mission_complete(mission_id: str = Path(...), result: str = Path(...), authorization: Optional[str] = Header(None)):
    result_norm = (result or "").upper()
    db = SessionLocal()
    try:
        mission = db.query(Mission).filter(Mission.id == mission_id).first()
        if not mission:
            raise HTTPException(status_code=404, detail="Mission not found")
            
        mission.status = result_norm
        # Update extra data if it exists
        if mission.data:
            new_data = dict(mission.data)
            new_data["completed_at"] = datetime.now(timezone.utc).isoformat()
            mission.data = new_data
            flag_modified(mission, "data")
            
        db.commit()
        log_audit("mission_complete", "system", {"mission_id": mission_id, "result": result_norm})
        return {"status": "success", "mission_id": mission_id, "status": result_norm}
    except Exception as e:
        db.rollback()
        logger.error(f"Error completing mission: {e}")
        raise HTTPException(status_code=500, detail="Failed to update mission")
    finally:
        db.close()

@app.delete("/api/missions/{mission_id}")
def api_delete_mission(mission_id: str = Path(...), authorization: Optional[str] = Header(None)):
    db = SessionLocal()
    try:
        mission = db.query(Mission).filter(Mission.id == mission_id).first()
        if not mission:
             raise HTTPException(status_code=404, detail="Mission not found")
             
        db.delete(mission)
        db.commit()
        log_audit("delete_mission", "system", {"mission_id": mission_id})
        return {"status": "success", "message": "Mission deleted"}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Error deleting mission: {e}")
        raise HTTPException(status_code=500, detail="Failed to delete mission")
    finally:
        db.close()

@app.patch("/api/missions/{mission_id}")
def api_update_mission(mission_id: str = Path(...), data: dict = Body(...), authorization: Optional[str] = Header(None)):
    """Update mission order fields (Gesamtbefehl form) for a given mission."""
    db = SessionLocal()
    try:
        mission = db.query(Mission).filter(Mission.id == mission_id).first()
        if not mission:
            raise HTTPException(status_code=404, detail="Mission not found")
        existing = dict(mission.data) if mission.data else {}
        existing.update(data)
        mission.data = existing
        flag_modified(mission, "data")
        db.commit()
        return {"status": "success", "mission_id": mission_id}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Error updating mission: {e}")
        raise HTTPException(status_code=500, detail="Failed to update mission")
    finally:
        db.close()


# Allowed MIME types for mission attachments (images + PDF; Word excluded)
_ALLOWED_MIME = {
    "image/jpeg", "image/png", "image/gif", "image/webp", "image/bmp",
    "image/svg+xml", "application/pdf",
}
_ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg", ".pdf"}
_BLOCKED_EXT = {".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx"}


@app.post("/api/missions/{mission_id}/upload")
async def api_upload_mission_attachment(
    mission_id: str = Path(...),
    file: UploadFile = File(...),
    authorization: Optional[str] = Header(None),
):
    """Upload an image or PDF attachment for a mission (Word files are not accepted)."""
    db = SessionLocal()
    try:
        mission = db.query(Mission).filter(Mission.id == mission_id).first()
        if not mission:
            raise HTTPException(status_code=404, detail="Mission not found")

        original_name = file.filename or "upload"
        _, ext = os.path.splitext(original_name)
        ext_lower = ext.lower()

        if ext_lower in _BLOCKED_EXT:
            raise HTTPException(status_code=400, detail="Word/Office files are not allowed.")
        if ext_lower and ext_lower not in _ALLOWED_EXT:
            raise HTTPException(status_code=400, detail=f"File type '{ext_lower}' is not allowed. Allowed: images and PDF.")

        content_type = file.content_type or ""
        if content_type and content_type not in _ALLOWED_MIME:
            raise HTTPException(status_code=400, detail=f"MIME type '{content_type}' is not allowed.")

        # Store under uploads/<mission_id>/
        mission_uploads = os.path.join(uploads_dir, mission_id)
        os.makedirs(mission_uploads, exist_ok=True)

        safe_name = f"{uuid.uuid4().hex}{ext_lower}"
        dest_path = os.path.join(mission_uploads, safe_name)
        content = await file.read()
        with open(dest_path, "wb") as fh:
            fh.write(content)

        file_url = f"/uploads/{mission_id}/{safe_name}"

        # Store attachment info in mission.data
        existing = dict(mission.data) if mission.data else {}
        attachments = existing.get("attachments", [])
        attachments.append({
            "url": file_url,
            "original_name": original_name,
            "content_type": content_type,
            "uploaded_at": datetime.now(timezone.utc).isoformat(),
        })
        existing["attachments"] = attachments
        mission.data = existing
        flag_modified(mission, "data")
        db.commit()
        return {"status": "success", "url": file_url, "original_name": original_name}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Error uploading attachment: {e}")
        raise HTTPException(status_code=500, detail="Failed to upload file")
    finally:
        db.close()


@app.get("/api/missions/{mission_id}/attachments")
def api_get_mission_attachments(mission_id: str = Path(...)):
    """Return list of attachments for a mission."""
    db = SessionLocal()
    try:
        mission = db.query(Mission).filter(Mission.id == mission_id).first()
        if not mission:
            raise HTTPException(status_code=404, detail="Mission not found")
        attachments = (mission.data or {}).get("attachments", [])
        return {"attachments": attachments}
    finally:
        db.close()


@app.get("/api/mission_unit_stats/{mission_id}")
def api_mission_unit_stats(mission_id: str = Path(...), db: Session = Depends(get_db)):
    """
    Returns unit statistics and status history for a specific mission.
    Uses user data entries within the mission time window from DB.
    """
    mission = db.query(Mission).filter(Mission.id == mission_id).first()
    if not mission:
        return {"status": "error", "message": "Mission not found", "unit_stats": []}
    
    # Get mission time window
    def parse_time(t):
        if not t: return None
        if isinstance(t, datetime):
            # Ensure timezone-aware
            if t.tzinfo is None:
                return t.replace(tzinfo=timezone.utc)
            return t
        if isinstance(t, (int, float)):
            dt = datetime.fromtimestamp(t if t < 1e12 else t / 1000, tz=timezone.utc)
            return dt
        try:
            dt = datetime.fromisoformat(str(t).replace('Z', '+00:00'))
            # Ensure timezone-aware
            if dt.tzinfo is None:
                return dt.replace(tzinfo=timezone.utc)
            return dt
        except: return None
    
    start_dt = parse_time(mission.data.get("start_time") if mission.data else None) or parse_time(mission.created_at)
    end_dt = parse_time(mission.data.get("completed_at") if mission.data else None) or datetime.now(timezone.utc)
    
    # Load users from DB
    users = db.query(User).all()
    unit_stats = []
    
    for user in users:
        # History is stored in user.data.history
        history = (user.data or {}).get("history", [])
        if not isinstance(history, list) or len(history) == 0:
            continue
        
        # Filter history entries within mission window and parse timestamps once
        filtered_history = []
        for entry in history:
            entry_time = parse_time(entry.get("timestamp"))
            if entry_time and start_dt <= entry_time <= end_dt:
                filtered_history.append((entry_time, entry))  # Store parsed time with entry
                
        if not filtered_history:
            continue
            
        # Sort by parsed timestamp (already parsed, no redundant parsing)
        filtered_history.sort(key=lambda x: x[0])
        
        # Extract just the entry dictionaries after sorting
        sorted_entries = [entry for _, entry in filtered_history]
        
        # Calculate first and last status
        first_status = sorted_entries[0].get("status", "UNKNOWN") if sorted_entries else "UNKNOWN"
        last_status = sorted_entries[-1].get("status", "UNKNOWN") if sorted_entries else "UNKNOWN"
        
        # Calculate total_changes (count of status changes)
        total_changes = len(sorted_entries)
        
        # Calculate durations for each status (time spent in each status in seconds)
        # Use the filtered_history list which contains (timestamp, entry) tuples for efficiency
        durations = {}
        for i in range(len(filtered_history)):
            current_time, current_entry = filtered_history[i]
            current_status = current_entry.get("status")
            
            if not current_status:
                continue
                
            # Calculate duration until next status change or end of mission
            if i < len(filtered_history) - 1:
                next_time, _ = filtered_history[i + 1]
                duration = (next_time - current_time).total_seconds()
            else:
                # Last entry - calculate duration until mission end
                duration = (end_dt - current_time).total_seconds()
            
            # Add duration to the status
            if current_status in durations:
                durations[current_status] += duration
            else:
                durations[current_status] = duration
        
        # Build the unit stats object with the expected structure
        unit_stats.append({
            "name": user.fullname or user.username,
            "device": user.device or user.username,
            "first_status": first_status,
            "last_status": last_status,
            "total_changes": total_changes,
            "durations": durations,
            "history": sorted_entries
        })
            
    return {"status": "success", "mission_id": mission_id, "unit_stats": unit_stats}

# Map markers
@app.get("/api/map_markers")
def get_map_markers():
    # markers.read is available to all roles (guest+)
    db = SessionLocal()
    try:
        markers = db.query(MapMarker).all()
        # Convert to dict list for JSON response, excluding meshtastic-synced markers
        # (those are rendered exclusively via /api/meshtastic/nodes → updateMeshtasticNodes)
        return [
            {
                "id": m.id,
                "lat": m.lat,
                "lng": m.lng,
                "name": m.name,
                "description": m.description,
                "type": m.type,
                "color": m.color,
                "icon": m.icon,
                "created_by": m.created_by,
                "created_at": m.created_at.isoformat() if m.created_at else None,
                "data": m.data
            } for m in markers
            if m.type != "node" and (not m.created_by or m.created_by not in _MESHTASTIC_CREATED_BY)
        ]
    finally:
        db.close()

@app.post("/api/map_markers")
def create_map_marker(data: dict = Body(...), authorization: Optional[str] = Header(None)):
    # Permission system removed - all authenticated users can create markers
    # Get current user for audit logging
    current_username = "system"
    try:
        payload = verify_token(authorization)
        if payload and payload.get("username"):
            current_username = payload.get("username")
    except: pass
    
    lat = data.get("lat"); lng = data.get("lng"); name = data.get("name")
    if lat is None or lng is None or not name:
        raise HTTPException(status_code=400, detail="Latitude, longitude, and name required")
        
    db = SessionLocal()
    try:
        new_marker = MapMarker(
            lat=float(lat),
            lng=float(lng),
            name=name or data.get("type", "marker"),
            description=data.get("description"),
            type=data.get("type", "friendly"),
            color=data.get("color", "#ff0000"),
            icon=data.get("icon", "default"),
            created_by=current_username,
            data=data  # Store full data payload in JSON field as well
        )
        db.add(new_marker)
        db.commit()
        db.refresh(new_marker)
        
        log_audit("create_marker", current_username, {"marker_id": new_marker.id})
        
        # Format for broadcast
        marker_dict = {
            "id": new_marker.id,
            "lat": new_marker.lat,
            "lng": new_marker.lng,
            "name": new_marker.name,
            "callsign": new_marker.name,
            "description": new_marker.description,
            "type": new_marker.type,
            "color": new_marker.color,
            "icon": new_marker.icon,
            "created_by": new_marker.created_by,
            "timestamp": new_marker.created_at.isoformat() if new_marker.created_at else datetime.now(timezone.utc).isoformat(),
            "data": new_marker.data
        }
        
        # Broadcast marker update to all connected clients
        broadcast_websocket_update("markers", "marker_created", marker_dict)

        # Forward to ATAK/TAK server if enabled
        if AUTONOMOUS_MODULES_AVAILABLE:
            try:
                cot_event = CoTProtocolHandler.marker_to_cot(marker_dict)
                if cot_event:
                    cot_xml = cot_event.to_xml()
                    ok = forward_cot_to_tak(cot_xml)
                    if ok:
                        logger.info("CoT forward on marker_created succeeded: marker_id=%s", new_marker.id)
                    else:
                        logger.debug("CoT forward on marker_created skipped (TAK forwarding disabled or not configured): marker_id=%s", new_marker.id)
                    mcast_ok = _forward_cot_multicast(cot_xml)
                    if mcast_ok:
                        logger.debug("CoT SA Multicast send on marker_created succeeded: marker_id=%s", new_marker.id)
            except Exception as _fwd_err:
                logger.warning("CoT forward on marker_created failed: %s", _fwd_err)

        return {"status": "success", "marker": marker_dict}
    except Exception as e:
        db.rollback()
        logger.error(f"Error creating marker: {e}")
        raise HTTPException(status_code=500, detail="Failed to create marker")
    finally:
        db.close()

@app.put("/api/map_markers/{marker_id}")
def update_map_marker(marker_id: str, data: dict = Body(...), authorization: Optional[str] = Header(None)):
    current_username = "system"
    try:
        payload = verify_token(authorization)
        if payload and payload.get("username"):
            current_username = payload.get("username")
    except: pass
    
    db = SessionLocal()
    try:
        marker = db.query(MapMarker).filter(MapMarker.id == marker_id).first()
        if not marker:
            raise HTTPException(status_code=404, detail="Marker not found")
            
        if "lat" in data: marker.lat = float(data["lat"])
        if "lng" in data: marker.lng = float(data["lng"])
        if "name" in data: marker.name = data["name"]
        if "description" in data: marker.description = data["description"]
        if "type" in data: marker.type = data["type"]
        if "color" in data: marker.color = data["color"]
        if "icon" in data: marker.icon = data["icon"]
        
        if "data" in data and isinstance(data["data"], dict):
            current_extra = marker.data if marker.data else {}
            current_extra.update(data["data"])
            marker.data = current_extra
        elif "data" in data:
            marker.data = data["data"]
            
        db.commit()
        db.refresh(marker)
        
        marker_dict = {
            "id": marker.id,
            "lat": marker.lat,
            "lng": marker.lng,
            "name": marker.name,
            "callsign": marker.name,
            "description": marker.description,
            "type": marker.type,
            "color": marker.color,
            "icon": marker.icon,
            "created_by": marker.created_by,
            "timestamp": marker.created_at.isoformat() if marker.created_at else datetime.now(timezone.utc).isoformat(),
            "data": marker.data
        }
        
        log_audit("update_marker", current_username, {"marker_id": marker_id})
        broadcast_websocket_update("markers", "marker_updated", marker_dict)

        # Forward to ATAK/TAK server if enabled
        if AUTONOMOUS_MODULES_AVAILABLE:
            try:
                cot_event = CoTProtocolHandler.marker_to_cot(marker_dict)
                if cot_event:
                    cot_xml = cot_event.to_xml()
                    ok = forward_cot_to_tak(cot_xml)
                    if ok:
                        logger.info("CoT forward on marker_updated succeeded: marker_id=%s", marker_id)
                    else:
                        logger.debug("CoT forward on marker_updated skipped (TAK forwarding disabled or not configured): marker_id=%s", marker_id)
                    mcast_ok = _forward_cot_multicast(cot_xml)
                    if mcast_ok:
                        logger.debug("CoT SA Multicast send on marker_updated succeeded: marker_id=%s", marker_id)
            except Exception as _fwd_err:
                logger.warning("CoT forward on marker_updated failed: %s", _fwd_err)

        return {"status": "success", "marker": marker_dict}

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Error updating marker: {e}")
        raise HTTPException(status_code=500, detail="Failed to update marker")
    finally:
        db.close()

@app.delete("/api/map_markers/{marker_id}")
def delete_map_marker(marker_id: str, authorization: Optional[str] = Header(None)):
    current_username = "system"
    try:
        payload = verify_token(authorization)
        if payload and payload.get("username"):
            current_username = payload.get("username")
    except: pass
    
    db = SessionLocal()
    try:
        marker = db.query(MapMarker).filter(MapMarker.id == marker_id).first()
        if not marker:
            raise HTTPException(status_code=404, detail="Marker not found")
        
        # Prevent deletion of GPS position markers (except by the owning user for position updates)
        if marker.type == 'gps_position' and marker.created_by != current_username:
            raise HTTPException(status_code=403, detail="GPS position markers cannot be deleted")

        # Capture marker data before deletion for CoT tombstone forwarding
        marker_snapshot = {
            "id": marker.id,
            "lat": marker.lat,
            "lng": marker.lng,
            "name": marker.name,
            "description": marker.description,
            "type": marker.type,
            "data": marker.data,
        }

        db.delete(marker)
        db.commit()
        
        log_audit("delete_marker", current_username, {"marker_id": marker_id})
        broadcast_websocket_update("markers", "marker_deleted", {"id": marker_id})
        broadcast_websocket_update("symbols", "symbol_deleted", {"id": marker_id})

        # Forward CoT tombstone to TAK server so remote ATAK clients remove the entity
        if AUTONOMOUS_MODULES_AVAILABLE:
            try:
                tombstone = CoTProtocolHandler.marker_to_cot_tombstone(marker_snapshot)
                if tombstone:
                    tombstone_xml = tombstone.to_xml()
                    ok = forward_cot_to_tak(tombstone_xml)
                    if ok:
                        logger.info("CoT tombstone forwarded on marker_deleted: marker_id=%s", marker_id)
                    else:
                        logger.debug("CoT tombstone skipped on marker_deleted (TAK forwarding disabled or not configured): marker_id=%s", marker_id)
                    mcast_ok = _forward_cot_multicast(tombstone_xml)
                    if mcast_ok:
                        logger.debug("CoT SA Multicast tombstone sent on marker_deleted: marker_id=%s", marker_id)
            except Exception as _fwd_err:
                logger.warning("CoT tombstone forward on marker_deleted failed: %s", _fwd_err)

        return {"status": "success"}
    except Exception as e:
        db.rollback()
        logger.error(f"Error deleting marker: {e}")
        raise HTTPException(status_code=500, detail="Failed to delete marker")
    finally:
        db.close()


# -------------------------
# Drawings API (for line/polygon drawings)
# -------------------------
@app.get("/api/drawings")
def get_drawings():
    """Get all drawings (DB-backed)"""
    db = SessionLocal()
    try:
        drawings = db.query(Drawing).all()
        return [
            {
                "id": d.id,
                "name": d.name,
                "type": d.type,
                "coordinates": d.coordinates,
                "color": d.color,
                "weight": d.weight,
                "created_by": d.created_by,
                "timestamp": d.created_at.isoformat() if d.created_at else None,
                "data": d.data
            } for d in drawings
        ]
    finally:
        db.close()

@app.post("/api/drawings")
def create_drawing(data: dict = Body(...)):
    """Create a new drawing (DB-backed)"""
    db = SessionLocal()
    try:
        drawing = Drawing(
            name=data.get("name", "Drawing"),
            type=data.get("type", "polyline"),
            coordinates=data.get("coordinates", []),
            color=data.get("color", "#3388ff"),
            weight=data.get("weight", 3),
            created_by=data.get("created_by", "system"),
            data=data
        )
        db.add(drawing)
        db.commit()
        db.refresh(drawing)
        
        drawing_dict = {
            "id": drawing.id,
            "name": drawing.name,
            "type": drawing.type,
            "coordinates": drawing.coordinates,
            "color": drawing.color,
            "weight": drawing.weight,
            "created_by": drawing.created_by,
            "timestamp": drawing.created_at.isoformat()
        }
        
        # Broadcast to all clients
        broadcast_websocket_update("drawings", "drawing_created", drawing_dict)
        return {"status": "success", "drawing": drawing_dict}
    except Exception as e:
        db.rollback()
        logger.error(f"Error creating drawing: {e}")
        raise HTTPException(status_code=500, detail="Failed to create drawing")
    finally:
        db.close()

@app.put("/api/drawings/{drawing_id}")
def update_drawing(drawing_id: str, data: dict = Body(...)):
    """Update an existing drawing (DB-backed)"""
    db = SessionLocal()
    try:
        drawing = db.query(Drawing).filter(Drawing.id == drawing_id).first()
        if not drawing:
            raise HTTPException(status_code=404, detail="Drawing not found")
        
        if "coordinates" in data: drawing.coordinates = data["coordinates"]
        if "color" in data: drawing.color = data["color"]
        if "weight" in data: drawing.weight = data["weight"]
        if "name" in data: drawing.name = data["name"]
        if "type" in data: drawing.type = data["type"]
        
        if drawing.data:
            new_data = dict(drawing.data)
            new_data.update(data)
            drawing.data = new_data
        else:
            drawing.data = data
            
        db.commit()
        db.refresh(drawing)
        
        drawing_dict = {
            "id": drawing.id,
            "name": drawing.name,
            "type": drawing.type,
            "coordinates": drawing.coordinates,
            "color": drawing.color,
            "weight": drawing.weight,
            "created_by": drawing.created_by,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
        
        # Broadcast to all clients
        broadcast_websocket_update("drawings", "drawing_updated", drawing_dict)
        return {"status": "success", "drawing": drawing_dict}
    except Exception as e:
        db.rollback()
        logger.error(f"Error updating drawing: {e}")
        raise HTTPException(status_code=500, detail="Failed to update drawing")
    finally:
        db.close()

@app.delete("/api/drawings/{drawing_id}")
def delete_drawing(drawing_id: str):
    """Delete a drawing (DB-backed)"""
    db = SessionLocal()
    try:
        drawing = db.query(Drawing).filter(Drawing.id == drawing_id).first()
        if not drawing:
             raise HTTPException(status_code=404, detail="Drawing not found")
        db.delete(drawing)
        db.commit()
        broadcast_websocket_update("drawings", "drawing_deleted", {"id": drawing_id})
        return {"status": "success"}
    except Exception as e:
        db.rollback()
        logger.error(f"Error deleting drawing: {e}")
        raise HTTPException(status_code=500, detail="Failed to delete drawing")
    finally:
        db.close()

# -------------------------
# Overlays API (for image overlays)
# -------------------------
@app.get("/api/overlays")
def get_overlays():
    """Get all overlays (DB-backed)"""
    db = SessionLocal()
    try:
        overlays = db.query(Overlay).all()
        return [
            {
                "id": o.id,
                "name": o.name,
                "imageUrl": o.image_url,
                "bounds": o.bounds,
                "opacity": o.opacity,
                "rotation": o.rotation,
                "created_by": o.created_by,
                "timestamp": o.created_at.isoformat() if o.created_at else None,
                "data": o.data
            } for o in overlays
        ]
    finally:
        db.close()

@app.post("/api/overlays")
def create_overlay(data: dict = Body(...)):
    """Create a new overlay (DB-backed)"""
    db = SessionLocal()
    try:
        overlay = Overlay(
            name=data.get("name", "Overlay"),
            image_url=data.get("imageUrl", ""),
            bounds=data.get("bounds", {}),
            opacity=data.get("opacity", 1.0),
            rotation=data.get("rotation", 0),
            created_by=data.get("created_by", "system"),
            data=data
        )
        db.add(overlay)
        db.commit()
        db.refresh(overlay)
        
        overlay_dict = {
            "id": overlay.id,
            "name": overlay.name,
            "imageUrl": overlay.image_url,
            "bounds": overlay.bounds,
            "opacity": overlay.opacity,
            "rotation": overlay.rotation,
            "created_by": overlay.created_by,
            "timestamp": overlay.created_at.isoformat()
        }
        
        broadcast_websocket_update("overlays", "overlay_created", overlay_dict)
        logger.info("Overlay created: id=%s name=%r created_by=%r (TAK CoT sync not applicable for image overlays)", overlay.id, overlay.name, overlay.created_by)
        return {"status": "success", "overlay": overlay_dict}
    except Exception as e:
        db.rollback()
        logger.error(f"Error creating overlay: {e}")
        raise HTTPException(status_code=500, detail="Failed to create overlay")
    finally:
        db.close()

@app.put("/api/overlays/{overlay_id}")
def update_overlay(overlay_id: str, data: dict = Body(...)):
    """Update an existing overlay (DB-backed)"""
    db = SessionLocal()
    try:
        overlay = db.query(Overlay).filter(Overlay.id == overlay_id).first()
        if not overlay:
            raise HTTPException(status_code=404, detail="Overlay not found")
            
        if "name" in data: overlay.name = data["name"]
        if "imageUrl" in data: overlay.image_url = data["imageUrl"]
        if "bounds" in data: overlay.bounds = data["bounds"]
        if "opacity" in data: overlay.opacity = float(data["opacity"])
        if "rotation" in data: overlay.rotation = float(data["rotation"])
        
        if overlay.data:
            new_data = dict(overlay.data)
            new_data.update(data)
            overlay.data = new_data
        else:
            overlay.data = data
            
        db.commit()
        db.refresh(overlay)
        
        overlay_dict = {
            "id": overlay.id,
            "name": overlay.name,
            "imageUrl": overlay.image_url,
            "bounds": overlay.bounds,
            "opacity": overlay.opacity,
            "rotation": overlay.rotation,
            "created_by": overlay.created_by,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
        
        broadcast_websocket_update("overlays", "overlay_updated", overlay_dict)
        logger.info("Overlay updated: id=%s name=%r (TAK CoT sync not applicable for image overlays)", overlay_id, overlay_dict.get("name"))
        return {"status": "success", "overlay": overlay_dict}
    except Exception as e:
        db.rollback()
        logger.error(f"Error updating overlay: {e}")
        raise HTTPException(status_code=500, detail="Failed to update overlay")
    finally:
        db.close()

@app.delete("/api/overlays/{overlay_id}")
def delete_overlay(overlay_id: str):
    """Delete an overlay (DB-backed)"""
    db = SessionLocal()
    try:
        overlay = db.query(Overlay).filter(Overlay.id == overlay_id).first()
        if not overlay:
            raise HTTPException(status_code=404, detail="Overlay not found")
        db.delete(overlay)
        db.commit()
        broadcast_websocket_update("overlays", "overlay_deleted", {"id": overlay_id})
        logger.info("Overlay deleted: id=%s (TAK CoT sync not applicable for image overlays)", overlay_id)
        return {"status": "success"}
    except Exception as e:
        db.rollback()
        logger.error(f"Error deleting overlay: {e}")
        raise HTTPException(status_code=500, detail="Failed to delete overlay")
    finally:
        db.close()

# -------------------------
# Symbols API (for map symbols/icons)
# -------------------------
@app.get("/api/symbols")
def get_symbols(db: Session = Depends(get_db)):
    """Get all symbols (from unified MapMarker table)"""
    markers = db.query(MapMarker).filter(MapMarker.type == "legacy_symbol").all()
    return [
        {
            "id": m.id,
            "lat": m.lat,
            "lng": m.lng,
            "symbolType": (m.data or {}).get("symbolType", "circle"),
            "name": m.name,
            "created_by": m.created_by,
            "timestamp": m.created_at.isoformat()
        } for m in markers
    ]

@app.post("/api/symbols")
def create_symbol(data: dict = Body(...), db: Session = Depends(get_db)):
    """Create a new symbol in DB"""
    symbol_id = str(uuid.uuid4())
    new_marker = MapMarker(
        id=symbol_id,
        lat=float(data.get("lat", 0)),
        lng=float(data.get("lng", 0)),
        name=data.get("name", "Symbol"),
        type="legacy_symbol",
        created_by=data.get("created_by", "system"),
        data={"symbolType": data.get("symbolType", "circle")}
    )
    db.add(new_marker)
    db.commit()
    
    symbol_dict = {
        "id": symbol_id,
        "lat": new_marker.lat,
        "lng": new_marker.lng,
        "symbolType": data.get("symbolType", "circle"),
        "name": new_marker.name,
        "created_by": new_marker.created_by,
        "timestamp": datetime.now(timezone.utc).isoformat()
    }
    
    broadcast_websocket_update("symbols", "symbol_created", symbol_dict)
    return {"status": "success", "symbol": symbol_dict}

@app.put("/api/symbols/{symbol_id}")
def update_symbol(symbol_id: str, data: dict = Body(...), db: Session = Depends(get_db)):
    """Update an existing symbol in DB"""
    marker = db.query(MapMarker).filter(MapMarker.id == symbol_id, MapMarker.type == "legacy_symbol").first()
    if not marker:
        raise HTTPException(status_code=404, detail="Symbol not found")
    
    if "lat" in data: marker.lat = float(data["lat"])
    if "lng" in data: marker.lng = float(data["lng"])
    if "name" in data: marker.name = data["name"]
    if "symbolType" in data:
        current_data = marker.data if marker.data else {}
        current_data["symbolType"] = data["symbolType"]
        marker.data = current_data
    
    db.commit()
    
    symbol_dict = {
        "id": marker.id,
        "lat": marker.lat,
        "lng": marker.lng,
        "symbolType": (marker.data or {}).get("symbolType"),
        "name": marker.name,
        "created_by": marker.created_by,
        "timestamp": datetime.now(timezone.utc).isoformat()
    }
    
    broadcast_websocket_update("symbols", "symbol_updated", symbol_dict)
    return {"status": "success", "symbol": symbol_dict}

@app.delete("/api/symbols/{symbol_id}")
def delete_symbol(symbol_id: str, db: Session = Depends(get_db)):
    """Delete a symbol from DB"""
    marker = db.query(MapMarker).filter(MapMarker.id == symbol_id, MapMarker.type == "legacy_symbol").first()
    if not marker:
        raise HTTPException(status_code=404, detail="Symbol not found")
        
    db.delete(marker)
    db.commit()
    broadcast_websocket_update("symbols", "symbol_deleted", {"id": symbol_id})
    return {"status": "success"}

# -------------------------
# Sync API (for cross-page synchronization)
# -------------------------
@app.post("/api/sync/markers")
async def sync_markers(data: dict = Body(...)):
    """Broadcast marker changes to all clients"""
    markers = data.get("markers", [])
    
    # Save to database
    if markers:
        save_json("map_markers", markers)
    
    # Broadcast to all connected clients
    broadcast_websocket_update("markers", "markers_update", {"markers": markers})
    
    return {"status": "ok", "synced": len(markers)}

@app.post("/api/sync/overlays")
async def sync_overlays(data: dict = Body(...)):
    """Broadcast overlay changes to all clients"""
    overlays = data.get("overlays", [])
    
    # Save to database
    if overlays:
        save_json("overlays", overlays)
    
    # Broadcast to all connected clients
    broadcast_websocket_update("overlays", "overlays_update", {"overlays": overlays})
    
    return {"status": "ok", "synced": len(overlays)}

@app.post("/api/sync/drawings")
async def sync_drawings(data: dict = Body(...)):
    """Broadcast drawing changes to all clients"""
    drawings = data.get("drawings", [])
    
    # Save to database
    if drawings:
        save_json("drawings", drawings)
    
    # Broadcast to all connected clients
    broadcast_websocket_update("drawings", "drawings_update", {"drawings": drawings})
    
    return {"status": "ok", "synced": len(drawings)}

# -------------------------
# Unified Sync API for Auto-Synchronization
# -------------------------
@app.post("/api/sync/upload")
async def sync_upload(data: dict = Body(...), authorization: Optional[str] = Header(None)):
    """
    Upload map/message/COT data from admin client for distribution to all clients.
    Called every 2 seconds from admin_map.html to upload changes.
    Requires authentication.
    
    Body:
        {
            "markers": [...],       # Map markers
            "drawings": [...],      # Drawings (lines, polygons)
            "overlays": [...],      # Overlay images
            "symbols": [...],       # Tactical symbols
            "messages": [...],      # Chat/status messages
            "cot_events": [...],    # CoT (Cursor-on-Target) events
            "timestamp": "..."      # ISO timestamp
        }
    """
    # Verify authentication
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Authentication required")
    
    try:
        timestamp = data.get("timestamp", datetime.now(timezone.utc).isoformat())
        
        # Process and save each data type
        updates = {}
        
        # Markers
        if "markers" in data and isinstance(data["markers"], list):
            save_json("map_markers", data["markers"])
            updates["markers"] = len(data["markers"])
            # Broadcast to WebSocket clients
            if websocket_manager:
                await websocket_manager.publish_to_channel('markers', {
                    'type': 'markers_update',
                    'markers': data["markers"],
                    'timestamp': timestamp
                })
        
        # Drawings
        if "drawings" in data and isinstance(data["drawings"], list):
            save_json("drawings", data["drawings"])
            updates["drawings"] = len(data["drawings"])
            if websocket_manager:
                await websocket_manager.publish_to_channel('drawings', {
                    'type': 'drawings_update',
                    'drawings': data["drawings"],
                    'timestamp': timestamp
                })
        
        # Overlays
        if "overlays" in data and isinstance(data["overlays"], list):
            save_json("overlays", data["overlays"])
            updates["overlays"] = len(data["overlays"])
            if websocket_manager:
                await websocket_manager.publish_to_channel('overlays', {
                    'type': 'overlays_update',
                    'overlays': data["overlays"],
                    'timestamp': timestamp
                })
        
        # Symbols
        if "symbols" in data and isinstance(data["symbols"], list):
            save_json("symbols", data["symbols"])
            updates["symbols"] = len(data["symbols"])
            if websocket_manager:
                await websocket_manager.publish_to_channel('symbols', {
                    'type': 'symbols_update',
                    'symbols': data["symbols"],
                    'timestamp': timestamp
                })
        
        # Messages
        if "messages" in data and isinstance(data["messages"], list):
            # Append to messages log
            existing_messages = load_json("meshtastic_messages")
            if not isinstance(existing_messages, list):
                existing_messages = []
            existing_messages.extend(data["messages"])
            # Keep only last MAX_STORED_MESSAGES messages
            if len(existing_messages) > MAX_STORED_MESSAGES:
                existing_messages = existing_messages[-MAX_STORED_MESSAGES:]
            save_json("meshtastic_messages", existing_messages)
            updates["messages"] = len(data["messages"])
            if websocket_manager:
                await websocket_manager.publish_to_channel('messages', {
                    'type': 'messages_update',
                    'messages': data["messages"],
                    'timestamp': timestamp
                })
        
        # CoT Events
        if "cot_events" in data and isinstance(data["cot_events"], list):
            updates["cot_events"] = len(data["cot_events"])
            # Broadcast CoT events to subscribed clients and forward to TAK server
            for cot_event in data["cot_events"]:
                if websocket_manager:
                    await websocket_manager.publish_to_channel('cot', {
                        'type': 'cot_event',
                        'event': cot_event,
                        'timestamp': timestamp
                    })
                # Forward raw CoT XML to TAK server if present
                if AUTONOMOUS_MODULES_AVAILABLE:
                    cot_xml = cot_event.get("xml") if isinstance(cot_event, dict) else None
                    if cot_xml:
                        cot_uid = cot_event.get("uid", "<unknown>") if isinstance(cot_event, dict) else "<unknown>"
                        try:
                            ok = forward_cot_to_tak(cot_xml)
                            if ok:
                                logger.info("CoT event forwarded to TAK server via sync_upload: uid=%s", cot_uid)
                            else:
                                logger.debug("CoT event forward skipped via sync_upload (TAK forwarding disabled or not configured): uid=%s", cot_uid)
                        except Exception as _fwd_err:
                            logger.warning("CoT event forward via sync_upload failed: uid=%s err=%s", cot_uid, _fwd_err)
        
        logger.info(f"Sync upload processed: {updates}")
        
        return {
            "status": "success",
            "message": "Data uploaded and distributed",
            "updates": updates,
            "timestamp": timestamp
        }
        
    except Exception as e:
        logger.exception("sync_upload failed: %s", e)
        raise HTTPException(status_code=500, detail=f"Sync upload failed: {str(e)}")

@app.get("/api/sync/download")
def sync_download(authorization: Optional[str] = Header(None)):
    """
    Download current map/message/COT state for client synchronization.
    Called every 2 seconds from all client pages to get latest state.
    Requires authentication.
    
    Returns:
        {
            "markers": [...],       # Map markers
            "drawings": [...],      # Drawings
            "overlays": [...],      # Overlays
            "symbols": [...],       # Symbols
            "messages": [...],      # Recent messages (last MAX_RETURNED_MESSAGES)
            "timestamp": "...",     # Server timestamp
            "data_modified": "..."  # Last modification time of data
        }
    """
    # Verify authentication
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Authentication required")
    
    try:
        # Load all current state
        markers = load_json("map_markers")
        drawings = load_json("drawings")
        overlays = load_json("overlays")
        symbols = load_json("symbols")
        messages = load_json("meshtastic_messages")
        
        # Ensure all are lists
        if not isinstance(markers, list):
            markers = []
        if not isinstance(drawings, list):
            drawings = []
        if not isinstance(overlays, list):
            overlays = []
        if not isinstance(symbols, list):
            symbols = []
        if not isinstance(messages, list):
            messages = []
        
        # Return only recent messages (last MAX_RETURNED_MESSAGES)
        recent_messages = messages[-MAX_RETURNED_MESSAGES:] if len(messages) > MAX_RETURNED_MESSAGES else messages
        
        # Get last modification time from the marker database file
        # This provides a real timestamp of when data was last changed
        data_modified = datetime.now(timezone.utc).isoformat()
        try:
            markers_path = DB_PATHS.get("map_markers")
            if markers_path and os.path.exists(markers_path):
                mtime = os.path.getmtime(markers_path)
                data_modified = datetime.fromtimestamp(mtime).isoformat()
        except Exception:
            pass  # Use current time as fallback
        
        return {
            "status": "success",
            "markers": markers,
            "drawings": drawings,
            "overlays": overlays,
            "symbols": symbols,
            "messages": recent_messages,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "data_modified": data_modified
        }
        
    except Exception as e:
        logger.exception("sync_download failed: %s", e)
        raise HTTPException(status_code=500, detail=f"Sync download failed: {str(e)}")

# -------------------------
# Meshtastic: nodes/messages (simple endpoints)
# -------------------------
@app.get("/api/meshtastic/nodes")
def meshtastic_nodes():
    # Return meshtastic_nodes ensuring lat/lng are numeric and default to 0.0 when missing
    nodes = load_json("meshtastic_nodes")
    if not isinstance(nodes, list):
        nodes = []

    # Fallback: if JSON DB is empty, try loading from SQLAlchemy
    if not nodes:
        db = SessionLocal()
        try:
            db_nodes = db.query(MeshtasticNode).all()
            for dn in db_nodes:
                nodes.append({
                    "id": dn.id,
                    "mesh_id": dn.id,
                    "name": dn.long_name or dn.short_name or dn.id,
                    "longName": dn.long_name,
                    "shortName": dn.short_name,
                    "lat": dn.lat if dn.lat is not None else 0.0,
                    "lng": dn.lng if dn.lng is not None else 0.0,
                    "altitude": dn.altitude,
                    "battery": dn.battery_level,
                    "is_online": dn.is_online,
                    "hardware_model": dn.hardware_model,
                    "last_heard": int(dn.last_heard.timestamp()) if dn.last_heard else None,
                })
        except Exception as e:
            logger.warning("Fallback SQLAlchemy node load failed: %s", e)
        finally:
            db.close()

    normalized = []
    for n in nodes:
        nn = dict(n)
        try:
            lat = nn.get("lat")
            lng = nn.get("lng")
            if lat is None or lng is None:
                nn["lat"] = 0.0
                nn["lng"] = 0.0
            else:
                nn["lat"] = float(lat)
                nn["lng"] = float(lng)
        except Exception:
            nn["lat"] = 0.0
            nn["lng"] = 0.0
        normalized.append(nn)
    return normalized

@app.get("/api/meshtastic/my_nodes")
def meshtastic_my_nodes(authorization: Optional[str] = Header(None)):
    """
    Returns meshtastic nodes that belong to the current user.
    Filters nodes by matching the user's device field with the node's name field.
    """
    # Get current user
    token = None
    if authorization:
        if authorization.lower().startswith("bearer "):
            token = authorization[7:].strip()
        else:
            token = authorization.strip()
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    payload = verify_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid token")
    users = load_json("users")
    user = next((u for u in users if u.get("id") == payload.get("user_id") or u.get("username") == payload.get("username")), None)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    # Get user's device name
    user_device = user.get("device", "")
    
    # Load and filter nodes
    nodes = load_json("meshtastic_nodes")
    if not isinstance(nodes, list):
        return []
    
    # Filter nodes that match user's device name
    filtered_nodes = []
    for n in nodes:
        nn = dict(n)
        # Match node's name with user's device
        if nn.get("name") == user_device:
            try:
                lat = nn.get("lat")
                lng = nn.get("lng")
                if lat is None or lng is None:
                    nn["lat"] = 0.0
                    nn["lng"] = 0.0
                else:
                    nn["lat"] = float(lat)
                    nn["lng"] = float(lng)
            except Exception:
                nn["lat"] = 0.0
                nn["lng"] = 0.0
            filtered_nodes.append(nn)
    
    return filtered_nodes

@app.get("/api/meshtastic/messages")
def meshtastic_messages(limit: int = 100):
    msgs = load_json("meshtastic_messages")
    if not isinstance(msgs, list):
        return []
    return msgs[-limit:]

@app.post("/api/meshtastic/send")
def meshtastic_send(data: dict = Body(...)):
    message = {"id": str(uuid.uuid4()), "from": data.get("from"), "text": data.get("text"), "ts": int(datetime.now(timezone.utc).timestamp())}
    msgs = load_json("meshtastic_messages")
    if not isinstance(msgs, list):
        msgs = []
    msgs.append(message)
    save_json("meshtastic_messages", msgs)
    log_audit("send_message", "system", {"to": data.get("from")})
    return {"status": "success", "message": message}

# Global variable to store active meshtastic connection
_active_meshtastic_connection = None
_active_meshtastic_port = None
_meshtastic_connection_lock = threading.Lock()
_meshtastic_port_operation_lock = threading.Lock()  # Lock for exclusive port operations (preview/import)

# Global gateway service instance (runs in separate thread)
_gateway_service = None
_gateway_thread = None
_gateway_service_lock = threading.Lock()

# Global CoT listener service instance
_cot_listener_service = None
_cot_listener_lock = threading.Lock()

def _close_meshtastic_interface(iface, port_name: str = "unknown", operation: str = "operation"):
    """
    Safely close a Meshtastic interface with proper error handling and logging.
    
    Args:
        iface: The Meshtastic interface object to close
        port_name: Name of the port for logging
        operation: Description of the operation for logging
    
    Returns:
        bool: True if closed successfully, False otherwise
    """
    if iface is None:
        logger.debug(f"[Port:{port_name}] No interface to close for {operation}")
        return True
    
    try:
        if hasattr(iface, "close"):
            logger.info(f"[Port:{port_name}] Closing interface after {operation}")
            iface.close()
            logger.info(f"[Port:{port_name}] Interface closed successfully")
            return True
        else:
            logger.warning(f"[Port:{port_name}] Interface has no close() method")
            return False
    except Exception as e:
        logger.error(f"[Port:{port_name}] Error closing interface after {operation}: {e}")
        return False

def _check_and_close_persistent_connection(port: str, operation: str = "operation"):
    """
    Check if there's a persistent connection on the same port and close it.
    
    Args:
        port: The port to check
        operation: Description of the operation requesting the check
    
    Returns:
        bool: True if no conflict or conflict resolved, False if port might be busy
    """
    global _active_meshtastic_connection, _active_meshtastic_port
    
    with _meshtastic_connection_lock:
        if _active_meshtastic_connection and _active_meshtastic_port == port:
            logger.warning(f"[Port:{port}] Persistent connection exists on port, closing for exclusive {operation}")
            _close_meshtastic_interface(_active_meshtastic_connection, port, "persistent connection cleanup")
            _active_meshtastic_connection = None
            _active_meshtastic_port = None
            logger.info(f"[Port:{port}] Persistent connection closed, port now free for {operation}")
            return True
        elif _active_meshtastic_connection and _active_meshtastic_port:
            logger.info(f"[Port:{port}] Persistent connection exists on different port ({_active_meshtastic_port}), no conflict")
            return True
        else:
            logger.info(f"[Port:{port}] No persistent connection, port is free for {operation}")
            return True

class PortOperationLock:
    """
    Context manager for acquiring and releasing the port operation lock.
    Ensures consistent error handling and logging across all port operations.
    """
    def __init__(self, port: str, operation: str = "operation", timeout: int = 30):
        self.port = port
        self.operation = operation
        self.timeout = timeout
        self.acquired = False
    
    def __enter__(self):
        logger.info(f"[Port:{self.port}] {self.operation.capitalize()} request received")
        self.acquired = _meshtastic_port_operation_lock.acquire(blocking=True, timeout=self.timeout)
        if not self.acquired:
            logger.error(f"[Port:{self.port}] Failed to acquire port operation lock (timeout)")
            raise HTTPException(status_code=503, detail="Port is busy - another operation is in progress. Please try again in a moment.")
        logger.info(f"[Port:{self.port}] Acquired exclusive lock for {self.operation}")
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.acquired:
            _meshtastic_port_operation_lock.release()
            logger.info(f"[Port:{self.port}] Released exclusive lock after {self.operation}")
        return False  # Don't suppress exceptions

@app.post("/api/meshtastic/connect")
async def meshtastic_connect(data: dict = Body(...)):
    """
    Connect to a Meshtastic device on the specified COM port.
    This establishes a persistent connection that can be used for ongoing communication.
    
    Port Management:
    - Creates persistent connection (not for preview/import)
    - Uses connection lock to prevent concurrent access
    - Properly closes any existing connection first
    """
    global _active_meshtastic_connection, _active_meshtastic_port, _gateway_service
    
    port = data.get("port")
    if not port:
        raise HTTPException(status_code=400, detail="Port parameter is required")
    
    if not meshtastic:
        logger.error(f"[Port:{port}] Meshtastic library not available on server")
        raise HTTPException(status_code=503, detail="Meshtastic library not available on server")
    
    logger.info(f"[Port:{port}] === Persistent connection request ===")
    
    # Check if gateway service is running on this port and stop it first
    # This prevents PermissionError when import_nodes.html tries to connect
    # to a port that the gateway service (started by meshtastic.html) holds open
    with _gateway_service_lock:
        if _gateway_service and _gateway_service.running and _gateway_service.port == port:
            logger.warning(f"[Port:{port}] Gateway service is running on this port — stopping it to allow direct connection")
            try:
                _gateway_service.stop()
                _gateway_service = None
                logger.info(f"[Port:{port}] Gateway service stopped, waiting for OS to release port")
                time.sleep(1.5)  # Wait for OS to release serial port (Windows needs ~1s)
            except Exception as e:
                logger.error(f"[Port:{port}] Failed to stop gateway service: {e}")
    
    # Use lock to prevent concurrent connection attempts
    with _meshtastic_connection_lock:
        try:
            # Close existing connection if any
            if _active_meshtastic_connection:
                old_port = _active_meshtastic_port or "unknown"
                logger.info(f"[Port:{old_port}] Closing existing persistent connection")
                _close_meshtastic_interface(_active_meshtastic_connection, old_port, "existing connection cleanup")
                _active_meshtastic_connection = None
                _active_meshtastic_port = None
            
            # Try to establish new connection using the same robust pattern as preview
            logger.info(f"[Port:{port}] Attempting to establish persistent connection")
            iface = None
            connection_error = None
            
            try:
                if hasattr(meshtastic, "serial_interface") and hasattr(meshtastic.serial_interface, "SerialInterface"):
                    logger.info(f"[Port:{port}] Trying meshtastic.serial_interface.SerialInterface(port)")
                    iface = meshtastic.serial_interface.SerialInterface(port)  # type: ignore[attr-defined]
                    logger.info(f"[Port:{port}] ✓ Successfully connected via meshtastic.serial_interface.SerialInterface")
            except Exception as e1:
                logger.warning(f"[Port:{port}] ✗ meshtastic.serial_interface.SerialInterface failed: {e1}")
                connection_error = e1
                try:
                    if hasattr(meshtastic, "SerialInterface"):
                        logger.info(f"[Port:{port}] Trying meshtastic.SerialInterface(port)")
                        iface = meshtastic.SerialInterface(port)  # type: ignore[attr-defined]
                        logger.info(f"[Port:{port}] ✓ Successfully connected via meshtastic.SerialInterface")
                        connection_error = None
                except Exception as e2:
                    logger.warning(f"[Port:{port}] ✗ meshtastic.SerialInterface failed: {e2}")
                    connection_error = e2
            
            if not iface:
                error_msg = f"Failed to connect to Meshtastic device on port {port}"
                if connection_error:
                    error_msg += f": {str(connection_error)}"
                logger.error(f"[Port:{port}] {error_msg}")
                logger.error(f"[Port:{port}] Port status: FAILED - Device might be busy, not found, or permission denied")
                raise HTTPException(status_code=500, detail=error_msg)
            
            # Store the connection
            _active_meshtastic_connection = iface
            _active_meshtastic_port = port
            
            # Give the device time to initialize (use async sleep)
            await asyncio.sleep(1)
            
            logger.info(f"[Port:{port}] ✓ Persistent connection established successfully")
            logger.info(f"[Port:{port}] Port status: CONNECTED (persistent)")
            log_audit("meshtastic_connect", "system", {"port": port})
            
            return {
                "status": "success",
                "message": f"Connected to Meshtastic device on {port}",
                "port": port,
                "connected": True
            }
            
        except HTTPException:
            raise
        except Exception as e:
            logger.exception(f"[Port:{port}] ✗ Error connecting to Meshtastic device: {e}")
            logger.error(f"[Port:{port}] Port status: ERROR - {type(e).__name__}")
            raise HTTPException(status_code=500, detail=f"Connection failed: {str(e)}")

@app.post("/api/meshtastic/disconnect")
def meshtastic_disconnect():
    """
    Disconnect from the active Meshtastic device.
    
    Port Management:
    - Closes persistent connection if exists
    - Uses connection lock for thread safety
    """
    global _active_meshtastic_connection, _active_meshtastic_port
    
    # Use lock to prevent concurrent access
    with _meshtastic_connection_lock:
        if not _active_meshtastic_connection:
            logger.info("[Disconnect] No active persistent connection to close")
            return {"status": "success", "message": "No active connection", "connected": False}
        
        try:
            port = _active_meshtastic_port or "unknown"
            logger.info(f"[Port:{port}] Disconnecting persistent connection")
            
            _close_meshtastic_interface(_active_meshtastic_connection, port, "disconnect request")
            _active_meshtastic_connection = None
            _active_meshtastic_port = None
            
            logger.info(f"[Port:{port}] Persistent connection closed successfully")
            logger.info(f"[Port:{port}] Port status: FREE")
            log_audit("meshtastic_disconnect", "system", {"port": port})
            
            return {
                "status": "success",
                "message": f"Disconnected from {port}",
                "connected": False
            }
        except Exception as e:
            port = _active_meshtastic_port or "unknown"
            logger.exception(f"[Port:{port}] Error disconnecting from Meshtastic device: {e}")
            # Force clear the connection even on error
            _active_meshtastic_connection = None
            _active_meshtastic_port = None
            raise HTTPException(status_code=500, detail=f"Disconnect failed: {str(e)}")

@app.get("/api/meshtastic/connection_status")
def meshtastic_connection_status():
    """
    Get the current Meshtastic connection status.
    
    Port Management:
    - Read-only operation
    - Uses connection lock for thread-safe read
    """
    global _active_meshtastic_connection, _active_meshtastic_port
    
    # Use lock for thread-safe read
    with _meshtastic_connection_lock:
        if _active_meshtastic_connection and _active_meshtastic_port:
            logger.debug(f"[Port:{_active_meshtastic_port}] Connection status: CONNECTED (persistent)")
            return {
                "connected": True,
                "port": _active_meshtastic_port,
                "status": "connected"
            }
        else:
            logger.debug("[Connection] Connection status: DISCONNECTED")
            return {
                "connected": False,
                "port": None,
                "status": "disconnected"
            }

# -------------------------
# Serial port scanning endpoint
# -------------------------
@app.get("/api/scan_ports")
def api_scan_ports():
    """
    Return a list of available serial ports. Keeps device names like COM7 intact.
    If pyserial is not installed, return an empty list.
    
    Port Management:
    - Only scans for available ports, does not open them
    - No locking required as this is read-only operation
    """
    logger.info("[PortScan] Scanning for available serial ports")
    
    if serial_list_ports is None:
        logger.warning("[PortScan] pyserial not available; returning empty list")
        return []
    
    try:
        ports = []
        comports = list(serial_list_ports.comports())
        logger.info(f"[PortScan] Found {len(comports)} serial port(s)")
        
        for p in comports:
            port_info = {
                "device": getattr(p, "device", "") or getattr(p, "name", "") or "",
                "description": getattr(p, "description", ""),
                "hwid": getattr(p, "hwid", ""),
                "manufacturer": getattr(p, "manufacturer", None),
                "vid": getattr(p, "vid", None),
                "pid": getattr(p, "pid", None),
                "serial_number": getattr(p, "serial_number", None)
            }
            ports.append(port_info)
            logger.info(f"[PortScan] Port found: {port_info['device']} - {port_info['description']}")
        
        logger.info(f"[PortScan] Scan complete: {len(ports)} port(s) available")
        return ports
    except Exception as e:
        logger.exception("[PortScan] Port scan failed: %s", e)
        raise HTTPException(status_code=500, detail=f"Port scan failed: {str(e)}")

# -------------------------
# Meshtastic read helpers (robust) and friendly-name-aware builder
# -------------------------
def _build_nodes_from_serial(port: str, friendly_map: Dict[str, str], default_pattern: Optional[str], use_real_if_available: bool):
    """
    Attempt to read nodes from meshtastic library (multiple constructor patterns) and apply friendly_map.
    Returns empty list if device connection fails (no simulation fallback).
    Returns tuple: (list of normalized node dicts with 'name' resolved and lat/lng numeric (0.0 fallback), is_simulated: bool)
    Note: is_simulated is always False now, kept for API compatibility.
    
    Port Management:
    - Uses exclusive lock during operation
    - Properly closes interface on success and failure
    - Logs all port access attempts and results
    """
    nodes_result = []
    is_simulated = False

    def resolve_friendly(mesh_id, raw_user, fallback_name=None):
        # Prefer device-provided names first (longName, shortName)
        try:
            if isinstance(raw_user, dict):
                if raw_user.get("longName"):
                    return raw_user.get("longName")
                if raw_user.get("shortName"):
                    return raw_user.get("shortName")
        except Exception:
            pass
        # Then check friendly_map variants
        if mesh_id is not None:
            s = str(mesh_id)
            variants = [s]
            if s.startswith("!"):
                variants.append(s.lstrip("!"))
            else:
                variants.append("!" + s)
            for v in variants:
                if v in friendly_map:
                    return friendly_map[v]
        # fallback to provided fallback_name or generated node-id
        return fallback_name or (f"node-{str(mesh_id)[:8]}" if mesh_id else None)

    # Check if meshtastic library is available
    if not use_real_if_available:
        logger.error(f"[Port:{port}] Real device connection disabled (use_real_if_available=False)")
        return [], False
    
    if not meshtastic:
        logger.error(f"[Port:{port}] Meshtastic library not available on server")
        return [], False

    # Try meshtastic library if available
    iface = None
    connection_error = None
    reused_persistent = False
    
    try:
        logger.info(f"[Port:{port}] === Starting Meshtastic port access operation ===")
        logger.info(f"[Port:{port}] Attempting to connect to Meshtastic device")
        
        # Try to reuse existing persistent connection on the same port
        # This avoids port release timing issues on Windows
        with _meshtastic_connection_lock:
            if _active_meshtastic_connection and _active_meshtastic_port == port:
                logger.info(f"[Port:{port}] Reusing existing persistent connection for node reading")
                iface = _active_meshtastic_connection
                reused_persistent = True
        
        if not iface:
            # Check and close any persistent connection on this port
            _check_and_close_persistent_connection(port, "preview/import operation")
            
            # Also check if gateway service is running on this port and stop it
            with _gateway_service_lock:
                global _gateway_service
                if _gateway_service and _gateway_service.running and _gateway_service.port == port:
                    logger.warning(f"[Port:{port}] Gateway service is running on this port — stopping it for preview/import")
                    try:
                        _gateway_service.stop()
                        _gateway_service = None
                        logger.info(f"[Port:{port}] Gateway service stopped for preview/import")
                    except Exception as e:
                        logger.error(f"[Port:{port}] Failed to stop gateway service: {e}")
            
            # Delay to allow OS to release serial port after closing (Windows needs ~0.5s)
            time.sleep(0.5)
        
        # Try various constructor access patterns defensively
        try:
            if hasattr(meshtastic, "serial_interface") and hasattr(meshtastic.serial_interface, "SerialInterface"):
                logger.info(f"[Port:{port}] Trying meshtastic.serial_interface.SerialInterface(port)")
                iface = meshtastic.serial_interface.SerialInterface(port)  # type: ignore[attr-defined]
                logger.info(f"[Port:{port}] ✓ Successfully connected via meshtastic.serial_interface.SerialInterface")
        except Exception as e1:
            logger.warning(f"[Port:{port}] ✗ meshtastic.serial_interface.SerialInterface failed: {e1}")
            connection_error = e1
            try:
                if hasattr(meshtastic, "SerialInterface"):
                    logger.info(f"[Port:{port}] Trying meshtastic.SerialInterface(port)")
                    iface = meshtastic.SerialInterface(port)  # type: ignore[attr-defined]
                    logger.info(f"[Port:{port}] ✓ Successfully connected via meshtastic.SerialInterface")
                    connection_error = None
            except Exception as e2:
                logger.warning(f"[Port:{port}] ✗ meshtastic.SerialInterface failed: {e2}")
                connection_error = e2
                # try no-arg constructors
                try:
                    if hasattr(meshtastic, "serial_interface") and hasattr(meshtastic.serial_interface, "SerialInterface"):
                        logger.info(f"[Port:{port}] Trying meshtastic.serial_interface.SerialInterface() no-arg")
                        iface = meshtastic.serial_interface.SerialInterface()
                        logger.info(f"[Port:{port}] ✓ Successfully connected via meshtastic.serial_interface.SerialInterface() no-arg")
                        connection_error = None
                except Exception as e3:
                    logger.warning(f"[Port:{port}] ✗ meshtastic.serial_interface.SerialInterface() no-arg failed: {e3}")
                    connection_error = e3
                    try:
                        logger.info(f"[Port:{port}] Trying meshtastic.SerialInterface() no-arg")
                        iface = meshtastic.SerialInterface()
                        logger.info(f"[Port:{port}] ✓ Successfully connected via meshtastic.SerialInterface() no-arg")
                        connection_error = None
                    except Exception as e4:
                        logger.warning(f"[Port:{port}] ✗ meshtastic.SerialInterface() no-arg failed: {e4}")
                        connection_error = e4
                        iface = None

        if iface:
            logger.info(f"[Port:{port}] Interface connected successfully, waiting for device initialization...")
            # Give the device time to populate nodes (important for real devices)
            time.sleep(2)
            
            # Attempt to read nodes from different attributes / methods
            logger.info(f"[Port:{port}] Attempting to read nodes from device")
            nodes_obj = getattr(iface, "nodes", None) or {}
            logger.info(f"[Port:{port}] Got nodes object, type: {type(nodes_obj)}, length: {len(nodes_obj) if hasattr(nodes_obj, '__len__') else 'N/A'}")
            if not nodes_obj:
                getNodes = getattr(iface, "getNodes", None) or getattr(iface, "get_nodes", None)
                if callable(getNodes):
                    try:
                        nodes_obj = getNodes() or {}
                        logger.info(f"[Port:{port}] Got nodes via getNodes(), length: {len(nodes_obj) if hasattr(nodes_obj, '__len__') else 'N/A'}")
                    except Exception as e:
                        logger.warning(f"[Port:{port}] getNodes() failed: {e}")
                        nodes_obj = nodes_obj or {}
            if not nodes_obj:
                nodes_obj = getattr(iface, "remoteNodes", None) or getattr(iface, "node_cache", None) or {}
                logger.info(f"[Port:{port}] Trying remoteNodes/node_cache, got: {type(nodes_obj)}")


            # Normalize nodes_obj items
            items = []
            if isinstance(nodes_obj, dict):
                items = list(nodes_obj.items())
                logger.info(f"[Port:{port}] Processing {len(items)} nodes from dict")
            else:
                try:
                    # nodes_obj might be iterable of node objects
                    items = [(getattr(n, "id", str(i)), n) for i, n in enumerate(nodes_obj)]
                    logger.info(f"[Port:{port}] Processing {len(items)} nodes from iterable")
                except Exception as e:
                    logger.warning(f"[Port:{port}] Failed to iterate nodes_obj: {e}")
                    items = []

            for key, raw in items:
                try:
                    # Convert node object to dict if needed
                    if not isinstance(raw, dict):
                        # Try to get the raw dict representation
                        if hasattr(raw, '__dict__'):
                            raw = raw.__dict__
                        elif hasattr(raw, 'raw'):
                            raw = getattr(raw, "raw", None)
                        elif callable(getattr(raw, "to_dict", None)):
                            raw = raw.to_dict()
                    
                    if raw is None or not isinstance(raw, dict):
                        logger.debug(f"[Port:{port}] Skipping node {key}, no valid dict representation")
                        continue
                    
                    # Extract mesh_id
                    mesh_id = raw.get("num") or raw.get("mesh_id") or raw.get("meshId") or raw.get("id") or key
                    if isinstance(raw.get("user"), dict) and raw.get("user").get("id"):
                        mesh_id = raw["user"]["id"]
                    
                    # Extract user info - CRITICAL for longName/shortName
                    user = {}
                    if isinstance(raw.get("user"), dict):
                        user = raw["user"]
                    
                    # Get fallback name
                    fallback_name = raw.get("name") or raw.get("callsign") or None
                    
                    # Resolve the display name using priority: friendly_map, longName, shortName, fallback
                    name = resolve_friendly(mesh_id, user, fallback_name)
                    if not name:
                        name = fallback_name or f"node-{str(mesh_id)[:8]}"
                    
                    # Extract position data
                    lat = raw.get("lat")
                    lng = raw.get("lng")
                    pos = raw.get("position")
                    
                    # Try position sub-object if top-level lat/lng not present
                    if pos and isinstance(pos, dict):
                        if lat is None:
                            lat = pos.get("latitude") or pos.get("latitudeI")
                        if lng is None:
                            lng = pos.get("longitude") or pos.get("longitudeI")
                    
                    # convert microdegree ints if present (common in Meshtastic)
                    try:
                        if isinstance(lat, int) and abs(lat) > 1e6:
                            lat = float(lat) / 1e7
                    except Exception:
                        pass
                    try:
                        if isinstance(lng, int) and abs(lng) > 1e6:
                            lng = float(lng) / 1e7
                    except Exception:
                        pass
                    
                    # Ensure numeric lat/lng and default to 0.0 only if truly missing
                    try:
                        latf = float(lat) if lat is not None and str(lat) != "" else 0.0
                    except Exception:
                        latf = 0.0
                    try:
                        lngf = float(lng) if lng is not None and str(lng) != "" else 0.0
                    except Exception:
                        lngf = 0.0
                    
                    # Validate coordinate ranges
                    if not (abs(latf) <= 90 and abs(lngf) <= 180):
                        logger.warning(f"[Port:{port}] Invalid coordinates for node {mesh_id}: lat={latf}, lng={lngf}, setting to 0.0")
                        latf = 0.0
                        lngf = 0.0

                    # Extract longName and shortName for frontend convenience
                    longName = None
                    shortName = None
                    if isinstance(user, dict):
                        longName = user.get("longName") or user.get("longname")
                        shortName = user.get("shortName") or user.get("shortname")
                    
                    logger.debug(f"[Port:{port}] Node {mesh_id}: longName={longName}, shortName={shortName}, name={name}, coords=({latf}, {lngf})")

                    node_rec = {
                        "id": raw.get("id") or str(uuid.uuid4()),
                        "device": raw.get("device") or port,
                        "name": str(name),
                        "mesh_id": str(mesh_id),
                        "longName": longName,
                        "shortName": shortName,
                        "battery": raw.get("battery") or (raw.get("deviceMetrics") and raw.get("deviceMetrics").get("batteryLevel")),
                        "snr": raw.get("snr"),
                        "rssi": raw.get("rssi"),
                        "last_heard": raw.get("last_heard") or raw.get("lastHeard") or int(datetime.now(timezone.utc).timestamp()),
                        "lat": latf,
                        "lng": lngf,
                        "callsign": raw.get("callsign") or None,
                        "imported_from": port,
                        "raw": raw
                    }
                    nodes_result.append(node_rec)
                except Exception as e:
                    logger.exception(f"[Port:{port}] Error normalizing node from meshtastic: {key}, error: {e}")
            
            logger.info(f"[Port:{port}] Successfully extracted {len(nodes_result)} real nodes from device")
            
            # Only close the interface if we created a new one (not reusing persistent)
            if not reused_persistent:
                _close_meshtastic_interface(iface, port, "node extraction")
            
            if nodes_result:
                logger.info(f"[Port:{port}] === Operation completed successfully: {len(nodes_result)} nodes ===")
                return nodes_result, is_simulated
            else:
                logger.warning(f"[Port:{port}] No nodes found from device")
                logger.info(f"[Port:{port}] === Operation completed: 0 nodes found ===")
                return [], False
        else:
            # Failed to create interface
            error_detail = str(connection_error) if connection_error else "Unknown error"
            logger.error(f"[Port:{port}] ✗ Failed to create interface: {error_detail}")
            logger.error(f"[Port:{port}] Port status: FAILED TO OPEN - Device might be busy, not found, or permission denied")
            logger.info(f"[Port:{port}] === Operation failed: Could not connect to device ===")
            return [], False
            
    except Exception as e:
        logger.exception(f"[Port:{port}] ✗ Exception during meshtastic connection: {e}")
        logger.error(f"[Port:{port}] Port status: ERROR - {type(e).__name__}: {str(e)}")
        
        # CRITICAL: Ensure interface is closed on exception (only if not reusing persistent)
        if iface and not reused_persistent:
            _close_meshtastic_interface(iface, port, "exception cleanup")
        
        logger.info(f"[Port:{port}] === Operation failed with exception ===")
        return [], False

@app.post("/api/preview_meshtastic")
def api_preview_meshtastic(data: dict = Body(...)):
    port = (data.get("port") or data.get("device") or "unknown")
    friendly_map = data.get("friendly_names") if isinstance(data.get("friendly_names"), dict) else {}
    default_pattern = data.get("default_name_pattern")
    
    # Use context manager for exclusive port operation lock
    with PortOperationLock(port, "preview operation"):
        try:
            nodes, is_simulated = _build_nodes_from_serial(port, friendly_map, default_pattern, use_real_if_available=True)
            
            response = {
                "status": "success",
                "nodes": nodes,
                "is_simulated": is_simulated,
                "node_count": len(nodes),
                "device_connected": not is_simulated,
                "port": port
            }
            
            logger.info(f"[Port:{port}] Preview completed: {len(nodes)} nodes found")
            return response
            
        except Exception as e:
            logger.exception(f"[Port:{port}] Preview failed: %s", e)
            raise HTTPException(status_code=500, detail=f"Preview failed: {str(e)}")

@app.post("/api/import_meshtastic")
def api_import_meshtastic(data: dict = Body(...)):
    """
    Import nodes: either use provided nodes (frontend preview) or read from COM port directly,
    apply friendly names, upsert into meshtastic_nodes DB and map_markers DB.
    Missing GPS positions are set to 0.0.
    
    Port Management:
    - Uses exclusive lock when reading from port
    - Only accesses port if no nodes provided
    - Properly closes connection after use
    """
    port = (data.get("port") or data.get("device") or "unknown")
    friendly_map = data.get("friendly_names") if isinstance(data.get("friendly_names"), dict) else {}
    default_pattern = data.get("default_name_pattern")
    provided_nodes = data.get("nodes") if isinstance(data.get("nodes"), list) else None

    logger.info(f"[Port:{port}] Import request received (provided_nodes: {len(provided_nodes) if provided_nodes else 0})")

    try:
        nodes_db = load_json("meshtastic_nodes")
        if not isinstance(nodes_db, list):
            nodes_db = []
        markers_db = load_json("map_markers")
        if not isinstance(markers_db, list):
            markers_db = []

        imported = []

        def _normalize_node(raw):
            mesh_id = raw.get("mesh_id") or raw.get("id") or raw.get("meshId") or raw.get("node_id")
            user = raw.get("user") if isinstance(raw.get("user"), dict) else {}
            friendly = None

            # Priority 1: Check friendly_map (user-defined names from frontend)
            if mesh_id:
                mesh_id_str = str(mesh_id)
                # Try exact match and variants with/without "!"
                variants = [mesh_id_str]
                if mesh_id_str.startswith("!"):
                    variants.append(mesh_id_str.lstrip("!"))
                else:
                    variants.append("!" + mesh_id_str)
                for variant in variants:
                    if variant in friendly_map:
                        friendly = friendly_map[variant]
                        break
            
            # Extract longName/shortName from device (always extract for metadata)
            long_name = None
            short_name = None
            try:
                # Check top-level enrichment from frontend
                long_name = raw.get("longName")
                short_name = raw.get("shortName")
                
                # Check user object
                if not long_name:
                    long_name = user.get("longName") or user.get("longname")
                if not short_name:
                    short_name = user.get("shortName") or user.get("shortname")
                
                # Check nested raw.user structure
                if not long_name or not short_name:
                    raw_user = raw.get("raw", {}).get("user") if isinstance(raw.get("raw"), dict) else None
                    if isinstance(raw_user, dict):
                        if not long_name:
                            long_name = raw_user.get("longName")
                        if not short_name:
                            short_name = raw_user.get("shortName")
            except Exception:
                pass
            
            # Priority 2: Use device-provided names (if no user override from friendly_map)
            if not friendly:
                friendly = long_name or short_name

            # Priority 3: Node-level fields
            if not friendly:
                friendly = raw.get("name") or raw.get("callsign")

            # Priority 4: Fallback to pattern or generated name
            if not friendly:
                if default_pattern and "{id}" in default_pattern and mesh_id:
                    friendly = default_pattern.replace("{id}", str(mesh_id))
                else:
                    friendly = f"node-{str(mesh_id)[:8]}" if mesh_id else f"node-{str(uuid.uuid4())[:8]}"

            # Normalize lat/lng
            lat = raw.get("lat")
            lng = raw.get("lng")
            pos = raw.get("position") or (raw.get("raw") and raw.get("raw").get("position"))
            if pos and isinstance(pos, dict):
                lat = lat or pos.get("latitude") or pos.get("latitudeI")
                lng = lng or pos.get("longitude") or pos.get("longitudeI")
            try:
                # convert microdegree ints if present
                if isinstance(lat, int) and abs(lat) > 1e6:
                    lat = float(lat) / 1e7
            except Exception:
                pass
            try:
                if isinstance(lng, int) and abs(lng) > 1e6:
                    lng = float(lng) / 1e7
            except Exception:
                pass
            try:
                latf = float(lat) if lat is not None and str(lat) != "" else None
                lngf = float(lng) if lng is not None and str(lng) != "" else None
            except Exception:
                latf = None; lngf = None
            if latf is None or lngf is None or not (abs(latf) <= 90 and abs(lngf) <= 180):
                latf = 0.0; lngf = 0.0

            node_rec = {
                "id": raw.get("id") or str(uuid.uuid4()),
                "device": raw.get("device") or port,
                "name": str(friendly),
                "mesh_id": mesh_id,
                "longName": long_name,
                "shortName": short_name,
                "battery": raw.get("battery"),
                "snr": raw.get("snr"),
                "rssi": raw.get("rssi"),
                "last_heard": raw.get("last_heard") or int(datetime.now(timezone.utc).timestamp()),
                "lat": latf,
                "lng": lngf,
                "callsign": raw.get("callsign") or None,
                "imported_from": port,
                "raw": raw
            }
            return node_rec

        # If nodes are provided (from preview), use them directly
        # Otherwise, read from port (requires exclusive lock)
        if provided_nodes:
            logger.info(f"[Port:{port}] Using {len(provided_nodes)} provided nodes (no port access needed)")
            nodes_to_import = [_normalize_node(r) for r in provided_nodes]
        else:
            logger.info(f"[Port:{port}] No nodes provided, will read from port")
            
            # Use context manager for exclusive port operation lock
            with PortOperationLock(port, "import operation"):
                raw_nodes, _ = _build_nodes_from_serial(port, friendly_map, default_pattern, use_real_if_available=True)
                nodes_to_import = [_normalize_node(n) for n in raw_nodes]
                logger.info(f"[Port:{port}] Read {len(nodes_to_import)} nodes from port")

        for node_rec in nodes_to_import:
            mesh = node_rec.get("mesh_id")
            existing_node = None
            if mesh:
                for n in nodes_db:
                    if n.get("mesh_id") == mesh:
                        for k, v in node_rec.items():
                            if v is not None:
                                n[k] = v
                        existing_node = n
                        break
            else:
                for n in nodes_db:
                    if n.get("name") == node_rec.get("name") or n.get("id") == node_rec.get("id"):
                        for k, v in node_rec.items():
                            if v is not None:
                                n[k] = v
                        existing_node = n
                        break
            if not existing_node:
                nodes_db.append(node_rec)
                existing_node = node_rec
                imported.append(existing_node)

            try:
                lat_val = float(existing_node.get("lat")) if existing_node.get("lat") is not None else 0.0
                lng_val = float(existing_node.get("lng")) if existing_node.get("lng") is not None else 0.0
            except Exception:
                lat_val = 0.0; lng_val = 0.0
            # ensure valid ranges, else 0.0
            if not (abs(lat_val) <= 90 and abs(lng_val) <= 180):
                lat_val = 0.0; lng_val = 0.0
            existing_node["lat"] = lat_val
            existing_node["lng"] = lng_val

            marker_matched = None
            for m in markers_db:
                if m.get("unit_id") and mesh and str(m.get("unit_id")) == str(mesh):
                    marker_matched = m
                    break
                mname = (m.get("name") or "").lower()
                if mesh and str(mesh).lower() in mname:
                    marker_matched = m
                    break
                if (m.get("name") or "") == existing_node.get("name"):
                    marker_matched = m
                    break

            if marker_matched:
                marker_matched["lat"] = float(existing_node["lat"])
                marker_matched["lng"] = float(existing_node["lng"])
                marker_matched["name"] = f"{existing_node.get('device') or ''} = {existing_node.get('name')}"
                marker_matched["timestamp"] = datetime.now().isoformat()
                marker_matched["created_by"] = marker_matched.get("created_by", "import_meshtastic")
                marker_matched["unit_id"] = mesh
            else:
                new_marker = {
                    "id": str(uuid.uuid4()),
                    "lat": float(existing_node["lat"]),
                    "lng": float(existing_node["lng"]),
                    "name": f"{existing_node.get('device') or ''} = {existing_node.get('name')}",
                    "unit_id": mesh,
                    "status": "BASE",
                    "timestamp": datetime.now().isoformat(),
                    "created_by": "import_meshtastic"
                }
                markers_db.append(new_marker)

        save_json("meshtastic_nodes", nodes_db)
        save_json("map_markers", markers_db)

        # Also persist to SQLAlchemy MeshtasticNode table for sync worker and map
        db = SessionLocal()
        try:
            for node_rec in nodes_to_import:
                mesh = node_rec.get("mesh_id") or node_rec.get("id")
                if not mesh:
                    continue
                existing_db = db.query(MeshtasticNode).filter(MeshtasticNode.id == str(mesh)).first()
                try:
                    safe_lat = float(node_rec.get("lat", 0.0))
                    safe_lng = float(node_rec.get("lng", 0.0))
                except (ValueError, TypeError):
                    safe_lat = 0.0
                    safe_lng = 0.0
                if existing_db:
                    existing_db.long_name = node_rec.get("longName") or node_rec.get("name")
                    existing_db.short_name = node_rec.get("shortName") or node_rec.get("name")
                    existing_db.lat = safe_lat
                    existing_db.lng = safe_lng
                    existing_db.last_heard = datetime.now(timezone.utc)
                    existing_db.is_online = True
                    existing_db.raw_data = node_rec
                else:
                    new_node = MeshtasticNode(
                        id=str(mesh),
                        long_name=node_rec.get("longName") or node_rec.get("name"),
                        short_name=node_rec.get("shortName") or node_rec.get("name"),
                        lat=safe_lat,
                        lng=safe_lng,
                        last_heard=datetime.now(timezone.utc),
                        is_online=True,
                        raw_data=node_rec
                    )
                    db.add(new_node)
            db.commit()
        except Exception as db_err:
            db.rollback()
            logger.warning("Failed to persist imported nodes to SQLAlchemy: %s", db_err)
        finally:
            db.close()

        log_audit("import_meshtastic", "system", {"port": port, "imported": len(imported)})
        
        logger.info(f"[Port:{port}] Import completed successfully: {len(imported)} nodes imported")
        return {"status": "success", "imported": len(imported), "nodes": imported}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"[Port:{port}] Import failed: %s", e)
        raise HTTPException(status_code=500, detail=f"Import fehlgeschlagen: {str(e)}")

# -------------------------
# Meshtastic Gateway Import (Option A - API-based)
# -------------------------
@app.post("/api/meshtastic/import")
async def import_meshtastic_nodes(
    file: UploadFile = File(None),
    json_data: str = Form(None),
    gateway_data: str = Form(None)
):
    """
    Import Meshtastic nodes via Gateway JSON export.
    Accepts either:
    1. File upload (JSON from Gateway export)
    2. Direct JSON data (from frontend paste) — field: json_data or gateway_data
    
    Persists imported nodes to:
    - meshtastic_nodes JSON DB (for /api/meshtastic/nodes)
    - SQLAlchemy MeshtasticNode table (for sync worker and map markers)
    
    Returns: List of imported nodes with success/error status
    """
    try:
        # Import the Gateway parser module
        from meshtastic_gateway_parser import parse_meshtastic_node, validate_node_for_import
        
        # Accept both field names for backward compatibility
        raw_json = json_data or gateway_data
        
        # Validate that at least one input method is provided
        if not file and not raw_json:
            raise HTTPException(
                status_code=400, 
                detail="No data provided. Please upload a file or paste JSON data."
            )
        
        # Parse input (file or JSON string)
        nodes_data = []
        if file:
            content = await file.read()
            try:
                nodes_data = json.loads(content)
            except json.JSONDecodeError as e:
                raise HTTPException(
                    status_code=400, 
                    detail=f"The uploaded file is not valid JSON. Please ensure you exported the correct format from Meshtastic Gateway. Error: {str(e)}"
                )
        elif raw_json:
            try:
                nodes_data = json.loads(raw_json)
            except json.JSONDecodeError as e:
                raise HTTPException(
                    status_code=400, 
                    detail=f"The pasted data is not valid JSON. Please check the format. Error: {str(e)}"
                )
        
        # Ensure nodes_data is a list - simplify the logic
        if isinstance(nodes_data, dict):
            # Single node - wrap in list
            nodes_data = [nodes_data]
        elif not isinstance(nodes_data, list):
            raise HTTPException(
                status_code=400, 
                detail="Data must be a JSON array of nodes or a single node object"
            )
        
        # Parse each node using Gateway logic
        imported = []
        errors = []
        
        # Load existing nodes from JSON DB
        nodes_db = load_json("meshtastic_nodes")
        if not isinstance(nodes_db, list):
            nodes_db = []
        
        for raw_node in nodes_data:
            try:
                # Parse the node
                parsed = parse_meshtastic_node(raw_node)
                
                # Validate the node
                is_valid, error_reason = validate_node_for_import(parsed)
                
                if is_valid:
                    try:
                        lat_val = float(parsed['latitude']) if parsed['latitude'] is not None else 0.0
                        lng_val = float(parsed['longitude']) if parsed['longitude'] is not None else 0.0
                    except (ValueError, TypeError):
                        lat_val = 0.0
                        lng_val = 0.0

                    # Build a node record compatible with /api/meshtastic/nodes format
                    node_rec = {
                        'id': parsed['id'],
                        'mesh_id': parsed['id'],
                        'name': parsed['callsign'],
                        'longName': parsed['callsign'],
                        'shortName': parsed['callsign'],
                        'lat': lat_val,
                        'lng': lng_val,
                        'altitude': parsed.get('altitude', 0),
                        'has_gps': parsed['has_gps'],
                        'last_heard': int(datetime.now(timezone.utc).timestamp()),
                        'imported_from': 'gateway_import',
                        'source': 'meshtastic_gateway'
                    }

                    # Check for duplicates in JSON DB
                    existing = next((n for n in nodes_db if n.get('id') == parsed['id'] or n.get('mesh_id') == parsed['id']), None)
                    
                    if existing:
                        # Update existing node
                        for k, v in node_rec.items():
                            if v is not None:
                                existing[k] = v
                        existing['updated_at'] = datetime.now().isoformat()
                        imported.append({
                            'id': parsed['id'],
                            'name': parsed['callsign'],
                            'mesh_id': parsed['id'],
                            'callsign': parsed['callsign'],
                            'action': 'updated'
                        })
                    else:
                        # Add new node
                        node_rec['created_at'] = datetime.now().isoformat()
                        nodes_db.append(node_rec)
                        imported.append({
                            'id': parsed['id'],
                            'name': parsed['callsign'],
                            'mesh_id': parsed['id'],
                            'callsign': parsed['callsign'],
                            'action': 'created'
                        })
                else:
                    errors.append({
                        'node': parsed.get('callsign', 'Unknown'),
                        'id': parsed.get('id', 'Unknown'),
                        'reason': error_reason
                    })
            except Exception as e:
                # Extract node identifier for error reporting
                node_id = 'Unknown'
                try:
                    if isinstance(raw_node, dict):
                        user = raw_node.get('user', {})
                        node_id = user.get('longName') or user.get('shortName') or str(raw_node.get('num', 'Unknown'))
                except:
                    pass
                
                errors.append({
                    'node': node_id,
                    'reason': f'Parse error: {str(e)}'
                })
        
        # Save updated nodes to JSON DB
        if imported:
            save_json("meshtastic_nodes", nodes_db)

            # Also persist to SQLAlchemy MeshtasticNode table for sync worker and map
            db = SessionLocal()
            try:
                for imp in imported:
                    node_id = imp['id']
                    # Find the full record from nodes_db
                    full_rec = next((n for n in nodes_db if n.get('id') == node_id or n.get('mesh_id') == node_id), None)
                    if not full_rec:
                        continue
                    existing_db = db.query(MeshtasticNode).filter(MeshtasticNode.id == node_id).first()
                    try:
                        safe_lat = float(full_rec.get('lat', 0.0))
                        safe_lng = float(full_rec.get('lng', 0.0))
                    except (ValueError, TypeError):
                        safe_lat = 0.0
                        safe_lng = 0.0
                    if existing_db:
                        existing_db.long_name = full_rec.get('longName') or full_rec.get('name')
                        existing_db.short_name = full_rec.get('shortName') or full_rec.get('name')
                        existing_db.lat = safe_lat
                        existing_db.lng = safe_lng
                        existing_db.altitude = full_rec.get('altitude')
                        existing_db.last_heard = datetime.now(timezone.utc)
                        existing_db.is_online = True
                        existing_db.raw_data = full_rec
                    else:
                        new_node = MeshtasticNode(
                            id=node_id,
                            long_name=full_rec.get('longName') or full_rec.get('name'),
                            short_name=full_rec.get('shortName') or full_rec.get('name'),
                            lat=safe_lat,
                            lng=safe_lng,
                            altitude=full_rec.get('altitude'),
                            last_heard=datetime.now(timezone.utc),
                            is_online=True,
                            raw_data=full_rec
                        )
                        db.add(new_node)
                db.commit()
            except Exception as db_err:
                db.rollback()
                logger.warning("Failed to persist gateway nodes to SQLAlchemy: %s", db_err)
            finally:
                db.close()

            log_audit("import_meshtastic_gateway", "system", {
                "imported": len(imported),
                "errors": len(errors),
                "source": "file" if file else "json_paste"
            })
        
        return {
            'status': 'success' if imported else 'partial' if errors else 'no_data',
            'success_count': len(imported),
            'error_count': len(errors),
            'imported_nodes': imported,
            'error_details': errors,
            'message': f'Successfully imported {len(imported)} node(s). {len(errors)} error(s).'
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Gateway import failed: %s", e)
        raise HTTPException(status_code=500, detail=f"Import failed: {str(e)}")

# -------------------------
# Ingest single node endpoint (gateway or other producers may use)
# -------------------------
@app.post("/api/ingest_node")
def api_ingest_node(data: dict = Body(...)):
    """
    Upsert a single node record pushed by a gateway or other service.
    Ensures lat/lng numeric and sets 0.0 when missing.
    """
    db = SessionLocal()
    try:
        node_data = dict(data or {})
        
        # 1. Extract Identifiers
        mesh_id = node_data.get("mesh_id") or node_data.get("meshId") or node_data.get("id") or None
        user = node_data.get("user", {}) if isinstance(node_data.get("user"), dict) else {}
        
        # 2. Determine Friendly Name
        friendly = (node_data.get("name") or node_data.get("friendly") or user.get("longName") or user.get("shortName") or node_data.get("callsign") or None)
        if not friendly:
            friendly = f"node-{str(mesh_id)[:8] if mesh_id else str(uuid.uuid4())[:8]}"

        # 3. Normalize Lat/Lng
        lat = node_data.get("lat")
        lng = node_data.get("lng")
        pos = node_data.get("position") or (node_data.get("raw") and node_data.get("raw").get("position"))
        if pos and isinstance(pos, dict):
            lat = lat or pos.get("latitude") or pos.get("latitudeI")
            lng = lng or pos.get("longitude") or pos.get("longitudeI")
            
        try:
            # Microdegrees
            if isinstance(lat, int) and abs(lat) > 1e6: lat = float(lat) / 1e7
            if isinstance(lng, int) and abs(lng) > 1e6: lng = float(lng) / 1e7
        except: pass
        
        try:
            latf = float(lat) if lat is not None and str(lat) != "" else 0.0
            lngf = float(lng) if lng is not None and str(lng) != "" else 0.0
        except: latf = 0.0; lngf = 0.0
        
        if not (abs(latf) <= 90 and abs(lngf) <= 180): latf = 0.0; lngf = 0.0

        # 4. Upsert MeshtasticNode
        updated = False
        existing_node = None
        
        # Try to find existing node
        query = db.query(MeshtasticNode)
        if mesh_id:
            existing_node = query.filter(MeshtasticNode.id == str(mesh_id)).first()
        
        if not existing_node and node_data.get("id"):
             existing_node = query.filter(MeshtasticNode.id == str(node_data.get("id"))).first()
             
        if existing_node:
            # Update
            existing_node.lat = latf
            existing_node.lng = lngf
            existing_node.last_heard = datetime.now(timezone.utc)
            existing_node.battery_level = node_data.get("battery")
            existing_node.long_name = friendly # Update name if needed?
            existing_node.hardware_model = node_data.get("hardware")
            existing_node.raw_data = node_data
            updated = True
        else:
            # Create
            node_id = str(mesh_id) if mesh_id else (node_data.get("id") or str(uuid.uuid4()))
            existing_node = MeshtasticNode(
                id=node_id,
                long_name=friendly,
                lat=latf,
                lng=lngf,
                last_heard=datetime.now(timezone.utc),
                battery_level=node_data.get("battery"),
                hardware_model=node_data.get("hardware"),
                raw_data=node_data,
                is_online=True
            )
            db.add(existing_node)

        # 5. Upsert MapMarker
        # Try to find corresponding marker
        marker = None
        # Strategy: Match by 'unit_id' in data JSON, or by name convention
        marker_name_convention = f"{node_data.get('device') or ''} = {friendly}"
        
        # Try finding by unit_id in data
        # Note: querying JSON 'data' field depends on DB backend capability. SQLite supports JSON.
        # But to be safe and simple, let's check name matching too as fallback.
        
        # Optimization: Fetch potential markers
        # For strict correctness we should perhaps use a dedicated column implementation in Phase 4.
        # For now, searching by name is the legacy behavior we preserve.
        
        marker = db.query(MapMarker).filter(MapMarker.name == marker_name_convention).first()
        
        if marker:
             marker.lat = latf
             marker.lng = lngf
             marker.timestamp = datetime.now(timezone.utc) # Note: MapMarker model uses created_at, update it? 
             # MapMarker doesn't have updated_at default, let's assume we just update position
        else:
            marker = MapMarker(
                id=str(uuid.uuid4()),
                name=marker_name_convention,
                lat=latf,
                lng=lngf,
                type="node",
                created_by="ingest_node",
                data={"unit_id": existing_node.id, "hardware": existing_node.hardware_model}
            )
            db.add(marker)
        
        db.commit()
        db.refresh(existing_node)

        # Forward live node position to the TAK server so TAK distributes it globally.
        if _forward_meshtastic_node_to_tak(str(existing_node.id), friendly, latf, lngf):
            logger.info("Meshtastic node %s forwarded to TAK server", existing_node.id)

        log_audit("ingest_node", "gateway", {"mesh_id": mesh_id, "name": friendly})
        
        return {
            "status": "success", 
            "updated": updated, 
            "mesh_id": mesh_id, 
            "node": {
                "id": existing_node.id, 
                "name": existing_node.long_name, 
                "lat": existing_node.lat, 
                "lng": existing_node.lng
            }
        }

    except Exception as e:
        db.rollback()
        logger.exception("ingest_node failed: %s", e)
        raise HTTPException(status_code=500, detail="Ingest failed")
    finally:
        db.close()

# -------------------------
# Config, Health, Stats, Audit
# -------------------------
@app.get("/api/health")
def health():
    return {"status": "ok", "timestamp": datetime.now().isoformat()}

@app.get("/api/server_info")
def get_server_info():
    """Get server information including local IP address."""
    local_ip, all_ips = get_local_ip()
    
    # Detect protocol based on SSL certificate presence
    cert_file = os.path.join(base_path, "cert.pem")
    key_file = os.path.join(base_path, "key.pem")
    use_ssl = os.path.exists(cert_file) and os.path.exists(key_file)
    protocol = "https" if use_ssl else "http"
    
    return {
        "status": "ok",
        "local_ip": local_ip,
        "all_detected_ips": all_ips,
        "port": 8101,
        "base_url": f"{protocol}://{local_ip}:8101",
        "timestamp": datetime.now().isoformat()
    }

@app.get("/api/config")
def get_config():
    return load_json("config")

@app.post("/api/config")
def save_config(data: dict = Body(...)):
    config = load_json("config")
    if not isinstance(config, dict):
        config = {}
    config.update(data)
    save_json("config", config)
    log_audit("update_config", "system", {})
    return {"status": "success", "config": config}

@app.get("/api/stats")
def get_stats():
    users = load_json("users")
    groups = load_json("groups")
    markers = load_json("map_markers")
    nodes = load_json("nodes")
    missions = load_json("missions")
    return {
        "users": {"total": len(users), "active": len([u for u in users if u.get("active")])},
        "groups": len(groups),
        "map_markers": len(markers),
        "nodes": {"total": len(nodes), "active": len([n for n in nodes if n.get("status") == "ACTIVE"])} if isinstance(nodes, list) else {"total":0,"active":0},
        "missions": {"total": len(missions), "ongoing": len([m for m in missions if m.get("status") == "ONGOING"])} if isinstance(missions, list) else {"total":0,"ongoing":0},
        "timestamp": datetime.now().isoformat()
    }

@app.get("/api/audit_log")
def get_audit_log(limit: int = 100):
    """Get recent audit logs from DB"""
    db = SessionLocal()
    try:
        logs = db.query(AuditLog).order_by(AuditLog.timestamp.desc()).limit(limit).all()
        return [
            {
                "id": l.id,
                "timestamp": l.timestamp.isoformat() if l.timestamp else None,
                "action": l.event_type,
                "user_id": l.user,
                "details": json.loads(l.details) if l.details else {}
            } for l in logs
        ]
    finally:
        db.close()

# -------------------------
# QR endpoints with redirect logic (admin.html flow)
# -------------------------
def _get_client_ip(request: Request):
    try:
        xff = request.headers.get("x-forwarded-for")
        if xff:
            ip = xff.split(",")[0].strip()
            if ip:
                return ip
    except Exception:
        pass
    try:
        if request.client and request.client.host:
            return request.client.host
    except Exception:
        pass
    return "0.0.0.0"

@app.post("/api/qr/create")
def api_qr_create(data: dict = Body(...), request: Request = None, db: Session = Depends(get_db)):
    redirect_url = (data.get("redirect_url") or "").strip()
    if not redirect_url:
        raise HTTPException(status_code=400, detail="redirect_url required")
    max_uses = int(data.get("max_uses", 100))
    allowed_ips = data.get("allowed_ips") if isinstance(data.get("allowed_ips"), list) else []
    label = data.get("label") or "qr_redirect"
    expires_days = int(data.get("expires_days", 365))
    generate_png = bool(data.get("generate_png", False))

    token = str(uuid.uuid4())
    qr_id = str(uuid.uuid4())
    expires_at_dt = datetime.now(timezone.utc) + timedelta(days=expires_days)
    
    png_b64 = None
    qr_url = None
    if generate_png and qrcode:
        try:
            local_ip, _ = get_local_ip()
            cert_file = os.path.join(base_path, "cert.pem")
            key_file = os.path.join(base_path, "key.pem")
            protocol = "https" if (os.path.exists(cert_file) and os.path.exists(key_file)) else "http"
            qr_url = f"{protocol}://{local_ip}:8101/qr/{token}"
            qr_img = qrcode.make(qr_url)
            from io import BytesIO
            buf = BytesIO()
            qr_img.save(buf, format="PNG")
            png_b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        except Exception:
            logger.exception("QR PNG generation failed")

    new_qr = QRCode(
        id=qr_id,
        token=token,
        type="redirect",
        created_by="system",
        expires_at=expires_at_dt,
        max_uses=max_uses,
        uses=0,
        allowed_ips=allowed_ips,
        data={
            "label": label,
            "redirect_url": redirect_url,
            "qr_url": qr_url,
            "png_base64": png_b64
        }
    )
    db.add(new_qr)
    db.commit()

    log_audit("create_qr", "system", {"token": token, "created_by_ip": _get_client_ip(request) if request else None})
    return {
        "status": "success", 
        "qr": {
            "id": qr_id,
            "token": token,
            "label": label,
            "redirect_url": redirect_url,
            "qr_url": qr_url,
            "max_uses": max_uses,
            "uses": 0,
        },
        "png_base64": png_b64
    }

@app.get("/api/qr/list")
def api_qr_list(db: Session = Depends(get_db)):
    qrs = db.query(QRCode).all()
    out = []
    for q in qrs:
        out.append({
            "id": q.id,
            "token": q.token,
            "label": (q.data or {}).get("label"),
            "type": q.type,
            "redirect_url": (q.data or {}).get("redirect_url"),
            "max_uses": q.max_uses,
            "uses": q.uses,
            "expires_at": q.expires_at.isoformat() if q.expires_at else None
        })
    return out

@app.get("/api/qr/{token}")
def api_qr_info(token: str, db: Session = Depends(get_db)):
    qr = db.query(QRCode).filter(QRCode.token == token).first()
    if not qr:
        raise HTTPException(status_code=404, detail="QR token not found")
    return {
        "id": qr.id,
        "token": qr.token,
        "label": (qr.data or {}).get("label"),
        "type": qr.type,
        "redirect_url": (qr.data or {}).get("redirect_url"),
        "max_uses": qr.max_uses,
        "uses": qr.uses,
        "expires_at": qr.expires_at.isoformat() if qr.expires_at else None
    }

@app.get("/api/qr/{token}/png")
def api_qr_png(token: str, db: Session = Depends(get_db)):
    qr = db.query(QRCode).filter(QRCode.token == token).first()
    if not qr:
        raise HTTPException(status_code=404, detail="QR token not found")
    png = (qr.data or {}).get("png_base64")
    if not png:
        raise HTTPException(status_code=404, detail="PNG not available for this token")
    img_bytes = base64.b64decode(png)
    return Response(content=img_bytes, media_type="image/png")

@app.get("/qr/{token}", include_in_schema=False)
def qr_redirect(token: str, request: Request, db: Session = Depends(get_db)):
    qr = db.query(QRCode).filter(QRCode.token == token).first()
    client_ip = _get_client_ip(request)

    if not qr:
        raise HTTPException(status_code=404, detail="QR token not found")

    # Validate redirect_url exists
    redirect_url = (qr.data or {}).get("redirect_url")
    if not redirect_url or not redirect_url.strip():
        logger.error(f"QR token {token} has no redirect_url")
        raise HTTPException(status_code=500, detail="QR code configuration error: missing redirect URL")

    if qr.expires_at:
        try:
            if datetime.now(timezone.utc) > qr.expires_at:
                raise HTTPException(status_code=410, detail="QR token expired")
        except HTTPException:
            raise
        except Exception:
            pass

    uses = qr.uses
    max_uses = qr.max_uses
    if max_uses > 0 and uses >= max_uses: # max_uses = 0 means unlimited
        raise HTTPException(status_code=410, detail="QR token usage exhausted")

    allowed = qr.allowed_ips or []
    if allowed:
        import ipaddress
        allowed_ok = False
        try:
            client_addr = ipaddress.ip_address(client_ip)
            for a in allowed:
                a = str(a).strip()
                if not a:
                    continue
                try:
                    if client_ip == a:
                        allowed_ok = True
                        break
                except Exception:
                    pass
                try:
                    net = ipaddress.ip_network(a, strict=False)
                    if client_addr in net:
                        allowed_ok = True
                        break
                except Exception:
                    pass
        except Exception:
            allowed_ok = False
        if not allowed_ok:
            log_audit("qr_redirect_blocked", "system", {"token": token, "client_ip": client_ip})
            raise HTTPException(status_code=403, detail="Your IP is not allowed for this QR")

    qr.uses = uses + 1
    db.commit()
    
    log_audit("qr_redirect_success", "system", {"token": token, "client_ip": client_ip, "redirect_url": redirect_url})
    return RedirectResponse(url=redirect_url.strip(), status_code=302)

# -------------------------
# Registration QR endpoints (for user registration flow)
# -------------------------
@app.post("/api/registration_qr")
def api_create_registration_qr(data: dict = Body(...)):
    try:
        max_uses = int(data.get("max_uses", 1))
        expires_days = int(data.get("expires_days", 365))
        label = data.get("label", "registration")
        token = str(uuid.uuid4())
        entry = {
            "id": str(uuid.uuid4()),
            "token": token,
            "label": label,
            "type": "registration",  # Added type field for consolidated database
            "max_uses": max_uses,
            "uses": 0,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "expires_at": (datetime.now(timezone.utc) + timedelta(days=expires_days)).isoformat()
        }
        # Now using qr_codes (consolidated database) instead of registration_qr_codes
        qr_list = load_json("qr_codes")
        if not isinstance(qr_list, list):
            qr_list = []
        
        png_b64 = None
        if qrcode:
            try:
                qr_img = qrcode.make(token)
                from io import BytesIO
                buf = BytesIO()
                qr_img.save(buf, format="PNG")
                png_b64 = base64.b64encode(buf.getvalue()).decode("ascii")
                entry["png_base64"] = png_b64
            except Exception:
                logger.exception("QR generation failed")
        
        qr_list.append(entry)
        save_json("qr_codes", qr_list)
        
        log_audit("create_registration_qr", "system", {"token": token, "label": label})
        resp = {"status": "success", "registration_qr": entry}
        if png_b64:
            resp["png_base64"] = png_b64
        return resp
    except Exception as e:
        logger.exception("create_registration_qr failed: %s", e)
        raise HTTPException(status_code=500, detail="QR creation failed")

@app.get("/api/registration_qr")
def api_list_registration_qr():
    # Filter by type="registration" from consolidated qr_codes database
    qr_list = load_json("qr_codes")
    if not isinstance(qr_list, list):
        return []
    return [qr for qr in qr_list if qr.get("type") == "registration"]

@app.post("/api/registration_qr/use")
def api_use_registration_qr(data: dict = Body(...)):
    token = (data.get("token") or "").strip()
    if not token:
        raise HTTPException(status_code=400, detail="token required")
    # Find registration QR in consolidated database
    qr_list = load_json("qr_codes")
    reg = next((r for r in qr_list if r.get("token") == token and r.get("type") == "registration"), None)
    if not reg:
        raise HTTPException(status_code=404, detail="QR token not found")
    try:
        if reg.get("uses", 0) >= reg.get("max_uses", 1):
            raise HTTPException(status_code=400, detail="QR token max uses exceeded")
        if reg.get("expires_at"):
            try:
                if datetime.now(timezone.utc) > datetime.fromisoformat(reg.get("expires_at")):
                    raise HTTPException(status_code=400, detail="QR token expired")
            except Exception:
                pass
        reg["uses"] = reg.get("uses", 0) + 1
        save_json("qr_codes", qr_list)  # Save to consolidated qr_codes database
        log_audit("use_registration_qr", "system", {"token": token})
        return {"status": "success", "uses": reg["uses"]}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("use_registration_qr failed: %s", e)
        raise HTTPException(status_code=500, detail="QR use failed")

@app.get("/api/qr_codes")
def api_get_qr_codes():
    # Return all QR codes (both types) or filter by type if needed
    qrs = load_json("qr_codes")
    return qrs if isinstance(qrs, list) else []

def sync_meshtastic_nodes_to_map_markers_db():
    """
    Sync MeshtasticNode entries to MapMarker entries in the database.
    Creates or updates markers for each node.
    """
    db = SessionLocal()
    try:
        nodes = db.query(MeshtasticNode).all()
        synced_count = 0
        
        for node in nodes:
            # Check if marker exists for this node
            # We use created_by or some other field to link them, or just a convention name?
            # Ideally store unit_id in marker if supported. 
            # In models.py MapMarker has 'data' json field, maybe put it there?
            # Or use name matching as legacy did?
            # Legacy used: mesh_id matching
            
            # For now, let's look for marker with same data->unit_id or name
           
            # Try to find by explicit ID if we store it in data, otherwise name
            # Ideally we should add 'unit_id' to MapMarker model to be clean, but data JSON works.
            
            # Using name format: "Device = Name"
            marker_name = f"{node.short_name or node.long_name or node.id}"
            
            # Check existing
            marker = db.query(MapMarker).filter(MapMarker.name == marker_name).first()
            
            if not marker:
                # Create new
                marker = MapMarker(
                    name=marker_name,
                    lat=node.lat or 0.0,
                    lng=node.lng or 0.0,
                    type="node",
                    created_by="meshtastic_sync",
                    data={"unit_id": node.id, "hardware": node.hardware_model}
                )
                db.add(marker)
            else:
                # Update existing
                marker.lat = node.lat or 0.0
                marker.lng = node.lng or 0.0
                marker.data = {"unit_id": node.id, "hardware": node.hardware_model}
                
            synced_count += 1
            
        db.commit()
        return {"status": "success", "synced": synced_count}
    except Exception as e:
        db.rollback()
        logger.error(f"Sync Meshtastic DB failed: {e}")
        return {"status": "error", "message": str(e)}
    finally:
        db.close()

# -------------------------
# Manual trigger for node->marker sync
# -------------------------
@app.post("/api/sync_meshtastic_markers")
def api_sync_meshtastic_markers():
    res = sync_meshtastic_nodes_to_map_markers_db()
    if res.get("status") == "success":
        return res
    raise HTTPException(status_code=500, detail=res.get("message", "sync failed"))

# -------------------------
# Cleanup mesh device databases
# -------------------------
@app.post("/api/cleanup_mesh_databases")
def api_cleanup_mesh_databases():
    """
    Clean up mesh device databases to remove all stored mesh nodes and related map markers.
    This prevents conflicts between simulation data and real device data.
    """
    db = SessionLocal()
    try:
        # 1. Count current nodes
        nodes_count = db.query(MeshtasticNode).count()
        
        # 2. Delete nodes
        db.query(MeshtasticNode).delete()
        
        # 3. Delete markers created by meshtastic operations
        # Check if created_by matches 'import_meshtastic' or 'ingest_node'
        deleted_markers = db.query(MapMarker).filter(
            MapMarker.created_by.in_(["import_meshtastic", "ingest_node", "meshtastic_sync"])
        ).delete(synchronize_session=False)
        
        db.commit()

        # Also clear JSON DB files
        save_json("meshtastic_nodes", [])
        # Only remove meshtastic-created markers from JSON DB, not all markers
        markers_db = load_json("map_markers")
        if isinstance(markers_db, list):
            markers_db = [m for m in markers_db if m.get("created_by") not in ("import_meshtastic", "ingest_node", "meshtastic_sync")]
            save_json("map_markers", markers_db)
        
        # Log action
        log_audit("cleanup_mesh_databases", "system", {
            "nodes_removed": nodes_count,
            "markers_removed": deleted_markers
        })
        
        logger.info(f"Cleaned mesh databases: removed {nodes_count} nodes and {deleted_markers} markers")
        
        return {
            "status": "success",
            "nodes_removed": nodes_count,
            "markers_removed": deleted_markers,
            "message": f"Successfully cleaned mesh databases: {nodes_count} nodes and {deleted_markers} markers removed"
        }
    except Exception as e:
        db.rollback()
        logger.exception("cleanup_mesh_databases failed: %s", e)
        raise HTTPException(status_code=500, detail=f"Cleanup failed: {str(e)}")
    finally:
        db.close()

# ===========================
# Gateway Service Endpoints
# ===========================

def _gateway_broadcast_callback(event_type: str, data: Dict):
    """Callback function for gateway service to broadcast WebSocket events"""
    try:
        # Bridge incoming Meshtastic messages into the general chat channel
        if event_type == "gateway_message" and data.get("direction") == "incoming":
            db = None
            try:
                db = SessionLocal()
                _ensure_default_channels(db)
                sender = data.get("sender_name") or data.get("from") or "unknown_mesh_node"
                text = data.get("text", "")
                if text:
                    new_msg = ChatMessage(
                        channel=MESH_CHAT_CHANNEL,
                        sender=sender,
                        content=text,
                        timestamp=datetime.now(timezone.utc),
                        type="text",
                        delivered_to=[],
                        read_by=[],
                    )
                    db.add(new_msg)
                    db.commit()
                    db.refresh(new_msg)
                    msg_dict = _chat_message_to_dict(new_msg)
                    if websocket_manager and _MAIN_EVENT_LOOP:
                        asyncio.run_coroutine_threadsafe(
                            websocket_manager.publish_to_channel(
                                'chat', {"type": "new_message", "data": msg_dict}
                            ),
                            _MAIN_EVENT_LOOP
                        )
                    logger.info(f"Mesh→Chat bridge: {sender}: {text[:60]}{'...' if len(text) > 60 else ''}")
            except Exception as bridge_err:
                logger.error(f"Mesh→Chat bridge error: {bridge_err}")
            finally:
                if db is not None:
                    try:
                        db.close()
                    except Exception:
                        pass

        # Add type field
        message = {
            "type": event_type,
            **data
        }
        
        # Use data server if available, otherwise fall back to websocket manager
        if DATA_SERVER_AVAILABLE and data_server_manager:
            try:
                data_server_manager.broadcast_to_channel("gateway", message)
            except Exception as e:
                logger.warning(f"Data server broadcast failed, falling back to WebSocket: {e}")
                if websocket_manager and _MAIN_EVENT_LOOP:
                    asyncio.run_coroutine_threadsafe(
                        websocket_manager.broadcast(message),
                        _MAIN_EVENT_LOOP
                    )
        elif websocket_manager and _MAIN_EVENT_LOOP:
            asyncio.run_coroutine_threadsafe(
                websocket_manager.broadcast(message),
                _MAIN_EVENT_LOOP
            )
    except Exception as e:
        logger.error(f"Gateway broadcast callback error: {e}")

@app.post("/api/gateway/start")
async def gateway_start(data: dict = Body(...)):
    """
    Start the Meshtastic Gateway Service
    
    Body:
        port: COM port (e.g., "COM7", "/dev/ttyUSB0")
        auto_sync: Enable automatic sync (default: True)
        sync_interval: Sync interval in seconds (default: 300)
    """
    if not GATEWAY_SERVICE_AVAILABLE:
        raise HTTPException(status_code=501, detail="Gateway service not available - meshtastic/pyserial/pubsub required")
    
    global _gateway_service, _gateway_thread
    
    port = data.get("port")
    if not port:
        raise HTTPException(status_code=400, detail="Port is required")
    
    auto_sync = data.get("auto_sync", True)
    sync_interval = data.get("sync_interval", 300)
    
    with _gateway_service_lock:
        # Check if already running
        if _gateway_service and _gateway_service.running:
            return {
                "status": "already_running",
                "message": "Gateway service is already running",
                "current_port": _gateway_service.port
            }
        
        try:
            # Create gateway service instance with broadcast callback
            _gateway_service = MeshtasticGatewayService(
                port, 
                base_path=base_path,
                broadcast_callback=_gateway_broadcast_callback
            )
            
            # Start in background thread
            def run_gateway():
                success = _gateway_service.start(auto_sync=auto_sync, sync_interval=sync_interval)
                if not success:
                    logger.error("Gateway service failed to start")
            
            _gateway_thread = threading.Thread(target=run_gateway, daemon=True, name="GatewayServiceThread")
            _gateway_thread.start()
            
            # Wait a bit to check if connection succeeded
            time.sleep(2)
            
            if _gateway_service.stats["connected"]:
                logger.info(f"Gateway service started on {port}")
                
                # Broadcast status update via WebSocket
                if websocket_manager:
                    try:
                        asyncio.create_task(websocket_manager.broadcast({
                            "type": "gateway_status",
                            "status": "started",
                            "port": port,
                            "timestamp": datetime.now(timezone.utc).isoformat()
                        }))
                    except Exception as e:
                        logger.warning(f"Failed to broadcast gateway status: {e}")
                
                return {
                    "status": "success",
                    "message": f"Gateway service started on {port}",
                    "port": port,
                    "auto_sync": auto_sync,
                    "sync_interval": sync_interval
                }
            else:
                _gateway_service = None
                _gateway_thread = None
                raise HTTPException(status_code=500, detail=f"Failed to connect to device on {port}")
                
        except Exception as e:
            logger.error(f"Failed to start gateway service: {e}")
            _gateway_service = None
            _gateway_thread = None
            raise HTTPException(status_code=500, detail=f"Failed to start gateway: {str(e)}")


@app.post("/api/gateway/stop")
async def gateway_stop():
    """Stop the Meshtastic Gateway Service"""
    if not GATEWAY_SERVICE_AVAILABLE:
        raise HTTPException(status_code=501, detail="Gateway service not available")
    
    global _gateway_service, _gateway_thread
    
    with _gateway_service_lock:
        if not _gateway_service:
            return {"status": "not_running", "message": "Gateway service is not running"}
        
        try:
            port = _gateway_service.port
            _gateway_service.stop()
            
            # Wait for thread to finish
            if _gateway_thread and _gateway_thread.is_alive():
                _gateway_thread.join(timeout=5)
            
            _gateway_service = None
            _gateway_thread = None
            
            logger.info(f"Gateway service stopped")
            
            # Broadcast status update via WebSocket
            if websocket_manager:
                try:
                    asyncio.create_task(websocket_manager.broadcast({
                        "type": "gateway_status",
                        "status": "stopped",
                        "timestamp": datetime.now(timezone.utc).isoformat()
                    }))
                except Exception as e:
                    logger.warning(f"Failed to broadcast gateway status: {e}")
            
            return {
                "status": "success",
                "message": "Gateway service stopped",
                "port": port
            }
        except Exception as e:
            logger.error(f"Error stopping gateway service: {e}")
            raise HTTPException(status_code=500, detail=f"Failed to stop gateway: {str(e)}")


@app.get("/api/gateway/status")
def gateway_status():
    """Get current gateway service status"""
    if not GATEWAY_SERVICE_AVAILABLE:
        return {
            "available": False,
            "message": "Gateway service not available - meshtastic/pyserial/pubsub required"
        }
    
    with _gateway_service_lock:
        if not _gateway_service:
            return {
                "available": True,
                "running": False,
                "connected": False,
                "message": "Gateway service not started"
            }
        
        status = _gateway_service.get_status()
        status["available"] = True
        
        # Calculate uptime
        if status.get("uptime_start"):
            try:
                start_time = datetime.fromisoformat(status["uptime_start"].replace('Z', '+00:00'))
                uptime_seconds = (datetime.now(timezone.utc) - start_time).total_seconds()
                status["uptime_seconds"] = int(uptime_seconds)
            except:
                status["uptime_seconds"] = 0
        
        return status


@app.post("/api/gateway/sync")
def gateway_sync():
    """Trigger manual synchronization of all nodes"""
    if not GATEWAY_SERVICE_AVAILABLE:
        raise HTTPException(status_code=501, detail="Gateway service not available")
    
    with _gateway_service_lock:
        if not _gateway_service or not _gateway_service.running:
            raise HTTPException(status_code=400, detail="Gateway service is not running")
        
        try:
            _gateway_service.full_sync()
            return {
                "status": "success",
                "message": "Manual sync completed",
                "nodes_synced": _gateway_service.stats["nodes_synced"]
            }
        except Exception as e:
            logger.error(f"Manual sync failed: {e}")
            raise HTTPException(status_code=500, detail=f"Sync failed: {str(e)}")


@app.get("/api/gateway/ports")
def gateway_ports():
    """List available serial ports"""
    if not GATEWAY_SERVICE_AVAILABLE:
        raise HTTPException(status_code=501, detail="Gateway service not available")
    
    try:
        ports = gateway_list_ports() if gateway_list_ports else []
        return {
            "status": "success",
            "ports": ports
        }
    except Exception as e:
        logger.error(f"Failed to list ports: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to list ports: {str(e)}")


@app.post("/api/gateway/test-port")
async def gateway_test_port(data: dict = Body(...)):
    """Test connection to a serial port"""
    if not GATEWAY_SERVICE_AVAILABLE:
        raise HTTPException(status_code=501, detail="Gateway service not available")
    
    port = data.get("port")
    if not port:
        raise HTTPException(status_code=400, detail="Port is required")
    
    try:
        # Create temporary service to test connection
        test_service = MeshtasticGatewayService(port, base_path=base_path)
        
        # Try to connect
        success = test_service.connect()
        
        # Disconnect immediately
        if success:
            test_service.disconnect()
            return {
                "status": "success",
                "message": f"Successfully connected to {port}",
                "port": port
            }
        else:
            return {
                "status": "failed",
                "message": f"Failed to connect to {port}",
                "port": port
            }
    except Exception as e:
        logger.error(f"Port test failed for {port}: {e}")
        return {
            "status": "error",
            "message": str(e),
            "port": port
        }


@app.get("/api/gateway/nodes")
def gateway_nodes():
    """Get nodes imported by gateway service"""
    try:
        nodes_db_path = os.path.join(base_path, "meshtastic_nodes_db.json")
        
        if not os.path.exists(nodes_db_path):
            return {"status": "success", "nodes": []}
        
        with open(nodes_db_path, 'r', encoding='utf-8') as f:
            nodes = json.load(f)
        
        # Filter only gateway-imported nodes
        gateway_nodes = [n for n in nodes if n.get("imported_from") == "gateway_service"]
        
        return {
            "status": "success",
            "nodes": gateway_nodes,
            "count": len(gateway_nodes)
        }
    except Exception as e:
        logger.error(f"Failed to load gateway nodes: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to load nodes: {str(e)}")


@app.get("/api/gateway/messages")
def gateway_messages(limit: int = 100):
    """Get messages received by gateway service"""
    try:
        messages_db_path = os.path.join(base_path, "meshtastic_messages_db.json")
        
        if not os.path.exists(messages_db_path):
            return {"status": "success", "messages": []}
        
        with open(messages_db_path, 'r', encoding='utf-8') as f:
            messages = json.load(f)
        
        # Return last N messages
        recent_messages = messages[-limit:] if len(messages) > limit else messages
        
        return {
            "status": "success",
            "messages": recent_messages,
            "count": len(recent_messages),
            "total": len(messages)
        }
    except Exception as e:
        logger.error(f"Failed to load gateway messages: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to load messages: {str(e)}")


@app.post("/api/gateway/send-message")
async def gateway_send_message(data: dict = Body(...)):
    """Send a message via gateway service"""
    if not GATEWAY_SERVICE_AVAILABLE:
        raise HTTPException(status_code=501, detail="Gateway service not available")
    
    with _gateway_service_lock:
        if not _gateway_service or not _gateway_service.running:
            raise HTTPException(status_code=400, detail="Gateway service is not running")
        
        text = data.get("text")
        if not text:
            raise HTTPException(status_code=400, detail="Message text is required")
        
        try:
            # Send via gateway's interface
            if _gateway_service.interface:
                _gateway_service.interface.sendText(text)
                
                logger.info(f"Message sent via gateway: {text[:50]}...")
                
                # Broadcast message via WebSocket
                if websocket_manager:
                    try:
                        asyncio.create_task(websocket_manager.broadcast({
                            "type": "gateway_message",
                            "direction": "outgoing",
                            "text": text,
                            "timestamp": datetime.now(timezone.utc).isoformat()
                        }))
                    except Exception as e:
                        logger.warning(f"Failed to broadcast message: {e}")
                
                return {
                    "status": "success",
                    "message": "Message sent",
                    "text": text
                }
            else:
                raise HTTPException(status_code=500, detail="Gateway interface not available")
        except Exception as e:
            logger.error(f"Failed to send message via gateway: {e}")
            raise HTTPException(status_code=500, detail=f"Failed to send message: {str(e)}")

# ===========================
# CoT Listener Service
# ===========================

def _cot_listener_ingest_callback(xml_string: str) -> None:
    """
    Ingest callback for the CoT listener service.

    Parses the received CoT XML, upserts the corresponding map marker into
    the database, and broadcasts the change to all WebSocket clients.
    Mirrors the logic in POST /api/cot/ingest without the HTTP layer.
    """
    if not AUTONOMOUS_MODULES_AVAILABLE:
        return
    try:
        if not CoTProtocolHandler.validate_cot_xml(xml_string):
            logger.debug("CoT listener: invalid CoT XML ignored")
            return
        cot_event = CoTEvent.from_xml(xml_string)
        if not cot_event:
            return
        marker_dict = CoTProtocolHandler.cot_to_marker(cot_event)
        with SessionLocal() as db:
            existing = db.query(MapMarker).filter(MapMarker.id == marker_dict["id"]).first()
            if existing:
                existing.lat = marker_dict["lat"]
                existing.lng = marker_dict["lng"]
                existing.name = marker_dict.get("name") or marker_dict["id"]
                existing.type = marker_dict.get("type", "unknown")
                extra = dict(existing.data) if isinstance(existing.data, dict) else {}
                extra["cot_type"] = marker_dict.get("cot_type")
                extra["source"] = "cot"
                extra["callsign"] = marker_dict.get("callsign")
                existing.data = extra
                db.commit()
                db.refresh(existing)
                event_type = "marker_updated"
                stored = {
                    "id": existing.id, "lat": existing.lat, "lng": existing.lng,
                    "name": existing.name, "type": existing.type,
                    "color": existing.color, "icon": existing.icon,
                    "created_by": existing.created_by,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "data": existing.data,
                }
            else:
                new_marker = MapMarker(
                    id=marker_dict["id"],
                    lat=marker_dict["lat"],
                    lng=marker_dict["lng"],
                    name=marker_dict.get("name") or marker_dict["id"],
                    description=marker_dict.get("description"),
                    type=marker_dict.get("type", "unknown"),
                    color="#3498db",
                    icon="default",
                    created_by="cot_ingest",
                    data={"cot_type": marker_dict.get("cot_type"), "source": "cot",
                          "callsign": marker_dict.get("callsign")},
                )
                db.add(new_marker)
                db.commit()
                db.refresh(new_marker)
                event_type = "marker_created"
                stored = {
                    "id": new_marker.id, "lat": new_marker.lat, "lng": new_marker.lng,
                    "name": new_marker.name, "type": new_marker.type,
                    "color": new_marker.color, "icon": new_marker.icon,
                    "created_by": new_marker.created_by,
                    "timestamp": new_marker.created_at.isoformat() if new_marker.created_at else datetime.now(timezone.utc).isoformat(),
                    "data": new_marker.data,
                }
        broadcast_websocket_update("markers", event_type, stored)
        # Echo back to TAK server if forwarding is enabled
        forward_cot_to_tak(xml_string)
    except Exception as exc:
        logger.exception("_cot_listener_ingest_callback failed: %s", exc)


def _start_cot_listener() -> bool:
    """Start the CoT listener service using config.json settings."""
    global _cot_listener_service
    if not COT_LISTENER_AVAILABLE:
        return False
    with _cot_listener_lock:
        if _cot_listener_service and _cot_listener_service.stats.get("running"):
            return True
        cfg = load_json("config") or {}
        tcp_port = int(cfg.get("cot_listener_tcp_port", 8088))
        udp_port = int(cfg.get("cot_listener_udp_port", 4242))
        multicast_enabled = bool(cfg.get("sa_multicast_enabled", False))
        multicast_group = str(cfg.get("sa_multicast_group", CoTListenerService.SA_MULTICAST_GROUP))
        multicast_port = int(cfg.get("sa_multicast_port", CoTListenerService.SA_MULTICAST_PORT))
        _cot_listener_service = CoTListenerService(
            tcp_port=tcp_port,
            udp_port=udp_port,
            ingest_callback=_cot_listener_ingest_callback,
            multicast_enabled=multicast_enabled,
            multicast_group=multicast_group,
            multicast_port=multicast_port,
        )
        return _cot_listener_service.start()


def _stop_cot_listener() -> None:
    """Stop the CoT listener service."""
    global _cot_listener_service
    with _cot_listener_lock:
        if _cot_listener_service:
            _cot_listener_service.stop()
            _cot_listener_service = None


@app.get("/api/cot/listener/status", summary="Get CoT listener service status")
def cot_listener_status():
    """Return the current status of the local CoT socket listener."""
    if not COT_LISTENER_AVAILABLE:
        return {"available": False, "message": "CoT listener service not available"}
    with _cot_listener_lock:
        if not _cot_listener_service:
            return {"available": True, "running": False, "message": "CoT listener not started"}
        return {"available": True, **_cot_listener_service.get_status()}


@app.post("/api/cot/listener/start", summary="Start CoT listener service")
def cot_listener_start(authorization: Optional[str] = Header(None)):
    """
    Start the local CoT TCP/UDP socket listener so that ATAK clients can
    send CoT XML directly to this server.  Configuration is read from
    config.json (cot_listener_tcp_port, cot_listener_udp_port).
    """
    if not COT_LISTENER_AVAILABLE:
        raise HTTPException(status_code=501, detail="CoT listener service not available")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Authentication required")
    if verify_token(authorization.split(" ")[1]) is None:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    if not _start_cot_listener():
        raise HTTPException(status_code=500, detail="Failed to start CoT listener service")
    with _cot_listener_lock:
        status = _cot_listener_service.get_status() if _cot_listener_service else {}
    return {"status": "started", **status}


@app.post("/api/cot/listener/stop", summary="Stop CoT listener service")
def cot_listener_stop(authorization: Optional[str] = Header(None)):
    """Stop the local CoT socket listener."""
    if not COT_LISTENER_AVAILABLE:
        raise HTTPException(status_code=501, detail="CoT listener service not available")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Authentication required")
    if verify_token(authorization.split(" ")[1]) is None:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    _stop_cot_listener()
    return {"status": "stopped"}


# ===========================
# CoT (Cursor-on-Target) Protocol Endpoints
# ===========================

@app.post("/api/cot/event", summary="Create CoT event from data")
def create_cot_event(data: Dict = Body(...)):
    """Create a CoT XML event from marker/position data"""
    if not AUTONOMOUS_MODULES_AVAILABLE:
        raise HTTPException(status_code=501, detail="Autonomous modules not available")
    
    try:
        cot_event = CoTEvent(
            uid=data.get("uid", str(uuid.uuid4())),
            cot_type=data.get("cot_type", CoTEvent.build_cot_type()),
            lat=float(data.get("lat", 0.0)),
            lon=float(data.get("lon", 0.0)),
            hae=float(data.get("hae", 0.0)),
            callsign=data.get("callsign"),
            remarks=data.get("remarks"),
            team_name=data.get("team_name"),
            team_role=data.get("team_role"),
            stale_minutes=data.get("stale_minutes", 5)
        )
        
        return {
            "status": "success",
            "xml": cot_event.to_xml(),
            "data": cot_event.to_dict()
        }
    except Exception as e:
        logger.exception("create_cot_event failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/cot/parse", summary="Parse CoT XML")
def parse_cot_xml(body: Dict = Body(...)):
    """Parse CoT XML string into structured data"""
    if not AUTONOMOUS_MODULES_AVAILABLE:
        raise HTTPException(status_code=501, detail="Autonomous modules not available")
    
    xml_string = body.get("xml")
    if not xml_string:
        raise HTTPException(status_code=400, detail="Missing xml field")
    
    try:
        cot_event = CoTEvent.from_xml(xml_string)
        if not cot_event:
            raise HTTPException(status_code=400, detail="Invalid CoT XML")
        
        return {
            "status": "success",
            "data": cot_event.to_dict()
        }
    except Exception as e:
        logger.exception("parse_cot_xml failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/cot/marker-to-cot", summary="Convert marker to CoT")
def marker_to_cot(marker: Dict = Body(...)):
    """Convert a map marker to CoT XML"""
    if not AUTONOMOUS_MODULES_AVAILABLE:
        raise HTTPException(status_code=501, detail="Autonomous modules not available")
    
    try:
        cot_event = CoTProtocolHandler.marker_to_cot(marker)
        if not cot_event:
            raise HTTPException(status_code=400, detail="Failed to convert marker")
        
        return {
            "status": "success",
            "xml": cot_event.to_xml(),
            "data": cot_event.to_dict()
        }
    except Exception as e:
        logger.exception("marker_to_cot failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/cot/ingest", summary="Ingest raw CoT XML from ATAK/TAK client")
async def ingest_cot_xml(request: Request):
    """
    Accept a raw Cursor-on-Target (CoT) XML message from an ATAK/TAK client.

    The request body should contain the CoT XML string (Content-Type: text/xml
    or application/xml).  The event is parsed, stored as a map marker and
    broadcast to all connected WebSocket clients so it appears on every
    client's map in real time.

    If TAK forwarding is enabled the event is also echoed to the configured
    TAK server so it can be relayed to other ATAK devices.
    """
    if not AUTONOMOUS_MODULES_AVAILABLE:
        raise HTTPException(status_code=501, detail="Autonomous modules not available")

    try:
        body = await request.body()
        xml_string = body.decode("utf-8", errors="replace").strip()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Cannot read request body: {e}")

    if not xml_string:
        raise HTTPException(status_code=400, detail="Empty request body")

    # Validate before doing anything else
    if not CoTProtocolHandler.validate_cot_xml(xml_string):
        raise HTTPException(status_code=400, detail="Invalid CoT XML")

    try:
        cot_event = CoTEvent.from_xml(xml_string)
        if not cot_event:
            raise HTTPException(status_code=400, detail="Failed to parse CoT XML")

        # Convert to map marker and upsert into DB
        marker_dict = CoTProtocolHandler.cot_to_marker(cot_event)

        with SessionLocal() as db:
            existing = db.query(MapMarker).filter(MapMarker.id == marker_dict["id"]).first()
            if existing:
                existing.lat   = marker_dict["lat"]
                existing.lng   = marker_dict["lng"]
                existing.name  = marker_dict.get("name") or marker_dict["id"]
                existing.type  = marker_dict.get("type", "unknown")
                # Preserve non-CoT fields already stored; only overwrite CoT-specific keys
                extra = dict(existing.data) if isinstance(existing.data, dict) else {}
                extra["cot_type"]  = marker_dict.get("cot_type")
                extra["source"]    = "cot"
                extra["callsign"]  = marker_dict.get("callsign")
                existing.data  = extra
                db.commit()
                db.refresh(existing)
                event_type = "marker_updated"
                stored_dict = {
                    "id": existing.id, "lat": existing.lat, "lng": existing.lng,
                    "name": existing.name, "type": existing.type,
                    "color": existing.color, "icon": existing.icon,
                    "created_by": existing.created_by,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "data": existing.data,
                }
            else:
                new_marker = MapMarker(
                    id=marker_dict["id"],
                    lat=marker_dict["lat"],
                    lng=marker_dict["lng"],
                    name=marker_dict.get("name") or marker_dict["id"],
                    description=marker_dict.get("description"),
                    type=marker_dict.get("type", "unknown"),
                    color="#3498db",
                    icon="default",
                    created_by="cot_ingest",
                    data={"cot_type": marker_dict.get("cot_type"), "source": "cot", "callsign": marker_dict.get("callsign")},
                )
                db.add(new_marker)
                db.commit()
                db.refresh(new_marker)
                event_type = "marker_created"
                stored_dict = {
                    "id": new_marker.id, "lat": new_marker.lat, "lng": new_marker.lng,
                    "name": new_marker.name, "type": new_marker.type,
                    "color": new_marker.color, "icon": new_marker.icon,
                    "created_by": new_marker.created_by,
                    "timestamp": new_marker.created_at.isoformat() if new_marker.created_at else datetime.now(timezone.utc).isoformat(),
                    "data": new_marker.data,
                }

        # Broadcast to WebSocket clients
        broadcast_websocket_update("markers", event_type, stored_dict)

        # Echo to TAK server (relay)
        forward_cot_to_tak(xml_string)

        return {
            "status": "success",
            "action": event_type,
            "marker": stored_dict,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("ingest_cot_xml failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/tak/config", summary="Get TAK server forwarding configuration")
def get_tak_config():
    """Return the current TAK server forwarding configuration."""
    cfg = load_json("config") or {}
    return {
        "tak_forward_enabled":  cfg.get("tak_forward_enabled", False),
        "tak_server_host":      cfg.get("tak_server_host", ""),
        "tak_server_port":      int(cfg.get("tak_server_port", 8089)),
        "tak_connection_type":  cfg.get("tak_connection_type", "udp"),
        "tak_username":         cfg.get("tak_username", ""),
        "tak_client_cert_path": cfg.get("tak_client_cert_path", ""),
        "tak_client_key_path":  cfg.get("tak_client_key_path", ""),
        "sa_multicast_enabled": cfg.get("sa_multicast_enabled", False),
        "sa_multicast_group":   cfg.get("sa_multicast_group", "239.2.3.1"),
        "sa_multicast_port":    int(cfg.get("sa_multicast_port", 6969)),
    }


@app.put("/api/tak/config", summary="Update TAK server forwarding configuration")
def update_tak_config(data: Dict = Body(...), authorization: Optional[str] = Header(None)):
    """
    Update the TAK server forwarding configuration.

    Body fields (all optional):
    - tak_forward_enabled (bool): Enable/disable forwarding to ATAK/TAK server
    - tak_server_host (str): TAK server IP or hostname
    - tak_server_port (int): Port on the TAK server (default 8089)
    - tak_connection_type (str): Connection type — "udp", "tcp", or "ssl" (default "udp")
    - tak_username (str): TAK server login username
    - tak_password (str): TAK server login password
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Authentication required")
    payload = verify_token(authorization.split(" ")[1])
    if payload is None:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    cfg = load_json("config") or {}

    if "tak_forward_enabled" in data:
        cfg["tak_forward_enabled"] = bool(data["tak_forward_enabled"])
    if "tak_server_host" in data:
        host = str(data["tak_server_host"]).strip()
        host = re.sub(r'^https?://', '', host).rstrip('/')
        cfg["tak_server_host"] = host
    if "tak_server_port" in data:
        try:
            port = int(data["tak_server_port"])
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="tak_server_port must be a numeric value")
        if not (1 <= port <= 65535):
            raise HTTPException(status_code=400, detail="tak_server_port must be 1–65535")
        cfg["tak_server_port"] = port
    if "tak_connection_type" in data:
        conn_type = str(data["tak_connection_type"]).lower()
        if conn_type not in ("udp", "tcp", "ssl"):
            raise HTTPException(status_code=400, detail="tak_connection_type must be 'udp', 'tcp', or 'ssl'")
        cfg["tak_connection_type"] = conn_type
    if "tak_username" in data:
        cfg["tak_username"] = str(data["tak_username"]).strip()
    if "tak_password" in data:
        cfg["tak_password"] = str(data["tak_password"])
    if "tak_client_cert_path" in data:
        cfg["tak_client_cert_path"] = str(data["tak_client_cert_path"]).strip()
    if "tak_client_key_path" in data:
        cfg["tak_client_key_path"] = str(data["tak_client_key_path"]).strip()
    # CoT listener settings (saved alongside TAK config for convenience)
    if "cot_listener_tcp_port" in data:
        try:
            port = int(data["cot_listener_tcp_port"])
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="cot_listener_tcp_port must be a numeric value")
        if not (1 <= port <= 65535):
            raise HTTPException(status_code=400, detail="cot_listener_tcp_port must be 1–65535")
        cfg["cot_listener_tcp_port"] = port
    if "cot_listener_udp_port" in data:
        try:
            port = int(data["cot_listener_udp_port"])
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="cot_listener_udp_port must be a numeric value")
        if not (1 <= port <= 65535):
            raise HTTPException(status_code=400, detail="cot_listener_udp_port must be 1–65535")
        cfg["cot_listener_udp_port"] = port
    if "cot_listener_enabled" in data:
        cfg["cot_listener_enabled"] = bool(data["cot_listener_enabled"])
    # SA Multicast settings
    if "sa_multicast_enabled" in data:
        cfg["sa_multicast_enabled"] = bool(data["sa_multicast_enabled"])
    if "sa_multicast_group" in data:
        cfg["sa_multicast_group"] = str(data["sa_multicast_group"]).strip()
    if "sa_multicast_port" in data:
        try:
            mport = int(data["sa_multicast_port"])
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="sa_multicast_port must be a numeric value")
        if not (1 <= mport <= 65535):
            raise HTTPException(status_code=400, detail="sa_multicast_port must be 1–65535")
        cfg["sa_multicast_port"] = mport

    save_json("config", cfg)
    logger.info("TAK config updated by %s: %s", payload.get("username"), {k: v for k, v in cfg.items() if k.startswith("tak_") and k != "tak_password"})

    # Auto-start or stop the TAK receiver thread based on the updated config so
    # that data from WinTAK/ATAK begins flowing to LPU5 without requiring a
    # server restart or a manual click on "TAK Connect".
    try:
        if cfg.get("tak_forward_enabled") and cfg.get("tak_server_host"):
            if cfg.get("tak_connection_type", "udp") in ("tcp", "ssl"):
                _start_tak_receiver_thread()
            # UDP is send-only; a receiver thread is not applicable – leave any
            # existing thread running so a switch back to tcp/ssl is seamless.
        else:
            # TAK integration disabled or no host configured – stop the thread.
            _stop_tak_receiver_thread()
    except Exception as _recv_err:
        logger.warning("Could not auto-manage TAK receiver thread after config update: %s", _recv_err)

    return {
        "status": "success",
        "tak_forward_enabled":  cfg.get("tak_forward_enabled", False),
        "tak_server_host":      cfg.get("tak_server_host", ""),
        "tak_server_port":      int(cfg.get("tak_server_port", 8089)),
        "tak_connection_type":  cfg.get("tak_connection_type", "udp"),
        "tak_username":         cfg.get("tak_username", ""),
        "tak_client_cert_path": cfg.get("tak_client_cert_path", ""),
        "tak_client_key_path":  cfg.get("tak_client_key_path", ""),
        "sa_multicast_enabled": cfg.get("sa_multicast_enabled", False),
        "sa_multicast_group":   cfg.get("sa_multicast_group", "239.2.3.1"),
        "sa_multicast_port":    int(cfg.get("sa_multicast_port", 6969)),
    }


@app.get("/api/tak/test", summary="Test TAK server connectivity")
def test_tak_connection():
    """
    Test connectivity to the configured TAK server by sending a CoT ping packet
    and attempting to read the server response where the protocol allows it.

    - UDP: sends a CoT t-x-c-t ping datagram; response not expected (UDP is fire-and-forget).
    - TCP/SSL: connects, sends a CoT t-x-c-t ping, and reads any response
      (TAK servers reply with a t-x-c-t-r ping-ack).

    Returns reachable, data_exchanged, and a descriptive message.
    """
    tak_cfg = _get_tak_config()
    host = tak_cfg.get("tak_server_host", "").strip()
    port = tak_cfg.get("tak_server_port", 8089)
    conn_type = tak_cfg.get("tak_connection_type", "udp")
    username = tak_cfg.get("tak_username", "")
    password = tak_cfg.get("tak_password", "")
    auth_data = _build_tak_auth_xml(username, password) if (username and password) else None

    if not host:
        return {"reachable": False, "data_exchanged": False, "message": "No TAK server host configured"}

    ping_data = _build_cot_ping_xml().encode("utf-8")

    if conn_type == "udp":
        # UDP is connectionless: send a CoT ping packet to verify the data path.
        # A response is not expected because TAK servers do not send UDP acks.
        try:
            addrs = socket.getaddrinfo(host, port, type=socket.SOCK_DGRAM)
            if not addrs:
                raise socket.gaierror(f"No address found for {host}")
            af, _, _, _, addr = addrs[0]
            sock = socket.socket(af, socket.SOCK_DGRAM)
            try:
                sock.settimeout(_TAK_SOCKET_TIMEOUT)
                sock.sendto(ping_data, addr)
            finally:
                sock.close()
            return {
                "reachable": True,
                "data_exchanged": True,
                "message": f"CoT ping sent to {host}:{port} (UDP – no response expected)",
            }
        except socket.gaierror as e:
            return {"reachable": False, "data_exchanged": False, "message": f"DNS resolution failed for {host}: {e}"}
        except (socket.timeout, OSError) as e:
            return {"reachable": False, "data_exchanged": False, "message": f"UDP send to {host}:{port} failed: {e}"}
    else:
        # TCP / SSL: connect, send a CoT ping, then attempt to read the server response.
        data_exchanged = False
        response_data = b""
        try:
            if conn_type == "ssl":
                addrs = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
                if not addrs:
                    raise socket.gaierror(f"No address found for {host}")
                af, _, _, _, addr = addrs[0]
                ctx = _build_tak_ssl_context(tak_cfg)
                raw = socket.socket(af, socket.SOCK_STREAM)
                raw.settimeout(_TAK_SOCKET_TIMEOUT)
                sock = ctx.wrap_socket(raw, server_hostname=host)
                sock.connect(addr)
            else:
                # create_connection handles both IPv4 and IPv6
                sock = socket.create_connection((host, port), timeout=_TAK_SOCKET_TIMEOUT)
            try:
                if auth_data:
                    sock.sendall(auth_data)
                sock.sendall(ping_data)
                data_exchanged = True
                # Try to read a server response (t-x-c-t-r ping-ack or any CoT reply).
                # Use a shorter timeout so the endpoint returns promptly if no ack comes.
                sock.settimeout(_TAK_PING_RESPONSE_TIMEOUT)
                try:
                    response_data = sock.recv(_TAK_RECV_BUFFER)
                except socket.timeout:
                    pass  # server did not respond within timeout – still counts as connected
            finally:
                sock.close()

            if response_data:
                return {
                    "reachable": True,
                    "data_exchanged": True,
                    "message": (
                        f"TAK server at {host}:{port} ({conn_type.upper()}) "
                        f"responded ({len(response_data)} bytes)"
                    ),
                }
            return {
                "reachable": True,
                "data_exchanged": data_exchanged,
                "message": (
                    f"Connected and sent CoT ping to {host}:{port} ({conn_type.upper()}); "
                    "no response received"
                ),
            }
        except socket.timeout:
            return {"reachable": False, "data_exchanged": False, "message": f"Connection to {host}:{port} timed out"}
        except (socket.gaierror, ConnectionRefusedError, ssl.SSLError, OSError) as e:
            if isinstance(e, ssl.SSLError) and "CERTIFICATE_REQUIRED" in str(e).upper():
                msg = (
                    f"Connection to {host}:{port} failed: {e} — "
                    "The server requires a client certificate (mutual TLS / mTLS). "
                    "If WinTAK or ATAK is running on the same machine, switch the connection type "
                    "to 'TCP' and set the port to 8087 — no certificate is needed for local "
                    "connections. For remote SSL servers, set the Client Certificate Path and "
                    "Client Key Path fields in the TAK Server settings."
                )
                return {"reachable": False, "data_exchanged": False, "message": msg}
            return {"reachable": False, "data_exchanged": False, "message": f"Connection to {host}:{port} failed: {e}"}


@app.get("/api/tak/status", summary="Get TAK receiver connection status")
def get_tak_status():
    """
    Return the current TAK receiver connection state and statistics.

    Returns:
    - connected: whether the receiver thread has an active socket
    - packets_received: number of CoT events successfully parsed from the server
    - parse_errors: number of malformed CoT packets that could not be parsed
    - last_error: last connection or parse error message, or null
    - connected_since: ISO timestamp of when the current connection was established
    - receiver_running: whether the background receiver thread is alive
    """
    stats = _get_tak_connection_stats()
    stats["receiver_running"] = (
        _TAK_RECEIVER_THREAD is not None and _TAK_RECEIVER_THREAD.is_alive()
    )
    return stats


@app.post("/api/tak/connect", summary="Manually start the TAK receiver thread")
def tak_connect(authorization: Optional[str] = Header(None)):
    """
    Manually start the persistent TAK receiver background thread.

    Requires a valid Bearer token.  The thread will auto-connect to the
    configured TAK server (tak_server_host / tak_server_port).
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Authentication required")
    payload = verify_token(authorization.split(" ")[1])
    if payload is None:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    tak_cfg = _get_tak_config()
    if not tak_cfg.get("tak_forward_enabled"):
        raise HTTPException(status_code=400, detail="TAK integration is disabled; enable tak_forward_enabled first")
    if not tak_cfg.get("tak_server_host"):
        raise HTTPException(status_code=400, detail="No TAK server host configured")
    if tak_cfg.get("tak_connection_type", "udp") not in ("tcp", "ssl"):
        raise HTTPException(status_code=400, detail="TAK receiver requires tcp or ssl connection type (udp is send-only)")

    started = _start_tak_receiver_thread()
    return {"status": "started" if started else "already_running"}


@app.post("/api/tak/disconnect", summary="Stop the TAK receiver thread")
def tak_disconnect(authorization: Optional[str] = Header(None)):
    """
    Gracefully stop the persistent TAK receiver background thread and close
    the active socket connection.

    Requires a valid Bearer token.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Authentication required")
    payload = verify_token(authorization.split(" ")[1])
    if payload is None:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    _stop_tak_receiver_thread()
    return {"status": "stopped"}


@app.post("/api/geofence/create", summary="Create geofence")
def create_geofence(data: Dict = Body(...)):
    """Create a new geofence zone"""
    if not AUTONOMOUS_MODULES_AVAILABLE or not geofencing_manager:
        raise HTTPException(status_code=501, detail="Geofencing not available")
    
    try:
        fence = GeoFence(
            zone_id=data.get("zone_id", str(uuid.uuid4())),
            name=data["name"],
            center_lat=float(data["center_lat"]),
            center_lon=float(data["center_lon"]),
            radius_meters=float(data["radius_meters"]),
            zone_type=data.get("zone_type", "exclusion"),
            alert_on_entry=data.get("alert_on_entry", True),
            alert_on_exit=data.get("alert_on_exit", False),
            enabled=data.get("enabled", True),
            metadata=data.get("metadata", {})
        )
        
        created_fence = geofencing_manager.create_geofence(fence)
        
        # Broadcast to all clients
        broadcast_websocket_update("geofence", "geofence_created", created_fence.to_dict())
        
        return {
            "status": "success",
            "geofence": created_fence.to_dict()
        }
    except KeyError as e:
        raise HTTPException(status_code=400, detail=f"Missing required field: {e}")
    except Exception as e:
        logger.exception("create_geofence failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/geofence/list", summary="List geofences")
def list_geofences_api(enabled_only: bool = False):
    """List all geofences"""
    if not AUTONOMOUS_MODULES_AVAILABLE or not geofencing_manager:
        raise HTTPException(status_code=501, detail="Geofencing not available")
    
    try:
        fences = geofencing_manager.list_geofences(enabled_only=enabled_only)
        return {
            "status": "success",
            "geofences": [f.to_dict() for f in fences]
        }
    except Exception as e:
        logger.exception("list_geofences failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/geofence/{zone_id}", summary="Get geofence")
def get_geofence_api(zone_id: str):
    """Get a specific geofence"""
    if not AUTONOMOUS_MODULES_AVAILABLE or not geofencing_manager:
        raise HTTPException(status_code=501, detail="Geofencing not available")
    
    fence = geofencing_manager.get_geofence(zone_id)
    if not fence:
        raise HTTPException(status_code=404, detail="Geofence not found")
    
    return {
        "status": "success",
        "geofence": fence.to_dict()
    }

@app.put("/api/geofence/{zone_id}", summary="Update geofence")
def update_geofence_api(zone_id: str, updates: Dict = Body(...)):
    """Update an existing geofence"""
    if not AUTONOMOUS_MODULES_AVAILABLE or not geofencing_manager:
        raise HTTPException(status_code=501, detail="Geofencing not available")
    
    try:
        fence = geofencing_manager.update_geofence(zone_id, updates)
        if not fence:
            raise HTTPException(status_code=404, detail="Geofence not found")
        
        # Broadcast to all clients
        broadcast_websocket_update("geofence", "geofence_updated", fence.to_dict())
        
        return {
            "status": "success",
            "geofence": fence.to_dict()
        }
    except Exception as e:
        logger.exception("update_geofence failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/api/geofence/{zone_id}", summary="Delete geofence")
def delete_geofence_api(zone_id: str):
    """Delete a geofence"""
    if not AUTONOMOUS_MODULES_AVAILABLE or not geofencing_manager:
        raise HTTPException(status_code=501, detail="Geofencing not available")
    
    if geofencing_manager.delete_geofence(zone_id):
        # Broadcast to all clients
        broadcast_websocket_update("geofence", "geofence_deleted", {"zone_id": zone_id})
        
        return {"status": "success", "message": "Geofence deleted"}
    else:
        raise HTTPException(status_code=404, detail="Geofence not found")

@app.post("/api/geofence/check", summary="Check position against geofences")
def check_geofence_position(data: Dict = Body(...)):
    """Check if a position triggers any geofence alerts"""
    if not AUTONOMOUS_MODULES_AVAILABLE or not geofencing_manager:
        raise HTTPException(status_code=501, detail="Geofencing not available")
    
    try:
        entity_id = data["entity_id"]
        lat = float(data["lat"])
        lon = float(data["lon"])
        
        alerts = geofencing_manager.check_position(entity_id, lat, lon)
        
        return {
            "status": "success",
            "alerts": alerts,
            "zones": [f.to_dict() for f in geofencing_manager.get_zones_containing(lat, lon)]
        }
    except KeyError as e:
        raise HTTPException(status_code=400, detail=f"Missing required field: {e}")
    except Exception as e:
        logger.exception("check_geofence_position failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))

# ===========================
# Autonomous Rule Engine Endpoints
# ===========================

@app.post("/api/rules/create", summary="Create autonomous rule")
def create_rule_api(data: Dict = Body(...)):
    """Create a new autonomous rule"""
    if not AUTONOMOUS_MODULES_AVAILABLE or not autonomous_engine:
        raise HTTPException(status_code=501, detail="Autonomous engine not available")
    
    try:
        rule = Rule(
            rule_id=data.get("rule_id", str(uuid.uuid4())),
            name=data["name"],
            description=data.get("description", ""),
            trigger_type=data["trigger_type"],
            trigger_config=data.get("trigger_config", {}),
            conditions=data.get("conditions", []),
            actions=data.get("actions", []),
            enabled=data.get("enabled", True),
            priority=data.get("priority", 5)
        )
        
        created_rule = autonomous_engine.create_rule(rule)
        return {
            "status": "success",
            "rule": created_rule.to_dict()
        }
    except KeyError as e:
        raise HTTPException(status_code=400, detail=f"Missing required field: {e}")
    except Exception as e:
        logger.exception("create_rule failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/rules/list", summary="List autonomous rules")
def list_rules_api(enabled_only: bool = False):
    """List all autonomous rules"""
    if not AUTONOMOUS_MODULES_AVAILABLE or not autonomous_engine:
        raise HTTPException(status_code=501, detail="Autonomous engine not available")
    
    try:
        rules = autonomous_engine.list_rules(enabled_only=enabled_only)
        return {
            "status": "success",
            "rules": [r.to_dict() for r in rules]
        }
    except Exception as e:
        logger.exception("list_rules failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/rules/{rule_id}", summary="Get rule")
def get_rule_api(rule_id: str):
    """Get a specific rule"""
    if not AUTONOMOUS_MODULES_AVAILABLE or not autonomous_engine:
        raise HTTPException(status_code=501, detail="Autonomous engine not available")
    
    rule = autonomous_engine.get_rule(rule_id)
    if not rule:
        raise HTTPException(status_code=404, detail="Rule not found")
    
    return {
        "status": "success",
        "rule": rule.to_dict()
    }

@app.put("/api/rules/{rule_id}", summary="Update rule")
def update_rule_api(rule_id: str, updates: Dict = Body(...)):
    """Update an existing rule"""
    if not AUTONOMOUS_MODULES_AVAILABLE or not autonomous_engine:
        raise HTTPException(status_code=501, detail="Autonomous engine not available")
    
    try:
        rule = autonomous_engine.update_rule(rule_id, updates)
        if not rule:
            raise HTTPException(status_code=404, detail="Rule not found")
        
        return {
            "status": "success",
            "rule": rule.to_dict()
        }
    except Exception as e:
        logger.exception("update_rule failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/api/rules/{rule_id}", summary="Delete rule")
def delete_rule_api(rule_id: str):
    """Delete a rule"""
    if not AUTONOMOUS_MODULES_AVAILABLE or not autonomous_engine:
        raise HTTPException(status_code=501, detail="Autonomous engine not available")
    
    if autonomous_engine.delete_rule(rule_id):
        return {"status": "success", "message": "Rule deleted"}
    else:
        raise HTTPException(status_code=404, detail="Rule not found")

@app.post("/api/rules/trigger", summary="Manually trigger rules")
def trigger_rules_api(data: Dict = Body(...)):
    """Manually trigger rules of a specific type"""
    if not AUTONOMOUS_MODULES_AVAILABLE or not autonomous_engine:
        raise HTTPException(status_code=501, detail="Autonomous engine not available")
    
    try:
        trigger_type = data.get("trigger_type", "manual")
        context = data.get("context", {})
        
        results = autonomous_engine.trigger_rules(trigger_type, context)
        
        return {
            "status": "success",
            "results": results
        }
    except Exception as e:
        logger.exception("trigger_rules failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))

# ===========================
# Chat Channels Endpoints
# ===========================

# Default chat channels (seeded into DB on first use)
DEFAULT_CHANNELS = [
    {"id": "all", "name": "All Units", "description": "Broadcast to all units", "color": "#ffffff", "is_default": True},
]

# Channel ID that is bridged bidirectionally to the Meshtastic mesh (LPU5 – Mesh)
MESH_CHAT_CHANNEL = "all"

def _ensure_default_channels(db):
    """Seed default channels into DB if they don't exist yet."""
    for ch in DEFAULT_CHANNELS:
        existing = db.query(ChatChannel).filter(ChatChannel.id == ch["id"]).first()
        if not existing:
            db.add(ChatChannel(
                id=ch["id"], name=ch["name"], description=ch.get("description", ""),
                color=ch.get("color", "#ffffff"), is_default=ch.get("is_default", False), members=[]
            ))
        else:
            # Update existing channels to match the is_default setting from DEFAULT_CHANNELS
            existing.is_default = ch.get("is_default", False)
    try:
        db.commit()
    except Exception:
        db.rollback()

def _extract_username_from_auth(authorization: str) -> str:
    """Extract username from Authorization header. Returns username or raises HTTPException."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid authorization header")
    parts = authorization.split(" ", 1)
    token = parts[1].strip() if len(parts) > 1 else ""
    if not token:
        raise HTTPException(status_code=401, detail="Empty token")
    user_payload = verify_token(token)
    if not user_payload or not isinstance(user_payload, dict):
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    return user_payload.get("username") or user_payload.get("sub") or "Unknown"

def _chat_message_to_dict(m):
    """Convert a ChatMessage ORM object to a serializable dict."""
    return {
        "id": m.id,
        "channel_id": m.channel,
        "username": m.sender,
        "text": m.content,
        "timestamp": m.timestamp.isoformat() if m.timestamp else None,
        "type": m.type or "text",
        "delivered_to": m.delivered_to if m.delivered_to else [],
        "read_by": m.read_by if m.read_by else [],
    }

@app.get("/api/chat/channels", summary="Get chat channels")
def get_chat_channels(authorization: Optional[str] = Header(None)):
    """Get list of available chat channels filtered to channels the current user may access."""
    db = SessionLocal()
    try:
        _ensure_default_channels(db)
        channels = db.query(ChatChannel).all()

        # Determine which channels this user is allowed to see
        allowed_ids = None  # None = all channels visible
        if authorization and authorization.startswith("Bearer "):
            try:
                user_payload = verify_token(authorization.split(" ", 1)[1].strip())
                username = (user_payload or {}).get("username") or (user_payload or {}).get("sub")
                if username:
                    user = db.query(User).filter(User.username == username).first()
                    if user:
                        # Admin and operator roles see all channels
                        if user.role not in ("admin", "operator"):
                            # Always include "all" channel; non-admin users only see
                            # their assigned channels even when the list is empty
                            allowed_ids = set(user.chat_channels or []) | {"all"}
            except Exception as _ch_err:
                logger.debug("Channel filter auth check failed: %s", _ch_err)

        return {
            "status": "success",
            "channels": [
                {
                    "id": ch.id,
                    "name": ch.name,
                    "description": ch.description or "",
                    "color": ch.color or "#ffffff",
                    "members": ch.members if ch.members else [],
                    "is_default": ch.is_default if ch.is_default is not None else False,
                    "created_by": ch.created_by or "",
                }
                for ch in channels
                if allowed_ids is None or ch.id in allowed_ids
            ],
        }
    except Exception as e:
        logger.exception("get_chat_channels failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()

@app.post("/api/chat/channels", summary="Create a chat channel")
async def create_chat_channel(data: Dict = Body(...), authorization: str = Header(None)):
    """Create a new chat channel"""
    db = SessionLocal()
    try:
        username = _extract_username_from_auth(authorization)
        name = (data.get("name") or "").strip()
        if not name:
            raise HTTPException(status_code=400, detail="Channel name is required")
        # Generate a safe id from the name
        channel_id = data.get("id") or name.lower().replace(" ", "_")
        existing = db.query(ChatChannel).filter(
            (ChatChannel.id == channel_id) | (ChatChannel.name == name)
        ).first()
        if existing:
            raise HTTPException(status_code=409, detail="Channel already exists")
        new_channel = ChatChannel(
            id=channel_id,
            name=name,
            description=data.get("description", ""),
            color=data.get("color", "#ffffff"),
            created_by=username,
            members=data.get("members", []),
            is_default=False,
        )
        db.add(new_channel)
        db.commit()
        db.refresh(new_channel)
        ch_dict = {
            "id": new_channel.id, "name": new_channel.name,
            "description": new_channel.description or "", "color": new_channel.color,
            "members": new_channel.members if new_channel.members else [],
            "is_default": False, "created_by": new_channel.created_by or "",
        }
        if AUTONOMOUS_MODULES_AVAILABLE and websocket_manager:
            await websocket_manager.publish_to_channel('chat', {"type": "channel_created", "data": ch_dict})
        return {"status": "success", "channel": ch_dict}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.exception("create_chat_channel failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()

@app.delete("/api/chat/channels/{channel_id}", summary="Delete a chat channel")
async def delete_chat_channel(channel_id: str, authorization: str = Header(None)):
    """Delete a custom chat channel (default channels cannot be deleted)"""
    db = SessionLocal()
    try:
        username = _extract_username_from_auth(authorization)
        channel = db.query(ChatChannel).filter(ChatChannel.id == channel_id).first()
        if not channel:
            raise HTTPException(status_code=404, detail="Channel not found")
        if channel.is_default:
            raise HTTPException(status_code=403, detail="Cannot delete default channel")
        db.delete(channel)
        db.commit()
        if AUTONOMOUS_MODULES_AVAILABLE and websocket_manager:
            await websocket_manager.publish_to_channel('chat', {"type": "channel_deleted", "data": {"id": channel_id, "deleted_by": username}})
        return {"status": "success", "detail": f"Channel {channel_id} deleted"}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.exception("delete_chat_channel failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()

@app.put("/api/chat/channels/{channel_id}/members", summary="Update channel members")
async def update_channel_members(channel_id: str, data: Dict = Body(...), authorization: str = Header(None)):
    """Update the member list of a chat channel and sync each affected user's chat_channels."""
    db = SessionLocal()
    try:
        _extract_username_from_auth(authorization)
        channel = db.query(ChatChannel).filter(ChatChannel.id == channel_id).first()
        if not channel:
            raise HTTPException(status_code=404, detail="Channel not found")

        old_members = set(channel.members or [])
        new_members = set(data.get("members", []))
        channel.members = list(new_members)

        # Sync user.chat_channels for added/removed members
        added = new_members - old_members
        removed = old_members - new_members
        affected_usernames = added | removed
        if affected_usernames:
            for user in db.query(User).filter(User.username.in_(affected_usernames)).all():
                user_channels = set(user.chat_channels or ["all"])
                if user.username in added:
                    user_channels.add(channel_id)
                else:
                    user_channels.discard(channel_id)
                # Always keep "all"
                user_channels.add("all")
                user.chat_channels = list(user_channels)

        db.commit()
        return {"status": "success", "channel_id": channel_id, "members": channel.members}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.exception("update_channel_members failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()

@app.get("/api/chat/messages/{channel_id}", summary="Get chat messages for a channel")
def get_chat_messages(channel_id: str, limit: int = 100, authorization: Optional[str] = Header(None)):
    """Get recent chat messages from DB for a specific channel"""
    db = SessionLocal()
    try:
        # Enforce channel access if auth is provided
        if authorization and authorization.startswith("Bearer "):
            try:
                user_payload = verify_token(authorization.split(" ", 1)[1].strip())
                username = (user_payload or {}).get("username") or (user_payload or {}).get("sub")
                if username:
                    user = db.query(User).filter(User.username == username).first()
                    if user and user.role not in ("admin", "operator"):
                        user_chat_channels = user.chat_channels or []
                        allowed = set(user_chat_channels) | {"all"}
                        if channel_id not in allowed:
                            raise HTTPException(status_code=403, detail="Access to this channel is not permitted")
            except HTTPException:
                raise
            except Exception as _access_err:
                logger.debug("Channel access check failed: %s", _access_err)
                pass
        messages = db.query(ChatMessage).filter(ChatMessage.channel == channel_id).order_by(ChatMessage.timestamp.desc()).limit(limit).all()
        # Reverse to get chronological order for UI
        messages.reverse()
        return {
            "status": "success",
            "messages": [_chat_message_to_dict(m) for m in messages],
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("get_chat_messages failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()

@app.post("/api/chat/message", summary="Send chat message")
async def send_chat_message(message: Dict = Body(...), authorization: str = Header(None)):
    """Send a chat message to a channel (DB-backed)"""
    db = SessionLocal()
    try:
        username = _extract_username_from_auth(authorization)

        channel_id = message.get("channel_id")
        text = message.get("text", "").strip()

        if not channel_id or not text:
            raise HTTPException(status_code=400, detail="channel_id and text are required")

        # Verify channel exists (check DB channels including defaults)
        _ensure_default_channels(db)
        channel_exists = db.query(ChatChannel).filter(ChatChannel.id == channel_id).first()
        if not channel_exists:
            raise HTTPException(status_code=404, detail="Channel not found")

        # Enforce that the sender is allowed to post to this channel
        sender_user = db.query(User).filter(User.username == username).first()
        if sender_user and sender_user.role not in ("admin", "operator"):
            user_chat_channels = sender_user.chat_channels or []
            allowed = set(user_chat_channels) | {"all"}
            if channel_id not in allowed:
                raise HTTPException(status_code=403, detail="You are not a member of this channel")

        # Create message in DB
        new_msg = ChatMessage(
            channel=channel_id,
            sender=username,
            content=text,
            timestamp=datetime.now(timezone.utc),
            type=message.get("type", "text"),
            delivered_to=[],
            read_by=[],
        )
        db.add(new_msg)
        db.commit()
        db.refresh(new_msg)

        msg_dict = _chat_message_to_dict(new_msg)

        # Broadcast to WebSocket clients
        if AUTONOMOUS_MODULES_AVAILABLE and websocket_manager:
            await websocket_manager.publish_to_channel('chat', {"type": "new_message", "data": msg_dict})

        # Forward to Meshtastic mesh when the message is on the bridged channel
        if channel_id == MESH_CHAT_CHANNEL:
            with _gateway_service_lock:
                gw = _gateway_service
            if gw and gw.running and gw.interface:
                try:
                    # Strip ASCII control characters before transmitting
                    safe_username = "".join(c for c in username if c >= " ")
                    safe_text = "".join(c for c in text if c >= " ")
                    mesh_text = f"[{safe_username}] {safe_text}"
                    gw.interface.sendText(mesh_text)
                    logger.info(f"Chat→Mesh bridge: {mesh_text[:80]}{'...' if len(mesh_text) > 80 else ''}")
                except Exception as mesh_err:
                    logger.warning(f"Chat→Mesh bridge error: {mesh_err}")

        return {"status": "success", "message": msg_dict}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.exception("send_chat_message failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()

@app.post("/api/chat/message/{message_id}/delivered", summary="Mark message as delivered")
async def mark_message_delivered(message_id: str, authorization: str = Header(None)):
    """Mark a chat message as delivered to the current user (single checkmark)"""
    db = SessionLocal()
    try:
        username = _extract_username_from_auth(authorization)
        msg = db.query(ChatMessage).filter(ChatMessage.id == message_id).first()
        if not msg:
            raise HTTPException(status_code=404, detail="Message not found")
        if msg.sender == username:
            return {"status": "success", "message_id": message_id, "delivered_to": msg.delivered_to or []}
        delivered = msg.delivered_to if msg.delivered_to else []
        if username not in delivered:
            delivered.append(username)
            msg.delivered_to = delivered
            db.commit()
            if AUTONOMOUS_MODULES_AVAILABLE and websocket_manager:
                await websocket_manager.publish_to_channel('chat', {"type": "message_delivered", "data": {"message_id": message_id, "delivered_to": delivered}})
        return {"status": "success", "message_id": message_id, "delivered_to": delivered}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.exception("mark_message_delivered failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()

@app.post("/api/chat/message/{message_id}/read", summary="Mark message as read")
async def mark_message_read(message_id: str, authorization: str = Header(None)):
    """Mark a chat message as read by the current user (double checkmark)"""
    db = SessionLocal()
    try:
        username = _extract_username_from_auth(authorization)
        msg = db.query(ChatMessage).filter(ChatMessage.id == message_id).first()
        if not msg:
            raise HTTPException(status_code=404, detail="Message not found")
        if msg.sender == username:
            return {"status": "success", "message_id": message_id, "read_by": msg.read_by or []}
        read_list = msg.read_by if msg.read_by else []
        if username not in read_list:
            read_list.append(username)
            msg.read_by = read_list
            # Also ensure delivered
            delivered = msg.delivered_to if msg.delivered_to else []
            if username not in delivered:
                delivered.append(username)
                msg.delivered_to = delivered
            db.commit()
            if AUTONOMOUS_MODULES_AVAILABLE and websocket_manager:
                await websocket_manager.publish_to_channel('chat', {"type": "message_read", "data": {"message_id": message_id, "read_by": read_list, "delivered_to": delivered}})
        return {"status": "success", "message_id": message_id, "read_by": read_list}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.exception("mark_message_read failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()

@app.post("/api/chat/messages/mark-read", summary="Mark multiple messages as read")
async def mark_messages_read_bulk(data: Dict = Body(...), authorization: str = Header(None)):
    """Mark multiple messages as read by the current user"""
    db = SessionLocal()
    try:
        username = _extract_username_from_auth(authorization)
        message_ids = data.get("message_ids", [])
        if not message_ids:
            raise HTTPException(status_code=400, detail="message_ids list is required")
        updated = []
        for mid in message_ids:
            msg = db.query(ChatMessage).filter(ChatMessage.id == mid).first()
            if msg and msg.sender != username:
                read_list = msg.read_by if msg.read_by else []
                delivered = msg.delivered_to if msg.delivered_to else []
                changed = False
                if username not in read_list:
                    read_list.append(username)
                    msg.read_by = read_list
                    changed = True
                if username not in delivered:
                    delivered.append(username)
                    msg.delivered_to = delivered
                    changed = True
                if changed:
                    updated.append(mid)
        db.commit()
        if updated and AUTONOMOUS_MODULES_AVAILABLE and websocket_manager:
            await websocket_manager.publish_to_channel('chat', {"type": "messages_read", "data": {"message_ids": updated, "read_by_user": username}})
        return {"status": "success", "updated": updated}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.exception("mark_messages_read_bulk failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()

# ===========================
# Map Symbol Placement Endpoints
# ===========================

MAP_SYMBOLS_FILE = "symbol_definitions.json"  # Renamed from map_place_symbol.json

def load_map_symbols():
    """Load map symbols database"""
    try:
        with open(MAP_SYMBOLS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        default_data = {"symbols": []}
        save_map_symbols(default_data)
        return default_data
    except json.JSONDecodeError:
        return {"symbols": []}

def save_map_symbols(data):
    """Save map symbols database"""
    with open(MAP_SYMBOLS_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def get_symbol_priority(symbol_type: str) -> int:
    """Get priority for symbol type (lower number = higher priority)"""
    priorities = {
        "raute": 1,      # diamond/rhombus
        "rechteck": 2,   # rectangle
        "viereck": 3,    # square
        "blume": 4       # flower
    }
    return priorities.get(symbol_type.lower(), 999)

@app.get("/api/map/symbols", summary="Get all map symbols")
def get_map_symbols():
    """Get all placed map symbols (DB-backed)"""
    try:
        with SessionLocal() as db:
            symbols = db.query(MapMarker).all()
            # Convert to dict list
            symbol_list = []
            for s in symbols:
                # Skip meshtastic-synced markers — rendered by updateMeshtasticNodes()
                if s.type == "node" or (s.created_by and s.created_by in _MESHTASTIC_CREATED_BY):
                    continue
                # Skip ATAK-echoed meshtastic node markers (uid prefix "mesh-") —
                # these originate from _forward_meshtastic_node_to_tak and are
                # re-ingested as TAK units; they are already shown as blue circles
                # by updateMeshtasticNodes() so we exclude them here to avoid a
                # duplicate white-dot rendering.
                if s.created_by == "tak_server" and s.id.startswith("mesh-"):
                    continue
                # Basic fields
                s_dict = {
                    "id": s.id,
                    "lat": s.lat,
                    "lng": s.lng,
                    "type": s.type,
                    "label": s.name,
                    "description": s.description,
                    "color": s.color,
                    "icon": s.icon,
                    "username": s.created_by
                }
                # Add extra data if available
                if s.data:
                    s_dict.update(s.data)
                symbol_list.append(s_dict)
                
            return {"status": "success", "symbols": symbol_list}
    except Exception as e:
        logger.exception("get_map_symbols failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/map/symbols", summary="Place a new map symbol")
async def place_map_symbol(symbol: Dict = Body(...), authorization: str = Header(None)):
    """Place a new symbol on the map (DB-backed)"""
    try:
        # Verify user authentication
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Missing or invalid authorization header")
        
        token = authorization.split(" ")[1]
        user_payload = verify_token(token)
        
        if user_payload is None:
            raise HTTPException(status_code=401, detail="Invalid or expired token")
        
        username = user_payload.get("username", "Unknown")
        
        lat = symbol.get("lat")
        lng = symbol.get("lng")
        # Normalise to lowercase so that type IDs are consistent across all
        # TAK clients (ATAK/ITAK/WinTAK/XTAK) — e.g. "raute" == "Raute".
        symbol_type = symbol.get("type", "marker").lower()
        source_page = symbol.get("source_page", "unknown")
        
        if lat is None or lng is None:
            raise HTTPException(status_code=400, detail="lat and lng are required")
        
        timestamp = datetime.now(timezone.utc).isoformat()
        
        with SessionLocal() as db:
            # Check for conflicts
            threshold = 0.0001
            conflicts = db.query(MapMarker).filter(
                MapMarker.lat >= lat - threshold,
                MapMarker.lat <= lat + threshold,
                MapMarker.lng >= lng - threshold,
                MapMarker.lng <= lng + threshold
            ).all()
            
            # For gps_position type, remove all previous GPS markers from this user
            # to ensure only the latest position is shown (no duplicates)
            if symbol_type == "gps_position":
                old_gps = db.query(MapMarker).filter(
                    MapMarker.type == "gps_position",
                    MapMarker.created_by == username
                ).all()
                for old in old_gps:
                    db.delete(old)
                if old_gps:
                    db.commit()
            
            # Create new marker
            new_symbol = MapMarker(
                id=str(uuid.uuid4()),
                lat=lat,
                lng=lng,
                type=symbol_type,
                name=symbol.get("label") or symbol_type,
                color=symbol.get("color", "#3498db"),
                icon=symbol.get("icon", "fa-map-marker"),
                created_by=username,
                created_at=datetime.now(timezone.utc),
                data={
                    "source_page": source_page,
                    "timestamp": timestamp,
                    "label": symbol.get("label", "")
                }
            )
            db.add(new_symbol)
            db.commit()
            db.refresh(new_symbol)
            
            # Prepare for broadcast
            symbol_data = {
                "id": new_symbol.id,
                "name": new_symbol.name,
                "lat": new_symbol.lat,
                "lng": new_symbol.lng,
                "type": new_symbol.type,
                "username": new_symbol.created_by,
                "source_page": source_page,
                "timestamp": timestamp,
                "label": new_symbol.name,
                "color": new_symbol.color,
                "icon": new_symbol.icon,
                "how": "h-g-i-g-o",
            }
        
        # Broadcast to WebSocket clients using helper
        broadcast_websocket_update("symbols", "symbol_created", symbol_data)
        broadcast_websocket_update("markers", "marker_created", symbol_data)

        # Forward to ATAK/TAK server if enabled
        if AUTONOMOUS_MODULES_AVAILABLE:
            try:
                cot_event = CoTProtocolHandler.marker_to_cot(symbol_data)
                if cot_event:
                    ok = forward_cot_to_tak(cot_event.to_xml())
                    if ok:
                        logger.info("CoT forward on place_map_symbol succeeded: symbol_id=%s", new_symbol.id)
                    else:
                        logger.debug("CoT forward on place_map_symbol skipped (TAK forwarding disabled or not configured): symbol_id=%s", new_symbol.id)
            except Exception as _fwd_err:
                logger.warning("CoT forward on place_map_symbol failed: %s", _fwd_err)

        return {"status": "success", "symbol": symbol_data}

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("place_map_symbol failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/api/map/symbols/{symbol_id}", summary="Delete a map symbol")
async def delete_map_symbol(symbol_id: str, authorization: str = Header(None)):
    """Delete a map symbol (DB-backed). GPS position markers cannot be deleted by other users."""
    try:
        # Verify user authentication
        payload = verify_token(authorization)
        current_username = payload.get("username", "system") if payload else "system"
        
        with SessionLocal() as db:
            marker = db.query(MapMarker).filter(MapMarker.id == symbol_id).first()
            if not marker:
                raise HTTPException(status_code=404, detail="Symbol not found")
            
            # Prevent deletion of GPS position markers (except by the owning user for position updates)
            if marker.type == 'gps_position' and marker.created_by != current_username:
                raise HTTPException(status_code=403, detail="GPS position markers cannot be deleted")

            # Capture values before deletion and commit to avoid detached instance access
            marker_lat = marker.lat
            marker_lng = marker.lng
            marker_type = marker.type
            marker_name = marker.name

            db.delete(marker)
            db.commit()

        # Broadcast to WebSocket clients using helper
        broadcast_websocket_update("symbols", "symbol_deleted", {"id": symbol_id})
        broadcast_websocket_update("markers", "marker_deleted", {"id": symbol_id})

        # Forward tombstone to ATAK/TAK server if enabled
        if AUTONOMOUS_MODULES_AVAILABLE:
            try:
                marker_snapshot = {
                    "id": symbol_id,
                    "lat": marker_lat,
                    "lng": marker_lng,
                    "type": marker_type,
                    "name": marker_name,
                }
                tombstone = CoTProtocolHandler.marker_to_cot_tombstone(marker_snapshot)
                if tombstone:
                    ok = forward_cot_to_tak(tombstone.to_xml())
                    if ok:
                        logger.info("CoT tombstone on delete_map_symbol succeeded: symbol_id=%s", symbol_id)
                    else:
                        logger.debug("CoT tombstone on delete_map_symbol skipped (TAK forwarding disabled or not configured): symbol_id=%s", symbol_id)
            except Exception as _fwd_err:
                logger.warning("CoT tombstone on delete_map_symbol failed: %s", _fwd_err)
        
        return {"status": "success", "message": "Symbol deleted"}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("delete_map_symbol failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))

# ===========================
# Stream Share State (for polling by stream_share.html)
# ===========================
# In-memory state tracking the currently active shared video stream.
# Written by the stream_share WebSocket relay and POST /api/stream_share,
# read by GET /api/stream_share (polled by stream_share.html iframe).
# Thread-safe for simple dict replacement (GIL-protected single assignment).
_active_stream_share: Dict = {"active": False}

# Per-slot stream share state for multicast distribution (up to 15 simultaneous streams).
# Each slot can carry an independent stream targeted to specific units.
MAX_STREAM_SLOTS = 15
_active_stream_shares: Dict[int, Dict] = {i: {"active": False, "slot": i} for i in range(1, MAX_STREAM_SLOTS + 1)}

# Server-side camera frame rate limiting: track last relay time per source connection.
# Prevents flooding the event loop when clients send frames faster than they can be relayed.
_CAMERA_FRAME_MIN_INTERVAL = 0.15  # minimum seconds between relayed frames (≈6.67 fps max)
_camera_last_relay: Dict[str, float] = {}  # connection_id -> last relay timestamp

# Short-lived cache for unit lookups to avoid a DB round-trip on every poll.
# Maps user_id -> (unit_name, expiry_timestamp).
_USER_UNIT_CACHE_TTL = 60  # seconds
_user_unit_cache: Dict[str, tuple] = {}

def _get_user_unit_from_token(authorization: Optional[str], db: Session) -> Optional[str]:
    """Return the unit name for the user identified by the Bearer token, or None.

    Results are cached for _USER_UNIT_CACHE_TTL seconds to avoid repeated DB
    queries on frequently-polled stream endpoints.
    """
    if not authorization:
        return None
    token = authorization.replace("Bearer ", "").strip()
    payload = verify_token(token)
    if not payload:
        return None
    user_id = payload.get("user_id")
    if not user_id:
        return None

    now = time.monotonic()
    cached = _user_unit_cache.get(user_id)
    if cached is not None:
        unit, expires = cached
        if now < expires:
            return unit
        del _user_unit_cache[user_id]

    user = db.query(User).filter(User.id == user_id).first()
    unit = user.unit if user else None
    _user_unit_cache[user_id] = (unit, now + _USER_UNIT_CACHE_TTL)
    return unit

@app.get("/api/stream_share", summary="Get current shared stream state")
def get_stream_share():
    """Return the currently active shared stream info (polled by stream_share.html iframe)."""
    return _active_stream_share

@app.post("/api/stream_share", summary="Set shared stream state")
async def set_stream_share(data: Dict = Body(...)):
    """Update the shared stream state (called by stream.html when sharing)."""
    global _active_stream_share
    _active_stream_share = {
        "active": data.get("active", False),
        "stream_url": data.get("stream_url"),
        "stream_type": data.get("stream_type", "video"),
        "isCamera": data.get("isCamera", False),
        "source": data.get("source"),
        "timestamp": datetime.now(timezone.utc).isoformat()
    }
    # Also broadcast via WebSocket
    broadcast_websocket_update("camera", "stream_share", _active_stream_share)
    return {"status": "success"}

@app.get("/api/stream_slots", summary="Get all active stream slots")
def get_stream_slots(authorization: Optional[str] = Header(None), db: Session = Depends(get_db)):
    """Return stream slot states visible to the requesting user.

    Slots with an empty ``target_units`` list are visible to everyone.
    Slots with a non-empty ``target_units`` list are only visible to users
    whose unit is contained in that list.
    """
    user_unit = _get_user_unit_from_token(authorization, db)
    visible_slots = []
    for slot in _active_stream_shares.values():
        target_units = slot.get("target_units", [])
        if not target_units or (user_unit and user_unit in target_units):
            visible_slots.append(slot)
    return {"slots": visible_slots, "max_slots": MAX_STREAM_SLOTS}

@app.get("/api/stream_share/{slot}", summary="Get shared stream state for a specific slot")
def get_stream_share_slot(slot: int = Path(..., ge=1, le=MAX_STREAM_SLOTS), authorization: Optional[str] = Header(None), db: Session = Depends(get_db)):
    """Return the stream info for the given slot if the requesting user's unit is allowed.

    When ``target_units`` is empty the slot is treated as a broadcast visible to all users.
    When ``target_units`` is non-empty only users whose unit appears in that list receive the
    active stream data; all other users receive an inactive placeholder response.
    """
    slot_data = _active_stream_shares.get(slot, {"active": False, "slot": slot})
    target_units = slot_data.get("target_units", [])
    if not target_units:
        return slot_data
    user_unit = _get_user_unit_from_token(authorization, db)
    if user_unit and user_unit in target_units:
        return slot_data
    return {"active": False, "slot": slot}

@app.post("/api/stream_share/{slot}", summary="Set shared stream state for a specific slot")
async def set_stream_share_slot(slot: int = Path(..., ge=1, le=MAX_STREAM_SLOTS), data: Dict = Body(...)):
    """Update the stream state for a specific slot (called by stream.html multicast broadcasting)."""
    global _active_stream_shares
    slot_state = {
        "active": data.get("active", False),
        "slot": slot,
        "streamId": data.get("streamId"),
        "stream_url": data.get("stream_url"),
        "stream_type": data.get("stream_type", "video"),
        "isCamera": data.get("isCamera", False),
        "source": data.get("source"),
        "details": data.get("details"),
        "target_units": data.get("target_units", []),
        "username": data.get("username"),
        "timestamp": datetime.now(timezone.utc).isoformat()
    }
    _active_stream_shares[slot] = slot_state
    broadcast_websocket_update("camera", "stream_share", slot_state)
    return {"status": "success", "slot": slot}

# ===========================
# WebSocket Endpoint
# ===========================

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """
    WebSocket endpoint for real-time updates.
    Implements server-side relay for video streaming and map data synchronization.
    """
    if not AUTONOMOUS_MODULES_AVAILABLE or not websocket_manager or not websocket_event_handler:
        await websocket.close(code=1011, reason="WebSocket not available")
        return
    
    connection_id = str(uuid.uuid4())
    
    try:
        await websocket_manager.connect(websocket, connection_id)
        
        # Extract token from query params or first message for session IP tracking
        token = None
        try:
            # Try to get token from query params
            if hasattr(websocket, 'query_params'):
                token = websocket.query_params.get('token')
        except Exception:
            pass
        
        while True:
            try:
                data = await websocket.receive_json()
                
                # Update connection activity tracking
                websocket_manager.update_connection_activity(connection_id)
                
                # First message might contain authentication token
                if not token and isinstance(data, dict):
                    token = data.get('token') or data.get('auth_token')
                
                # SERVER-SIDE RELAY: Broadcast camera/stream messages to all subscribed clients
                # This enables global video streaming and data synchronization
                # We relay these messages and skip the standard handler (relay_handled=True)
                # to avoid "unknown message" errors for broadcast-only message types
                message_type = data.get('type')
                relay_handled = False
                
                if message_type == 'camera_frame':
                    # Server-side rate limiting: skip frames that arrive too fast to prevent
                    # event loop starvation which would block mobile HTTPS connections.
                    now = time.monotonic()
                    last = _camera_last_relay.get(connection_id, 0.0)
                    if now - last >= _CAMERA_FRAME_MIN_INTERVAL:
                        _camera_last_relay[connection_id] = now
                        logger.debug(f"Relaying camera frame from {connection_id}")
                        await websocket_manager.publish_to_channel('camera', {
                            'type': 'camera_frame',
                            'channel': 'camera',
                            'frame': data.get('frame'),
                            'streamId': data.get('streamId', 'camera_main'),
                            'timestamp': datetime.now(timezone.utc).isoformat(),
                            'source_connection': connection_id
                        })
                    relay_handled = True
                    
                elif message_type == 'stream_share':
                    # Relay stream sharing notifications to camera channel
                    logger.info(f"Relaying stream_share from {connection_id}: active={data.get('active')}")
                    # Persist state for polling endpoint (/api/stream_share)
                    global _active_stream_share
                    is_camera = data.get('isCamera', False)
                    stream_type = data.get('stream_type', 'mjpeg' if is_camera else 'video')
                    _active_stream_share = {
                        "active": data.get('active', False),
                        "streamId": data.get('streamId', 'camera_main'),
                        "stream_url": data.get('stream_url'),
                        "stream_type": stream_type,
                        "isCamera": is_camera,
                        "source": data.get('source'),
                        "details": data.get('details'),
                        "timestamp": datetime.now(timezone.utc).isoformat()
                    }
                    # Also update per-slot state when a slot is provided (multicast distribution)
                    slot = data.get('slot')
                    if slot is not None:
                        try:
                            slot_int = int(slot)
                            if 1 <= slot_int <= MAX_STREAM_SLOTS:
                                _active_stream_shares[slot_int] = {
                                    "active": data.get('active', False),
                                    "slot": slot_int,
                                    "streamId": data.get('streamId', 'camera_main'),
                                    "stream_url": data.get('stream_url'),
                                    "stream_type": stream_type,
                                    "isCamera": is_camera,
                                    "source": data.get('source'),
                                    "details": data.get('details'),
                                    "target_units": data.get('target_units', []),
                                    "username": data.get('username'),
                                    "timestamp": datetime.now(timezone.utc).isoformat()
                                }
                        except (ValueError, TypeError):
                            pass
                    relay_msg = {
                        'type': 'stream_share',
                        'channel': 'camera',
                        'streamId': data.get('streamId', 'camera_main'),
                        'active': data.get('active', False),
                        'isCamera': is_camera,
                        'stream_url': data.get('stream_url'),
                        'stream_type': stream_type,
                        'source': data.get('source'),
                        'details': data.get('details'),
                        'target_units': data.get('target_units', []),
                        'timestamp': datetime.now(timezone.utc).isoformat(),
                        'source_connection': connection_id
                    }
                    if slot is not None:
                        relay_msg['slot'] = slot
                    await websocket_manager.publish_to_channel('camera', relay_msg)
                    relay_handled = True
                    
                elif message_type == 'stream_available':
                    # EUD announces that a camera stream is available for admin review.
                    # This is relayed to admins (stream.html) but does NOT update
                    # _active_stream_share, so stream_share.html will not display it
                    # until an admin explicitly broadcasts the stream.
                    logger.info(f"Relaying stream_available from {connection_id}: active={data.get('active')}")
                    await websocket_manager.publish_to_channel('camera', {
                        'type': 'stream_available',
                        'channel': 'camera',
                        'streamId': data.get('streamId', 'camera_main'),
                        'active': data.get('active', False),
                        'isCamera': data.get('isCamera', False),
                        'source': data.get('source'),
                        'details': data.get('details'),
                        'timestamp': datetime.now(timezone.utc).isoformat(),
                        'source_connection': connection_id
                    })
                    relay_handled = True

                elif message_type == 'camera_stream_stop':
                    # Relay stream stop to camera channel
                    logger.info(f"Relaying camera_stream_stop from {connection_id}")
                    await websocket_manager.publish_to_channel('camera', {
                        'type': 'camera_stream_stop',
                        'channel': 'camera',
                        'timestamp': datetime.now(timezone.utc).isoformat(),
                        'source_connection': connection_id
                    })
                    relay_handled = True
                    
                elif message_type == 'broadcast_selected':
                    # Relay broadcast selection so the source (e.g. overview.html) can start sending frames
                    stream_id = str(data.get('streamId', '')).replace('\n', '').replace('\r', '')
                    logger.info(f"Relaying broadcast_selected from {connection_id}: streamId={stream_id}")
                    await websocket_manager.publish_to_channel('camera', {
                        'type': 'broadcast_selected',
                        'channel': 'camera',
                        'streamId': data.get('streamId'),
                        'source': data.get('source'),
                        'details': data.get('details'),
                        'timestamp': datetime.now(timezone.utc).isoformat(),
                        'source_connection': connection_id
                    })
                    relay_handled = True
                    
                # Relay map data updates (markers, drawings, overlays, symbols)
                elif message_type == 'marker_update':
                    await websocket_manager.publish_to_channel('markers', data)
                    relay_handled = True
                elif message_type == 'drawing_update':
                    await websocket_manager.publish_to_channel('drawings', data)
                    relay_handled = True
                elif message_type == 'overlay_update':
                    await websocket_manager.publish_to_channel('overlays', data)
                    relay_handled = True
                elif message_type == 'symbol_update':
                    await websocket_manager.publish_to_channel('symbols', data)
                    relay_handled = True
                
                # Process standard handler if message wasn't relay-only
                if not relay_handled:
                    await websocket_event_handler.handle_message(connection_id, data)
                    
            except WebSocketDisconnect:
                # Client disconnected, break out of receive loop
                break
            except json.JSONDecodeError as e:
                logger.error(f"Invalid JSON from {connection_id}: {e}")
                try:
                    await websocket_manager.send_personal_message(connection_id, {
                        "type": "error",
                        "error": "Invalid JSON format"
                    })
                except Exception:
                    break  # Connection is dead, exit loop
            except Exception as e:
                # Check if this is a connection-related error
                error_str = str(e).lower()
                if 'disconnect' in error_str or 'closed' in error_str or 'receive' in error_str:
                    # Connection is dead, break out
                    break
                logger.error(f"Error processing message from {connection_id}: {e}")
                # For other errors, continue processing
            
    except WebSocketDisconnect:
        websocket_manager.disconnect(connection_id)
        _camera_last_relay.pop(connection_id, None)
        logger.info(f"WebSocket client disconnected: {connection_id}")
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
        websocket_manager.disconnect(connection_id)
        _camera_last_relay.pop(connection_id, None)

@app.get("/api/websocket/status", summary="Get WebSocket status")
def get_websocket_status():
    """Get WebSocket connection status with health metrics"""
    if not AUTONOMOUS_MODULES_AVAILABLE or not websocket_manager:
        raise HTTPException(status_code=501, detail="WebSocket not available")
    
    stats = websocket_manager.get_all_connection_stats()
    
    return {
        "status": "success",
        "connections": websocket_manager.get_connection_count(),
        "channels": {
            channel: websocket_manager.get_channel_subscribers(channel)
            for channel in websocket_manager.list_channels()
        },
        "stats": stats
    }

# ===========================
# System Health and Status
# ===========================

@app.get("/api/system/health", summary="System health check")
def system_health():
    """Get system health status"""
    health_status = {
        "status": "healthy",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "modules": {
            "core": True,
            "autonomous": AUTONOMOUS_MODULES_AVAILABLE,
            "websocket": websocket_manager is not None,
            "data_server": DATA_SERVER_AVAILABLE and data_server_manager is not None,
            "geofencing": geofencing_manager is not None,
            "autonomous_engine": autonomous_engine is not None,
            "cot_protocol": AUTONOMOUS_MODULES_AVAILABLE
        }
    }
    
    if websocket_manager:
        health_status["websocket_connections"] = websocket_manager.get_connection_count()
    
    # Add data server status
    if DATA_SERVER_AVAILABLE and data_server_manager:
        health_status["data_server_running"] = data_server_manager.is_running()
        if data_server_manager.is_running():
            ds_status = data_server_manager.get_status()
            if ds_status:
                health_status["data_server_connections"] = ds_status.get("active_connections", 0)
    
    if geofencing_manager:
        health_status["geofences_active"] = len(geofencing_manager.list_geofences(enabled_only=True))
    
    if autonomous_engine:
        health_status["rules_active"] = len(autonomous_engine.list_rules(enabled_only=True))
    
    return health_status

@app.get("/api/data_server/status", summary="Get data server status")
def get_data_server_status():
    """Get status of the separate data distribution server"""
    if not DATA_SERVER_AVAILABLE or not data_server_manager:
        raise HTTPException(status_code=501, detail="Data server not available")
    
    if not data_server_manager.is_running():
        return {
            "status": "stopped",
            "message": "Data server is not running"
        }
    
    # Get detailed status from data server
    status = data_server_manager.get_status()
    if status:
        return {
            "status": "success",
            "data_server": status
        }
    else:
        return {
            "status": "error",
            "message": "Failed to get data server status"
        }

# ===========================
# SDR (Software-Defined Radio) Endpoints
# ===========================

import subprocess as _subprocess
import shutil as _shutil
import math as _math
import struct as _struct
import socket as _socket

# Optional: pyrtlsdr — install with `pip install pyrtlsdr` on the deployment host
try:
    from rtlsdr import RtlSdr as _RtlSdr  # type: ignore
    _RTLSDR_LIB = True
except ImportError:
    _RtlSdr = None
    _RTLSDR_LIB = False

# Optional: numpy — provides fast FFT; install with `pip install numpy`
try:
    import numpy as _np  # type: ignore
    _NUMPY_LIB = True
except ImportError:
    _np = None
    _NUMPY_LIB = False

# RTL-SDR USB identifiers (Realtek chipset, common dongles)
_RTL_SDR_VID = 0x0bda
_RTL_SDR_PIDS = {0x2832, 0x2838, 0x2888}
_SDR_DEFAULT_NFFT = 1024

# -6.0 dB: empirical reference-level calibration offset (full-scale IQ → approx dBm into 50 Ω)
_SDR_POWER_OFFSET_DB = -6.0

# rtl_tcp defaults — rtl_tcp ships with the rtl-sdr package.
# Start with: rtl_tcp -a 0.0.0.0   (Linux/Raspberry Pi)
#         or: rtl_tcp.exe           (Windows)
_RTL_TCP_DEFAULT_HOST = "127.0.0.1"
_RTL_TCP_DEFAULT_PORT = 1234


def _detect_rtlsdr_devices() -> list:
    """
    Detect RTL-SDR USB sticks using multiple methods:
    1. pyrtlsdr device enumeration (most reliable)
    2. lsusb output parsing (Linux/macOS)
    3. pyserial VID/PID scan (cross-platform)
    4. Presence of rtl_test CLI tool
    """
    devices = []

    # Method 1: pyrtlsdr
    if _RTLSDR_LIB:
        try:
            count = _RtlSdr.get_device_count()
            for i in range(count):
                try:
                    name = _RtlSdr.get_device_name(i)
                except Exception:
                    name = f"RTL-SDR Device #{i}"
                devices.append({"index": i, "name": name, "source": "pyrtlsdr", "available": True})
            if devices:
                return devices
        except Exception:
            pass

    # Method 2: lsusb (Linux/macOS)
    if _shutil.which("lsusb"):
        try:
            out = _subprocess.run(["lsusb"], capture_output=True, text=True, timeout=5)
            for line in out.stdout.splitlines():
                ll = line.lower()
                if "0bda:2832" in ll or "0bda:2838" in ll or ("realtek" in ll and "sdr" in ll):
                    devices.append({"index": len(devices), "name": line.strip(), "source": "lsusb", "available": True})
        except Exception:
            pass

    # Method 3: pyserial VID/PID
    if serial_list_ports:
        try:
            for p in serial_list_ports.comports():
                vid = getattr(p, "vid", None)
                pid = getattr(p, "pid", None)
                if vid == _RTL_SDR_VID and pid in _RTL_SDR_PIDS:
                    devices.append({
                        "index": len(devices),
                        "name": f"{p.description or p.device} (RTL-SDR)",
                        "source": "serial_usb",
                        "device": p.device,
                        "available": True,
                    })
        except Exception:
            pass

    # Method 4: CLI tools present (implies RTL-SDR library installed)
    if not devices and _shutil.which("rtl_test"):
        devices.append({"index": 0, "name": "RTL-SDR (rtl-sdr tools detected)", "source": "rtl_tools", "available": True})

    return devices


# rtl_tcp defaults — rtl_tcp ships with the rtl-sdr package.
# Start with: rtl_tcp -a 0.0.0.0   (Linux/Raspberry Pi)
#         or: rtl_tcp.exe           (Windows)
def _check_rtl_tcp(host: str = _RTL_TCP_DEFAULT_HOST, port: int = _RTL_TCP_DEFAULT_PORT) -> bool:
    """Return True if an rtl_tcp server is reachable and responds with the expected magic header."""
    try:
        with _socket.create_connection((host, port), timeout=1) as sock:
            header = sock.recv(12)
            return len(header) == 12 and header.startswith(b"RTL0")
    except Exception:
        return False


def _get_spectrum_rtl_tcp(
    center_freq_hz: float,
    sample_rate_hz: float,
    gain: float,
    nfft: int,
    host: str = _RTL_TCP_DEFAULT_HOST,
    port: int = _RTL_TCP_DEFAULT_PORT,
) -> dict:
    """
    Acquire real IQ samples from a running rtl_tcp server and return an FFT spectrum.

    rtl_tcp ships with the standard rtl-sdr package.  Start it with:
        Linux / Raspberry Pi:  rtl_tcp -a 0.0.0.0
        Windows:               rtl_tcp.exe
    The default address is 127.0.0.1:1234.
    """
    CMD_SET_FREQ        = 0x01
    CMD_SET_SAMPLE_RATE = 0x02
    CMD_SET_GAIN_MODE   = 0x03  # 1 = manual
    CMD_SET_GAIN        = 0x04  # value in tenths of dB (e.g. 200 = 20.0 dB)
    DONGLE_INFO_SIZE    = 12

    def _cmd(t: int, v: int) -> bytes:
        return _struct.pack(">BI", t, v)

    n_bytes = nfft * 2  # one I byte + one Q byte per sample
    sock = _socket.create_connection((host, port), timeout=5)
    try:
        # Handshake: read dongle info header sent by rtl_tcp on connection
        header = b""
        while len(header) < DONGLE_INFO_SIZE:
            chunk = sock.recv(DONGLE_INFO_SIZE - len(header))
            if not chunk:
                raise OSError("rtl_tcp disconnected during handshake")
            header += chunk
        if not header.startswith(b"RTL0"):
            raise OSError("Not an rtl_tcp server (unexpected magic bytes)")

        # Configure the dongle
        sock.sendall(
            _cmd(CMD_SET_FREQ,        int(center_freq_hz))
            + _cmd(CMD_SET_SAMPLE_RATE, int(sample_rate_hz))
            + _cmd(CMD_SET_GAIN_MODE, 1)
            + _cmd(CMD_SET_GAIN,      int(gain * 10))
        )

        # Read raw IQ bytes: I=uint8, Q=uint8, both biased at 128
        raw = b""
        while len(raw) < n_bytes:
            chunk = sock.recv(min(4096, n_bytes - len(raw)))
            if not chunk:
                break
            raw += chunk
    finally:
        sock.close()

    if len(raw) < n_bytes:
        raise OSError(f"rtl_tcp: received only {len(raw)}/{n_bytes} IQ bytes")

    if _NUMPY_LIB:
        iq    = _np.frombuffer(raw[:n_bytes], dtype=_np.uint8)
        i_f   = (iq[0::2].astype(_np.float32) - 128.0) / 128.0
        q_f   = (iq[1::2].astype(_np.float32) - 128.0) / 128.0
        samples = i_f[:nfft] + 1j * q_f[:nfft]
        window  = _np.hanning(nfft)
        fft_out = _np.fft.fftshift(_np.fft.fft(samples * window, nfft))
        power   = (20.0 * _np.log10(_np.abs(fft_out) / nfft + 1e-10) + _SDR_POWER_OFFSET_DB).tolist()
    else:
        # Pure-Python fallback (no numpy): approximate magnitude via |I|+|Q|
        power = []
        for k in range(nfft):
            i_val = (raw[k * 2]     - 128) / 128.0
            q_val = (raw[k * 2 + 1] - 128) / 128.0
            mag   = (abs(i_val) + abs(q_val)) / 2.0 + 1e-10
            power.append(round(20.0 * _math.log10(mag) + _SDR_POWER_OFFSET_DB, 2))

    return {"spectrum": power, "source": "rtl_tcp", "nfft": nfft}


def _get_spectrum_data(
    center_freq_hz: float,
    sample_rate_hz: float,
    gain: float,
    nfft: int,
    rtl_tcp_host: str = _RTL_TCP_DEFAULT_HOST,
    rtl_tcp_port: int = _RTL_TCP_DEFAULT_PORT,
) -> dict:
    """
    Acquire spectrum data from real hardware only. Tries (in order):
    1. pyrtlsdr + numpy — direct IQ sampling + FFT
    2. rtl_power subprocess — CLI-based sweeping
    3. rtl_tcp server — connect to a running rtl_tcp instance (real hardware via TCP)
    Raises HTTP 503 if no acquisition path succeeds.
    """
    # 1. pyrtlsdr + numpy
    if _RTLSDR_LIB and _NUMPY_LIB:
        try:
            sdr = _RtlSdr()
            try:
                sdr.sample_rate = sample_rate_hz
                sdr.center_freq = center_freq_hz
                sdr.gain = gain
                samples = sdr.read_samples(nfft * 8)
            finally:
                sdr.close()

            window = _np.hanning(nfft)
            fft_data = _np.fft.fftshift(_np.fft.fft(samples[:nfft] * window, nfft))
            # Normalize to approx dBm (relative to 1 mW into 50 Ω)
            # -6.0 dB: empirical reference-level offset (full-scale IQ → ~dBm into 50 Ω)
            power = (20.0 * _np.log10(_np.abs(fft_data) / nfft + 1e-10) - 6.0).tolist()
            return {"spectrum": power, "source": "rtlsdr_hardware", "nfft": nfft}
        except Exception as exc:
            logger.warning("RTL-SDR hardware read failed: %s", exc)

    # 2. rtl_power subprocess
    if _shutil.which("rtl_power"):
        try:
            freq_start = int(center_freq_hz - sample_rate_hz / 2)
            freq_end = int(center_freq_hz + sample_rate_hz / 2)
            step = max(1, int(sample_rate_hz / nfft))
            result = _subprocess.run(
                ["rtl_power", "-f", f"{freq_start}:{freq_end}:{step}",
                 "-g", str(int(gain)), "-e", "1s", "/dev/null"],
                capture_output=True, text=True, timeout=10,
            )
            powers: list = []
            for line in result.stdout.strip().splitlines():
                parts = line.split(",")
                if len(parts) > 6:
                    try:
                        powers.extend(float(x.strip()) for x in parts[6:])
                    except ValueError:
                        pass
            if powers:
                # Resample to exactly nfft bins
                src_len = len(powers)
                resampled = [powers[int(i * src_len / nfft)] for i in range(nfft)]
                return {"spectrum": resampled, "source": "rtl_power", "nfft": nfft}
        except Exception as exc:
            logger.warning("rtl_power subprocess failed: %s", exc)

    # 3. rtl_tcp server — real hardware accessed via TCP (no local driver required)
    try:
        return _get_spectrum_rtl_tcp(
            center_freq_hz, sample_rate_hz, gain, nfft, rtl_tcp_host, rtl_tcp_port
        )
    except Exception as exc:
        logger.warning("rtl_tcp acquisition failed (%s:%s): %s", rtl_tcp_host, rtl_tcp_port, exc)

    raise HTTPException(
        status_code=503,
        detail=(
            "RTL-SDR driver not available. Install one of:\n"
            "  pip install pyrtlsdr numpy\n"
            "  sudo apt install rtl-sdr\n"
            "Then start rtl_tcp with: rtl_tcp -a 0.0.0.0"
        ),
    )


@app.get("/api/sdr/devices", summary="Detect RTL-SDR USB devices")
def sdr_get_devices():
    """
    Detect connected RTL-SDR dongles using pyrtlsdr, lsusb, pyserial VID/PID,
    and CLI tool presence.  Returns device list and backend capability flags.
    """
    devices = _detect_rtlsdr_devices()
    return {
        "devices": devices,
        "count": len(devices),
        "capabilities": {
            "pyrtlsdr":  _RTLSDR_LIB,
            "numpy":     _NUMPY_LIB,
            "rtl_power": bool(_shutil.which("rtl_power")),
            "rtl_test":  bool(_shutil.which("rtl_test")),
        },
    }


@app.get("/api/sdr/status", summary="RTL-SDR hardware status")
def sdr_status():
    """Return current RTL-SDR hardware availability and library status."""
    devices = _detect_rtlsdr_devices()
    return {
        "available":       len(devices) > 0,
        "device_count":    len(devices),
        "devices":         devices,
        "library_rtlsdr":  _RTLSDR_LIB,
        "library_numpy":   _NUMPY_LIB,
        "cli_rtl_power":   bool(_shutil.which("rtl_power")),
        "cli_rtl_test":    bool(_shutil.which("rtl_test")),
    }


@app.post("/api/sdr/connect", summary="Connect to RTL-SDR device")
def sdr_connect_device(data: dict = Body(...)):
    """
    Connect to a real RTL-SDR device.  Returns 503 if no hardware is detected
    and rtl_tcp is not reachable.

    Body fields (all optional):
    - device_index (int)       — 0-based device index (default 0)
    - frequency_mhz (float)    — initial center frequency in MHz (default 433.92)
    - sample_rate_mhz (float)  — sample rate in MHz (default 2.4)
    - gain (float)             — tuner gain in dB (default 20.0)
    - rtl_tcp_host (str)       — rtl_tcp server host (default 127.0.0.1)
    - rtl_tcp_port (int)       — rtl_tcp server port (default 1234)
    """
    freq_mhz        = float(data.get("frequency_mhz",  433.920))
    sample_rate_mhz = float(data.get("sample_rate_mhz", 2.4))
    gain            = float(data.get("gain", 20.0))
    tcp_host        = str(data.get("rtl_tcp_host", _RTL_TCP_DEFAULT_HOST))
    tcp_port        = int(data.get("rtl_tcp_port", _RTL_TCP_DEFAULT_PORT))

    devices         = _detect_rtlsdr_devices()
    hw_available    = len(devices) > 0
    has_local_driver = _RTLSDR_LIB or bool(_shutil.which("rtl_power")) or bool(_shutil.which("rtl_test"))
    rtl_tcp_ok      = _check_rtl_tcp(tcp_host, tcp_port)

    if not hw_available and not rtl_tcp_ok:
        raise HTTPException(
            status_code=503,
            detail=(
                "No RTL-SDR hardware detected and rtl_tcp is not reachable.\n"
                "Options:\n"
                "  1. Install driver:  pip install pyrtlsdr numpy\n"
                "  2. Install rtl-sdr: sudo apt install rtl-sdr\n"
                "  3. Start rtl_tcp:   rtl_tcp -a 0.0.0.0  (then set host/port in the UI)"
            ),
        )

    if rtl_tcp_ok:
        mode = "rtl_tcp"
    elif has_local_driver:
        mode = "hardware"
    else:
        # Hardware detected via COM/USB, but no local driver installed.
        # driver_available=False signals the UI to show installation instructions.
        mode = "com_port"

    return {
        "status":            "connected",
        "mode":              mode,
        "device_count":      len(devices),
        "devices":           devices,
        "frequency_mhz":     freq_mhz,
        "sample_rate_mhz":   sample_rate_mhz,
        "gain":              gain,
        "driver_available":  has_local_driver or rtl_tcp_ok,
        "rtl_tcp":           rtl_tcp_ok,
        "rtl_tcp_host":      tcp_host,
        "rtl_tcp_port":      tcp_port,
        "capabilities": {
            "pyrtlsdr":  _RTLSDR_LIB,
            "numpy":     _NUMPY_LIB,
            "rtl_power": bool(_shutil.which("rtl_power")),
            "rtl_tcp":   rtl_tcp_ok,
        },
    }


@app.post("/api/sdr/measure", summary="Get SDR spectrum measurement")
def sdr_measure(data: dict = Body(...)):
    """
    Return a spectrum measurement as an array of power values (dBm per FFT bin).

    Body fields:
    - frequency_mhz (float)   — center frequency in MHz (default 433.92)
    - sample_rate_mhz (float) — bandwidth in MHz (default 2.4)
    - gain (float)            — tuner gain in dB (default 20.0)
    - nfft (int)              — FFT size, power-of-2, 64–2048 (default 1024)
    - rtl_tcp_host (str)      — rtl_tcp server host (default 127.0.0.1)
    - rtl_tcp_port (int)      — rtl_tcp server port (default 1234)

    Response:
    - spectrum (list[float])  — nfft power values in dBm
    - source (str)            — 'rtlsdr_hardware', 'rtl_power', or 'rtl_tcp'
    - freq_start_mhz / freq_end_mhz — display frequency range
    - bw_per_bin_hz           — bandwidth per bin
    """
    freq_mhz        = float(data.get("frequency_mhz",  433.920))
    sample_rate_mhz = float(data.get("sample_rate_mhz", 2.4))
    gain            = float(data.get("gain", 20.0))
    nfft_req        = int(data.get("nfft", _SDR_DEFAULT_NFFT))
    tcp_host        = str(data.get("rtl_tcp_host", _RTL_TCP_DEFAULT_HOST))
    tcp_port        = int(data.get("rtl_tcp_port", _RTL_TCP_DEFAULT_PORT))

    # Clamp nfft to nearest power-of-2 in [64, 2048]
    # Use `nfft * 2 <= min(nfft_req, 2048)` so we can actually reach 2048.
    nfft = 64
    while nfft * 2 <= min(nfft_req, 2048):
        nfft *= 2

    center_freq_hz  = freq_mhz * 1e6
    sample_rate_hz  = sample_rate_mhz * 1e6

    result = _get_spectrum_data(center_freq_hz, sample_rate_hz, gain, nfft, tcp_host, tcp_port)

    return {
        "spectrum":        result["spectrum"],
        "source":          result["source"],
        "nfft":            nfft,
        "freq_start_mhz":  (center_freq_hz - sample_rate_hz / 2) / 1e6,
        "freq_end_mhz":    (center_freq_hz + sample_rate_hz / 2) / 1e6,
        "center_freq_mhz": freq_mhz,
        "sample_rate_mhz": sample_rate_mhz,
        "bw_per_bin_hz":   sample_rate_hz / nfft,
        "timestamp":       datetime.now(timezone.utc).isoformat(),
    }


@app.get("/api/dependencies/check", summary="Check hardware and software dependencies")
def check_dependencies():
    """
    Check the availability of all required and optional dependencies.

    Returns a structured report with:
    - Python packages (required and optional)
    - System tools (rtl_tcp, rtl_power, rtl_test, rtl_fm)
    - Missing items with installation instructions
    - Overall ready status
    """
    import importlib.util as _iutil
    import platform as _platform

    is_windows = _platform.system() == "Windows"

    def _pkg_present(name: str) -> bool:
        return _iutil.find_spec(name) is not None

    # --- Python package checks ---
    python_deps = [
        {
            "name": "fastapi",
            "required": True,
            "present": _pkg_present("fastapi"),
            "install": "pip install fastapi",
            "description": "Web framework (core)",
        },
        {
            "name": "uvicorn",
            "required": True,
            "present": _pkg_present("uvicorn"),
            "install": "pip install uvicorn[standard]",
            "description": "ASGI server (core)",
        },
        {
            "name": "meshtastic",
            "required": False,
            "present": _pkg_present("meshtastic"),
            "install": "pip install meshtastic",
            "description": "Meshtastic device communication",
        },
        {
            "name": "serial",
            "required": False,
            "present": _pkg_present("serial.tools.list_ports"),
            "install": "pip install pyserial",
            "description": "Serial port access (Meshtastic/SDR)",
        },
        {
            "name": "rtlsdr",
            "required": False,
            # Use the module-level flag set at import time (same as the rest of the SDR code)
            "present": _RTLSDR_LIB,
            "install": "pip install pyrtlsdr",
            "description": "Direct RTL-SDR hardware access",
        },
        {
            "name": "numpy",
            "required": False,
            # Use the module-level flag set at import time (same as the rest of the SDR code)
            "present": _NUMPY_LIB,
            "install": "pip install numpy",
            "description": "Fast FFT for SDR spectrum analysis",
        },
    ]

    # --- System tool checks ---
    if is_windows:
        rtl_tcp_install = (
            "Download rtl-sdr tools from https://osmocom.org/projects/rtl-sdr/wiki "
            "and add rtl_tcp.exe to your PATH"
        )
        rtl_sdr_install = (
            "Download rtl-sdr tools from https://osmocom.org/projects/rtl-sdr/wiki "
            "and add the executables to your PATH"
        )
    else:
        rtl_tcp_install = "sudo apt install rtl-sdr  (Debian/Ubuntu/Raspberry Pi)"
        rtl_sdr_install = "sudo apt install rtl-sdr  (Debian/Ubuntu/Raspberry Pi)"

    system_deps = [
        {
            "name": "rtl_tcp",
            "present": bool(_shutil.which("rtl_tcp") or _shutil.which("rtl_tcp.exe")),
            "install": rtl_tcp_install,
            "description": (
                "RTL-SDR TCP server — required to stream SDR data over TCP. "
                "Start with: rtl_tcp -a 0.0.0.0"
            ),
        },
        {
            "name": "rtl_power",
            "present": bool(_shutil.which("rtl_power") or _shutil.which("rtl_power.exe")),
            "install": rtl_sdr_install,
            "description": "RTL-SDR power sweep tool (optional, used for spectrum scan)",
        },
        {
            "name": "rtl_test",
            "present": bool(_shutil.which("rtl_test") or _shutil.which("rtl_test.exe")),
            "install": rtl_sdr_install,
            "description": "RTL-SDR test/detection tool (optional)",
        },
        {
            "name": "rtl_fm",
            "present": bool(_shutil.which("rtl_fm") or _shutil.which("rtl_fm.exe")),
            "install": rtl_sdr_install,
            "description": "RTL-SDR FM demodulator (optional, used for audio streaming)",
        },
    ]

    missing_required = [d for d in python_deps if d["required"] and not d["present"]]
    missing_optional = (
        [d for d in python_deps if not d["required"] and not d["present"]]
        + [d for d in system_deps if not d["present"]]
    )

    return {
        "ready": len(missing_required) == 0,
        "platform": _platform.system(),
        "python_packages": python_deps,
        "system_tools": system_deps,
        "missing_required": [d["name"] for d in missing_required],
        "missing_optional": [d["name"] for d in missing_optional],
        "install_hints": {
            d["name"]: d["install"]
            for d in missing_required + missing_optional
        },
    }


def _make_wav_header(sample_rate: int, channels: int, bits_per_sample: int) -> bytes:
    """Return a streaming-friendly WAV header (data chunk size = 0xFFFFFFFF for unknown length)."""
    byte_rate   = sample_rate * channels * bits_per_sample // 8
    block_align = channels * bits_per_sample // 8
    return _struct.pack(
        '<4sI4s4sIHHIIHH4sI',
        b'RIFF', 0xFFFFFFFF, b'WAVE',
        b'fmt ', 16, 1, channels,
        sample_rate, byte_rate, block_align, bits_per_sample,
        b'data', 0xFFFFFFFF,
    )


@app.get("/api/sdr/audio", summary="Stream demodulated SDR audio")
async def sdr_audio_stream(
    frequency_mhz: float = 433.920,
    mode: str = "fm",
    sample_rate: int = 200000,
    audio_rate: int = 48000,
    gain: float = 20.0,
    squelch: int = 0,
):
    """
    Stream demodulated audio from an RTL-SDR dongle as WAV/PCM.

    Query params:
    - frequency_mhz  – center frequency in MHz (default 433.920)
    - mode           – demodulation mode: fm, am, ssb/usb, lsb, wbfm (default fm)
    - sample_rate    – SDR sample rate in Hz passed to rtl_fm (default 200000)
    - audio_rate     – output audio sample rate in Hz (default 48000)
    - gain           – tuner gain in dB (default 20.0)
    - squelch        – squelch level 0–9 (default 0 = off)

    Tries in order:
    1. rtl_fm subprocess — pipes raw 16-bit PCM wrapped in a WAV header
    2. pyrtlsdr + numpy — FM demodulation on IQ samples
    Returns 503 if no hardware or tool is available.
    """
    mode_lower = mode.lower()

    # 1. rtl_fm CLI tool
    if _shutil.which("rtl_fm"):
        _mode_map = {
            "fm": "fm", "am": "am",
            "ssb": "usb", "usb": "usb", "lsb": "lsb", "wbfm": "wbfm",
        }
        rtl_mode = _mode_map.get(mode_lower, "fm")
        cmd = [
            "rtl_fm",
            "-f", str(int(frequency_mhz * 1e6)),
            "-M", rtl_mode,
            "-s", str(sample_rate),
            "-r", str(audio_rate),
            "-g", str(int(gain)),
        ]
        if squelch > 0:
            cmd += ["-l", str(squelch)]
        cmd.append("-")

        proc = _subprocess.Popen(cmd, stdout=_subprocess.PIPE, stderr=_subprocess.DEVNULL)
        wav_hdr = _make_wav_header(audio_rate, 1, 16)

        async def _rtl_fm_stream():
            yield wav_hdr
            try:
                loop = asyncio.get_event_loop()
                while True:
                    chunk = await loop.run_in_executor(None, proc.stdout.read, 4096)
                    if not chunk:
                        break
                    yield chunk
            finally:
                proc.terminate()

        return StreamingResponse(
            _rtl_fm_stream(),
            media_type="audio/wav",
            headers={"Cache-Control": "no-cache"},
        )

    # 2. pyrtlsdr + numpy FM demodulation
    if _RTLSDR_LIB and _NUMPY_LIB:
        # Choose SDR_RATE as the smallest multiple of audio_rate that is >= 192000
        SDR_RATE  = audio_rate * max(1, 192000 // audio_rate)
        N_SAMPLES = SDR_RATE * 2
        dec       = max(1, SDR_RATE // audio_rate)
        wav_hdr   = _make_wav_header(audio_rate, 1, 16)

        async def _numpy_fm_stream():
            yield wav_hdr
            sdr = _RtlSdr()
            try:
                sdr.sample_rate = SDR_RATE
                sdr.center_freq = frequency_mhz * 1e6
                sdr.gain        = gain
                loop = asyncio.get_event_loop()
                while True:
                    samples = await loop.run_in_executor(None, sdr.read_samples, N_SAMPLES)
                    iq    = _np.array(samples)
                    phase = _np.angle(iq[1:] * _np.conj(iq[:-1]))
                    audio = phase[::dec]
                    pcm   = _np.clip(audio * (32767.0 / _np.pi), -32768, 32767).astype(_np.int16)
                    yield pcm.tobytes()
            except Exception as exc:
                logger.warning("pyrtlsdr audio stream error: %s", exc)
            finally:
                try:
                    sdr.close()
                except Exception:
                    pass

        return StreamingResponse(
            _numpy_fm_stream(),
            media_type="audio/wav",
            headers={"Cache-Control": "no-cache"},
        )

    raise HTTPException(
        status_code=503,
        detail="No RTL-SDR hardware or rtl_fm tool available for audio streaming",
    )


# -------------------------
# Generic error handler
# -------------------------
@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code, content={"status": "error", "message": exc.detail})

# -------------------------
# Catch-all file fallback (after API routes)
# -------------------------
@app.get("/{full_path:path}", include_in_schema=False)
def catch_all(full_path: str, request: Request):
    """
    Catch-all route for serving static HTML files.
    
    Note: This route does not interfere with WebSocket connections.
    With uvicorn[standard] installed, the WebSocket endpoint at /ws is properly
    handled by FastAPI's WebSocket route before this HTTP GET route is considered.
    The 'ws' prefix in blocked_prefixes ensures file system lookups don't occur for it.
    """
    blocked_prefixes = ("api", "static", "assets", "uploads", "_", "favicon.ico", "ws")
    if any(full_path == p or full_path.startswith(p + "/") for p in blocked_prefixes):
        raise HTTPException(status_code=404, detail="Not found")
    candidate = os.path.join(base_path, full_path)
    if os.path.isfile(candidate):
        ext = os.path.splitext(candidate)[1].lower()
        if ext == ".html":
            return FileResponse(candidate, media_type="text/html")
        return FileResponse(candidate)
    candidate_index = os.path.join(candidate, "index.html")
    if os.path.isfile(candidate_index):
        return FileResponse(candidate_index, media_type="text/html")
    raise HTTPException(status_code=404, detail=f"File {full_path} not found. Please use .html extension.")

# -------------------------
# Run (development)
# -------------------------
if __name__ == "__main__":
    import uvicorn
    
    # Detect local network IP
    local_ip, all_detected_ips = get_local_ip()
    
    # Check for SSL certificates
    cert_file = os.path.join(base_path, "cert.pem")
    key_file = os.path.join(base_path, "key.pem")
    use_ssl = os.path.exists(cert_file) and os.path.exists(key_file)
    
    # AUTO-GENERATE SSL CERTIFICATES IF NOT PRESENT (HTTPS by default)
    if not use_ssl:
        logger.info("="*60)
        logger.info("  SSL certificates not found - Generating automatically...")
        logger.info("="*60)
        try:
            # Try to import generate_cert module
            from generate_cert import generate_self_signed_cert
            success = generate_self_signed_cert(cert_file, key_file, local_ip)
            if success:
                use_ssl = True
                logger.info("  *** SSL certificates generated successfully! ***")
                logger.info("  *** Server will start with HTTPS enabled ***")
            else:
                logger.warning("  Certificate generation failed - falling back to HTTP")
        except Exception as e:
            logger.warning(f"  Could not generate SSL certificates: {e}")
            logger.warning("  Server will start in HTTP mode")
            logger.info("  To enable HTTPS manually, run: python generate_cert.py")
        logger.info("="*60)
    
    protocol = "https" if use_ssl else "http"
    
    logger.info("="*60)
    logger.info("  LPU5 TACTICAL TRACKER - Server Starting")
    logger.info("="*60)
    logger.info(f"  Primary Network IP: {local_ip}")
    
    # Display all detected IPs for transparency
    if all_detected_ips and len(all_detected_ips) > 1:
        logger.info(f"  All Detected IPs: {', '.join(all_detected_ips)}")
    
    # Highlight if the WLAN IP (192.168.8.x) is detected
    # Check if primary_ip is already the WLAN IP (most efficient)
    if local_ip.startswith("192.168.8."):
        logger.info(f"  *** WLAN IP (192.168.8.x) DETECTED: {local_ip} ***")
        logger.info(f"  *** Use this IP to access from your mobile device! ***")
    
    logger.info(f"  Server will bind to: 0.0.0.0:8101 (all interfaces)")
    
    # SSL/HTTPS Status
    if use_ssl:
        logger.info("  *** HTTPS ENABLED with SSL certificates ***")
        logger.info("  *** Camera access will work on all devices! ***")
        logger.info("  Note: You may need to accept the self-signed certificate in your browser")
    else:
        logger.info("  HTTP Mode (no SSL certificates found)")
        logger.info("  Camera access requires localhost or HTTPS")
        logger.info("  To enable HTTPS: Generate cert.pem and key.pem in the project root")
    
    logger.info(f"  Access URLs:")
    logger.info(f"    - From this device: {protocol}://127.0.0.1:8101/landing.html")
    logger.info(f"    - From network:     {protocol}://{local_ip}:8101/landing.html")
    
    # Additional access URLs for each detected IP (avoid duplicates)
    if all_detected_ips:
        alternative_ips = [ip for ip in all_detected_ips if ip != local_ip]
        if alternative_ips:
            logger.info(f"  Alternative Network URLs:")
            for ip in alternative_ips:
                logger.info(f"    - {protocol}://{ip}:8101/landing.html")
    
    logger.info("="*60)
    
    # Run with or without SSL
    if use_ssl:
        uvicorn.run(
            app, 
            host="0.0.0.0", 
            port=8101, 
            log_level="info",
            ssl_certfile=cert_file,
            ssl_keyfile=key_file,
            timeout_keep_alive=300,
            timeout_graceful_shutdown=60,
            limit_concurrency=1000,
            limit_max_requests=10000
        )
    else:
        uvicorn.run(
            app,
            host="0.0.0.0",
            port=8101,
            log_level="info",
            timeout_keep_alive=300,
            timeout_graceful_shutdown=60
        )