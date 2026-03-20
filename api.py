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
from starlette.middleware.gzip import GZipMiddleware
from datetime import datetime, timedelta, timezone
import os
import json
import re
import uuid
import hashlib
import jwt
import base64
from typing import Optional, Any, Dict, List, Tuple
import logging
import pathlib
import queue
import random
import threading
import time
import socket
import ssl
import asyncio
import sys
import xml.sax.saxutils as _sax_utils
import requests

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
# Set to True during application shutdown to suppress expected warnings
_SHUTDOWN_IN_PROGRESS = threading.Event()

# Database imports
from database import Base, SessionLocal, engine, get_db
from models import User, Unit, MapMarker, Mission, MeshtasticNode, AutonomousRule, Geofence, ChatMessage, ChatChannel, AuditLog, Drawing, Overlay, APISession, UserGroup, QRCode, PendingRegistration, DeletedMarker, FederatedServer, FederationChallenge
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
        if "tak_team" not in _user_cols:
            _conn.execute(sa_text("ALTER TABLE users ADD COLUMN tak_team VARCHAR DEFAULT 'Cyan'"))
        if "tak_role" not in _user_cols:
            _conn.execute(sa_text("ALTER TABLE users ADD COLUMN tak_role VARCHAR DEFAULT 'Team Member'"))
        if "tak_display_type" not in _user_cols:
            _conn.execute(sa_text("ALTER TABLE users ADD COLUMN tak_display_type VARCHAR DEFAULT 'General Ground Unit'"))

# Import new autonomous modules
try:
    from websocket_manager import ConnectionManager, WebSocketEventHandler, Channels
    WEBSOCKET_AVAILABLE = True
except ImportError as _ws_import_err:
    logger.warning(f"WebSocket manager not available: {_ws_import_err}")
    WEBSOCKET_AVAILABLE = False

try:
    from cot_protocol import CoTEvent, CoTProtocolHandler
    from geofencing import GeofencingManager, GeoFence, haversine_distance
    from autonomous_engine import AutonomousEngine, Rule
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
    from cot_listener_service import CoTListenerService, iTAKBridgeServer
    COT_LISTENER_AVAILABLE = True
except Exception as e:  # pragma: no cover
    logger.warning("CoT listener service not available: %s", e)
    CoTListenerService = None
    iTAKBridgeServer = None
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

# Import federation module
try:
    from federation import (
        load_or_generate_server_keypair as _fed_load_keypair,
        get_server_info as _fed_get_server_info,
        sign_challenge as _fed_sign_challenge,
        verify_signature as _fed_verify_signature,
        generate_challenge as _fed_generate_challenge,
        make_server_info_qr_png as _fed_make_qr_png,
        compute_fingerprint_from_pem as _fed_fingerprint,
        _CHALLENGE_EXPIRE_SECONDS as _FED_CHALLENGE_EXPIRE_SECONDS,
    )
    FEDERATION_AVAILABLE = True
except Exception as _fed_err:
    logger.warning("Federation module not available: %s", _fed_err)
    FEDERATION_AVAILABLE = False

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
        logger.info("✅ Main event loop captured for thread-safe broadcasts")
    except RuntimeError:
        try:
            _MAIN_EVENT_LOOP = asyncio.get_event_loop()
            logger.info("✅ Fallback event loop captured for thread-safe broadcasts")
        except Exception as e:
            logger.warning(f"Could not capture event loop: {e}")

    ensure_db_files()
    ensure_default_admin()
    ensure_default_unit()

    # Warm up the in-memory deleted-marker cache from the database so that
    # CoT echo-backs are suppressed immediately on startup.
    _load_deleted_markers_from_db()

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
        interval = int(cfg.get("meshtastic_sync_interval_seconds", 60))
    except Exception:
        enabled = True
        interval = 60
    global _MESHTASTIC_SYNC_THREAD
    if enabled and (_MESHTASTIC_SYNC_THREAD is None or not _MESHTASTIC_SYNC_THREAD.is_alive()):
        _MESHTASTIC_SYNC_STOP_EVENT.clear()
        _MESHTASTIC_SYNC_THREAD = threading.Thread(target=_meshtastic_sync_worker, args=(interval,), daemon=True, name="meshtastic-sync")
        _MESHTASTIC_SYNC_THREAD.start()
        logger.info("✅ Meshtastic sync worker started (interval=%ss)", interval)

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
        # Default to False – the CoT/ATAK listener only starts when
        # explicitly enabled via "cot_listener_enabled": true in config.json.
        cot_listener_enabled = cfg.get("cot_listener_enabled", False)
    except Exception:
        cot_listener_enabled = False
    if cot_listener_enabled and COT_LISTENER_AVAILABLE:
        try:
            if _start_cot_listener():
                logger.info("✅ CoT listener service started")
            else:
                logger.warning("⚠️  Failed to start CoT listener service")
        except Exception as e:
            logger.error("Error starting CoT listener service: %s", e)

    # Auto-start iTAK CoT bridge (SSL on 127.0.0.1:8089) when enabled
    try:
        cfg = load_json("config") or {}
        itak_bridge_enabled = cfg.get("itak_bridge_enabled", False)
    except Exception:
        itak_bridge_enabled = False
    if itak_bridge_enabled and COT_LISTENER_AVAILABLE and iTAKBridgeServer is not None:
        try:
            if _start_itak_bridge():
                logger.info("✅ iTAK CoT bridge started (SSL 127.0.0.1:%d)",
                            int(cfg.get("itak_bridge_port", iTAKBridgeServer.DEFAULT_PORT)))
            else:
                logger.warning("⚠️  Failed to start iTAK CoT bridge")
        except Exception as e:
            logger.error("Error starting iTAK CoT bridge: %s", e)

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

    # Start automatic federation sync worker if enabled
    try:
        cfg = load_json("config") or {}
        fed_sync_enabled = cfg.get("federation_auto_sync", True)
        fed_sync_interval = int(cfg.get("federation_sync_interval_seconds", 300))
    except Exception:
        fed_sync_enabled = True
        fed_sync_interval = 300
    if fed_sync_enabled and FEDERATION_AVAILABLE:
        global _FEDERATION_SYNC_THREAD
        if _FEDERATION_SYNC_THREAD is None or not _FEDERATION_SYNC_THREAD.is_alive():
            _FEDERATION_SYNC_STOP_EVENT.clear()
            _FEDERATION_SYNC_THREAD = threading.Thread(
                target=_federation_sync_worker,
                args=(fed_sync_interval,),
                daemon=True,
                name="federation-auto-sync",
            )
            _FEDERATION_SYNC_THREAD.start()
            logger.info("✅ Federation auto-sync worker started (interval=%ss)", fed_sync_interval)

    logger.info("Startup complete. DB files ensured.")

    yield

    # ---- Shutdown logic ----
    _SHUTDOWN_IN_PROGRESS.set()
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

    # Stop iTAK CoT bridge
    try:
        _stop_itak_bridge()
    except Exception as e:
        logger.error("Error stopping iTAK CoT bridge: %s", e)

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

    # Stop federation auto-sync thread
    try:
        _FEDERATION_SYNC_STOP_EVENT.set()
    except Exception:
        pass

    # Stop auto-started rtl_tcp process
    try:
        _stop_rtl_tcp_proc()
    except Exception as e:
        logger.error("Error stopping auto-started rtl_tcp: %s", e)

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

# GZip compression for all responses > 500 bytes – dramatically reduces
# bandwidth for JSON API payloads and HTML pages.
app.add_middleware(GZipMiddleware, minimum_size=500)


@app.middleware("http")
async def _add_cache_headers(request: Request, call_next):
    """Append Cache-Control headers for static assets so browsers reuse them
    instead of re-downloading on every page load."""
    response = await call_next(request)
    path = request.url.path
    if path.startswith(("/assets/", "/uploads/", "/static/")):
        # 1 hour cache, revalidate after that
        response.headers.setdefault("Cache-Control", "public, max-age=3600, stale-while-revalidate=86400")
    return response

# Base path - define BEFORE using it
base_path = os.path.dirname(os.path.abspath(__file__))
logger.info(f"LPU5 API initialized. Base path: {base_path}")

# Initialize autonomous systems (if available)
websocket_manager = None
websocket_event_handler = None
geofencing_manager = None
autonomous_engine = None
data_server_manager = None

# Initialize WebSocket manager independently – chat & real-time updates must
# work even when heavier autonomous modules (cot_protocol, geofencing,
# autonomous_engine) failed to import.
if WEBSOCKET_AVAILABLE:
    try:
        websocket_manager = ConnectionManager()
        websocket_event_handler = WebSocketEventHandler(websocket_manager)
        logger.info("WebSocket manager initialized")
    except Exception as _ws_init_err:
        logger.error(f"Failed to initialize WebSocket manager: {_ws_init_err}")
        websocket_manager = None
        websocket_event_handler = None

if AUTONOMOUS_MODULES_AVAILABLE:
    try:
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
    "tak_logins": os.path.join(base_path, "tak_logins_db.json"),
    "tak_login_settings": os.path.join(base_path, "tak_login_settings.json"),
}

DEFAULT_DB_CONTENTS: Dict[str, Any] = {
    "config": {},
    "qr_codes": [],
    "pending_registrations": [],
    "meshtastic_nodes": [],
    "map_markers": [],
    "meshtastic_messages": [],
    "tak_logins": [],
    "tak_login_settings": {
        "server_name": "TAK Server",
        "server_host": "",
        "server_port": 8089,
        "protocol": "ssl",
        "display_name": "LPU5",
    },
}

# -------------------------
# JSON DB helpers
# -------------------------

# In-memory cache for JSON files (especially config) to avoid repeated disk I/O.
# _json_cache maps key -> (mtime, data).  On load we stat the file and skip
# re-reading if mtime has not changed.
_json_cache: Dict[str, Tuple[float, Any]] = {}
_json_cache_lock = threading.Lock()

def load_json(key: str) -> Any:
    path = DB_PATHS.get(key)
    if not path or not os.path.exists(path):
        return DEFAULT_DB_CONTENTS.get(key, {})
    try:
        mtime = os.path.getmtime(path)
        with _json_cache_lock:
            cached = _json_cache.get(key)
            if cached and cached[0] == mtime:
                return cached[1]
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        with _json_cache_lock:
            _json_cache[key] = (mtime, data)
        return data
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
    # Invalidate cache so the next load_json picks up the new data.
    with _json_cache_lock:
        _json_cache.pop(key, None)
    logger.debug("Saved %s -> %s", key, path)

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


def get_current_user(authorization: Optional[str] = Header(None)) -> dict:
    """FastAPI dependency: extract and verify the current user from the
    ``Authorization: Bearer <token>`` header.  Returns the decoded JWT
    payload dict (contains at least ``user_id`` and ``username``).
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
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    return payload


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
    if not websocket_manager:
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
                if _SHUTDOWN_IN_PROGRESS.is_set():
                    logger.debug(f"Main event loop not available or not running for broadcast to {channel}")
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
                logger.warning("Default admin created: username='administrator' — change the default password immediately!")
        
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


def _build_cot_pong_xml() -> str:
    """Build a CoT t-x-c-t-r ping-ack XML string to reply to a server ping."""
    now = datetime.now(timezone.utc)
    stale = now + timedelta(seconds=30)
    fmt = lambda dt: dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<event version="2.0" uid="{_LPU5_COT_UID}" type="t-x-c-t-r" how="m-g"'
        f' time="{fmt(now)}" start="{fmt(now)}" stale="{fmt(stale)}">'
        '<point lat="0.0" lon="0.0" hae="0.0" ce="9999999.0" le="9999999.0"/>'
        '<detail/></event>'
    )


# Fixed UID used for LPU5's SA beacon so the TAK server recognises the gateway
# as a persistent entity across reconnects.
_LPU5_COT_UID = "LPU5-GW"
# UID prefix used by _forward_chat_to_atak when building GeoChat CoT events.
# Events whose UID starts with this prefix are LPU5-originated echo-backs.
_LPU5_GEOCHAT_UID_PREFIX = "GeoChat.LPU5-"

# Deduplication cache for incoming ATAK GeoChat (b-t-f) events.
# Maps event UID → ingestion timestamp.  Entries older than
# _GEOCHAT_SEEN_TTL are lazily purged.  This prevents the same chat
# message from being saved and relayed multiple times when it arrives
# via several paths (TAK server echo, multicast, TCP relay).
_GEOCHAT_SEEN_UIDS: Dict[str, float] = {}
_GEOCHAT_SEEN_UIDS_LOCK = threading.Lock()
_GEOCHAT_SEEN_TTL = 300  # seconds – keep UIDs for 5 minutes

# Content-based deduplication for GeoChat messages.
# Maps hash(sender + text) → ingestion timestamp.  Catches duplicate
# messages that arrive with *different* UIDs (e.g. TAK server re-wraps
# the event, or ATAK resends after receiving its own echo).  Uses a
# shorter TTL than the UID cache because legitimate repeated messages
# (same sender, same text) should still be accepted after the window.
_GEOCHAT_SEEN_CONTENT: Dict[str, float] = {}
_GEOCHAT_SEEN_CONTENT_LOCK = threading.Lock()
_GEOCHAT_SEEN_CONTENT_TTL = 60  # seconds – 1-minute content dedup window


def _get_cot_listener_endpoint() -> str:
    """Return the CoT TCP endpoint string for the LPU5 SA beacon's <contact> element.

    Reads ``cot_listener_host`` and ``cot_listener_tcp_port`` from config.json.
    When a host is configured the endpoint is formatted as ``<host>:<port>:tcp``
    which tells ATAK/WinTAK where to connect in order to send CoT data directly
    to LPU5 (making LPU5-GW appear as a proper Contact, not just a map marker).

    Returns an empty string when no host is configured so the <contact> element
    is emitted without an endpoint attribute (backwards-compatible behaviour).
    """
    try:
        cfg = load_json("config") or {}
        host = str(cfg.get("cot_listener_host", "")).strip()
        if not host:
            return ""
        port = int(cfg.get("cot_listener_tcp_port", 8088))
        return f"{host}:{port}:tcp"
    except Exception:
        return ""


def _build_lpu5_sa_xml() -> str:
    """Build a CoT SA (Situational Awareness) beacon that identifies LPU5 to the TAK server.

    Sending this event immediately after connecting (and optionally after auth)
    announces LPU5 as a named entity on the TAK network.  Without this
    announcement many TAK server implementations (including ATAK in server mode)
    treat the sender as anonymous and route subsequent CoT events only to
    specific users rather than broadcasting to all connected clients
    (including WinTAK users).

    When ``cot_listener_host`` is configured in config.json the <contact>
    element will include an ``endpoint`` attribute so that ATAK/WinTAK displays
    LPU5-GW as a reachable Contact (rather than just a passive map entity) and
    allows operators to send CoT data from ATAK directly to LPU5.
    """
    from datetime import timedelta
    now = datetime.now(timezone.utc)
    stale = now + timedelta(minutes=5)
    fmt = lambda dt: dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    endpoint = _get_cot_listener_endpoint()
    endpoint_attr = f' endpoint="{endpoint}"' if endpoint else ""
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<event version="2.0" uid="{_LPU5_COT_UID}" type="a-f-G-U-C" how="m-g"'
        f' time="{fmt(now)}" start="{fmt(now)}" stale="{fmt(stale)}">'
        '<point lat="0.0" lon="0.0" hae="0.0" ce="9999999.0" le="9999999.0"/>'
        '<detail>'
        f'<contact callsign="{_LPU5_COT_UID}"{endpoint_attr}/>'
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
    # Capture outgoing CoT for the data monitor (best-effort)
    _cot_monitor_record(cot_xml, ">>>", "forward_to_tak")
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


def _forward_cot_to_tcp_clients(cot_xml: str) -> int:
    """
    Push a CoT XML string to all WinTAK/ATAK clients that are directly connected
    via the TCP CoT listener on port 8088 (bidirectional data exchange).

    Returns the number of TCP clients successfully reached.
    """
    with _cot_listener_lock:
        svc = _cot_listener_service
    if svc is None:
        return 0
    return svc.send_to_clients(cot_xml)


def _forward_cot_to_itak_bridge(cot_xml: str) -> int:
    """
    Push a CoT XML string to all iTAK clients connected via the local
    SSL bridge on port 8089 (bidirectional data exchange).

    Returns the number of iTAK clients successfully reached.
    """
    with _itak_bridge_lock:
        svc = _itak_bridge_service
    if svc is None:
        return 0
    return svc.send_to_clients(cot_xml)


def _build_atak_geochat_xml(sender_uid: str, sender_callsign: str, text: str) -> str:
    """
    Build an ATAK-compatible GeoChat CoT XML string (type b-t-f).

    This allows LPU5 chat messages to appear in the ATAK GeoChat window on all
    connected TAK clients.  The event is sent to the "All Chat Rooms" chatroom
    so every ATAK user sees it regardless of their active chatroom.
    """
    _ATAK_CHATROOM = "All Chat Rooms"
    now = datetime.now(timezone.utc)
    stale = now + timedelta(minutes=10)
    fmt = lambda dt: dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    msg_id = str(uuid.uuid4())
    # Sanitise sender_uid: strip characters that could break XML attributes
    safe_sender_uid = re.sub(r'[<>&"\']', '_', sender_uid)
    event_uid = f"GeoChat.{safe_sender_uid}.{_ATAK_CHATROOM}.{msg_id}"
    time_str = fmt(now)
    safe_text = _sax_utils.escape(text)
    safe_callsign = _sax_utils.escape(sender_callsign)
    safe_uid_attr = _sax_utils.escape(safe_sender_uid)
    safe_event_uid = _sax_utils.escape(event_uid)
    safe_msg_id = _sax_utils.escape(msg_id)
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<event version="2.0" uid="{safe_event_uid}" type="b-t-f" how="h-g-i-g-o"'
        f' time="{time_str}" start="{time_str}" stale="{fmt(stale)}">'
        '<point lat="0.0" lon="0.0" hae="0.0" ce="9999999.0" le="9999999.0"/>'
        '<detail>'
        f'<__chat parent="RootContactGroup" groupOwner="false" messageId="{safe_msg_id}"'
        f' chatroom="{_ATAK_CHATROOM}" id="{_ATAK_CHATROOM}" senderCallsign="{safe_callsign}">'
        f'<chatgrp uid0="{safe_uid_attr}" uid1="{_ATAK_CHATROOM}" id="{_ATAK_CHATROOM}"/>'
        '</__chat>'
        f'<remarks source="BAO.F.ATAK.{safe_uid_attr}" to="{_ATAK_CHATROOM}" time="{time_str}">{safe_text}</remarks>'
        '</detail>'
        '</event>'
    )


def _forward_chat_to_atak(sender: str, text: str) -> bool:
    """
    Forward a LPU5 chat message to ATAK/TAK clients as a GeoChat CoT event (type b-t-f).

    Uses *sender* as the callsign so ATAK users see who sent the message.
    The CoT event is delivered via all available paths independently:
    1. Configured TAK server (TCP/UDP/SSL)
    2. Directly connected TCP clients on port 8088
    3. SA Multicast group (239.2.3.1:6969) for LAN ATAK devices

    Returns True if at least one delivery path succeeded.
    """
    try:
        cot_xml = _build_atak_geochat_xml(
            sender_uid=f"LPU5-{sender}",
            sender_callsign=sender,
            text=text,
        )
        delivered = False
        # 1. Forward to configured TAK server
        if forward_cot_to_tak(cot_xml):
            delivered = True
        # 2. Push to directly connected TCP clients (port 8088)
        tcp_count = _forward_cot_to_tcp_clients(cot_xml)
        if tcp_count > 0:
            delivered = True
        # 3. Send via SA Multicast for LAN ATAK devices
        if _forward_cot_multicast(cot_xml):
            delivered = True
        # 4. Push to iTAK bridge clients (SSL port 8089)
        if _forward_cot_to_itak_bridge(cot_xml) > 0:
            delivered = True
        if delivered:
            logger.info("Chat→ATAK bridge: %s: %s", sender, text[:80])
        return delivered
    except Exception as _chat_err:
        logger.warning("Chat→ATAK bridge error: %s", _chat_err)
        return False


def _ingest_atak_geochat(root) -> bool:
    """
    Parse an ATAK GeoChat CoT element (type b-t-f) and save it as a LPU5 chat message.

    *root* must be the already-parsed ``<event>`` XML element.
    Silently skips events without a recognisable message body.
    Echo-backs of LPU5-originated GeoChat messages (UID prefix ``GeoChat.LPU5-``)
    are detected and skipped to prevent duplicate entries – the original message
    was already persisted by :func:`send_chat_message`.

    Returns ``True`` when the message was actually ingested (new), ``False`` when
    it was skipped (echo-back, duplicate, or missing data).  Callers use the
    return value to decide whether to relay the event to other connections.
    """
    try:
        event_uid = root.get("uid", "")

        # --- Echo-back detection ---
        # When LPU5 sends a chat message, _forward_chat_to_atak builds the CoT
        # with sender_uid="LPU5-<username>", producing a UID of the form
        # "GeoChat.LPU5-<username>.<chatroom>.<uuid>".  If the TAK server or
        # a multicast/TCP echo sends this back, we must not save it again
        # because the original message is already in the DB.
        if event_uid.startswith(_LPU5_GEOCHAT_UID_PREFIX):
            logger.debug("ATAK GeoChat: skipping LPU5 echo-back (uid=%s)", event_uid)
            return False

        # --- UID-based deduplication ---
        # The same GeoChat event can arrive via multiple paths (TAK server
        # echo, multicast, TCP relay).  We track recently-seen UIDs so we
        # ingest and relay each event only once.
        # Events without a UID are let through (they will likely fail later
        # validation anyway) to avoid polluting the cache with empty keys.
        if event_uid:
            now_ts = time.time()
            with _GEOCHAT_SEEN_UIDS_LOCK:
                if event_uid in _GEOCHAT_SEEN_UIDS:
                    logger.debug("ATAK GeoChat: duplicate uid=%s, skipping", event_uid)
                    return False
                _GEOCHAT_SEEN_UIDS[event_uid] = now_ts
                # Lazy purge – only when the cache has grown large enough to
                # justify the iteration cost.
                if len(_GEOCHAT_SEEN_UIDS) > 200:
                    stale = [k for k, v in _GEOCHAT_SEEN_UIDS.items() if now_ts - v > _GEOCHAT_SEEN_TTL]
                    for k in stale:
                        del _GEOCHAT_SEEN_UIDS[k]

        detail = root.find("detail")
        if detail is None:
            return False
        # Extract sender callsign from __chat element
        chat_elem = detail.find("__chat")
        sender_callsign = None
        if chat_elem is not None:
            sender_callsign = chat_elem.get("senderCallsign")
        if not sender_callsign:
            # Fall back to the event UID as sender identifier
            sender_callsign = root.get("uid", "ATAK")
        # Extract message text from <remarks>
        remarks_elem = detail.find("remarks")
        if remarks_elem is None or not (remarks_elem.text or "").strip():
            logger.debug("ATAK GeoChat: no remarks text in event uid=%s, skipping", event_uid)
            return False
        text = remarks_elem.text.strip()

        # --- Content-based deduplication ---
        # Catches the same message arriving with *different* UIDs (e.g.
        # TAK server re-wraps, ATAK resends after echo, or multicast
        # delivers a second copy).  The key is sender+text; if the
        # identical text from the same sender was already ingested
        # within the last _GEOCHAT_SEEN_CONTENT_TTL seconds, skip it.
        content_key = hashlib.sha256(
            f"{sender_callsign}\n{text}".encode("utf-8", errors="replace")
        ).hexdigest()
        now_content = time.time()
        with _GEOCHAT_SEEN_CONTENT_LOCK:
            prev_ts = _GEOCHAT_SEEN_CONTENT.get(content_key)
            if prev_ts is not None and (now_content - prev_ts) < _GEOCHAT_SEEN_CONTENT_TTL:
                logger.debug(
                    "ATAK GeoChat: duplicate content from %s (uid=%s), "
                    "already ingested %.1fs ago – skipping",
                    sender_callsign, event_uid, now_content - prev_ts,
                )
                return False
            _GEOCHAT_SEEN_CONTENT[content_key] = now_content
            # Lazy purge stale entries
            if len(_GEOCHAT_SEEN_CONTENT) > 200:
                stale_c = [
                    k for k, v in _GEOCHAT_SEEN_CONTENT.items()
                    if now_content - v > _GEOCHAT_SEEN_CONTENT_TTL
                ]
                for k in stale_c:
                    del _GEOCHAT_SEEN_CONTENT[k]

        db = SessionLocal()
        try:
            _ensure_default_channels(db)
            new_msg = ChatMessage(
                channel=MESH_CHAT_CHANNEL,
                sender=sender_callsign,
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
                    _MAIN_EVENT_LOOP,
                )
            logger.info("ATAK→Chat bridge: %s: %s", sender_callsign, text[:80])
            return True
        finally:
            db.close()
    except Exception as _geo_err:
        logger.warning("ATAK GeoChat ingest error: %s", _geo_err)
        return False


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

# Thread-safe in-memory cache of deleted marker IDs for fast lookups.
# The authoritative source of truth is the `deleted_markers` DB table which
# survives server restarts.  This cache is populated at startup from the DB
# and updated on every new deletion so hot-path checks avoid a DB round-trip.
_deleted_marker_ids: set = set()
_deleted_marker_ids_lock = threading.Lock()


def _load_deleted_markers_from_db() -> None:
    """Populate the in-memory deleted-marker cache from the database on startup."""
    try:
        with SessionLocal() as db:
            rows = db.query(DeletedMarker.marker_id).all()
            with _deleted_marker_ids_lock:
                for (mid,) in rows:
                    _deleted_marker_ids.add(mid)
            logger.info("Loaded %d permanently deleted marker IDs from DB", len(rows))
    except Exception as _e:
        logger.warning("Could not load deleted markers from DB: %s", _e)


def _record_deleted_marker(marker_id: str, deleted_by: str = "system") -> None:
    """
    Permanently record a marker ID as deleted.

    Writes to both the in-memory fast-path cache and the `deleted_markers`
    database table so the suppression survives server restarts.  Also evicts
    the marker from the CoT dedup caches so a future CoT event with the same
    UID is immediately checked against the deleted-markers table rather than
    silently passing through the dedup filter.
    """
    with _deleted_marker_ids_lock:
        _deleted_marker_ids.add(marker_id)
    # Persist to DB (upsert — safe to call multiple times for the same ID)
    try:
        with SessionLocal() as db:
            existing = db.query(DeletedMarker).filter(DeletedMarker.marker_id == marker_id).first()
            if not existing:
                db.add(DeletedMarker(marker_id=marker_id, deleted_by=deleted_by))
                db.commit()
    except Exception as _e:
        logger.warning("Could not persist deleted marker %s to DB: %s", marker_id, _e)
    # Evict from CoT dedup caches so re-checks are forced on the next incoming event.
    with _TAK_INCOMING_CACHE_LOCK:
        _TAK_INCOMING_CACHE.pop(marker_id, None)
    with _TAK_FORWARD_CACHE_LOCK:
        _TAK_FORWARD_CACHE.pop(marker_id, None)


def _is_deleted_marker(marker_id: str) -> bool:
    """
    Return True if the marker was explicitly deleted in LPU5.

    Checks the in-memory cache first (fast path); falls back to the database
    if the ID is not cached (e.g. right after a startup before the background
    load completes).
    """
    with _deleted_marker_ids_lock:
        if marker_id in _deleted_marker_ids:
            return True
    # Fallback: check DB directly (handles the brief window between startup
    # and the async cache warm-up, and also acts as a safety net).
    try:
        with SessionLocal() as db:
            row = db.query(DeletedMarker).filter(DeletedMarker.marker_id == marker_id).first()
            if row:
                with _deleted_marker_ids_lock:
                    _deleted_marker_ids.add(marker_id)
                return True
    except Exception:
        pass
    return False


# Deduplication cache for incoming CoT events.
# Maps uid → (lat, lng, callsign, lpu5_type) of the last processed state.
# Events whose state has not changed are silently dropped to avoid redundant
# DB writes, WebSocket broadcasts, and log spam.
_TAK_INCOMING_CACHE: Dict[str, tuple] = {}
_TAK_INCOMING_CACHE_LOCK = threading.Lock()

# Deduplication cache for the outgoing periodic TAK sync.
# Maps marker_id → (lat, lng, name, type) of the state last forwarded to TAK.
# Only changed markers are re-sent during each sync cycle.
_TAK_FORWARD_CACHE: Dict[str, tuple] = {}
_TAK_FORWARD_CACHE_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Backwards-compatible alias used in a few existing call sites.
# ---------------------------------------------------------------------------
def _is_recently_deleted_marker(marker_id: str) -> bool:
    """Alias for _is_deleted_marker (deletion is now permanent, not time-bounded)."""
    return _is_deleted_marker(marker_id)


def _process_incoming_cot(cot_xml: str) -> None:
    """Parse an incoming CoT XML event, upsert a MapMarker, and broadcast to WebSocket clients."""
    # Capture for the CoT data monitor (best-effort, never blocks the main flow)
    _cot_monitor_record(cot_xml, "<<<", "tak_server")
    import xml.etree.ElementTree as _ET
    try:
        root = _ET.fromstring(cot_xml)
        if root.tag != "event":
            return

        uid = root.get("uid")
        if not uid:
            return
        event_type = root.get("type", "")

        # Skip echo-backs of LPU5-originated markers so ATAK does not corrupt
        # or duplicate them:
        #   • "GPS-<username>" UIDs are own GPS position markers forwarded to
        #     ATAK; filtering here prevents ATAK's echo-back from overwriting the
        #     gps_position type and from creating a duplicate tak_maker overlay.
        #   • _LPU5_COT_UID ("LPU5-GW") is the LPU5 gateway SA beacon that
        #     some TAK servers reflect back; ingesting it would create a spurious
        #     map marker at (0, 0).
        #   • "mesh-<node_id>" UIDs are processed below with type "node" and are
        #     guarded against echo-back corruption at the upsert stage (line ~1640).
        if uid.startswith("GPS-") or uid == _LPU5_COT_UID:
            logger.debug("CoT: skipping LPU5 echo-back for UID: %s", uid)
            with _TAK_RECEIVER_STATS_LOCK:
                _TAK_RECEIVER_STATS["packets_received"] += 1
            return

        # Process unit/marker types, ATAK drawings, and GeoChat messages;
        # skip ping-acks and other non-tactical system types.
        relevant_prefixes = ("a-f", "a-h", "a-n", "a-u", "a-p", "b-m-p", "b-t-f", "u-d", "b-a")
        if not any(event_type.startswith(p) for p in relevant_prefixes):
            return

        # --- ATAK GeoChat (b-t-f) → LPU5 chat bridge ---
        if event_type.startswith("b-t-f"):
            is_new = _ingest_atak_geochat(root)
            # Only relay when the message was genuinely new.  Duplicates
            # (same UID arriving via TAK-server echo, multicast, or TCP
            # relay) are suppressed to prevent an infinite relay loop.
            if is_new:
                _forward_cot_to_tcp_clients(cot_xml)
                _forward_cot_multicast(cot_xml)
                _forward_cot_to_itak_bridge(cot_xml)
            with _TAK_RECEIVER_STATS_LOCK:
                _TAK_RECEIVER_STATS["packets_received"] += 1
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

        # Extract callsign and team from detail/contact/__group
        detail = root.find("detail")
        callsign = uid
        team_name = None
        if detail is not None:
            contact = detail.find("contact")
            if contact is not None:
                callsign = contact.get("callsign") or callsign
            grp = detail.find("__group")
            if grp is not None:
                team_name = grp.get("name") or None

        # Detect incoming CoT source type before the CoT-type→LPU5-type mapping
        # so that ATAK Meshtastic nodes and ATAK SA/GPS positions receive the
        # correct LPU5 icon instead of the generic blue rectangle ("friendly").
        how = root.get("how", "m-g")
        # Presence of <meshtastic> in <detail> is the canonical indicator that
        # the event was forwarded by an ATAK Meshtastic plugin.  Guard with the
        # module-availability flag so the fallback path doesn't raise NameError.
        if AUTONOMOUS_MODULES_AVAILABLE:
            _has_mesh_detail = CoTProtocolHandler.detail_has_meshtastic(detail)
        else:
            # Inline fallback when cot_protocol is unavailable: check directly.
            _has_mesh_detail = (detail is not None and detail.find("meshtastic") is not None)

        # Extract shortName from <meshtastic shortName="..."> for Meshtastic markers.
        _mesh_short_name = None
        if _has_mesh_detail and detail is not None:
            _mesh_el = detail.find("meshtastic")
            if _mesh_el is not None:
                _mesh_short_name = _mesh_el.get("shortName") or None

        # Map CoT type to LPU5 internal type
        if AUTONOMOUS_MODULES_AVAILABLE:
            lpu5_type = CoTProtocolHandler.cot_type_to_lpu5(event_type)
        else:
            # Meshtastic equipment types (a-f-G-E...) map directly to
            # meshtastic_node so Meshtastic nodes are never shown as rectangles.
            if event_type.startswith("a-f-G-E"):
                lpu5_type = "meshtastic_node"
            elif event_type.startswith("a-f"):
                lpu5_type = "friendly"
            elif event_type.startswith("a-h"):
                lpu5_type = "hostile"
            elif event_type.startswith("a-n"):
                lpu5_type = "neutral"
            elif event_type.startswith("a-u"):
                lpu5_type = "unknown"
            elif event_type == "b-m-p-s-m":
                lpu5_type = "hostile"
            elif event_type == "u-d-r":
                lpu5_type = "friendly"
            else:
                lpu5_type = "hostile"

        # For spot-map markers the CoT type is the same for all LPU5 shapes.
        # When the callsign matches a known LPU5 shape name use it directly so
        # that ATAK-placed markers labelled "neutral" or "unknown" are rendered
        # with the correct icon in the LPU5 web UI.
        if AUTONOMOUS_MODULES_AVAILABLE and event_type == "b-m-p-s-m" and callsign:
            callsign_lower = callsign.lower()
            if callsign_lower in CoTProtocolHandler.LPU5_TO_COT_TYPE:
                lpu5_type = callsign_lower

        # Override with more specific types for ATAK-sourced events.
        # Meshtastic SA beacons forwarded by an ATAK Meshtastic plugin carry a
        # <meshtastic> element in their <detail> block.  These are checked first
        # because ATAK Meshtastic SA beacons use how="h-g-*" just like regular
        # ATAK GPS SA beacons; the <meshtastic> element is the authoritative
        # signal that this is a Meshtastic node, not a human ATAK user.
        #   • <meshtastic> in detail  → Meshtastic node forwarded by ATAK plugin
        #   • how starts with "h-g" (GPS-derived) → tak_maker (ATAK user SA
        #     beacon; LPU5's own GPS positions use UIDs "GPS-*" and are filtered
        #     above)
        #   • All other friendly CoT events (h-e, h-t, m-g, etc.) →
        #     meshtastic_node so relayed Meshtastic nodes render with the
        #     correct icon.
        if _has_mesh_detail or lpu5_type == "meshtastic_node":
            lpu5_type = "meshtastic_node"
        elif lpu5_type == "friendly" and how.startswith("h-g"):
            lpu5_type = "tak_maker"
        elif lpu5_type == "friendly":
            lpu5_type = "meshtastic_node"
        else:
            # All CoT events originate from ATAK/WinTAK. Remap the four basic
            # shape types to their CBT variants so ATAK-sourced markers are
            # visually distinguished from natively created LPU5 markers.
            if AUTONOMOUS_MODULES_AVAILABLE:
                lpu5_type = CoTProtocolHandler.ATAK_TO_CBT_TYPE.get(lpu5_type, lpu5_type)
            else:
                lpu5_type = {
                    "hostile":  "cbt_hostile",
                    "friendly": "cbt_friendly",
                    "neutral":  "cbt_neutral",
                    "unknown":  "cbt_unknown",
                }.get(lpu5_type, lpu5_type)

        # "mesh-<node_id>" UIDs are Meshtastic nodes imported back via
        # ATAK/WinTAK SA/COT import.  Always assign type "meshtastic_node" so
        # they render with the Meshtastic blue-circle icon instead of a generic
        # ground marker.
        if uid.startswith("mesh-"):
            lpu5_type = "meshtastic_node"

        # Deduplication: skip identical events to avoid redundant DB writes,
        # WebSocket broadcasts, and log spam when the TAK server re-sends the
        # same position in rapid succession.
        _incoming_key = (lat, lng, callsign, lpu5_type)
        with _TAK_INCOMING_CACHE_LOCK:
            if _TAK_INCOMING_CACHE.get(uid) == _incoming_key:
                # State unchanged – count the packet but skip all side-effects.
                with _TAK_RECEIVER_STATS_LOCK:
                    _TAK_RECEIVER_STATS["packets_received"] += 1
                return
            _TAK_INCOMING_CACHE[uid] = _incoming_key

        # Upsert MapMarker
        db = SessionLocal()
        try:
            marker = db.query(MapMarker).filter(MapMarker.id == uid).first()

            # Resolve the effective LPU5 type, potentially preserving an existing
            # Meshtastic classification when the incoming CoT lacks a
            # <meshtastic> element.  ATAK may strip custom detail elements when
            # redistributing CoT events, causing a correctly-identified Meshtastic
            # node to revert to cbt_friendly on subsequent position updates.
            # All Meshtastic-originated types (meshtastic_node, node, gateway)
            # must be preserved so the correct icon is rendered.
            _MESHTASTIC_DB_TYPES = {"meshtastic_node", "node", "gateway"}
            effective_type = lpu5_type
            if (marker
                    and marker.type in _MESHTASTIC_DB_TYPES
                    and not _has_mesh_detail
                    and effective_type not in _MESHTASTIC_DB_TYPES):
                effective_type = marker.type

            if marker:
                if uid.startswith("mesh-") or marker.created_by not in _TAK_INGEST_SOURCES:
                    # ATAK is echoing back a marker that LPU5 (or Meshtastic) originated.
                    # Skip the update entirely to prevent the native LPU5 type from being
                    # overwritten with a CBT variant (e.g. "hostile" → "cbt_hostile").
                    return
                marker.lat = lat
                marker.lng = lng
                marker.name = callsign
                marker.type = effective_type
                new_data = dict(marker.data) if marker.data else {}
                new_data["cot_type"] = event_type
                if team_name:
                    new_data["team"] = team_name
                if _mesh_short_name:
                    new_data["shortName"] = _mesh_short_name[:4]
                marker.data = new_data
                flag_modified(marker, "data")
            else:
                if _is_recently_deleted_marker(uid):
                    # ATAK is echoing back a marker that was just deleted in LPU5.
                    # Suppress recreation so the deletion takes effect immediately.
                    logger.debug("CoT: suppressing recreation of recently deleted marker: %s", uid)
                    return
                initial_data = {"cot_type": event_type}
                if team_name:
                    initial_data["team"] = team_name
                if _mesh_short_name:
                    initial_data["shortName"] = _mesh_short_name[:4]
                marker = MapMarker(
                    id=uid,
                    name=callsign,
                    lat=lat,
                    lng=lng,
                    type=effective_type,
                    created_by="tak_server",
                    data=initial_data,
                )
                db.add(marker)
            db.commit()

            # Compute shortName and symbolLink for the WebSocket broadcast.
            _MESH_TYPES_SET = {"meshtastic_node", "node", "gateway", "gps_position"}
            _ws_short_name = None
            if effective_type in _MESH_TYPES_SET:
                raw_sn = _mesh_short_name or (callsign[:4] if callsign else None)
                _ws_short_name = raw_sn[:4] if raw_sn else None
            _ws_symbol_link = (
                CoTProtocolHandler.get_symbol_link(effective_type)
                if AUTONOMOUS_MODULES_AVAILABLE else None
            )

            # Broadcast to WebSocket clients
            broadcast_websocket_update("markers", "tak_maker_update", {
                "id": uid,
                "name": callsign,
                "callsign": callsign,
                "lat": lat,
                "lng": lng,
                "type": effective_type,
                "cot_type": event_type,
                "team": team_name,
                "created_by": "tak_server",
                "shortName": _ws_short_name,
                "symbolLink": _ws_symbol_link,
            })
            # Relay to directly connected TCP clients (WinTAK/ATAK on port
            # 8088) and SA Multicast so that markers received from the TAK
            # server are also visible on locally connected TAK devices that
            # are not themselves connected to the remote TAK server.
            try:
                _forward_cot_to_tcp_clients(cot_xml)
                _forward_cot_multicast(cot_xml)
                _forward_cot_to_itak_bridge(cot_xml)
            except Exception as _relay_err:
                logger.debug("CoT relay to local clients failed: %s", _relay_err)
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

                # Use a short receive timeout so the SA beacon refresh timer
                # and the stop-event check fire promptly even when the server
                # sends no data.
                sock.settimeout(5.0)

                # Receive loop
                buf = b""
                sa_refresh_interval = 25  # seconds between SA beacon refreshes
                last_sa_time = time.time()
                while not _TAK_RECEIVER_STOP.is_set():
                    # Periodically re-send the SA beacon so the LPU5 gateway
                    # presence on the TAK server never goes stale (stale = 5 min
                    # but we refresh every 25 s to stay well ahead of the deadline
                    # and to keep the connection alive on servers that use the SA
                    # interval to detect dead clients).
                    now_ts = time.time()
                    if now_ts - last_sa_time >= sa_refresh_interval:
                        try:
                            with _TAK_SEND_LOCK:
                                sock.sendall(_build_lpu5_sa_xml().encode("utf-8"))
                            last_sa_time = now_ts
                            logger.debug("TAK: refreshed SA beacon as %s", _LPU5_COT_UID)
                        except Exception as _sa_err:
                            logger.warning("TAK SA beacon refresh failed: %s", _sa_err)
                            break
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
                            pkt_str = packet.decode("utf-8", errors="ignore")
                            # Respond to server pings (t-x-c-t) with a ping-ack
                            # (t-x-c-t-r).  TAK servers close connections whose
                            # clients never reply, which would silently break
                            # bidirectional data exchange.
                            # The space before 'type=' anchors the match to an XML
                            # attribute, reducing false positives from inner text.
                            if (' type="t-x-c-t"' in pkt_str or " type='t-x-c-t'" in pkt_str) and \
                                    "t-x-c-t-r" not in pkt_str:
                                try:
                                    with _TAK_SEND_LOCK:
                                        sock.sendall(_build_cot_pong_xml().encode("utf-8"))
                                    logger.debug("TAK: sent ping-ack (t-x-c-t-r)")
                                except Exception as _pong_err:
                                    logger.warning("TAK ping-ack send failed: %s", _pong_err)
                                    break
                            else:
                                _process_incoming_cot(pkt_str)
                    except socket.timeout:
                        continue  # No data yet; check stop event and SA refresh timer
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
                # Deduplication: only forward markers whose state has changed
                # since the last successful sync to avoid redundant TAK traffic.
                _fwd_key = (marker.lat, marker.lng, marker.name, marker.type)
                with _TAK_FORWARD_CACHE_LOCK:
                    if _TAK_FORWARD_CACHE.get(marker.id) == _fwd_key:
                        skipped += 1
                        continue

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
                        with _TAK_FORWARD_CACHE_LOCK:
                            _TAK_FORWARD_CACHE[marker.id] = _fwd_key
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
# Meshtastic role values that indicate a node acts as a network gateway/router.
_MESHTASTIC_GATEWAY_ROLES = {"ROUTER", "ROUTER_CLIENT"}

def _forward_meshtastic_node_to_tak(node_id: str, name: str, lat: float, lng: float,
                                    is_gateway: bool = False) -> bool:
    """
    Convert a single Meshtastic node position to a CoT event and forward it to
    the configured TAK server.  Returns True on successful forward.
    Nodes without GPS are forwarded at coordinates (0.0, 0.0) so TAK still
    receives them and can distribute the node identity globally.
    Silently skips only when TAK forwarding is disabled.

    Every Meshtastic node — regardless of its role — is forwarded as CoT type
    ``a-f-G-E-S-U-M`` (Meshtastic equipment) with a ``<contact endpoint>``
    attribute pointing to the LPU5 CoT listener.  This causes ATAK/WinTAK to
    display each node as an individually reachable Meshtastic contact rather
    than clustering multiple person-PLI markers together.

    Args:
        node_id:    Meshtastic node identifier.
        name:       Human-readable node name / callsign.
        lat:        Latitude (decimal degrees, 0.0 if unavailable).
        lng:        Longitude (decimal degrees, 0.0 if unavailable).
        is_gateway: Retained for backwards-compatibility; no longer changes
                    the forwarded CoT type because all nodes now use the same
                    Meshtastic equipment type (``a-f-G-E-S-U-M``).
    """
    if not AUTONOMOUS_MODULES_AVAILABLE:
        return False
    try:
        # All Meshtastic nodes are forwarded as individual gateways so that
        # ATAK shows each node as its own independently reachable contact.
        marker_dict: Dict[str, Any] = {
            "id": f"mesh-{node_id}",
            "name": name,
            "callsign": name,
            "lat": lat,
            "lng": lng,
            "type": "gateway",
            "meshtastic_node": True,
            "node_id": node_id,
            "source": "meshtastic",
        }
        # Include the CoT listener endpoint for every node so ATAK displays
        # each Meshtastic node as an individually reachable contact.
        endpoint = _get_cot_listener_endpoint()
        if endpoint:
            marker_dict["contact_endpoint"] = endpoint
        cot_event = CoTProtocolHandler.marker_to_cot(marker_dict)
        if cot_event:
            cot_xml = cot_event.to_xml()
            _forward_cot_multicast(cot_xml)
            _forward_cot_to_itak_bridge(cot_xml)
            return forward_cot_to_tak(cot_xml)
    except Exception as _fwd_err:
        logger.debug("TAK forward for Meshtastic node %s failed: %s", node_id, _fwd_err)
    return False


def sync_meshtastic_nodes_to_map_markers_once():
    """
    One-shot sync: reads meshtastic_nodes from DB and upserts markers into map_markers table.
    Nodes without valid lat/lng will be assigned 0.0, 0.0 per requirement.

    All Meshtastic nodes are stored and forwarded to ATAK as CoT type
    ``a-f-G-E-S-U-M`` (Meshtastic equipment) with a ``<contact endpoint>``
    attribute so ATAK shows every node as an individually reachable Meshtastic
    contact rather than clustering multiple person-PLI markers together.
    ROUTER/ROUTER_CLIENT nodes are additionally marked with type ``"gateway"``
    in the LPU5 database for visual differentiation.
    """
    db = SessionLocal()
    try:
        nodes = db.query(MeshtasticNode).all()
        # Index existing markers created by meshtastic sync (all three
        # creation sources so we never create a duplicate for a node that
        # was first seen via the ingest_node endpoint).
        existing_markers = db.query(MapMarker).filter(MapMarker.created_by.in_(list(_MESHTASTIC_CREATED_BY))).all()

        by_unit = {str(m.data.get("unit_id") if isinstance(m.data, dict) else ""): m for m in existing_markers if m.data}

        created = 0
        updated = 0

        # Resolve the CoT listener endpoint once so it can be embedded in
        # every marker's data dict for the periodic broadcast worker.
        cot_endpoint = _get_cot_listener_endpoint()

        # Collect per-node gateway status while iterating so we can reuse it
        # in the TAK forwarding step without duplicating the detection logic.
        node_gateway_flags: Dict[str, bool] = {}

        for n in nodes:
            mesh = n.id
            name = n.long_name or n.short_name or mesh or "node"
            lat = n.lat if n.lat is not None else 0.0
            lng = n.lng if n.lng is not None else 0.0

            # Detect gateway nodes via the Meshtastic node role stored in raw_data.
            raw = n.raw_data if isinstance(n.raw_data, dict) else {}
            node_role = str(raw.get("user", {}).get("role", "") or "").upper()
            node_is_gateway = node_role in _MESHTASTIC_GATEWAY_ROLES
            node_gateway_flags[str(mesh)] = node_is_gateway
            marker_type = "gateway" if node_is_gateway else "node"

            # find existing: first by unit_id in data, then by stable mesh-<id> marker ID
            # (the latter covers CoT-ingested markers that lack a unit_id in their data).
            stable_mesh_id = f"mesh-{mesh}"
            marker = by_unit.get(str(mesh))
            if marker is None:
                marker = db.query(MapMarker).filter(MapMarker.id == stable_mesh_id).first()

            if marker:
                # update
                marker.lat = float(lat)
                marker.lng = float(lng)
                marker.name = name
                marker.type = marker_type
                marker_data = marker.data if isinstance(marker.data, dict) else {}
                marker_data["updated_at"] = datetime.now(timezone.utc).isoformat()
                marker_data["is_gateway"] = node_is_gateway
                # Persist the endpoint so the periodic broadcast worker includes
                # it when forwarding via SA Multicast / TCP CoT.
                if cot_endpoint:
                    marker_data["contact_endpoint"] = cot_endpoint
                marker.data = marker_data
                updated += 1
            else:
                # stable_mesh_id is already pre-computed above ("mesh-{mesh}").
                # Use it so the periodic broadcast worker sends CoT events with
                # the same UID as _forward_meshtastic_node_to_tak().  Without a
                # stable ID, every broadcast cycle would produce a different CoT
                # UID which causes ATAK/WinTAK to accumulate duplicate contacts.
                marker_data: Dict[str, Any] = {"unit_id": mesh, "is_gateway": node_is_gateway}
                if cot_endpoint:
                    marker_data["contact_endpoint"] = cot_endpoint
                new_marker = MapMarker(
                    id=stable_mesh_id,
                    lat=float(lat),
                    lng=float(lng),
                    name=name,
                    type=marker_type,
                    created_by="import_meshtastic",
                    created_at=datetime.now(timezone.utc),
                    data=marker_data
                )
                db.add(new_marker)
                created += 1

        db.commit()
        logger.info("sync_meshtastic_nodes_to_map_markers_once completed: created=%d updated=%d", created, updated)

        # Forward all nodes to the TAK server so TAK can distribute them globally.
        # Every node is forwarded as an individual Meshtastic contact (a-f-G-E-S-U-M)
        # with the LPU5 endpoint so ATAK shows each as a reachable contact.
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

def _meshtastic_sync_worker(interval_seconds: int = 60):
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

# State hash caches for change-detection in the periodic broadcast worker.
# Keyed by marker/overlay id → a tuple of the fields that trigger a re-broadcast.
# Only records whose state changed since the last cycle are included in the payload,
# which dramatically reduces WebSocket and SA-Multicast traffic when the map is
# largely static.
_BROADCAST_MARKER_HASH: Dict[str, tuple] = {}
_BROADCAST_OVERLAY_HASH: Dict[str, tuple] = {}

def _marker_broadcast_worker(interval_seconds: int = 60):
    """
    Periodic marker broadcast worker for real-time sync.

    Only markers/overlays whose state has changed since the last broadcast cycle are
    sent to WebSocket clients and the SA-Multicast / TCP-CoT channels.  A full-sync
    payload (all current markers) is still sent once when the worker first starts so
    that clients which connect while the server is idle receive the complete picture.
    """
    first_run = True
    while not _MARKER_BROADCAST_STOP_EVENT.is_set():
        db = SessionLocal()
        try:
            markers = db.query(MapMarker).all()

            # Build per-marker state tuples for change detection.
            changed_markers = []
            current_marker_ids: set = set()
            for m in markers:
                mid = str(m.id)
                current_marker_ids.add(mid)
                state = (m.lat, m.lng, m.name, m.type, m.color, m.icon, hash(str(m.data)))
                if first_run or _BROADCAST_MARKER_HASH.get(mid) != state:
                    _BROADCAST_MARKER_HASH[mid] = state
                    changed_markers.append(m)

            # Evict deleted markers from the hash cache.
            for stale_id in list(_BROADCAST_MARKER_HASH.keys()):
                if stale_id not in current_marker_ids:
                    del _BROADCAST_MARKER_HASH[stale_id]

            if first_run:
                # On first run broadcast the full state so new clients get everything.
                marker_list = [
                    {
                        "id": m.id, "lat": m.lat, "lng": m.lng, "name": m.name,
                        "type": m.type, "color": m.color, "icon": m.icon,
                        "created_by": m.created_by, "data": m.data,
                        "timestamp": m.created_at.isoformat() if m.created_at else datetime.now(timezone.utc).isoformat()
                    } for m in markers
                ]
                if marker_list:
                    broadcast_websocket_update("markers", "markers_sync", {"markers": marker_list, "sync_type": "initial"})
                    logger.debug("Initial broadcast: %s markers", len(marker_list))
            elif changed_markers:
                marker_list = [
                    {
                        "id": m.id, "lat": m.lat, "lng": m.lng, "name": m.name,
                        "type": m.type, "color": m.color, "icon": m.icon,
                        "created_by": m.created_by, "data": m.data,
                        "timestamp": m.created_at.isoformat() if m.created_at else datetime.now(timezone.utc).isoformat()
                    } for m in changed_markers
                ]
                broadcast_websocket_update("markers", "markers_sync", {"markers": marker_list, "sync_type": "periodic"})
                logger.debug("Periodic broadcast: %s/%s markers changed", len(changed_markers), len(markers))

            # Forward changed LPU5-originated markers via SA Multicast / TCP CoT.
            if AUTONOMOUS_MODULES_AVAILABLE and changed_markers:
                mcast_sent = 0
                tcp_sent = 0
                for m in changed_markers:
                    if m.created_by not in ("cot_ingest", "tak_server"):
                        try:
                            mdict = {
                                "id": m.id, "lat": m.lat, "lng": m.lng,
                                "name": m.name, "type": m.type,
                                "created_by": m.created_by,
                            }
                            if isinstance(m.data, dict):
                                for k, v in m.data.items():
                                    if k not in mdict:
                                        mdict[k] = v
                            cot_evt = CoTProtocolHandler.marker_to_cot(mdict)
                            if cot_evt:
                                cot_xml = cot_evt.to_xml()
                                if _forward_cot_multicast(cot_xml):
                                    mcast_sent += 1
                                if _forward_cot_to_tcp_clients(cot_xml):
                                    tcp_sent += 1
                                _forward_cot_to_itak_bridge(cot_xml)
                        except Exception as _mc_err:
                            logger.debug("SA Multicast send for marker %s failed: %s", m.id, _mc_err)
                if mcast_sent:
                    logger.debug("Sent %d markers via SA Multicast for periodic sync", mcast_sent)
                if tcp_sent:
                    logger.debug("Sent %d markers to TCP clients for periodic sync", tcp_sent)

            # Broadcast overlays — only changed ones after the first run.
            overlays = db.query(Overlay).all()
            changed_overlays = []
            current_overlay_ids: set = set()
            for o in overlays:
                oid = str(o.id)
                current_overlay_ids.add(oid)
                state = (o.name, hash(str(o.data)))
                if first_run or _BROADCAST_OVERLAY_HASH.get(oid) != state:
                    _BROADCAST_OVERLAY_HASH[oid] = state
                    changed_overlays.append(o)

            for stale_id in list(_BROADCAST_OVERLAY_HASH.keys()):
                if stale_id not in current_overlay_ids:
                    del _BROADCAST_OVERLAY_HASH[stale_id]

            if first_run:
                overlay_list = [
                    {
                        "id": o.id, "name": o.name, "data": o.data,
                        "created_by": o.created_by,
                        "timestamp": o.created_at.isoformat() if o.created_at else datetime.now(timezone.utc).isoformat()
                    } for o in overlays
                ]
                if overlay_list:
                    broadcast_websocket_update("overlays", "overlays_sync", {"overlays": overlay_list, "sync_type": "initial"})
                    logger.debug("Initial broadcast: %s overlays", len(overlay_list))
            elif changed_overlays:
                overlay_list = [
                    {
                        "id": o.id, "name": o.name, "data": o.data,
                        "created_by": o.created_by,
                        "timestamp": o.created_at.isoformat() if o.created_at else datetime.now(timezone.utc).isoformat()
                    } for o in changed_overlays
                ]
                broadcast_websocket_update("overlays", "overlays_sync", {"overlays": overlay_list, "sync_type": "periodic"})
                logger.debug("Periodic broadcast: %s/%s overlays changed", len(changed_overlays), len(overlays))

            first_run = False

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
        "language": (user.data or {}).get("language", "en"),
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
        "language": (user.data or {}).get("language", "en"),
        "tak_team": user.tak_team or "Cyan",
        "tak_role": user.tak_role or "Team Member",
        "tak_display_type": user.tak_display_type or "General Ground Unit",
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
            "tak_team": u.tak_team or "Cyan",
            "tak_role": u.tak_role or "Team Member",
            "tak_display_type": u.tak_display_type or "General Ground Unit",
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
    
    updatable_fields = ["email", "group_id", "is_active", "unit", "device", "rank", "fullname", "callsign",
                        "tak_team", "tak_role", "tak_display_type"]
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
            "status": "PENDING",
            "tak_team": data.get("tak_team") or "Cyan",
            "tak_role": data.get("tak_role") or "Team Member",
            "tak_display_type": data.get("tak_display_type") or "General Ground Unit",
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
        tak_team=reg_data.get("tak_team") or "Cyan",
        tak_role=reg_data.get("tak_role") or "Team Member",
        tak_display_type=reg_data.get("tak_display_type") or "General Ground Unit",
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
        # (those are rendered exclusively via /api/meshtastic/nodes → updateMeshtasticNodes).
        # Mesh nodes imported via ATAK/WinTAK CoT (type "node", created_by tak_server/
        # cot_ingest) are included here with their correct "node" type so that admin_map
        # and other API consumers can display them; the tactical web UI skips them in
        # loadMarkers() (isMeshtasticMarker) and renders them via updateMeshtasticNodes().
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
                "data": m.data,
                "shortName": (
                    (m.data or {}).get("shortName")
                    or (m.name[:4] if m.name else None)
                    if m.type in {"meshtastic_node", "node", "gateway", "gps_position"}
                    else None
                ),
                "symbolLink": (
                    CoTProtocolHandler.get_symbol_link(m.type)
                    if AUTONOMOUS_MODULES_AVAILABLE else None
                ),
            } for m in markers
            if not m.created_by or m.created_by not in _MESHTASTIC_CREATED_BY
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
                    tcp_ok = _forward_cot_to_tcp_clients(cot_xml)
                    if tcp_ok:
                        logger.debug("CoT TCP push on marker_created reached %d client(s): marker_id=%s", tcp_ok, new_marker.id)
                    _forward_cot_to_itak_bridge(cot_xml)
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

        # Keep data.label in sync with the authoritative name field so
        # that GET /api/map/symbols (which merges data into the response)
        # never returns a stale label after a rename.
        if "name" in data and isinstance(marker.data, dict):
            marker.data = {**marker.data, "label": marker.name}
            
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
                    tcp_ok = _forward_cot_to_tcp_clients(cot_xml)
                    if tcp_ok:
                        logger.debug("CoT TCP push on marker_updated reached %d client(s): marker_id=%s", tcp_ok, marker_id)
                    _forward_cot_to_itak_bridge(cot_xml)
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
        
        # Record the deletion permanently — suppresses ATAK echo-back from ever
        # recreating this marker, even after a server restart.
        _record_deleted_marker(marker_id, deleted_by=current_username)

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
                    tcp_ok = _forward_cot_to_tcp_clients(tombstone_xml)
                    if tcp_ok:
                        logger.debug("CoT TCP tombstone pushed to %d client(s) on marker_deleted: marker_id=%s", tcp_ok, marker_id)
                    _forward_cot_to_itak_bridge(tombstone_xml)
            except Exception as _fwd_err:
                logger.warning("CoT tombstone forward on marker_deleted failed: %s", _fwd_err)

        return {"status": "success"}
    except HTTPException:
        raise
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

    # Also include mesh nodes that were imported via ATAK/WinTAK SA/COT (type "node",
    # "meshtastic_node", or "gateway") and stored in map_markers by the CoT ingestion
    # pipeline.  These are identified by their "mesh-" UID prefix and CoT-ingest
    # created_by values.  Skip any node whose mesh_id already exists from the
    # primary meshtastic_nodes source to avoid duplicates.
    _COT_INGEST_SOURCES = {"tak_server", "cot_ingest"}
    _MESH_MARKER_TYPES = {"node", "meshtastic_node", "gateway"}
    existing_mesh_ids = {str(n.get("mesh_id") or n.get("id", "")) for n in nodes}
    db_mm = SessionLocal()
    try:
        cot_mesh_markers = db_mm.query(MapMarker).filter(
            MapMarker.created_by.in_(list(_COT_INGEST_SOURCES)),
            MapMarker.type.in_(list(_MESH_MARKER_TYPES)),
        ).all()
        for mm in cot_mesh_markers:
            # Derive the mesh node id from the marker id ("mesh-<node_id>")
            mesh_id = mm.id[5:] if mm.id.startswith("mesh-") else mm.id
            if mesh_id in existing_mesh_ids or mm.id in existing_mesh_ids:
                continue
            existing_mesh_ids.add(mesh_id)
            node_data = mm.data or {}
            short_name = node_data.get("shortName") or (mm.name[:2] if mm.name else "M")
            nodes.append({
                "id": mm.id,
                "mesh_id": mesh_id,
                "name": mm.name or mm.id,
                "longName": mm.name,
                "shortName": short_name,
                "lat": mm.lat if mm.lat is not None else 0.0,
                "lng": mm.lng if mm.lng is not None else 0.0,
                "type": mm.type,
                "hardware_model": node_data.get("hardware_model"),
                "battery": node_data.get("battery"),
                "is_online": node_data.get("is_online"),
                "created_by": mm.created_by,
            })
    except Exception as e:
        logger.warning("CoT mesh marker load failed: %s", e)
    finally:
        db_mm.close()

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

# Global iTAK CoT bridge service (SSL TCP on 127.0.0.1:8089)
_itak_bridge_service = None
_itak_bridge_lock = threading.Lock()

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

def _sanitize_for_json(obj, _depth=0):
    """Recursively convert an object to a JSON-serializable structure.

    Handles protobuf Message objects (which appear as google._upb._message.*
    instances and cannot be serialized directly by FastAPI / jsonable_encoder)
    by converting them to plain dicts via MessageToDict when available, or
    by falling back to iterating their fields.
    """
    _MAX_DEPTH = 20
    if _depth > _MAX_DEPTH:
        return None
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, dict):
        return {str(k): _sanitize_for_json(v, _depth + 1) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize_for_json(item, _depth + 1) for item in obj]
    # Protobuf Message: has a DESCRIPTOR attribute and ListFields() method
    if hasattr(obj, "DESCRIPTOR") and hasattr(obj, "ListFields"):
        try:
            from google.protobuf.json_format import MessageToDict
            return _sanitize_for_json(
                MessageToDict(
                    obj,
                    preserving_proto_field_name=True,
                    including_default_value_fields=False,
                ),
                _depth + 1,
            )
        except Exception:
            pass
        # Fallback: iterate declared fields
        try:
            result = {}
            for field, value in obj.ListFields():
                result[field.name] = _sanitize_for_json(value, _depth + 1)
            return result
        except Exception:
            pass
    # Generic object with __dict__
    if hasattr(obj, "__dict__"):
        try:
            return _sanitize_for_json(vars(obj), _depth + 1)
        except Exception:
            pass
    # to_dict() method (e.g. some meshtastic wrappers)
    if hasattr(obj, "to_dict") and callable(obj.to_dict):
        try:
            return _sanitize_for_json(obj.to_dict(), _depth + 1)
        except Exception:
            pass
    # Last resort: stringify
    return str(obj)


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
            if reused_persistent:
                logger.info(f"[Port:{port}] Using existing persistent connection (device already initialized)")
            else:
                logger.info(f"[Port:{port}] Interface connected successfully, waiting for device initialization...")
                # Give the device time to populate nodes (important for real devices)
                time.sleep(2)
            
            # Attempt to read nodes from different attributes / methods
            logger.info(f"[Port:{port}] Attempting to read nodes from device")
            nodes_obj = getattr(iface, "nodes", None) or {}
            logger.info(f"[Port:{port}] Got nodes object, type: {type(nodes_obj).__name__}, length: {len(nodes_obj) if hasattr(nodes_obj, '__len__') else 'N/A'}")
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
                        "raw": _sanitize_for_json(raw)
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
                marker_matched["name"] = existing_node.get("name") or ""
                marker_matched["timestamp"] = datetime.now().isoformat()
                marker_matched["created_by"] = marker_matched.get("created_by", "import_meshtastic")
                marker_matched["unit_id"] = mesh
            else:
                new_marker = {
                    "id": str(uuid.uuid4()),
                    "lat": float(existing_node["lat"]),
                    "lng": float(existing_node["lng"]),
                    "name": existing_node.get("name") or "",
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
             marker.type = "node"
             marker.timestamp = datetime.now(timezone.utc)
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

    # Read COT listener ports from config
    cfg = load_json("config") or {}
    cot_tcp_port = int(cfg.get("cot_listener_tcp_port", 8088))
    cot_udp_port = int(cfg.get("cot_listener_udp_port", 4242))
    
    return {
        "status": "ok",
        "local_ip": local_ip,
        "all_detected_ips": all_ips,
        "port": 8101,
        "protocol": protocol,
        "ssl": use_ssl,
        "cot_tcp_port": cot_tcp_port,
        "cot_udp_port": cot_udp_port,
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
            # Use caller-supplied base_url when available so the QR code
            # points to the domain / IP the admin chose in the UI.
            caller_base = (data.get("base_url") or "").strip().rstrip("/")
            if caller_base:
                qr_url = f"{caller_base}/qr/{token}"
            else:
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

@app.post("/api/qr/cot_mesh")
def api_qr_cot_mesh(data: dict = Body(...), request: Request = None, db: Session = Depends(get_db)):
    """Create a COT Mesh Login QR code.

    The QR encodes server connection details (host, port, SSL) plus a
    master_key that authorises COT-only access when no internet is
    available.  User credentials are NOT embedded – the user must enter
    them manually after scanning.
    """
    expires_days = int(data.get("expires_days", 365))
    label = data.get("label") or "cot_mesh_login"
    max_uses = int(data.get("max_uses", 999))

    local_ip, all_ips = get_local_ip()
    cert_file = os.path.join(base_path, "cert.pem")
    key_file = os.path.join(base_path, "key.pem")
    use_ssl = os.path.exists(cert_file) and os.path.exists(key_file)
    protocol = "https" if use_ssl else "http"
    server_port = 8101

    # Read COT listener ports from config
    cfg = load_json("config") or {}
    cot_tcp_port = int(cfg.get("cot_listener_tcp_port", 8088))
    cot_udp_port = int(cfg.get("cot_listener_udp_port", 4242))

    master_key = str(uuid.uuid4())
    qr_id = str(uuid.uuid4())
    expires_at_dt = datetime.now(timezone.utc) + timedelta(days=expires_days)

    # JSON payload that will be encoded into the QR image
    qr_payload = {
        "type": "cot_mesh_login",
        "master_key": master_key,
        "name": cfg.get("server_name") or "LPU5 Server",
        "host": local_ip,
        "port": server_port,
        "protocol": protocol,
        "ssl": use_ssl,
        "cot_tcp_port": cot_tcp_port,
        "cot_udp_port": cot_udp_port,
        "all_ips": all_ips,
    }

    png_b64 = None
    if qrcode:
        try:
            import json as _json
            qr_img = qrcode.make(_json.dumps(qr_payload))
            from io import BytesIO
            buf = BytesIO()
            qr_img.save(buf, format="PNG")
            png_b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        except Exception:
            logger.exception("COT Mesh QR PNG generation failed")

    new_qr = QRCode(
        id=qr_id,
        token=master_key,
        type="cot_mesh_login",
        created_by="system",
        expires_at=expires_at_dt,
        max_uses=max_uses,
        uses=0,
        allowed_ips=[],
        data={
            "label": label,
            "qr_payload": qr_payload,
            "png_base64": png_b64,
        },
    )
    db.add(new_qr)
    db.commit()

    log_audit(
        "create_cot_mesh_qr",
        "system",
        {"master_key": master_key, "created_by_ip": _get_client_ip(request) if request else None},
    )
    return {
        "status": "success",
        "qr": {
            "id": qr_id,
            "master_key": master_key,
            "label": label,
            "max_uses": max_uses,
            "uses": 0,
            "payload": qr_payload,
        },
        "png_base64": png_b64,
    }


@app.get("/api/qr/join")
def api_qr_join_get():
    """Retrieve the currently stored persistent Join QR code (IP + Port only).

    The Join QR code is stored inside config.json under the key
    ``join_qr``.  It survives server restarts and stays identical until
    the admin explicitly regenerates it via POST /api/qr/join.
    """
    cfg = load_json("config") or {}
    join_qr = cfg.get("join_qr")
    if not join_qr:
        return {"status": "empty", "join_qr": None}
    return {"status": "ok", "join_qr": join_qr}


@app.post("/api/qr/join")
def api_qr_join_create(request: Request = None):
    """Create (or replace) the persistent Join QR code.

    The QR encodes only the server's protocol, IP and port so that a
    user scanning it can quickly pre-fill the connection details.  No
    credentials are embedded – the user must enter those manually.

    The generated QR data and PNG are persisted in config.json so that
    ``GET /api/qr/join`` always returns the same QR until this endpoint
    is called again.
    """
    local_ip, all_ips = get_local_ip()
    cert_file = os.path.join(base_path, "cert.pem")
    key_file = os.path.join(base_path, "key.pem")
    use_ssl = os.path.exists(cert_file) and os.path.exists(key_file)
    protocol = "https" if use_ssl else "http"
    server_port = 8101

    qr_payload = {
        "type": "join",
        "protocol": protocol,
        "host": local_ip,
        "port": server_port,
        "ssl": use_ssl,
    }

    png_b64 = None
    if qrcode:
        try:
            qr_img = qrcode.make(json.dumps(qr_payload))
            from io import BytesIO
            buf = BytesIO()
            qr_img.save(buf, format="PNG")
            png_b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        except Exception:
            logger.exception("Join QR PNG generation failed")

    join_qr = {
        "payload": qr_payload,
        "png_base64": png_b64,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "created_by_ip": _get_client_ip(request) if request else None,
    }

    cfg = load_json("config") or {}
    cfg["join_qr"] = join_qr
    save_json("config", cfg)

    log_audit(
        "create_join_qr",
        "system",
        {"host": local_ip, "port": server_port, "created_by_ip": join_qr["created_by_ip"]},
    )
    return {"status": "success", "join_qr": join_qr}


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

# -------------------------
# TAK Login Management
# -------------------------

def _get_tak_login_settings() -> dict:
    """Return the admin-configured TAK login page settings."""
    settings = load_json("tak_login_settings")
    if not isinstance(settings, dict) or not settings:
        settings = DEFAULT_DB_CONTENTS["tak_login_settings"].copy()
    return settings


def _tak_login_info(entry: dict) -> dict:
    """Build the login info dict returned by claim/check endpoints."""
    s = _get_tak_login_settings()
    return {
        "username": entry["username"],
        "password": entry["password"],
        "server": s.get("server_host") or "",
        "port": int(s.get("server_port", 8089)),
        "ssl": s.get("protocol", "ssl") == "ssl",
        "name": s.get("display_name") or "LPU5",
    }

# --- TAK Login Settings (admin) ---

@app.get("/api/tak_login_settings")
def api_tak_login_settings_get():
    """Return the admin-configured TAK login page settings."""
    return _get_tak_login_settings()


@app.put("/api/tak_login_settings")
def api_tak_login_settings_update(data: dict = Body(...)):
    """Update TAK login page settings (admin)."""
    settings = _get_tak_login_settings()
    if "server_host" in data:
        host = str(data["server_host"]).strip()
        host = re.sub(r'^https?://', '', host).rstrip('/')
        settings["server_host"] = host
    if "server_port" in data:
        try:
            port = int(data["server_port"])
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="server_port must be numeric")
        if not (1 <= port <= 65535):
            raise HTTPException(status_code=400, detail="server_port must be 1-65535")
        settings["server_port"] = port
    if "protocol" in data:
        proto = str(data["protocol"]).lower()
        if proto not in ("tcp", "udp", "ssl"):
            raise HTTPException(status_code=400, detail="protocol must be tcp, udp, or ssl")
        settings["protocol"] = proto
    if "display_name" in data:
        settings["display_name"] = str(data["display_name"]).strip()
    if "server_name" in data:
        settings["server_name"] = str(data["server_name"]).strip()
    save_json("tak_login_settings", settings)
    return {"status": "success", "settings": settings}


# --- TAK Login Certificate (.p12) Generation ---

@app.post("/api/tak_logins/generate_p12")
def api_tak_logins_generate_p12(data: dict = Body(default={})):
    """Generate a PKCS#12 (.p12) client certificate bundle for ATAK/iTAK.

    The generated .p12 contains a self-signed client certificate and
    private key that can be imported into ATAK/iTAK for TAK server
    authentication.  The admin-configured server settings are embedded
    in the certificate's common name for reference.
    """
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
        from cryptography.hazmat.primitives.serialization import pkcs12
        import ipaddress as ipaddr
    except ImportError:
        raise HTTPException(
            status_code=500,
            detail="cryptography package not available – cannot generate certificates",
        )

    settings = _get_tak_login_settings()
    cn = data.get("username") or settings.get("display_name") or "LPU5-Client"
    p12_password = data.get("password") or "atakcerts"

    try:
        # Generate RSA key pair
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

        # Build subject
        subject = issuer = x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, str(cn)),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, settings.get("display_name") or "LPU5"),
        ])

        # SAN entries
        san_entries = [x509.DNSName("localhost")]
        server_host = settings.get("server_host") or ""
        if server_host:
            try:
                san_entries.append(x509.IPAddress(ipaddr.ip_address(server_host)))
            except ValueError:
                san_entries.append(x509.DNSName(server_host))

        now = datetime.now(timezone.utc)
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now)
            .not_valid_after(now + timedelta(days=730))
            .add_extension(
                x509.SubjectAlternativeName(san_entries), critical=False,
            )
            .add_extension(
                x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.CLIENT_AUTH]),
                critical=False,
            )
            .sign(key, hashes.SHA256())
        )

        # Serialize to PKCS#12
        p12_bytes = pkcs12.serialize_key_and_certificates(
            name=cn.encode("utf-8"),
            key=key,
            cert=cert,
            cas=None,
            encryption_algorithm=serialization.BestAvailableEncryption(
                p12_password.encode("utf-8")
            ),
        )

        p12_b64 = base64.b64encode(p12_bytes).decode("ascii")

        return {
            "status": "success",
            "p12_base64": p12_b64,
            "filename": f"{cn}.p12",
            "password": p12_password,
            "server": settings.get("server_host") or "",
            "port": int(settings.get("server_port", 8089)),
            "protocol": settings.get("protocol", "ssl"),
        }

    except Exception as exc:
        logger.exception("P12 certificate generation failed: %s", exc)
        raise HTTPException(status_code=500, detail="Certificate generation failed: " + str(exc))


@app.get("/api/tak_logins")
def api_tak_logins_list():
    """List all TAK login entries (admin view)."""
    logins = load_json("tak_logins")
    if not isinstance(logins, list):
        logins = []
    return logins

@app.post("/api/tak_logins")
def api_tak_logins_add(data: dict = Body(...)):
    """Add one or more TAK login entries."""
    entries = data.get("entries")
    if entries and isinstance(entries, list):
        items = entries
    else:
        username = (data.get("username") or "").strip()
        password = (data.get("password") or "").strip()
        if not username or not password:
            raise HTTPException(status_code=400, detail="username and password required")
        items = [{"username": username, "password": password}]

    logins = load_json("tak_logins")
    if not isinstance(logins, list):
        logins = []

    added = []
    for item in items:
        u = (item.get("username") or "").strip()
        p = (item.get("password") or "").strip()
        if not u or not p:
            continue
        entry = {
            "id": str(uuid.uuid4()),
            "username": u,
            "password": p,
            "assigned": False,
            "assigned_to_ip": None,
            "assigned_to_callsign": None,
            "assigned_to_unit": None,
            "assigned_at": None,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        logins.append(entry)
        added.append(entry)

    save_json("tak_logins", logins)
    return {"status": "success", "added": len(added), "entries": added}

@app.post("/api/tak_logins/qr")
def api_tak_logins_qr(data: dict = Body(default={}), request: Request = None):
    """Generate a QR code that links to the TAK login claim page."""
    caller_base = (data.get("base_url") or "").strip().rstrip("/")
    if not caller_base:
        local_ip, _ = get_local_ip()
        cert_file = os.path.join(base_path, "cert.pem")
        key_file = os.path.join(base_path, "key.pem")
        protocol = "https" if (os.path.exists(cert_file) and os.path.exists(key_file)) else "http"
        caller_base = f"{protocol}://{local_ip}:8101"
    claim_url = f"{caller_base}/tak_login.html"

    png_b64 = None
    if qrcode:
        try:
            qr_img = qrcode.make(claim_url)
            from io import BytesIO
            buf = BytesIO()
            qr_img.save(buf, format="PNG")
            png_b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        except Exception:
            logger.exception("TAK Login QR PNG generation failed")

    return {"status": "success", "claim_url": claim_url, "png_base64": png_b64}

@app.post("/api/tak_logins/claim")
def api_tak_logins_claim(data: dict = Body(...), request: Request = None):
    """Public endpoint – claim ONE available TAK login.

    Checks the caller IP. If this IP already has a login assigned it
    returns that same login (no double-claiming). Otherwise assigns
    the first free entry. The caller must provide callsign and unit.
    """
    callsign = (data.get("callsign") or "").strip()
    unit = (data.get("unit") or "").strip()
    if not callsign or not unit:
        raise HTTPException(status_code=400, detail="callsign and unit required")

    client_ip = _get_client_ip(request) if request else "0.0.0.0"

    logins = load_json("tak_logins")
    if not isinstance(logins, list):
        logins = []

    # Check if this IP already has a login
    existing = next(
        (e for e in logins if e.get("assigned") and e.get("assigned_to_ip") == client_ip),
        None,
    )
    if existing:
        # Update callsign / unit if changed
        existing["assigned_to_callsign"] = callsign
        existing["assigned_to_unit"] = unit
        save_json("tak_logins", logins)
        return {
            "status": "success",
            "login": _tak_login_info(existing),
            "message": "Already assigned",
        }

    # Find first unassigned entry
    free = next((e for e in logins if not e.get("assigned")), None)
    if not free:
        raise HTTPException(status_code=410, detail="No free TAK logins available")

    free["assigned"] = True
    free["assigned_to_ip"] = client_ip
    free["assigned_to_callsign"] = callsign
    free["assigned_to_unit"] = unit
    free["assigned_at"] = datetime.now(timezone.utc).isoformat()
    save_json("tak_logins", logins)

    return {
        "status": "success",
        "login": _tak_login_info(free),
        "message": "Login assigned",
    }

@app.get("/api/tak_logins/check")
def api_tak_logins_check(request: Request):
    """Public endpoint – check if the calling IP already has a login."""
    client_ip = _get_client_ip(request)
    logins = load_json("tak_logins")
    if not isinstance(logins, list):
        logins = []
    existing = next(
        (e for e in logins if e.get("assigned") and e.get("assigned_to_ip") == client_ip),
        None,
    )
    if existing:
        return {
            "status": "assigned",
            "login": _tak_login_info(existing),
            "callsign": existing.get("assigned_to_callsign"),
            "unit": existing.get("assigned_to_unit"),
        }
    return {"status": "available"}

@app.put("/api/tak_logins/{entry_id}")
def api_tak_logins_update(entry_id: str, data: dict = Body(...)):
    """Edit a TAK login entry (admin)."""
    logins = load_json("tak_logins")
    if not isinstance(logins, list):
        raise HTTPException(status_code=404, detail="Entry not found")
    entry = next((e for e in logins if e.get("id") == entry_id), None)
    if not entry:
        raise HTTPException(status_code=404, detail="Entry not found")

    if "username" in data:
        entry["username"] = (data["username"] or "").strip()
    if "password" in data:
        entry["password"] = (data["password"] or "").strip()
    save_json("tak_logins", logins)
    return {"status": "success", "entry": entry}

@app.delete("/api/tak_logins/{entry_id}")
def api_tak_logins_delete(entry_id: str):
    """Delete a TAK login entry (admin)."""
    logins = load_json("tak_logins")
    if not isinstance(logins, list):
        raise HTTPException(status_code=404, detail="Entry not found")
    before = len(logins)
    logins = [e for e in logins if e.get("id") != entry_id]
    if len(logins) == before:
        raise HTTPException(status_code=404, detail="Entry not found")
    save_json("tak_logins", logins)
    return {"status": "success"}

@app.post("/api/tak_logins/{entry_id}/release")
def api_tak_logins_release(entry_id: str):
    """Release / un-assign a TAK login entry so it becomes available again."""
    logins = load_json("tak_logins")
    if not isinstance(logins, list):
        raise HTTPException(status_code=404, detail="Entry not found")
    entry = next((e for e in logins if e.get("id") == entry_id), None)
    if not entry:
        raise HTTPException(status_code=404, detail="Entry not found")
    entry["assigned"] = False
    entry["assigned_to_ip"] = None
    entry["assigned_to_callsign"] = None
    entry["assigned_to_unit"] = None
    entry["assigned_at"] = None
    save_json("tak_logins", logins)
    return {"status": "success", "entry": entry}

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
                marker.type = "node"
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
    """Callback function for gateway service to broadcast WebSocket events.

    When a ``gateway_node_update`` event is received the node's live position
    is forwarded to the configured TAK server as a CoT event using the same
    UID format, type mapping, and XML structure as the direct Meshimporter
    (``_forward_meshtastic_node_to_tak``).  All nodes use CoT type
    ``a-f-G-E-S-U-M`` (Meshtastic equipment) with a contact endpoint so each
    node appears as an individual reachable Meshtastic contact in ATAK/WinTAK.
    """
    try:
        # Forward live Meshtastic node updates to the TAK server as CoT events.
        # This mirrors the CoT generation done by _forward_meshtastic_node_to_tak
        # (used by the direct import / sync path) so that both paths produce
        # identical CoT packets: same UID format, type mapping, and XML structure.
        if event_type == "gateway_node_update":
            try:
                mesh_id = data.get("mesh_id")  # raw mesh ID, e.g. "!12345678"
                name = data.get("name") or mesh_id or "node"
                lat = float(data.get("lat") or 0.0)
                lng = float(data.get("lng") or 0.0)
                is_gw = bool(data.get("is_gateway", False))
                if mesh_id:
                    if _forward_meshtastic_node_to_tak(mesh_id, name, lat, lng, is_gateway=is_gw):
                        logger.debug(
                            "Gateway live update forwarded to TAK: %s (%s) @ %.5f,%.5f",
                            name, mesh_id, lat, lng,
                        )
            except Exception as _tak_err:
                logger.debug("Gateway→TAK forward error for %s: %s", data.get("mesh_id"), _tak_err)

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
# CoT Data Monitor (integrated)
# ===========================

class _CoTMonitorStore:
    """Thread-safe store for captured CoT events with SSE broadcast.

    Mirrors the ``EventStore`` from ``cot_data_monitor.py`` so the monitor
    web UI can be served directly from the API without requiring the
    standalone monitor tool (which would crash due to port conflicts when
    the API is already running).
    """

    def __init__(self, max_events: int = 10_000):
        self._lock = threading.Lock()
        self._events: List[Dict[str, Any]] = []
        self._max = max_events
        self._sse_queues: List[queue.Queue] = []
        self.total = 0
        self.incoming = 0
        self.outgoing = 0
        self.by_type: Dict[str, int] = {}

    # -- write path ----------------------------------------------------------

    def add(self, parsed: Dict[str, Any], direction: str,
            source: str, raw_xml: str) -> None:
        record: Dict[str, Any] = {
            "parsed": parsed,
            "direction": direction,
            "source": source,
            "raw_xml": raw_xml,
        }
        with self._lock:
            idx = len(self._events)
            record["idx"] = idx
            self._events.append(record)
            if len(self._events) > self._max:
                self._events = self._events[-self._max:]
            # Stats
            self.total += 1
            if direction == "<<<":
                self.incoming += 1
            else:
                self.outgoing += 1
            t = parsed.get("detected_type", "?")
            self.by_type[t] = self.by_type.get(t, 0) + 1
            # Push to all SSE subscribers
            dead: List[int] = []
            for i, q in enumerate(self._sse_queues):
                try:
                    q.put_nowait(record)
                except queue.Full:
                    dead.append(i)
            for i in reversed(dead):
                self._sse_queues.pop(i)

    # -- read path -----------------------------------------------------------

    def get_all(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._events)

    def set_correction(self, idx: int, correction: str, notes: str) -> None:
        with self._lock:
            if 0 <= idx < len(self._events):
                self._events[idx]["correction"] = correction
                self._events[idx]["notes"] = notes

    def clear(self) -> None:
        with self._lock:
            self._events.clear()
            self.total = 0
            self.incoming = 0
            self.outgoing = 0
            self.by_type.clear()

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "total": self.total,
                "incoming": self.incoming,
                "outgoing": self.outgoing,
                "by_type": dict(self.by_type),
            }

    # -- SSE -----------------------------------------------------------------

    def subscribe_sse(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=500)
        with self._lock:
            self._sse_queues.append(q)
        return q

    def unsubscribe_sse(self, q: queue.Queue) -> None:
        with self._lock:
            try:
                self._sse_queues.remove(q)
            except ValueError:
                pass

    # -- export --------------------------------------------------------------

    def export_log(self) -> Dict[str, Any]:
        with self._lock:
            all_events = list(self._events)
        corrected = [e for e in all_events if e.get("correction")]
        return {
            "export_time": datetime.now(timezone.utc).isoformat(),
            "summary": {
                "total_events": len(all_events),
                "corrections_made": len(corrected),
            },
            "corrections": [
                {
                    "event_index": e.get("idx", 0) + 1,
                    "uid": e["parsed"].get("uid"),
                    "callsign": e["parsed"].get("callsign"),
                    "cot_type": e["parsed"].get("cot_type"),
                    "how": e["parsed"].get("how"),
                    "detected_type": e["parsed"].get("detected_type"),
                    "detection_reason": e["parsed"].get("detection_reason"),
                    "correct_type": e.get("correction"),
                    "notes": e.get("notes", ""),
                    "direction": ("ATAK->LPU5" if e["direction"] == "<<<"
                                  else "LPU5->ATAK"),
                    "raw_xml": e.get("raw_xml", ""),
                }
                for e in corrected
            ],
            "all_events": [
                {
                    "event_index": e.get("idx", 0) + 1,
                    "direction": ("ATAK->LPU5" if e["direction"] == "<<<"
                                  else "LPU5->ATAK"),
                    "source": e.get("source"),
                    **{k: e["parsed"].get(k) for k in (
                        "uid", "callsign", "cot_type", "how", "lat", "lon",
                        "detected_type", "detection_reason", "has_meshtastic",
                        "mesh_longName", "mesh_shortName", "is_echo_back",
                        "team", "role", "endpoint", "time", "stale",
                    )},
                    "correct_type": e.get("correction") or None,
                    "notes": e.get("notes") or None,
                }
                for e in all_events
            ],
        }


# Global monitor store – always active so captured events are available
# the moment the web UI is opened.
_cot_monitor_store = _CoTMonitorStore()


def _cot_monitor_record(cot_xml: str, direction: str, source: str) -> None:
    """Parse a raw CoT XML string and push it into the monitor store.

    This is a lightweight wrapper called from ``_process_incoming_cot`` and
    ``_cot_listener_ingest_callback`` so every CoT event that flows through
    the API is captured for the monitor web UI.
    """
    import xml.etree.ElementTree as _ET
    try:
        root = _ET.fromstring(cot_xml)
        if root.tag != "event":
            return
        uid = root.get("uid", "")
        event_type = root.get("type", "")
        how = root.get("how", "")

        point = root.find("point")
        lat = lon = 0.0
        if point is not None:
            try:
                lat = float(point.get("lat", 0))
                lon = float(point.get("lon", 0))
            except (TypeError, ValueError):
                pass

        detail = root.find("detail")
        callsign = uid
        team = role = endpoint = ""
        has_meshtastic = False
        mesh_longName = mesh_shortName = ""
        if detail is not None:
            contact = detail.find("contact")
            if contact is not None:
                callsign = contact.get("callsign") or callsign
                endpoint = contact.get("endpoint", "")
            grp = detail.find("__group")
            if grp is not None:
                team = grp.get("name", "")
                role = grp.get("role", "")
            mesh_el = detail.find("meshtastic")
            if mesh_el is not None:
                has_meshtastic = True
                mesh_longName = mesh_el.get("longName", "")
                mesh_shortName = mesh_el.get("shortName", "")

        # Detect LPU5 type (simplified version matching cot_data_monitor logic)
        detected_type = "unknown"
        detection_reason = ""
        if has_meshtastic:
            detected_type = "meshtastic_node"
            detection_reason = "<meshtastic> detail element"
        elif event_type.startswith("a-f-G-E"):
            detected_type = "meshtastic_node"
            detection_reason = f"CoT type {event_type} (equipment)"
        elif event_type.startswith("a-f"):
            if how.startswith("h-g"):
                detected_type = "tak_maker"
                detection_reason = f"friendly + how={how} (human-originated)"
            else:
                detected_type = "friendly"
                detection_reason = f"CoT type {event_type}"
        elif event_type.startswith("a-h"):
            detected_type = "hostile"
            detection_reason = f"CoT type {event_type}"
        elif event_type.startswith("a-n"):
            detected_type = "neutral"
            detection_reason = f"CoT type {event_type}"
        elif event_type.startswith("a-u"):
            detected_type = "unknown"
            detection_reason = f"CoT type {event_type}"
        elif event_type.startswith("a-p"):
            detected_type = "pending"
            detection_reason = f"CoT type {event_type}"
        elif event_type.startswith("b-t-f"):
            detected_type = "geochat"
            detection_reason = "GeoChat message"
        else:
            detection_reason = f"CoT type {event_type}"

        if uid.startswith("mesh-"):
            detected_type = "node"
            detection_reason = "UID starts with mesh-"
        elif uid.startswith("GPS-"):
            detected_type = "gps_position"
            detection_reason = "UID starts with GPS-"

        is_echo_back = uid.startswith("GPS-") or uid == _LPU5_COT_UID

        time_str = root.get("time", "")
        stale_str = root.get("stale", "")

        parsed = {
            "uid": uid,
            "callsign": callsign,
            "cot_type": event_type,
            "how": how,
            "lat": lat,
            "lon": lon,
            "detected_type": detected_type,
            "detection_reason": detection_reason,
            "has_meshtastic": has_meshtastic,
            "mesh_longName": mesh_longName,
            "mesh_shortName": mesh_shortName,
            "is_echo_back": is_echo_back,
            "team": team,
            "role": role,
            "endpoint": endpoint,
            "time": time_str,
            "stale": stale_str,
        }

        _cot_monitor_store.add(parsed, direction, source, cot_xml)
    except Exception:
        pass  # best-effort; never break the main CoT flow


# ===========================
# CoT Listener Service
# ===========================

def _cot_listener_on_client_connect(conn: "socket.socket", addr: tuple) -> None:
    """
    Called by CoTListenerService whenever a new TCP client (WinTAK/ATAK) connects.

    Sends the LPU5 SA beacon so WinTAK immediately knows it is talking to a
    valid gateway entity, and pushes the current state of all stored markers
    so the connecting client has up-to-date SA from the moment it joins.
    """
    try:
        # SA greeting: announce LPU5 as a named entity on the TAK network.
        conn.sendall(_build_lpu5_sa_xml().encode("utf-8"))
        logger.info("CoT TCP: sent SA greeting to new client %s", addr)
    except OSError as exc:
        logger.debug("CoT TCP: SA greeting to %s failed: %s", addr, exc)
        return
    # Push all existing markers so the client has immediate situational awareness.
    if not AUTONOMOUS_MODULES_AVAILABLE:
        return
    try:
        with SessionLocal() as db:
            markers = db.query(MapMarker).all()
        pushed = 0
        for m in markers:
            try:
                mdict = {
                    "id": m.id, "lat": m.lat, "lng": m.lng,
                    "name": m.name, "type": m.type,
                    "created_by": m.created_by,
                }
                if isinstance(m.data, dict):
                    for k, v in m.data.items():
                        if k not in mdict:
                            mdict[k] = v
                cot_evt = CoTProtocolHandler.marker_to_cot(mdict)
                if cot_evt:
                    conn.sendall(cot_evt.to_xml().encode("utf-8"))
                    pushed += 1
            except OSError:
                break
            except Exception:
                continue
        if pushed:
            logger.info("CoT TCP: pushed %d existing markers to new client %s", pushed, addr)
    except Exception as exc:
        logger.debug("CoT TCP: initial marker push to %s failed: %s", addr, exc)


def _cot_listener_ingest_callback(xml_string: str) -> None:
    """
    Ingest callback for the CoT listener service.

    Parses the received CoT XML, upserts the corresponding map marker into
    the database, and broadcasts the change to all WebSocket clients.
    Mirrors the logic in POST /api/cot/ingest without the HTTP layer.
    GeoChat events (type b-t-f) are saved as LPU5 chat messages instead of
    map markers.
    """
    # Capture for the CoT data monitor (best-effort, never blocks the main flow)
    _cot_monitor_record(xml_string, "<<<", "cot_listener")

    # Handle ATAK GeoChat (b-t-f) events BEFORE the AUTONOMOUS_MODULES_AVAILABLE
    # guard because GeoChat ingestion only needs stdlib xml.etree and the local
    # _ingest_atak_geochat helper – it must work even when the heavier autonomous
    # modules (cot_protocol, geofencing, autonomous_engine) failed to load.
    try:
        import xml.etree.ElementTree as _ET
        _root = _ET.fromstring(xml_string)
        if _root.get("type", "").startswith("b-t-f"):
            is_new = _ingest_atak_geochat(_root)
            # Only relay to the TAK server when the message was genuinely
            # new.  We intentionally do NOT echo back to TCP clients or
            # multicast here – the message already arrived from one of
            # those transports and relaying it back would send it to the
            # same ATAK device that sent it, risking an infinite loop.
            if is_new:
                forward_cot_to_tak(xml_string)
            return
    except Exception:
        pass

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
        # Suppress re-creation of permanently deleted markers.
        if _is_deleted_marker(marker_dict["id"]):
            logger.debug("CoT listener: suppressing deleted marker %s", marker_dict["id"])
            return
        with SessionLocal() as db:
            existing = db.query(MapMarker).filter(MapMarker.id == marker_dict["id"]).first()
            if existing:
                if (marker_dict["id"].startswith("mesh-")
                        or existing.created_by not in _TAK_INGEST_SOURCES):
                    # Echo-back of an LPU5-originated marker — skip the update to
                    # prevent the native LPU5 type from being overwritten with a
                    # CBT variant (e.g. "hostile" → "cbt_hostile").
                    return
                # Guard: don't downgrade a meshtastic_node/node/gateway marker to
                # cbt_friendly or tak_maker when the incoming CoT echo lacks a
                # <meshtastic> element.  ATAK may strip custom detail elements when
                # re-distributing CoT, which would cause the node to lose its
                # Meshtastic icon on every subsequent position update.
                incoming_type = marker_dict.get("type", "unknown")
                _MESH_DB_TYPES = {"meshtastic_node", "node", "gateway"}
                if (existing.type in _MESH_DB_TYPES
                        and not cot_event.has_meshtastic_detail
                        and incoming_type not in _MESH_DB_TYPES):
                    incoming_type = existing.type
                existing.lat = marker_dict["lat"]
                existing.lng = marker_dict["lng"]
                existing.name = marker_dict.get("name") or marker_dict["id"]
                existing.type = incoming_type
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
                # For "mesh-" UID markers from ATAK/WinTAK SA/COT import, always
                # use type "node" so they render with the Meshtastic blue-circle icon.
                new_type = "node" if marker_dict["id"].startswith("mesh-") else marker_dict.get("type", "unknown")
                new_marker = MapMarker(
                    id=marker_dict["id"],
                    lat=marker_dict["lat"],
                    lng=marker_dict["lng"],
                    name=marker_dict.get("name") or marker_dict["id"],
                    description=marker_dict.get("description"),
                    type=new_type,
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
        # Relay to all other directly connected TCP clients (e.g. multiple WinTAK
        # instances on port 8088) so SA data flows between them through LPU5.
        _forward_cot_to_tcp_clients(xml_string)
        # Also push via SA Multicast so ATAK/WinTAK on the LAN stays in sync.
        _forward_cot_multicast(xml_string)
        # Relay to iTAK bridge clients (SSL port 8089)
        _forward_cot_to_itak_bridge(xml_string)
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
            on_client_connect=_cot_listener_on_client_connect,
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
    if PERMISSIONS_AVAILABLE:
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
    if PERMISSIONS_AVAILABLE:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Authentication required")
        if verify_token(authorization.split(" ")[1]) is None:
            raise HTTPException(status_code=401, detail="Invalid or expired token")
    _stop_cot_listener()
    return {"status": "stopped"}


# ---------------------------------------------------------------------------
# iTAK CoT Bridge  (SSL TCP 127.0.0.1:8089 — Meshtastic ↔ iTAK)
# ---------------------------------------------------------------------------

def _start_itak_bridge() -> bool:
    """Start the iTAK CoT bridge using config.json settings."""
    global _itak_bridge_service
    if not COT_LISTENER_AVAILABLE or iTAKBridgeServer is None:
        return False
    with _itak_bridge_lock:
        if _itak_bridge_service and _itak_bridge_service.stats.get("running"):
            return True
        cfg = load_json("config") or {}
        port = int(cfg.get("itak_bridge_port", iTAKBridgeServer.DEFAULT_PORT))
        cert_path = str(cfg.get("itak_bridge_cert", "cert.pem"))
        key_path = str(cfg.get("itak_bridge_key", "key.pem"))
        _itak_bridge_service = iTAKBridgeServer(
            port=port,
            cert_path=cert_path,
            key_path=key_path,
            ingest_callback=_cot_listener_ingest_callback,
            on_client_connect=_cot_listener_on_client_connect,
        )
        return _itak_bridge_service.start()


def _stop_itak_bridge() -> None:
    """Stop the iTAK CoT bridge."""
    global _itak_bridge_service
    with _itak_bridge_lock:
        if _itak_bridge_service:
            _itak_bridge_service.stop()
            _itak_bridge_service = None


@app.get("/api/itak_bridge/status", summary="Get iTAK CoT bridge status")
def itak_bridge_status():
    """
    Return the current status of the local iTAK CoT bridge
    (SSL TCP on 127.0.0.1:8089).
    """
    if not COT_LISTENER_AVAILABLE or iTAKBridgeServer is None:
        return {"available": False, "message": "iTAK bridge not available"}
    with _itak_bridge_lock:
        if not _itak_bridge_service:
            return {"available": True, "running": False, "message": "iTAK bridge not started"}
        return {"available": True, **_itak_bridge_service.get_status()}


@app.post("/api/itak_bridge/start", summary="Start iTAK CoT bridge")
def itak_bridge_start(authorization: Optional[str] = Header(None)):
    """
    Start the local iTAK CoT bridge on 127.0.0.1:8089 (SSL/TLS).

    This creates a local TAK-compatible SSL server that iTAK on iPhone can
    connect to.  CoT events flow bidirectionally between iTAK and the
    Meshtastic mesh network.  The bridge starts automatically when the user
    activates Mesh in the app.
    """
    if not COT_LISTENER_AVAILABLE or iTAKBridgeServer is None:
        raise HTTPException(status_code=501, detail="iTAK bridge not available")
    if PERMISSIONS_AVAILABLE:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Authentication required")
        if verify_token(authorization.split(" ")[1]) is None:
            raise HTTPException(status_code=401, detail="Invalid or expired token")
    if not _start_itak_bridge():
        raise HTTPException(status_code=500, detail="Failed to start iTAK bridge")
    with _itak_bridge_lock:
        status = _itak_bridge_service.get_status() if _itak_bridge_service else {}
    return {"status": "started", **status}


@app.post("/api/itak_bridge/stop", summary="Stop iTAK CoT bridge")
def itak_bridge_stop(authorization: Optional[str] = Header(None)):
    """Stop the local iTAK CoT bridge."""
    if not COT_LISTENER_AVAILABLE or iTAKBridgeServer is None:
        raise HTTPException(status_code=501, detail="iTAK bridge not available")
    if PERMISSIONS_AVAILABLE:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Authentication required")
        if verify_token(authorization.split(" ")[1]) is None:
            raise HTTPException(status_code=401, detail="Invalid or expired token")
    _stop_itak_bridge()
    return {"status": "stopped"}


# ===========================
# CoT GeoChat Diagnostic & Push Endpoints
# ===========================

@app.get("/api/cot/geochat/events", summary="List recent GeoChat (b-t-f) messages")
def cot_geochat_events(
    limit: int = 50,
    authorization: Optional[str] = Header(None),
):
    """
    Return the most recent GeoChat messages that were received from ATAK/WinTAK
    via CoT type ``b-t-f`` and saved to the LPU5 chat channel ``all``.

    This is a read-only diagnostic endpoint – no authentication is required for
    convenience, but the caller may pass a Bearer token to retrieve user-specific
    channel-membership metadata in the future.

    Query parameters:
    - **limit** (int, default 50): maximum number of messages to return.
    """
    try:
        db = SessionLocal()
        try:
            msgs = (
                db.query(ChatMessage)
                .filter(ChatMessage.channel == MESH_CHAT_CHANNEL)
                .order_by(ChatMessage.timestamp.desc())
                .limit(max(1, min(limit, 500)))
                .all()
            )
            return {
                "channel": MESH_CHAT_CHANNEL,
                "count": len(msgs),
                "messages": [_chat_message_to_dict(m) for m in reversed(msgs)],
            }
        finally:
            db.close()
    except Exception as exc:
        logger.exception("cot_geochat_events failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/cot/geochat/push", summary="Manually push a GeoChat message into the 'all' channel")
async def cot_geochat_push(payload: Dict = Body(...), authorization: Optional[str] = Header(None)):
    """
    Inject a GeoChat message directly into the LPU5 ``all`` chat channel without
    requiring an actual CoT XML event.  This is useful for:

    - Testing the ATAK→LPU5 chat bridge from scripts or curl.
    - Forwarding chat from external TAK integrations that do not speak CoT.
    - Manual operator broadcasts that should appear in the ATAK GeoChat window.

    **Request body (JSON):**

    ```json
    {
        "callsign": "ALPHA-1",
        "text": "Hello from ATAK",
        "uid": "ANDROID-abc123"
    }
    ```

    - ``callsign`` (str, required): sender display name shown in the chat.
    - ``text`` (str, required): message body.
    - ``uid`` (str, optional): sender UID; defaults to callsign when omitted.

    The message is saved as a ``ChatMessage`` in the ``all`` channel and
    immediately broadcast to all connected WebSocket clients.  If TAK
    forwarding is enabled it is also echoed back to ATAK clients as a
    ``b-t-f`` CoT event.
    """
    callsign = (payload.get("callsign") or "").strip()
    text = (payload.get("text") or "").strip()
    uid = (payload.get("uid") or callsign).strip()

    if not callsign:
        raise HTTPException(status_code=400, detail="'callsign' is required")
    if not text:
        raise HTTPException(status_code=400, detail="'text' is required")

    try:
        db = SessionLocal()
        try:
            _ensure_default_channels(db)
            new_msg = ChatMessage(
                channel=MESH_CHAT_CHANNEL,
                sender=callsign,
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
        finally:
            db.close()
    except Exception as exc:
        logger.exception("cot_geochat_push DB error: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))

    # Broadcast to WebSocket clients
    if websocket_manager and _MAIN_EVENT_LOOP:
        try:
            asyncio.run_coroutine_threadsafe(
                websocket_manager.publish_to_channel(
                    'chat', {"type": "new_message", "data": msg_dict}
                ),
                _MAIN_EVENT_LOOP,
            )
        except Exception as ws_exc:
            logger.warning("cot_geochat_push WebSocket broadcast failed: %s", ws_exc)

    # Echo to ATAK/TAK clients as b-t-f CoT
    try:
        _forward_chat_to_atak(callsign, text)
    except Exception as atak_exc:
        logger.warning("cot_geochat_push ATAK forward failed: %s", atak_exc)

    logger.info("GeoChat push: [%s/%s] %s (len=%d)", uid, callsign, text[:80], len(text))
    return {"status": "ok", "message": msg_dict}


# ===========================
# CoT Data Monitor Endpoints
# ===========================

@app.get("/api/cot/monitor/events", summary="Get captured CoT events")
def cot_monitor_events():
    """Return all captured CoT events from the in-memory monitor store."""
    return {"events": _cot_monitor_store.get_all()}


@app.get("/api/cot/monitor/stream", summary="SSE stream of CoT events")
async def cot_monitor_stream(request: Request):
    """Server-Sent Events stream for real-time CoT data monitoring.

    Each event is sent as ``event: cot_event`` with a JSON ``data`` payload
    containing the parsed CoT fields, direction, source, and raw XML.
    A ``ping`` comment is sent every 15 seconds as a keep-alive.
    """
    q = _cot_monitor_store.subscribe_sse()

    async def _event_generator():
        loop = asyncio.get_event_loop()
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    record = await asyncio.wait_for(
                        loop.run_in_executor(None, lambda: q.get(timeout=1)),
                        timeout=15,
                    )
                    payload = json.dumps(record, ensure_ascii=False)
                    yield f"event: cot_event\ndata: {payload}\n\n"
                except (asyncio.TimeoutError, Exception):
                    yield ": ping\n\n"
        finally:
            _cot_monitor_store.unsubscribe_sse(q)

    return StreamingResponse(
        _event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "Access-Control-Allow-Origin": "*",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/cot/monitor/clear", summary="Clear captured CoT events")
def cot_monitor_clear():
    """Clear all captured CoT events from the monitor store."""
    _cot_monitor_store.clear()
    return {"ok": True}


@app.post("/api/cot/monitor/events/{idx}/correction",
          summary="Set correction for a captured event")
def cot_monitor_set_correction(idx: int, data: Dict = Body(...)):
    """Manually assign the correct marker type for a captured CoT event."""
    _cot_monitor_store.set_correction(
        idx,
        data.get("correction", ""),
        data.get("notes", ""),
    )
    return {"ok": True}


@app.get("/api/cot/monitor/export", summary="Export monitor log")
def cot_monitor_export():
    """Export all captured events with corrections as JSON."""
    return _cot_monitor_store.export_log()


@app.post("/api/cot/monitor/export", summary="Save monitor log server-side")
def cot_monitor_export_post(data: Dict = Body(...)):
    """Save the annotated monitor log to a server-side JSON file."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename = f"cot_monitor_log_{ts}.json"
    base_dir = str(pathlib.Path(__file__).resolve().parent)
    filepath = os.path.join(base_dir, filename)
    try:
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        logger.info("CoT monitor log saved to %s", filepath)
        return {"ok": True, "file": filepath}
    except OSError as exc:
        return {"ok": False, "error": str(exc)}


@app.get("/api/cot/monitor/stats", summary="Get CoT monitor statistics")
def cot_monitor_stats():
    """Return current statistics from the CoT data monitor."""
    return _cot_monitor_store.stats()


@app.get("/api/cot/monitor/ui", summary="CoT data monitor web UI")
def cot_monitor_ui():
    """Serve the CoT data-flow monitor HTML interface.

    This is the same ``cot_monitor_ui.html`` used by the standalone
    ``cot_data_monitor.py`` tool, but served directly from the API so
    there are no port conflicts.
    """
    html_path = os.path.join(
        str(pathlib.Path(__file__).resolve().parent), "cot_monitor_ui.html")
    if not os.path.isfile(html_path):
        raise HTTPException(status_code=404,
                            detail="cot_monitor_ui.html not found")
    return FileResponse(html_path, media_type="text/html")


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

        # Suppress recreation of permanently deleted markers so that ATAK
        # echo-backs cannot bring back a marker the user intentionally removed.
        if _is_deleted_marker(marker_dict["id"]):
            logger.debug("ingest_cot_xml: suppressing deleted marker %s", marker_dict["id"])
            return {"status": "suppressed", "reason": "marker permanently deleted"}

        with SessionLocal() as db:
            existing = db.query(MapMarker).filter(MapMarker.id == marker_dict["id"]).first()
            if existing:
                # Guard: never overwrite a marker that was created by a native LPU5
                # user or Meshtastic ingest.  ATAK echo-backs for LPU5-originated
                # markers would otherwise corrupt the marker type (e.g. "hostile" →
                # "cbt_hostile") and lose the original user-set label.
                if marker_dict["id"].startswith("mesh-") or existing.created_by not in _TAK_INGEST_SOURCES:
                    logger.debug(
                        "ingest_cot_xml: skipping echo-back update for LPU5-originated marker %s",
                        marker_dict["id"],
                    )
                    return {"status": "skipped", "reason": "echo-back of LPU5-originated marker"}
                # Guard: don't downgrade a meshtastic_node/node/gateway marker to
                # cbt_friendly or tak_maker when the incoming CoT echo lacks a
                # <meshtastic> element.  ATAK may strip custom detail elements when
                # re-distributing CoT, which would cause the node to lose its
                # Meshtastic icon on every subsequent position update.
                incoming_type = marker_dict.get("type", "unknown")
                _MESH_DB_TYPES = {"meshtastic_node", "node", "gateway"}
                if (existing.type in _MESH_DB_TYPES
                        and not cot_event.has_meshtastic_detail
                        and incoming_type not in _MESH_DB_TYPES):
                    incoming_type = existing.type
                existing.lat   = marker_dict["lat"]
                existing.lng   = marker_dict["lng"]
                existing.name  = marker_dict.get("name") or marker_dict["id"]
                existing.type  = incoming_type
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
                # For "mesh-" UID markers from ATAK/WinTAK SA/COT import, always
                # use type "node" so they render with the Meshtastic blue-circle icon.
                new_type = "node" if marker_dict["id"].startswith("mesh-") else marker_dict.get("type", "unknown")
                new_marker = MapMarker(
                    id=marker_dict["id"],
                    lat=marker_dict["lat"],
                    lng=marker_dict["lng"],
                    name=marker_dict.get("name") or marker_dict["id"],
                    description=marker_dict.get("description"),
                    type=new_type,
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
        "cot_listener_host":    cfg.get("cot_listener_host", ""),
        "cot_listener_tcp_port": int(cfg.get("cot_listener_tcp_port", 8088)),
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
    - cot_listener_host (str): External IP/hostname of this LPU5 server — used as the
      ``endpoint`` in the LPU5-GW SA beacon so ATAK shows it as a reachable Contact
    """
    payload = None
    if authorization and authorization.startswith("Bearer "):
        payload = verify_token(authorization.split(" ")[1])
    if payload is None:
        if PERMISSIONS_AVAILABLE:
            raise HTTPException(status_code=401, detail="Authentication required")
        payload = {"username": "system"}

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
    if "cot_listener_host" in data:
        cfg["cot_listener_host"] = str(data["cot_listener_host"]).strip()
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
        "cot_listener_host":    cfg.get("cot_listener_host", ""),
        "cot_listener_tcp_port": int(cfg.get("cot_listener_tcp_port", 8088)),
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
    try:
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
    except Exception as e:
        logger.exception("Unhandled error in test_tak_connection: %s", e)
        return {"reachable": False, "data_exchanged": False, "message": f"Internal error: {e}"}


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


# Source labels used by the TAK/CoT ingest paths
_TAK_INGEST_SOURCES = {"tak_server", "cot_ingest"}


@app.get("/api/tak/marker-diff", summary="Compare LPU5 markers with received ATAK/WinTAK markers")
def get_tak_marker_diff(db: Session = Depends(get_db)):
    """
    Return a diff between markers created in LPU5 and markers received from
    ATAK/WinTAK clients (identified by created_by == 'tak_server' or
    'cot_ingest').

    Response fields:
    - lpu5_only: markers that exist in LPU5 but have never been received
      back from any ATAK/WinTAK client (potentially missing on the TAK side).
    - tak_only: markers received from ATAK/WinTAK that have no matching
      LPU5-originated counterpart (TAK-sourced data).
    - synced: LPU5 markers whose UID was also echoed back from an ATAK/WinTAK
      client (confirmed present on both sides).
    - total_lpu5: total LPU5-originated marker count (excludes meshtastic).
    - total_tak: total markers received from ATAK/WinTAK.
    """
    all_markers = db.query(MapMarker).all()

    tak_markers = [m for m in all_markers if m.created_by in _TAK_INGEST_SOURCES]
    lpu5_markers = [
        m for m in all_markers
        if m.created_by not in _TAK_INGEST_SOURCES
        and m.created_by not in _MESHTASTIC_CREATED_BY
    ]

    tak_uids = {m.id for m in tak_markers}
    lpu5_uids = {m.id for m in lpu5_markers}

    lpu5_only = [m for m in lpu5_markers if m.id not in tak_uids]
    tak_only = [m for m in tak_markers if m.id not in lpu5_uids]
    synced = [m for m in lpu5_markers if m.id in tak_uids]

    def _m(m: MapMarker) -> dict:
        return {
            "id": m.id,
            "name": m.name,
            "lat": m.lat,
            "lng": m.lng,
            "type": m.type,
            "created_by": m.created_by,
        }

    return {
        "lpu5_only": [_m(m) for m in lpu5_only],
        "tak_only": [_m(m) for m in tak_only],
        "synced": [_m(m) for m in synced],
        "total_lpu5": len(lpu5_markers),
        "total_tak": len(tak_markers),
    }


@app.post("/api/tak/push-missing", summary="Push LPU5-only markers to connected ATAK/WinTAK clients")
def push_missing_to_tak(authorization: Optional[str] = Header(None), db: Session = Depends(get_db)):
    """
    Push all LPU5 markers that have not been received back from any ATAK/WinTAK
    client to every currently connected ATAK/WinTAK client (TCP + multicast +
    configured TAK server).

    Requires a valid Bearer token.

    Response fields:
    - pushed: number of markers successfully forwarded.
    - failed: number of markers that could not be converted or sent.
    - total_missing: total LPU5-only markers considered for pushing.
    """
    if PERMISSIONS_AVAILABLE:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Authentication required")
        payload = verify_token(authorization.split(" ")[1])
        if payload is None:
            raise HTTPException(status_code=401, detail="Invalid or expired token")

    if not AUTONOMOUS_MODULES_AVAILABLE:
        raise HTTPException(status_code=501, detail="CoT protocol module not available")

    tak_uids = {
        m.id for m in db.query(MapMarker).filter(
            MapMarker.created_by.in_(list(_TAK_INGEST_SOURCES))
        ).all()
    }

    lpu5_only = db.query(MapMarker).filter(
        ~MapMarker.created_by.in_(list(_TAK_INGEST_SOURCES)),
        ~MapMarker.created_by.in_(list(_MESHTASTIC_CREATED_BY)),
    ).all()
    lpu5_only = [m for m in lpu5_only if m.id not in tak_uids]

    pushed = 0
    failed = 0
    for m in lpu5_only:
        try:
            mdict = {
                "id": m.id, "name": m.name, "lat": m.lat, "lng": m.lng,
                "type": m.type, "created_by": m.created_by,
            }
            # Merge extra fields stored in m.data (e.g. cot_type, callsign) without
            # overwriting the core keys already set above.
            if isinstance(m.data, dict):
                for k, v in m.data.items():
                    if k not in mdict:
                        mdict[k] = v
            cot_evt = CoTProtocolHandler.marker_to_cot(mdict)
            if cot_evt:
                cot_xml = cot_evt.to_xml()
                _forward_cot_to_tcp_clients(cot_xml)
                _forward_cot_multicast(cot_xml)
                _forward_cot_to_itak_bridge(cot_xml)
                forward_cot_to_tak(cot_xml)
                pushed += 1
            else:
                failed += 1
        except Exception as _push_err:
            logger.debug("push-missing: marker %s failed: %s", m.id, _push_err)
            failed += 1

    logger.info("TAK push-missing: pushed=%d failed=%d total_missing=%d user=%s",
                pushed, failed, len(lpu5_only), payload.get("username"))
    return {"pushed": pushed, "failed": failed, "total_missing": len(lpu5_only)}


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
        if websocket_manager:
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
        if websocket_manager:
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
        if websocket_manager:
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

        # Forward to ATAK/TAK clients as GeoChat CoT (b-t-f)
        if channel_id == MESH_CHAT_CHANNEL:
            _forward_chat_to_atak(username, text)

        return {"status": "success", "message": msg_dict}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.exception("send_chat_message failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()

@app.post("/api/chat/image", summary="Upload image and send as chat message")
async def send_chat_image(
    file: UploadFile = File(...),
    channel_id: str = Form("all"),
    authorization: str = Header(None),
):
    """Upload an image and create a chat message with the image URL."""
    _MAX_CHAT_IMAGE_SIZE = 10 * 1024 * 1024  # 10 MB

    db = SessionLocal()
    try:
        username = _extract_username_from_auth(authorization)

        # Validate file type
        original_name = file.filename or "photo.jpg"
        _, ext = os.path.splitext(os.path.basename(original_name))
        ext_lower = ext.lower().strip()
        if not ext_lower:
            ext_lower = ".jpg"

        # Sanitize: only allow alphanumeric ext chars (no null bytes, path separators)
        if not all(c.isalnum() or c == '.' for c in ext_lower):
            raise HTTPException(status_code=400, detail="Invalid file extension")

        allowed_img_ext = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
        allowed_img_mime = {"image/jpeg", "image/png", "image/gif", "image/webp", "image/bmp"}

        if ext_lower not in allowed_img_ext:
            raise HTTPException(status_code=400, detail=f"File type '{ext_lower}' is not allowed. Allowed: {', '.join(sorted(allowed_img_ext))}")

        content_type = file.content_type or ""
        if content_type and content_type not in allowed_img_mime:
            raise HTTPException(status_code=400, detail=f"MIME type '{content_type}' is not allowed.")

        # Read file content with size limit
        content = await file.read()
        if len(content) > _MAX_CHAT_IMAGE_SIZE:
            raise HTTPException(status_code=400, detail=f"File too large. Maximum size is {_MAX_CHAT_IMAGE_SIZE // (1024*1024)} MB.")
        if len(content) == 0:
            raise HTTPException(status_code=400, detail="Empty file")

        # Verify it is a valid image using Pillow
        try:
            from PIL import Image as PILImage
            import io
            img = PILImage.open(io.BytesIO(content))
            img.verify()
        except Exception:
            raise HTTPException(status_code=400, detail="File is not a valid image")

        # Verify channel exists
        _ensure_default_channels(db)
        channel_exists = db.query(ChatChannel).filter(ChatChannel.id == channel_id).first()
        if not channel_exists:
            raise HTTPException(status_code=404, detail="Channel not found")

        # Enforce channel access
        sender_user = db.query(User).filter(User.username == username).first()
        if sender_user and sender_user.role not in ("admin", "operator"):
            user_chat_channels = sender_user.chat_channels or []
            allowed = set(user_chat_channels) | {"all"}
            if channel_id not in allowed:
                raise HTTPException(status_code=403, detail="You are not a member of this channel")

        # Save file to uploads/chat/
        chat_uploads = os.path.join(uploads_dir, "chat")
        os.makedirs(chat_uploads, exist_ok=True)
        safe_name = f"{uuid.uuid4().hex}{ext_lower}"
        dest_path = os.path.join(chat_uploads, safe_name)
        with open(dest_path, "wb") as fh:
            fh.write(content)

        file_url = f"/uploads/chat/{safe_name}"

        # Create chat message with type=image and content=URL
        new_msg = ChatMessage(
            channel=channel_id,
            sender=username,
            content=file_url,
            timestamp=datetime.now(timezone.utc),
            type="image",
            delivered_to=[],
            read_by=[],
        )
        db.add(new_msg)
        db.commit()
        db.refresh(new_msg)

        msg_dict = _chat_message_to_dict(new_msg)

        # Broadcast to WebSocket clients
        if websocket_manager:
            await websocket_manager.publish_to_channel('chat', {"type": "new_message", "data": msg_dict})

        return {"status": "success", "message": msg_dict}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.exception("send_chat_image failed: %s", e)
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
            if websocket_manager:
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
            if websocket_manager:
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
        if updated and websocket_manager:
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
        "hostile": 1,    # diamond/rhombus
        "friendly": 2,  # rectangle
        "viereck": 3,    # square
        "unknown": 4    # flower
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
                if s.type in ("node", "meshtastic_node", "gateway") or (s.created_by and s.created_by in _MESHTASTIC_CREATED_BY):
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
                    "username": s.created_by,
                    "created_by": s.created_by
                }
                # Add extra data if available
                if s.data:
                    s_dict.update(s.data)
                # Restore id / created_by / username / label so that stray keys
                # inside s.data cannot overwrite authoritative DB values.
                s_dict["id"] = s.id
                s_dict["label"] = s.name
                s_dict["created_by"] = s.created_by
                s_dict["username"] = s.created_by
                symbol_list.append(s_dict)
                
            return {"status": "success", "symbols": symbol_list}
    except Exception as e:
        logger.exception("get_map_symbols failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/map/symbols", summary="Place a new map symbol")
async def place_map_symbol(symbol: Dict = Body(...), authorization: str = Header(None)):
    """Place a new symbol on the map (DB-backed)"""
    try:
        # Verify user authentication; when permissions are disabled every
        # request is allowed – callers without a token are treated as
        # "anonymous" so that ATAK / TAK devices can post markers freely.
        username = "anonymous"
        if authorization and authorization.startswith("Bearer "):
            user_payload = verify_token(authorization.split(" ")[1])
            if user_payload is not None:
                username = user_payload.get("username", "anonymous")
            elif PERMISSIONS_AVAILABLE:
                raise HTTPException(status_code=401, detail="Invalid or expired token")
        elif PERMISSIONS_AVAILABLE:
            raise HTTPException(status_code=401, detail="Missing or invalid authorization header")
        
        lat = symbol.get("lat")
        lng = symbol.get("lng")
        # Normalise to lowercase so that type IDs are consistent across all
        # TAK clients (ATAK/ITAK/WinTAK/XTAK) — e.g. "hostile" == "Hostile".
        symbol_type = symbol.get("type", "marker").lower()
        source_page = symbol.get("source_page", "unknown")
        
        if lat is None or lng is None:
            raise HTTPException(status_code=400, detail="lat and lng are required")
        
        timestamp = datetime.now(timezone.utc).isoformat()
        
        with SessionLocal() as db:
            # For gps_position type, use a stable UID derived from the username
            # so that every position update reuses the same CoT UID.  ATAK/WinTAK
            # identifies contacts by UID: a changing UID creates a brand-new contact
            # every update cycle, producing hundreds of stale ghost markers on the
            # TAK map.  Using a fixed "GPS-<username>" UID ensures TAK moves the
            # existing contact instead of creating a new one.
            if symbol_type == "gps_position":
                safe_user = re.sub(r"[^a-zA-Z0-9_-]", "_", username)
                stable_uid = f"GPS-{safe_user}"
                # Update the existing GPS marker in-place if one already exists for
                # this user; only create a new record when none is present yet.
                existing_gps = db.query(MapMarker).filter(
                    MapMarker.id == stable_uid
                ).first()
                if existing_gps is None:
                    # Also clean up any legacy GPS markers that may have been
                    # created with random UUIDs before this fix was applied.
                    old_gps = db.query(MapMarker).filter(
                        MapMarker.type == "gps_position",
                        MapMarker.created_by == username
                    ).all()
                    for old in old_gps:
                        db.delete(old)
                    if old_gps:
                        db.commit()
                    new_symbol = MapMarker(
                        id=stable_uid,
                        lat=lat,
                        lng=lng,
                        type=symbol_type,
                        name=symbol.get("label") or symbol_type,
                        color=symbol.get("color", "#3498db"),
                        icon=symbol.get("icon", "fa-location-arrow"),
                        created_by=username,
                        created_at=datetime.now(timezone.utc),
                        data={
                            "source_page": source_page,
                            "timestamp": timestamp,
                            "label": symbol.get("label", ""),
                            "how": "h-g-i-g-o",
                        }
                    )
                    db.add(new_symbol)
                else:
                    existing_gps.lat = lat
                    existing_gps.lng = lng
                    existing_gps.name = symbol.get("label") or symbol_type
                    existing_gps.color = symbol.get("color", "#3498db")
                    marker_data = existing_gps.data if isinstance(existing_gps.data, dict) else {}
                    marker_data["timestamp"] = timestamp
                    marker_data["source_page"] = source_page
                    marker_data["how"] = "h-g-i-g-o"
                    existing_gps.data = marker_data
                    new_symbol = existing_gps
                db.commit()
                db.refresh(new_symbol)
            else:
                # Non-GPS markers: create new record
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
            
            # For GPS position markers, look up the owner's profile to enrich
            # the CoT event with the user's callsign and TAK team/role so that
            # WinTAK displays a properly identified person marker instead of a
            # generic GPS pin.  Username and callsign are separate fields:
            # username is the login name, callsign is the tactical identifier.
            user_callsign = None
            user_tak_team = None
            user_tak_role = None
            if symbol_type == "gps_position" and username != "anonymous":
                user_record = db.query(User).filter(User.username == username).first()
                if user_record:
                    user_callsign = user_record.callsign or None
                    user_tak_team = user_record.tak_team or None
                    user_tak_role = user_record.tak_role or None

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
            if user_callsign:
                symbol_data["callsign"] = user_callsign
            if user_tak_team:
                symbol_data["team"] = user_tak_team
            if user_tak_role:
                symbol_data["role"] = user_tak_role
        
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

        # Record the deletion permanently — suppresses ATAK echo-back from ever
        # recreating this marker, even after a server restart.
        _record_deleted_marker(symbol_id, deleted_by=current_username)

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
    if not websocket_manager or not websocket_event_handler:
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
                    stop_msg = {
                        'type': 'camera_stream_stop',
                        'channel': 'camera',
                        'slot': data.get('slot'),
                        'timestamp': datetime.now(timezone.utc).isoformat(),
                        'source_connection': connection_id
                    }
                    await websocket_manager.publish_to_channel('camera', stop_msg)
                    relay_handled = True
                    
                elif message_type == 'broadcast_selected':
                    # Relay broadcast selection so the source (e.g. overview.html) can start sending frames
                    stream_id = str(data.get('streamId', '')).replace('\n', '').replace('\r', '')
                    logger.info(f"Relaying broadcast_selected from {connection_id}: streamId={stream_id}")
                    bc_msg = {
                        'type': 'broadcast_selected',
                        'channel': 'camera',
                        'streamId': data.get('streamId'),
                        'active': data.get('active'),
                        'slot': data.get('slot'),
                        'source': data.get('source'),
                        'details': data.get('details'),
                        'timestamp': datetime.now(timezone.utc).isoformat(),
                        'source_connection': connection_id
                    }
                    await websocket_manager.publish_to_channel('camera', bc_msg)
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
    if not websocket_manager:
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

# Lock that serialises all SDR hardware access.  rtl_tcp (and the USB dongle
# itself) can only serve ONE client at a time.  Without this lock concurrent
# /api/sdr/measure requests would each open a new TCP connection, causing
# rtl_tcp to kill the previous session ("comm recv bye / Signal caught") and
# eventually return incomplete data or connection-refused errors.
_SDR_LOCK = threading.Lock()

# Directory where api.py lives — used to locate RTL-SDR binaries placed next
# to the server (common on Windows setups per the setup guide).
_PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))

# Auto-started rtl_tcp subprocess (managed by _ensure_rtl_tcp / shutdown).
_RTL_TCP_PROC: Optional[_subprocess.Popen] = None

# Last stderr output captured from a failed rtl_tcp auto-start attempt.
# Surfaced by /api/sdr/connect so the UI can show actionable diagnostics
# (e.g. "usb_open error -3 → install WinUSB driver via Zadig").
_RTL_TCP_LAST_ERROR: Optional[str] = None


def _find_rtl_tool(name: str) -> Optional[str]:
    """Find an RTL-SDR executable by *name* (e.g. ``"rtl_tcp"``).

    Search order:
    1. System PATH  (``shutil.which``)
    2. Project directory  (same folder as ``api.py``)
    3. Current working directory

    On Windows the ``.exe`` suffix is appended automatically when needed.
    Returns the full path to the executable, or *None*.
    """
    # 1. System PATH
    found = _shutil.which(name)
    if found:
        return found

    # On Windows also try with .exe suffix explicitly
    if sys.platform == "win32" and not name.endswith(".exe"):
        found = _shutil.which(name + ".exe")
        if found:
            return found

    # 2. Project directory (next to api.py)
    for candidate in (name, name + ".exe") if sys.platform == "win32" else (name,):
        p = os.path.join(_PROJECT_DIR, candidate)
        if os.path.isfile(p) and (sys.platform == "win32" or os.access(p, os.X_OK)):
            return p

    # 3. Current working directory (if different from project dir)
    cwd = os.getcwd()
    if os.path.abspath(cwd) != os.path.abspath(_PROJECT_DIR):
        for candidate in (name, name + ".exe") if sys.platform == "win32" else (name,):
            p = os.path.join(cwd, candidate)
            if os.path.isfile(p) and (sys.platform == "win32" or os.access(p, os.X_OK)):
                return p

    return None


def _ensure_rtl_tcp(host: str = _RTL_TCP_DEFAULT_HOST,
                    port: int = _RTL_TCP_DEFAULT_PORT) -> bool:
    """Start a local ``rtl_tcp`` process if one is not already running.

    Returns *True* when an rtl_tcp server is reachable at *host*:*port*
    after this call (either it was already running, or we started it).

    On failure the global ``_RTL_TCP_LAST_ERROR`` is populated with stderr
    output from the process so callers can surface actionable diagnostics
    (e.g. USB driver / permission errors).
    """
    global _RTL_TCP_PROC, _RTL_TCP_LAST_ERROR

    # Already reachable? Nothing to do.
    if _check_rtl_tcp(host, port):
        _RTL_TCP_LAST_ERROR = None
        return True

    # Only attempt auto-start on localhost — we cannot start remote servers.
    if host not in ("127.0.0.1", "localhost", "::1"):
        return False

    # If we previously started a process that died, clean up the reference.
    if _RTL_TCP_PROC is not None and _RTL_TCP_PROC.poll() is not None:
        _RTL_TCP_PROC = None

    # Already have a managed process running.
    if _RTL_TCP_PROC is not None:
        return _check_rtl_tcp(host, port)

    exe = _find_rtl_tool("rtl_tcp")
    if not exe:
        return False

    try:
        logger.info("Auto-starting rtl_tcp: %s -a %s -p %s", exe, host, port)
        _RTL_TCP_PROC = _subprocess.Popen(
            [exe, "-a", host, "-p", str(port)],
            stdout=_subprocess.DEVNULL,
            stderr=_subprocess.PIPE,
        )
        # Give the server a moment to bind its port.
        for _ in range(10):
            time.sleep(0.3)
            if _check_rtl_tcp(host, port):
                logger.info("rtl_tcp auto-started successfully (PID %s)", _RTL_TCP_PROC.pid)
                _RTL_TCP_LAST_ERROR = None
                return True
            # If the process already exited, read stderr and stop waiting.
            if _RTL_TCP_PROC.poll() is not None:
                break

        # Process started but never became reachable — capture stderr.
        stderr_text = ""
        if _RTL_TCP_PROC.poll() is not None:
            # Process has exited — use communicate() which is safe against
            # pipe-buffer deadlocks and handles cleanup correctly.
            try:
                _, stderr_bytes = _RTL_TCP_PROC.communicate(timeout=2)
                stderr_text = (stderr_bytes or b"").decode("utf-8", errors="replace").strip()
            except Exception:
                pass
        if stderr_text:
            _RTL_TCP_LAST_ERROR = stderr_text
            logger.warning("rtl_tcp exited with error output:\n%s", stderr_text)
        else:
            _RTL_TCP_LAST_ERROR = "rtl_tcp started but not reachable within 3 s"
            logger.warning(_RTL_TCP_LAST_ERROR)
    except Exception as exc:
        _RTL_TCP_LAST_ERROR = str(exc)
        logger.warning("Failed to auto-start rtl_tcp: %s", exc)

    return _check_rtl_tcp(host, port)


def _is_usb_permission_error(error_text: Optional[str] = None) -> bool:
    """Return *True* if *error_text* (or ``_RTL_TCP_LAST_ERROR``) indicates a
    USB driver / permission problem (e.g. ``usb_open error -3``)."""
    text = (error_text or _RTL_TCP_LAST_ERROR or "").lower()
    return any(marker in text for marker in (
        "usb_open error",
        "failed to open rtlsdr",
        "device permissions",
        "udev rules",
        "libusb_open",
        "access denied",
    ))


def _stop_rtl_tcp_proc():
    """Terminate the auto-started rtl_tcp process (if any)."""
    global _RTL_TCP_PROC
    if _RTL_TCP_PROC is not None:
        try:
            _RTL_TCP_PROC.terminate()
            _RTL_TCP_PROC.wait(timeout=5)
            logger.info("rtl_tcp auto-started process terminated")
        except Exception as exc:
            logger.warning("Error stopping rtl_tcp: %s", exc)
        _RTL_TCP_PROC = None


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
    if not devices and _find_rtl_tool("rtl_test"):
        devices.append({"index": 0, "name": "RTL-SDR (rtl-sdr tools detected)", "source": "rtl_tools", "available": True})

    return devices


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

    All paths are serialised by _SDR_LOCK because rtl_tcp (and the USB dongle)
    can only serve one client at a time.  Without the lock, concurrent requests
    race to open connections, causing rtl_tcp to kill previous sessions and
    produce "comm recv bye / Signal caught" restart loops.
    """
    if not _SDR_LOCK.acquire(timeout=10):
        raise HTTPException(
            status_code=503,
            detail="SDR hardware is busy – another measurement is in progress. Try again shortly.",
        )
    try:
        return _get_spectrum_data_locked(
            center_freq_hz, sample_rate_hz, gain, nfft, rtl_tcp_host, rtl_tcp_port
        )
    finally:
        _SDR_LOCK.release()


def _get_spectrum_data_locked(
    center_freq_hz: float,
    sample_rate_hz: float,
    gain: float,
    nfft: int,
    rtl_tcp_host: str = _RTL_TCP_DEFAULT_HOST,
    rtl_tcp_port: int = _RTL_TCP_DEFAULT_PORT,
) -> dict:
    """Inner acquisition – must only be called while holding _SDR_LOCK."""
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
    _rtl_power_exe = _find_rtl_tool("rtl_power")
    if _rtl_power_exe:
        try:
            freq_start = int(center_freq_hz - sample_rate_hz / 2)
            freq_end = int(center_freq_hz + sample_rate_hz / 2)
            step = max(1, int(sample_rate_hz / nfft))
            result = _subprocess.run(
                [_rtl_power_exe, "-f", f"{freq_start}:{freq_end}:{step}",
                 "-g", str(int(gain)), "-e", "1s", os.devnull],
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
    # Auto-start rtl_tcp if the executable exists locally but is not yet running.
    _ensure_rtl_tcp(rtl_tcp_host, rtl_tcp_port)
    try:
        result = _get_spectrum_rtl_tcp(
            center_freq_hz, sample_rate_hz, gain, nfft, rtl_tcp_host, rtl_tcp_port
        )
        # Small settling delay: give the dongle time to release before the next
        # caller connects.  Without this, back-to-back connections may arrive
        # before rtl_tcp has fully recycled, causing it to crash.
        time.sleep(0.15)
        return result
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
            "rtl_power": bool(_find_rtl_tool("rtl_power")),
            "rtl_test":  bool(_find_rtl_tool("rtl_test")),
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
        "cli_rtl_power":   bool(_find_rtl_tool("rtl_power")),
        "cli_rtl_test":    bool(_find_rtl_tool("rtl_test")),
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
    has_local_driver = _RTLSDR_LIB or bool(_find_rtl_tool("rtl_power")) or bool(_find_rtl_tool("rtl_test"))

    # Try to reach an existing rtl_tcp, or auto-start one if the binary is
    # available locally (common Windows setup: rtl_tcp.exe next to api.py).
    rtl_tcp_ok = _check_rtl_tcp(tcp_host, tcp_port)
    if not rtl_tcp_ok and (hw_available or _find_rtl_tool("rtl_tcp")):
        rtl_tcp_ok = _ensure_rtl_tcp(tcp_host, tcp_port)

    if not hw_available and not rtl_tcp_ok:
        # Surface the underlying USB/driver error when available.
        usb_err = _is_usb_permission_error()
        if usb_err and _RTL_TCP_LAST_ERROR:
            detail = (
                "RTL-SDR USB device found but cannot be opened (driver/permission issue).\n"
                "rtl_tcp error: " + _RTL_TCP_LAST_ERROR + "\n\n"
                "Fix:\n"
                "  Windows → install WinUSB driver with Zadig (https://zadig.akeo.ie/)\n"
                "  Linux   → install udev rules:  sudo apt install rtl-sdr\n"
                "See the 'Windows Setup Guide' section on the SDR page for step-by-step instructions."
            )
        else:
            detail = (
                "No RTL-SDR hardware detected and rtl_tcp is not reachable.\n"
                "Options:\n"
                "  1. Install driver:  pip install pyrtlsdr numpy\n"
                "  2. Install rtl-sdr: sudo apt install rtl-sdr\n"
                "  3. Start rtl_tcp:   rtl_tcp -a 0.0.0.0  (then set host/port in the UI)"
            )
        raise HTTPException(status_code=503, detail=detail)

    # Detect USB permission error when rtl_tcp failed to start — e.g. hardware
    # visible via lsusb/pyserial (hw_available=True) but rtl_tcp could not open
    # it due to a missing WinUSB driver.  When rtl_tcp is connected successfully
    # there is no USB error to report.
    usb_permission_error = _is_usb_permission_error() if not rtl_tcp_ok else False

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
        "usb_permission_error": usb_permission_error,
        "rtl_tcp_error":     _RTL_TCP_LAST_ERROR if not rtl_tcp_ok else None,
        "capabilities": {
            "pyrtlsdr":  _RTLSDR_LIB,
            "numpy":     _NUMPY_LIB,
            "rtl_power": bool(_find_rtl_tool("rtl_power")),
            "rtl_tcp":   rtl_tcp_ok,
        },
    }


@app.post("/api/sdr/scan_rtl_tcp", summary="Scan for rtl_tcp servers")
def sdr_scan_rtl_tcp(data: dict = Body(default={})):
    """
    Scan one or more hosts for a running rtl_tcp server on common ports.

    Body fields (all optional):
    - hosts (list[str])  — IP addresses / hostnames to probe
                           (default: ["127.0.0.1", local-network broadcast scan])
    - ports (list[int])  — TCP ports to try (default: [1234, 1235, 1236])
    - timeout (float)    — per-probe timeout in seconds (default 0.8)

    Returns:
    - results (list)     — each entry {host, port, reachable, magic_ok}
    - found  (list)      — entries where a valid rtl_tcp server was detected
    """
    default_ports = [1234, 1235, 1236, 28321]
    hosts   = data.get("hosts") or ["127.0.0.1"]
    ports   = data.get("ports") or default_ports
    timeout = float(data.get("timeout", 0.8))

    # Ensure ports are ints
    ports = [int(p) for p in ports]

    results = []
    found   = []

    for host in hosts:
        for port in ports:
            entry = {"host": host, "port": port, "reachable": False, "magic_ok": False}
            try:
                with _socket.create_connection((host, port), timeout=timeout) as sock:
                    entry["reachable"] = True
                    header = b""
                    while len(header) < 12:
                        chunk = sock.recv(12 - len(header))
                        if not chunk:
                            break
                        header += chunk
                    if len(header) >= 4 and header.startswith(b"RTL0"):
                        entry["magic_ok"] = True
                        found.append({"host": host, "port": port})
            except Exception:
                pass
            results.append(entry)

    return {"results": results, "found": found}


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
            "Download rtl-sdr tools from https://github.com/rtlsdrblog/rtl-sdr-blog/releases "
            "and place rtl_tcp.exe next to api.py (or add to PATH). "
            "Use Zadig (https://zadig.akeo.ie/) to install the WinUSB driver for the RTL-SDR device."
        )
        rtl_sdr_install = (
            "Download rtl-sdr tools from https://github.com/rtlsdrblog/rtl-sdr-blog/releases "
            "and place the executables next to api.py (or add to PATH)"
        )
    else:
        rtl_tcp_install = "sudo apt install rtl-sdr  (Debian/Ubuntu/Raspberry Pi)"
        rtl_sdr_install = "sudo apt install rtl-sdr  (Debian/Ubuntu/Raspberry Pi)"

    system_deps = [
        {
            "name": "rtl_tcp",
            "present": bool(_find_rtl_tool("rtl_tcp")),
            "install": rtl_tcp_install,
            "description": (
                "RTL-SDR TCP server — required to stream SDR data over TCP. "
                "Start with: rtl_tcp -a 0.0.0.0"
            ),
        },
        {
            "name": "rtl_power",
            "present": bool(_find_rtl_tool("rtl_power")),
            "install": rtl_sdr_install,
            "description": "RTL-SDR power sweep tool (optional, used for spectrum scan)",
        },
        {
            "name": "rtl_test",
            "present": bool(_find_rtl_tool("rtl_test")),
            "install": rtl_sdr_install,
            "description": "RTL-SDR test/detection tool (optional)",
        },
        {
            "name": "rtl_fm",
            "present": bool(_find_rtl_tool("rtl_fm")),
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
    _rtl_fm_exe = _find_rtl_tool("rtl_fm")
    if _rtl_fm_exe:
        _mode_map = {
            "fm": "fm", "am": "am",
            "ssb": "usb", "usb": "usb", "lsb": "lsb", "wbfm": "wbfm",
        }
        rtl_mode = _mode_map.get(mode_lower, "fm")
        cmd = [
            _rtl_fm_exe,
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

# ===========================================================================
# Federation API  (/api/federation/*)
# ===========================================================================
# Implements ATAK-inspired server federation:
#   1. Each server has an RSA key pair generated at startup.
#   2. Servers exchange public keys via QR code or REST.
#   3. A challenge/response handshake establishes mutual trust.
#   4. Only *trusted* peers participate in data synchronisation.
# ===========================================================================

# Use the canonical constant from federation.py; fall back to 120 s if module unavailable
_CHALLENGE_EXPIRE_SECONDS = _FED_CHALLENGE_EXPIRE_SECONDS if FEDERATION_AVAILABLE else 120


def _require_federation(db: Session = None):
    """Raise 503 if federation module is not available."""
    if not FEDERATION_AVAILABLE:
        raise HTTPException(status_code=503, detail="Federation module not available")


def _fed_configured_url() -> str:
    """Return the user-configured public federation URL, or empty string."""
    cfg = load_json("config") or {}
    return cfg.get("federation_own_url", "")


def _fed_local_server_info() -> dict:
    """Return this server's federation info with configured public URL."""
    return _fed_get_server_info(base_path, url=_fed_configured_url())


# ---------------------------------------------------------------------------
# GET /api/federation/info  – local server identity (public key + metadata)
# ---------------------------------------------------------------------------
@app.get("/api/federation/info", tags=["Federation"])
def federation_info(current_user: dict = Depends(get_current_user)):
    """
    Return this server's public key and metadata.
    Clients (and peer servers) use this to register this server in their registry.
    """
    _require_federation()
    try:
        info = _fed_local_server_info()
        return JSONResponse(content=info)
    except Exception as exc:
        logger.error("federation_info error: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


# ---------------------------------------------------------------------------
# GET /api/federation/qr  – QR code PNG encoding local server info
# ---------------------------------------------------------------------------
@app.get("/api/federation/qr", tags=["Federation"])
def federation_qr(current_user: dict = Depends(get_current_user)):
    """
    Return a QR code PNG that encodes this server's federation info.
    Scan this with another LPU5 server (or the admin UI) to onboard it.
    """
    _require_federation()
    try:
        info = _fed_local_server_info()
        png_bytes = _fed_make_qr_png(info)
        return Response(content=png_bytes, media_type="image/png")
    except Exception as exc:
        logger.error("federation_qr error: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


# ---------------------------------------------------------------------------
# POST /api/federation/servers  – register a remote server
# ---------------------------------------------------------------------------
@app.post("/api/federation/servers", tags=["Federation"])
def federation_register_server(
    body: Dict[str, Any] = Body(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """
    Register a remote LPU5 server in the local registry.

    Body fields
    -----------
    server_id   : str  – UUID of the remote server
    name        : str  – human-readable label
    public_key  : str  – PEM-encoded RSA public key
    url         : str  – (optional) base URL of the remote server
    meta        : dict – (optional) additional metadata
    """
    _require_federation()

    server_id = body.get("server_id", "")
    name = body.get("name", "")
    public_key_pem = body.get("public_key", "")
    url = body.get("url", "")
    meta = body.get("meta", {})

    if not server_id or not name or not public_key_pem:
        raise HTTPException(status_code=422, detail="server_id, name, and public_key are required")

    # Validate public key
    try:
        fingerprint = _fed_fingerprint(public_key_pem)
    except Exception:
        raise HTTPException(status_code=422, detail="Invalid public_key PEM")

    # Check for duplicates (by server_id)
    existing = db.query(FederatedServer).filter_by(server_id=server_id).first()
    if existing:
        # Update public key / metadata
        existing.name = name
        existing.public_key_pem = public_key_pem
        existing.fingerprint = fingerprint
        existing.url = url
        existing.meta = meta
        db.commit()
        log_audit("federation_server_updated", current_user.get("username", "?"),
                  {"server_id": server_id, "name": name})
        return JSONResponse(content={
            "status": "updated",
            "id": existing.id,
            "server_id": server_id,
            "fingerprint": fingerprint,
            "trusted": existing.trusted,
        })

    entry = FederatedServer(
        name=name,
        server_id=server_id,
        public_key_pem=public_key_pem,
        fingerprint=fingerprint,
        url=url,
        meta=meta,
        trusted=False,
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    log_audit("federation_server_registered", current_user.get("username", "?"),
              {"server_id": server_id, "name": name})

    # Trigger automatic bidirectional handshake in the background if URL is present
    if url:
        threading.Thread(
            target=_federation_auto_handshake,
            args=(url, server_id),
            daemon=True,
            name=f"fed-handshake-{server_id[:8]}",
        ).start()
        logger.info("federation_register_server: auto-handshake started for %s", server_id)

    return JSONResponse(status_code=201, content={
        "status": "registered",
        "id": entry.id,
        "server_id": server_id,
        "fingerprint": fingerprint,
        "trusted": False,
    })


# ---------------------------------------------------------------------------
# GET /api/federation/servers  – list registered servers
# ---------------------------------------------------------------------------
@app.get("/api/federation/servers", tags=["Federation"])
def federation_list_servers(
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """List all servers in the federation registry."""
    _require_federation()
    rows = db.query(FederatedServer).order_by(FederatedServer.registered_at.desc()).all()
    return JSONResponse(content=[
        {
            "id": r.id,
            "server_id": r.server_id,
            "name": r.name,
            "url": r.url,
            "fingerprint": r.fingerprint,
            "trusted": r.trusted,
            "registered_at": r.registered_at.isoformat() if r.registered_at else None,
            "last_seen": r.last_seen.isoformat() if r.last_seen else None,
            "meta": r.meta,
        }
        for r in rows
    ])


# ---------------------------------------------------------------------------
# GET /api/federation/servers/{server_id}  – single server details
# ---------------------------------------------------------------------------
@app.get("/api/federation/servers/{server_id}", tags=["Federation"])
def federation_get_server(
    server_id: str,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """Get details of a specific federated server (by its UUID server_id)."""
    _require_federation()
    row = db.query(FederatedServer).filter_by(server_id=server_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Server not found")
    return JSONResponse(content={
        "id": row.id,
        "server_id": row.server_id,
        "name": row.name,
        "url": row.url,
        "public_key": row.public_key_pem,
        "fingerprint": row.fingerprint,
        "trusted": row.trusted,
        "registered_at": row.registered_at.isoformat() if row.registered_at else None,
        "last_seen": row.last_seen.isoformat() if row.last_seen else None,
        "meta": row.meta,
    })


# ---------------------------------------------------------------------------
# DELETE /api/federation/servers/{server_id}  – remove a server
# ---------------------------------------------------------------------------
@app.delete("/api/federation/servers/{server_id}", tags=["Federation"])
def federation_delete_server(
    server_id: str,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """Remove a server from the federation registry."""
    _require_federation()
    row = db.query(FederatedServer).filter_by(server_id=server_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Server not found")
    db.delete(row)
    db.commit()
    log_audit("federation_server_deleted", current_user.get("username", "?"),
              {"server_id": server_id})
    return JSONResponse(content={"status": "deleted", "server_id": server_id})


# ---------------------------------------------------------------------------
# POST /api/federation/servers/{server_id}/auto-handshake  – trigger auto-handshake
# ---------------------------------------------------------------------------
@app.post("/api/federation/servers/{server_id}/auto-handshake", tags=["Federation"])
def federation_trigger_auto_handshake(
    server_id: str,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """
    Manually trigger the automatic bidirectional handshake with a registered
    peer server.  The peer's URL must be set.

    This performs the same connect-back handshake that runs automatically
    when a new server is registered via ``POST /api/federation/servers``.
    """
    _require_federation()
    row = db.query(FederatedServer).filter_by(server_id=server_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Server not found")
    if not row.url:
        raise HTTPException(status_code=422, detail="Server has no URL configured – cannot connect")

    threading.Thread(
        target=_federation_auto_handshake,
        args=(row.url, server_id),
        daemon=True,
        name=f"fed-handshake-{server_id[:8]}",
    ).start()
    return JSONResponse(content={
        "status": "handshake_started",
        "server_id": server_id,
        "peer_url": row.url,
    })


# ---------------------------------------------------------------------------
# POST /api/federation/servers/{server_id}/challenge  – issue a challenge
# ---------------------------------------------------------------------------
@app.post("/api/federation/servers/{server_id}/challenge", tags=["Federation"])
def federation_issue_challenge(
    server_id: str,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """
    Issue a cryptographic challenge to a registered peer server.

    The peer must call POST /api/federation/handshake/respond (on *this*
    server) – or the equivalent on the remote – with the signed challenge to
    complete the handshake and become trusted.

    Returns the challenge_id and base64-encoded challenge bytes.
    """
    _require_federation()
    row = db.query(FederatedServer).filter_by(server_id=server_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Server not found")

    challenge_b64 = _fed_generate_challenge()
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=_CHALLENGE_EXPIRE_SECONDS)
    ch = FederationChallenge(
        federated_server_id=row.id,
        challenge_b64=challenge_b64,
        expires_at=expires_at,
    )
    db.add(ch)
    db.commit()
    db.refresh(ch)
    return JSONResponse(content={
        "challenge_id": ch.id,
        "challenge": challenge_b64,
        "expires_at": expires_at.isoformat(),
    })


# ---------------------------------------------------------------------------
# POST /api/federation/servers/{server_id}/verify  – verify challenge response
# ---------------------------------------------------------------------------
@app.post("/api/federation/servers/{server_id}/verify", tags=["Federation"])
def federation_verify_challenge(
    server_id: str,
    body: Dict[str, Any] = Body(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """
    Verify the peer's signed response to a challenge.

    Body fields
    -----------
    challenge_id : str – ID returned by the /challenge endpoint
    signature    : str – base64-encoded RSA-PKCS#1v1.5-SHA256 signature

    On success the server's *trusted* flag is set to True.
    """
    _require_federation()
    row = db.query(FederatedServer).filter_by(server_id=server_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Server not found")

    challenge_id = body.get("challenge_id", "")
    signature_b64 = body.get("signature", "")
    if not challenge_id or not signature_b64:
        raise HTTPException(status_code=422, detail="challenge_id and signature are required")

    ch = db.query(FederationChallenge).filter_by(id=challenge_id, federated_server_id=row.id).first()
    if not ch:
        raise HTTPException(status_code=404, detail="Challenge not found")
    if ch.used:
        raise HTTPException(status_code=409, detail="Challenge already used")
    if datetime.now(timezone.utc) > ch.expires_at.replace(tzinfo=timezone.utc):
        raise HTTPException(status_code=410, detail="Challenge expired")

    valid = _fed_verify_signature(ch.challenge_b64, signature_b64, row.public_key_pem)
    ch.used = True
    db.commit()

    if not valid:
        log_audit("federation_handshake_failed", current_user.get("username", "?"),
                  {"server_id": server_id})
        raise HTTPException(status_code=403, detail="Signature verification failed")

    row.trusted = True
    row.last_seen = datetime.now(timezone.utc)
    db.commit()
    log_audit("federation_handshake_success", current_user.get("username", "?"),
              {"server_id": server_id, "name": row.name})
    return JSONResponse(content={
        "status": "trusted",
        "server_id": server_id,
        "name": row.name,
    })


# ---------------------------------------------------------------------------
# POST /api/federation/handshake/respond  – respond to a challenge from a peer
# ---------------------------------------------------------------------------
@app.post("/api/federation/handshake/respond", tags=["Federation"])
def federation_respond_to_challenge(
    body: Dict[str, Any] = Body(...),
):
    """
    Sign a challenge received from a peer server using this server's private key.

    No JWT required – peer servers call this endpoint during manual handshake
    and do not possess a local JWT.  Trust is established through RSA key
    verification (the caller verifies the returned signature against the
    public key obtained during server registration).

    Body fields
    -----------
    challenge : str – base64-encoded challenge bytes (from peer's /challenge endpoint)

    Returns the base64-encoded signature that should be sent back to the peer.
    """
    _require_federation()
    challenge_b64 = body.get("challenge", "")
    if not challenge_b64:
        raise HTTPException(status_code=422, detail="challenge is required")
    try:
        private_key, _ = _fed_load_keypair(base_path)
        signature_b64 = _fed_sign_challenge(challenge_b64, private_key)
    except Exception as exc:
        logger.error("federation_respond_to_challenge error: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))
    return JSONResponse(content={"signature": signature_b64})


# ---------------------------------------------------------------------------
# POST /api/federation/sync  – push local data to all trusted peers
# ---------------------------------------------------------------------------
@app.post("/api/federation/sync", tags=["Federation"])
def federation_sync(
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """
    Trigger synchronisation of local map markers to all trusted federated
    servers.  Each trusted peer receives a signed payload containing the
    current marker list.  Untrusted servers are skipped.

    Returns a summary of sync results per peer.
    """
    _require_federation()

    trusted_peers = db.query(FederatedServer).filter_by(trusted=True).all()
    if not trusted_peers:
        return JSONResponse(content={"status": "ok", "synced": 0, "results": []})

    # Build local marker payload
    markers = db.query(MapMarker).all()
    marker_list = [
        {
            "id": m.id, "name": m.name, "lat": m.lat, "lng": m.lng,
            "type": m.type, "color": m.color, "icon": m.icon,
            "description": m.description,
        }
        for m in markers
    ]

    # Sign the payload with our private key
    try:
        private_key, _ = _fed_load_keypair(base_path)
        local_info = _fed_local_server_info()
        payload_str = json.dumps(marker_list, separators=(",", ":"), sort_keys=True)
        payload_challenge = base64.b64encode(payload_str.encode()).decode()
        signature_b64 = _fed_sign_challenge(payload_challenge, private_key)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Signing error: {exc}")

    results = []
    for peer in trusted_peers:
        if not peer.url:
            results.append({"server_id": peer.server_id, "name": peer.name, "status": "skipped", "reason": "no URL configured"})
            continue
        try:
            resp = requests.post(
                f"{peer.url.rstrip('/')}/api/federation/ingest",
                json={
                    "source_server_id": local_info["server_id"],
                    "markers": marker_list,
                    "signature": signature_b64,
                    "payload_b64": payload_challenge,
                },
                timeout=10,
            )
            peer.last_seen = datetime.now(timezone.utc)
            db.commit()
            results.append({
                "server_id": peer.server_id,
                "name": peer.name,
                "status": "ok" if resp.status_code < 300 else "error",
                "http_status": resp.status_code,
            })
        except Exception as exc:
            results.append({"server_id": peer.server_id, "name": peer.name, "status": "error", "reason": str(exc)})

    log_audit("federation_sync", current_user.get("username", "?"),
              {"synced_peers": len([r for r in results if r.get("status") == "ok"])})
    return JSONResponse(content={"status": "ok", "synced": len(trusted_peers), "results": results})


# ---------------------------------------------------------------------------
# POST /api/federation/ingest  – receive data from a trusted peer
# ---------------------------------------------------------------------------
@app.post("/api/federation/ingest", tags=["Federation"])
def federation_ingest(
    body: Dict[str, Any] = Body(...),
    db: Session = Depends(get_db),
):
    """
    Receive and validate a signed data payload from a trusted federated peer.
    No user authentication is required here; trust is established via RSA
    signature verification against the sender's registered public key.

    Body fields
    -----------
    source_server_id : str  – sender's UUID
    markers          : list – list of marker dicts
    signature        : str  – base64-encoded RSA signature of payload_b64
    payload_b64      : str  – base64-encoded JSON payload that was signed
    """
    _require_federation()
    source_id = body.get("source_server_id", "")
    signature_b64 = body.get("signature", "")
    payload_b64 = body.get("payload_b64", "")
    markers = body.get("markers", [])

    if not source_id or not signature_b64 or not payload_b64:
        raise HTTPException(status_code=422, detail="source_server_id, signature, and payload_b64 are required")

    peer = db.query(FederatedServer).filter_by(server_id=source_id, trusted=True).first()
    if not peer:
        raise HTTPException(status_code=403, detail="Source server not registered or not trusted")

    if not _fed_verify_signature(payload_b64, signature_b64, peer.public_key_pem):
        log_audit("federation_ingest_rejected", "federation",
                  {"source_server_id": source_id, "reason": "invalid signature"})
        raise HTTPException(status_code=403, detail="Signature verification failed")

    peer.last_seen = datetime.now(timezone.utc)
    db.commit()
    log_audit("federation_ingest_accepted", "federation",
              {"source_server_id": source_id, "markers_received": len(markers)})
    return JSONResponse(content={
        "status": "accepted",
        "source_server_id": source_id,
        "markers_received": len(markers),
    })


# ---------------------------------------------------------------------------
# Automatic federation sync worker
# ---------------------------------------------------------------------------
_FEDERATION_SYNC_THREAD = None
_FEDERATION_SYNC_STOP_EVENT = threading.Event()
_FEDERATION_SYNC_LAST_RUN = None
_FEDERATION_SYNC_LAST_RESULT = None


def _federation_sync_worker(interval_seconds: int = 300):
    """
    Background worker that periodically pushes local map markers to all
    trusted federated peers.  Mirrors the manual POST /api/federation/sync
    logic but runs automatically in a daemon thread.
    """
    global _FEDERATION_SYNC_LAST_RUN, _FEDERATION_SYNC_LAST_RESULT
    while not _FEDERATION_SYNC_STOP_EVENT.is_set():
        # Sleep first, then sync (gives server time to fully start)
        _FEDERATION_SYNC_STOP_EVENT.wait(interval_seconds)
        if _FEDERATION_SYNC_STOP_EVENT.is_set():
            break

        db = SessionLocal()
        try:
            if not FEDERATION_AVAILABLE:
                continue

            trusted_peers = db.query(FederatedServer).filter_by(trusted=True).all()
            if not trusted_peers:
                _FEDERATION_SYNC_LAST_RUN = datetime.now(timezone.utc).isoformat()
                _FEDERATION_SYNC_LAST_RESULT = {"synced": 0, "results": []}
                continue

            markers = db.query(MapMarker).all()
            marker_list = [
                {
                    "id": m.id, "name": m.name, "lat": m.lat, "lng": m.lng,
                    "type": m.type, "color": m.color, "icon": m.icon,
                    "description": m.description,
                }
                for m in markers
            ]

            private_key, _ = _fed_load_keypair(base_path)
            local_info = _fed_local_server_info()
            payload_str = json.dumps(marker_list, separators=(",", ":"), sort_keys=True)
            payload_challenge = base64.b64encode(payload_str.encode()).decode()
            signature_b64 = _fed_sign_challenge(payload_challenge, private_key)

            results = []
            for peer in trusted_peers:
                if not peer.url:
                    results.append({"server_id": peer.server_id, "name": peer.name,
                                    "status": "skipped", "reason": "no URL configured"})
                    continue
                try:
                    resp = requests.post(
                        f"{peer.url.rstrip('/')}/api/federation/ingest",
                        json={
                            "source_server_id": local_info["server_id"],
                            "markers": marker_list,
                            "signature": signature_b64,
                            "payload_b64": payload_challenge,
                        },
                        timeout=10,
                    )
                    peer.last_seen = datetime.now(timezone.utc)
                    db.commit()
                    results.append({
                        "server_id": peer.server_id, "name": peer.name,
                        "status": "ok" if resp.status_code < 300 else "error",
                        "http_status": resp.status_code,
                    })
                except Exception as exc:
                    results.append({"server_id": peer.server_id, "name": peer.name,
                                    "status": "error", "reason": str(exc)})

            _FEDERATION_SYNC_LAST_RUN = datetime.now(timezone.utc).isoformat()
            _FEDERATION_SYNC_LAST_RESULT = {
                "synced": len(trusted_peers),
                "ok": len([r for r in results if r.get("status") == "ok"]),
                "results": results,
            }
            logger.info("Federation auto-sync completed: %d peers, %d ok",
                        _FEDERATION_SYNC_LAST_RESULT["synced"],
                        _FEDERATION_SYNC_LAST_RESULT["ok"])
        except Exception as exc:
            logger.warning("Federation auto-sync error: %s", exc)
            _FEDERATION_SYNC_LAST_RUN = datetime.now(timezone.utc).isoformat()
            _FEDERATION_SYNC_LAST_RESULT = {"error": str(exc)}
        finally:
            db.close()
    logger.info("Federation auto-sync worker stopped")


# ---------------------------------------------------------------------------
# GET /api/federation/sync/status – auto-sync status
# ---------------------------------------------------------------------------
@app.get("/api/federation/sync/status", tags=["Federation"])
def federation_sync_status(current_user: dict = Depends(get_current_user)):
    """Return the current status of the automatic federation sync worker."""
    running = _FEDERATION_SYNC_THREAD is not None and _FEDERATION_SYNC_THREAD.is_alive()
    try:
        cfg = load_json("config") or {}
    except Exception:
        cfg = {}
    return JSONResponse(content={
        "auto_sync_enabled": cfg.get("federation_auto_sync", True),
        "interval_seconds": cfg.get("federation_sync_interval_seconds", 300),
        "running": running,
        "last_run": _FEDERATION_SYNC_LAST_RUN,
        "last_result": _FEDERATION_SYNC_LAST_RESULT,
    })


# ---------------------------------------------------------------------------
# Automatic federation handshake  (connect-back via TCP to port 8101)
# ---------------------------------------------------------------------------
# When Server B registers Server A (e.g. via QR code), Server A might not
# know about Server B yet.  The automatic handshake solves this:
#   1. Server B calls Server A's /api/federation/handshake/init with its own
#      server info **and** a challenge for A to sign.
#   2. Server A registers B (if unknown), signs the challenge, creates a
#      counter-challenge for B, and returns everything in one response.
#   3. Server B verifies A's signature → marks A as trusted.
#   4. Server B signs A's counter-challenge and calls
#      /api/federation/handshake/complete on A.
#   5. Server A verifies B's counter-signature → marks B as trusted.
# Both endpoints are unauthenticated (like /ingest) because trust is proven
# through RSA signatures – the caller must possess the correct private key.
# ---------------------------------------------------------------------------


@app.post("/api/federation/handshake/init", tags=["Federation"])
def federation_handshake_init(
    body: Dict[str, Any] = Body(...),
    db: Session = Depends(get_db),
):
    """
    Phase 1 of the automatic mutual handshake.

    A remote server calls this endpoint to:
      * register itself on the local server (if not yet known),
      * present a challenge for the local server to sign, and
      * receive a counter-challenge to sign in return.

    No JWT required – trust is established through RSA key verification.

    Body fields
    -----------
    server_info : dict – remote server metadata
        server_id   : str  – UUID
        name        : str  – human-readable label
        public_key  : str  – PEM-encoded RSA public key
        url         : str  – (optional) base URL of the remote server
    challenge   : str – base64-encoded random bytes for the local server to sign

    Returns
    -------
    {
        server_info      : dict   – *this* server's metadata (id, name, key, url …),
        signature        : str    – local signature of the incoming challenge,
        counter_challenge: str    – base64-encoded random bytes for the caller to sign,
    }
    """
    _require_federation()

    remote = body.get("server_info") or {}
    remote_sid = remote.get("server_id", "")
    remote_name = remote.get("name", "")
    remote_pk_pem = remote.get("public_key", "")
    remote_url = remote.get("url", "")
    challenge_b64 = body.get("challenge", "")

    if not remote_sid or not remote_name or not remote_pk_pem or not challenge_b64:
        raise HTTPException(
            status_code=422,
            detail="server_info (server_id, name, public_key) and challenge are required",
        )

    # Validate remote public key
    try:
        fingerprint = _fed_fingerprint(remote_pk_pem)
    except Exception:
        raise HTTPException(status_code=422, detail="Invalid public_key PEM")

    # Register or update remote server ----------------------------------
    existing = db.query(FederatedServer).filter_by(server_id=remote_sid).first()
    if existing:
        existing.name = remote_name
        existing.public_key_pem = remote_pk_pem
        existing.fingerprint = fingerprint
        if remote_url:
            existing.url = remote_url
        db.commit()
        logger.info("federation_handshake_init: updated existing peer %s", remote_sid)
    else:
        entry = FederatedServer(
            name=remote_name,
            server_id=remote_sid,
            public_key_pem=remote_pk_pem,
            fingerprint=fingerprint,
            url=remote_url,
            trusted=False,
        )
        db.add(entry)
        db.commit()
        db.refresh(entry)
        logger.info("federation_handshake_init: registered new peer %s", remote_sid)

    # Sign the incoming challenge with OUR private key -------------------
    try:
        private_key, _ = _fed_load_keypair(base_path)
        signature_b64 = _fed_sign_challenge(challenge_b64, private_key)
    except Exception as exc:
        logger.error("federation_handshake_init signing error: %s", exc)
        raise HTTPException(status_code=500, detail=f"Signing error: {exc}")

    # Create a counter-challenge for the remote server to sign -----------
    counter_challenge_b64 = _fed_generate_challenge()
    peer_row = db.query(FederatedServer).filter_by(server_id=remote_sid).first()
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=_CHALLENGE_EXPIRE_SECONDS)
    ch = FederationChallenge(
        federated_server_id=peer_row.id,
        challenge_b64=counter_challenge_b64,
        expires_at=expires_at,
    )
    db.add(ch)
    db.commit()
    db.refresh(ch)

    local_info = _fed_local_server_info()
    log_audit("federation_handshake_init", "federation",
              {"remote_server_id": remote_sid, "remote_name": remote_name})
    return JSONResponse(content={
        "server_info": local_info,
        "signature": signature_b64,
        "counter_challenge": counter_challenge_b64,
        "counter_challenge_id": ch.id,
    })


@app.post("/api/federation/handshake/complete", tags=["Federation"])
def federation_handshake_complete(
    body: Dict[str, Any] = Body(...),
    db: Session = Depends(get_db),
):
    """
    Phase 2 of the automatic mutual handshake.

    The initiating server calls this endpoint with the signed counter-challenge.
    On successful verification the remote server is marked as *trusted*.

    No JWT required – trust is established through RSA signature verification.

    Body fields
    -----------
    server_id              : str – UUID of the calling server
    counter_challenge_id   : str – ID returned by /handshake/init
    signature              : str – base64-encoded RSA signature of the counter-challenge

    Returns
    -------
    { status: "trusted", server_id: str }
    """
    _require_federation()

    remote_sid = body.get("server_id", "")
    cc_id = body.get("counter_challenge_id", "")
    signature_b64 = body.get("signature", "")

    if not remote_sid or not cc_id or not signature_b64:
        raise HTTPException(
            status_code=422,
            detail="server_id, counter_challenge_id, and signature are required",
        )

    peer = db.query(FederatedServer).filter_by(server_id=remote_sid).first()
    if not peer:
        raise HTTPException(status_code=404, detail="Server not found in registry")

    ch = db.query(FederationChallenge).filter_by(
        id=cc_id, federated_server_id=peer.id
    ).first()
    if not ch:
        raise HTTPException(status_code=404, detail="Counter-challenge not found")
    if ch.used:
        raise HTTPException(status_code=409, detail="Counter-challenge already used")
    if datetime.now(timezone.utc) > ch.expires_at.replace(tzinfo=timezone.utc):
        raise HTTPException(status_code=410, detail="Counter-challenge expired")

    valid = _fed_verify_signature(ch.challenge_b64, signature_b64, peer.public_key_pem)
    ch.used = True
    db.commit()

    if not valid:
        log_audit("federation_handshake_complete_failed", "federation",
                  {"server_id": remote_sid})
        raise HTTPException(status_code=403, detail="Signature verification failed")

    peer.trusted = True
    peer.last_seen = datetime.now(timezone.utc)
    db.commit()
    log_audit("federation_handshake_complete_success", "federation",
              {"server_id": remote_sid, "name": peer.name})
    return JSONResponse(content={
        "status": "trusted",
        "server_id": remote_sid,
        "name": peer.name,
    })


def _federation_auto_handshake(peer_url: str, peer_server_id: str):
    """
    Background task: perform a full bidirectional handshake with a remote
    server via TCP (HTTP on port 8101).

    Steps
    -----
    1. Send local server info + challenge  → peer's /api/federation/handshake/init
    2. Verify peer's signature             → mark peer as trusted locally
    3. Sign peer's counter-challenge       → send to peer's /api/federation/handshake/complete
    4. Peer verifies our signature         → peer marks us as trusted
    """
    logger.info("federation_auto_handshake: starting with %s (%s)", peer_server_id, peer_url)
    db = SessionLocal()
    try:
        if not FEDERATION_AVAILABLE:
            logger.warning("federation_auto_handshake: federation not available")
            return

        private_key, _ = _fed_load_keypair(base_path)
        local_info = _fed_local_server_info()
        challenge_b64 = _fed_generate_challenge()

        # Phase 1 – call peer's /handshake/init --------------------------------
        init_url = f"{peer_url.rstrip('/')}/api/federation/handshake/init"
        logger.info("federation_auto_handshake: calling %s", init_url)
        resp = requests.post(
            init_url,
            json={
                "server_info": local_info,
                "challenge": challenge_b64,
            },
            timeout=15,
        )
        if resp.status_code != 200:
            logger.warning(
                "federation_auto_handshake: init call failed HTTP %s – %s",
                resp.status_code, resp.text[:300],
            )
            return
        init_data = resp.json()

        # Verify peer's signature of our challenge -----------------------------
        peer_info = init_data.get("server_info", {})
        peer_pk_pem = peer_info.get("public_key", "")
        peer_signature = init_data.get("signature", "")
        counter_challenge = init_data.get("counter_challenge", "")
        counter_challenge_id = init_data.get("counter_challenge_id", "")

        if not peer_pk_pem or not peer_signature or not counter_challenge:
            logger.warning("federation_auto_handshake: incomplete init response")
            return

        valid = _fed_verify_signature(challenge_b64, peer_signature, peer_pk_pem)
        if not valid:
            logger.warning("federation_auto_handshake: peer signature invalid – aborting")
            return

        # Mark peer as trusted locally -----------------------------------------
        peer_row = db.query(FederatedServer).filter_by(server_id=peer_server_id).first()
        if peer_row:
            peer_row.trusted = True
            peer_row.last_seen = datetime.now(timezone.utc)
            db.commit()
            logger.info("federation_auto_handshake: peer %s marked trusted", peer_server_id)

        # Phase 2 – sign counter-challenge & send to peer's /handshake/complete
        counter_sig = _fed_sign_challenge(counter_challenge, private_key)
        complete_url = f"{peer_url.rstrip('/')}/api/federation/handshake/complete"
        logger.info("federation_auto_handshake: calling %s", complete_url)
        resp2 = requests.post(
            complete_url,
            json={
                "server_id": local_info["server_id"],
                "counter_challenge_id": counter_challenge_id,
                "signature": counter_sig,
            },
            timeout=15,
        )
        if resp2.status_code == 200:
            logger.info(
                "federation_auto_handshake: mutual trust established with %s",
                peer_server_id,
            )
        else:
            logger.warning(
                "federation_auto_handshake: complete call failed HTTP %s – %s",
                resp2.status_code, resp2.text[:300],
            )
    except Exception as exc:
        logger.warning("federation_auto_handshake error with %s: %s", peer_server_id, exc)
    finally:
        db.close()


# -------------------------
# Catch-all file fallback (MUST be the last route registered)
# -------------------------
@app.get("/{full_path:path}", include_in_schema=False)
def catch_all(full_path: str, request: Request):
    """
    Catch-all route for serving static HTML files.
    Merged HTML pages redirect to the unified SPA (index.html#view).
    Standalone pages (register.html, overview.html) are served directly.

    Note: This route does not interfere with WebSocket connections.
    With uvicorn[standard] installed, the WebSocket endpoint at /ws is properly
    handled by FastAPI's WebSocket route before this HTTP GET route is considered.
    The 'ws' prefix in blocked_prefixes ensures file system lookups don't occur for it.
    """
    blocked_prefixes = ("api", "static", "assets", "uploads", "_", "favicon.ico", "ws")
    if any(full_path == p or full_path.startswith(p + "/") for p in blocked_prefixes):
        raise HTTPException(status_code=404, detail="Not found")

    # Map of old HTML filenames to SPA view IDs (merged into index.html).
    # Keys use the original filenames on disk (casing preserved intentionally).
    # NOTE: stream_share.html and stream_share_N.html are intentionally excluded
    # from the SPA redirect – they are pure video viewers (no Global_nav) that
    # are loaded inside iframes and must be served as standalone HTML files.
    # NOTE: mission_Overview.html is intentionally excluded – it is a standalone
    # read-only overview of mission orders (no Global_nav) accessible via QR code.
    _SPA_VIEWS = {
        "landing.html": "landing", "admin.html": "admin",
        "admin_map.html": "admin_map", "mission.html": "mission",
        "statistics.html": "statistics", "meshtastic.html": "meshtastic",
        "import_nodes.html": "import_nodes", "network.html": "network",
        "SDR.html": "sdr", "stream.html": "stream",
        "global_Intel.html": "global_intel",
        "cot_monitor_ui.html": "cot_monitor", "language.html": "language",
        "admin_map_toolbar_icons.html": "admin_map_toolbar",
    }

    # Redirect merged pages to the SPA with the correct hash fragment.
    # Target must be /index.html#view (not /#view) because the root URL /
    # serves the standalone landing.html – the hash would be meaningless there.
    if full_path in _SPA_VIEWS:
        return RedirectResponse(url=f"/index.html#{_SPA_VIEWS[full_path]}", status_code=302)

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
            log_level="warning",
            ssl_certfile=cert_file,
            ssl_keyfile=key_file,
            timeout_keep_alive=300,
            timeout_graceful_shutdown=60,
            limit_concurrency=1000
        )
    else:
        uvicorn.run(
            app,
            host="0.0.0.0",
            port=8101,
            log_level="warning",
            timeout_keep_alive=300,
            timeout_graceful_shutdown=60
        )