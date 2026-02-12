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
from fastapi import FastAPI, Body, HTTPException, Request, Path, Header, WebSocket, WebSocketDisconnect, UploadFile, File, Form
from fastapi.responses import FileResponse, JSONResponse, Response, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from datetime import datetime, timedelta, timezone
import os
import json
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
import asyncio
import sys

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
from database import SessionLocal, engine, get_db
from models import User, MapMarker, Mission, MeshtasticNode, AutonomousRule, Geofence, ChatMessage, ChatChannel, AuditLog, Drawing, Overlay, APISession, UserGroup, QRCode, PendingRegistration
from sqlalchemy.orm import Session
from fastapi import Depends

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

app = FastAPI(title="LPU5 Tactical Tracker API", version="2.1.0")
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
            data_server_port=8002,
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
    # Try to broadcast via data server first (preferred method)
    if DATA_SERVER_AVAILABLE and data_server_manager and data_server_manager.is_running():
        try:
            data_server_manager.broadcast(channel, event_type, data)
            return
        except Exception as e:
            logger.warning(f"Failed to broadcast via data server, falling back to direct WebSocket: {e}")
    
    # Fallback to direct WebSocket (backward compatibility)
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


# -------------------------
# Background meshtastic -> markers sync
# -------------------------
_MESHTASTIC_SYNC_THREAD = None
_MESHTASTIC_SYNC_STOP_EVENT = threading.Event()
# created_by values used by meshtastic code paths — used to filter meshtastic markers from general endpoints
_MESHTASTIC_CREATED_BY = {"import_meshtastic", "meshtastic_sync", "ingest_node"}

def sync_meshtastic_nodes_to_map_markers_once():
    """
    One-shot sync: reads meshtastic_nodes from DB and upserts markers into map_markers table.
    Nodes without valid lat/lng will be assigned 0.0, 0.0 per requirement.
    """
    db = SessionLocal()
    try:
        nodes = db.query(MeshtasticNode).all()
        # Index existing markers created by meshtastic sync
        existing_markers = db.query(MapMarker).filter(MapMarker.created_by == "import_meshtastic").all()
        
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


@app.on_event("startup")
def on_startup():
    # Store reference to main event loop for thread-safe broadcasts
    import asyncio
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
    
    # Start data server process if available
    if DATA_SERVER_AVAILABLE and data_server_manager:
        try:
            logger.info("Starting data distribution server...")
            if data_server_manager.start(timeout=15):
                logger.info("✅ Data server started successfully")
                status = data_server_manager.get_status()
                if status:
                    logger.info(f"   Data server status: {status.get('status')}")
                    logger.info(f"   WebSocket: ws://127.0.0.1:8002/ws")
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
    
    logger.info("Startup complete. DB files ensured.")

@app.on_event("shutdown")
def on_shutdown():
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

    return {
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
            {"name": "admin", "level": 4, "description": "Voller Systemzugriff"},
            {"name": "operator", "level": 3, "description": "Missions- und Marker-Verwaltung"},
            {"name": "user", "level": 2, "description": "Standard-Benutzer mit Selbstaktualisierung"},
            {"name": "guest", "level": 1, "description": "Nur-Lese-Zugriff auf öffentliche Daten"}
        ]
    }

# -------------------------
# Users endpoints (create/update/list/delete)
# -------------------------
@app.get("/api/users")
async def get_users(db: Session = Depends(get_db)):
    users = db.query(User).all()
    return [
        {
            "id": u.id,
            "username": u.username,
            "email": u.email,
            "role": u.role,
            "group_id": u.group_id,
            "unit": u.unit,
            "device": u.device,
            "rank": u.rank,
            "fullname": u.fullname,
            "callsign": u.callsign,
            "is_active": u.is_active,
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
        raise HTTPException(status_code=400, detail="Benutzername erforderlich")
    if not password:
        raise HTTPException(status_code=400, detail="Passwort erforderlich")
    if len(password) < 6:
        raise HTTPException(status_code=400, detail="Passwort muss mindestens 6 Zeichen sein")
    
    if db.query(User).filter(User.username == username).first():
        raise HTTPException(status_code=400, detail="Benutzername existiert bereits")
    
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
    return {k: v for k, v in user.__dict__.items() if k != "password_hash" and not k.startswith("_")}

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
            else:
                 setattr(user, field, data[field])
    
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
        raise HTTPException(status_code=400, detail="Passwort muss mindestens 6 Zeichen sein")
    
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
    if not username or not password or not unit:
        raise HTTPException(status_code=400, detail="Required fields missing (username, password, device/unit)")
    
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
            "unit": unit,
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
    new_user = User(
        id=str(uuid.uuid4()),
        username=reg.username,
        password_hash=reg.password_hash,
        unit=reg_data.get("unit"),
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
                "objective": m.name,
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
            "objective": mission.name,
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
        if isinstance(t, datetime): return t
        if isinstance(t, (int, float)):
            return datetime.fromtimestamp(t if t < 1e12 else t / 1000)
        try: return datetime.fromisoformat(str(t).replace('Z', '+00:00'))
        except: return None
    
    start_dt = parse_time(mission.data.get("start_time") if mission.data else None) or mission.created_at
    end_dt = parse_time(mission.data.get("completed_at") if mission.data else None) or datetime.now(timezone.utc)
    
    # Load users from DB
    users = db.query(User).all()
    unit_stats = []
    
    for user in users:
        # History is stored in user.data.history
        history = (user.data or {}).get("history", [])
        if not isinstance(history, list) or len(history) == 0:
            continue
        
        # Filter history entries within mission window
        filtered_history = []
        for entry in history:
            entry_time = parse_time(entry.get("timestamp"))
            if entry_time and start_dt <= entry_time <= end_dt:
                filtered_history.append(entry)
                
        if not filtered_history:
            continue
            
        # Sort by timestamp
        filtered_history.sort(key=lambda x: parse_time(x.get("timestamp")) or datetime.min)
        
        # Calculate statistics
        status_counts = {}
        for entry in filtered_history:
            status = entry.get("status")
            if status:
                status_counts[status] = status_counts.get(status, 0) + 1
                
        unit_stats.append({
            "username": user.username,
            "fullname": user.fullname or user.username,
            "role": user.role,
            "history_count": len(filtered_history),
            "last_history": filtered_history[-1] if filtered_history else None,
            "status_distribution": status_counts,
            "history": filtered_history
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
            "type": new_marker.type,
            "color": new_marker.color,
            "icon": new_marker.icon,
            "created_by": new_marker.created_by,
            "timestamp": new_marker.created_at.isoformat() if new_marker.created_at else datetime.now(timezone.utc).isoformat(),
            "data": new_marker.data
        }
        
        # Broadcast marker update to all connected clients
        broadcast_websocket_update("markers", "marker_created", marker_dict)
        
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
            "type": marker.type,
            "color": marker.color,
            "icon": marker.icon,
            "created_by": marker.created_by,
            "timestamp": marker.created_at.isoformat() if marker.created_at else datetime.now(timezone.utc).isoformat(),
            "data": marker.data
        }
        
        log_audit("update_marker", current_username, {"marker_id": marker_id})
        broadcast_websocket_update("markers", "marker_updated", marker_dict)
        
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
            
        db.delete(marker)
        db.commit()
        
        log_audit("delete_marker", current_username, {"marker_id": marker_id})
        broadcast_websocket_update("markers", "marker_deleted", {"id": marker_id})
        broadcast_websocket_update("symbols", "symbol_deleted", {"id": marker_id})
        
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
            # Broadcast CoT events to subscribed clients
            if websocket_manager:
                for cot_event in data["cot_events"]:
                    await websocket_manager.publish_to_channel('cot', {
                        'type': 'cot_event',
                        'event': cot_event,
                        'timestamp': timestamp
                    })
        
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
    global _active_meshtastic_connection, _active_meshtastic_port
    
    port = data.get("port")
    if not port:
        raise HTTPException(status_code=400, detail="Port parameter is required")
    
    if not meshtastic:
        logger.error(f"[Port:{port}] Meshtastic library not available on server")
        raise HTTPException(status_code=503, detail="Meshtastic library not available on server")
    
    logger.info(f"[Port:{port}] === Persistent connection request ===")
    
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
                type="friendly",
                created_by="ingest_node",
                data={"unit_id": existing_node.id, "hardware": existing_node.hardware_model}
            )
            db.add(marker)
        
        db.commit()
        db.refresh(existing_node)
        
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
        "port": 8001,
        "base_url": f"{protocol}://{local_ip}:8001",
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
            qr_url = f"{protocol}://{local_ip}:8001/qr/{token}"
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
                    type="friendly",
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

# ===========================
# Geofencing Endpoints
# ===========================

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
    {"id": "all", "name": "Alle Einheiten", "description": "Broadcast to all units", "color": "#ffffff"},
    {"id": "hq", "name": "HQ", "description": "Headquarters Communication", "color": "#3498db"},
    {"id": "fox", "name": "Fox", "description": "Fox Team Channel", "color": "#e67e22"},
    {"id": "alpha", "name": "Alpha", "description": "Alpha Team Channel", "color": "#2ecc71"},
    {"id": "bravo", "name": "Bravo", "description": "Bravo Team Channel", "color": "#9b59b6"}
]

def _ensure_default_channels(db):
    """Seed default channels into DB if they don't exist yet."""
    for ch in DEFAULT_CHANNELS:
        existing = db.query(ChatChannel).filter(ChatChannel.id == ch["id"]).first()
        if not existing:
            db.add(ChatChannel(
                id=ch["id"], name=ch["name"], description=ch.get("description", ""),
                color=ch.get("color", "#ffffff"), is_default=True, members=[]
            ))
    try:
        db.commit()
    except Exception:
        db.rollback()

def _extract_username_from_auth(authorization: str) -> str:
    """Extract username from Authorization header. Returns username or raises HTTPException."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid authorization header")
    token = authorization.split(" ", 1)[1].strip()
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
def get_chat_channels():
    """Get list of available chat channels (static defaults + DB custom channels)"""
    db = SessionLocal()
    try:
        _ensure_default_channels(db)
        channels = db.query(ChatChannel).all()
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
            await websocket_manager.broadcast(channel="chat", event="channel_created", data=ch_dict)
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
            await websocket_manager.broadcast(channel="chat", event="channel_deleted", data={"id": channel_id, "deleted_by": username})
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
    """Update the member list of a chat channel"""
    db = SessionLocal()
    try:
        _extract_username_from_auth(authorization)
        channel = db.query(ChatChannel).filter(ChatChannel.id == channel_id).first()
        if not channel:
            raise HTTPException(status_code=404, detail="Channel not found")
        channel.members = data.get("members", [])
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
def get_chat_messages(channel_id: str, limit: int = 100):
    """Get recent chat messages from DB for a specific channel"""
    db = SessionLocal()
    try:
        messages = db.query(ChatMessage).filter(ChatMessage.channel == channel_id).order_by(ChatMessage.timestamp.desc()).limit(limit).all()
        # Reverse to get chronological order for UI
        messages.reverse()
        return {
            "status": "success",
            "messages": [_chat_message_to_dict(m) for m in messages],
        }
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
            await websocket_manager.broadcast(
                channel="chat",
                event="new_message",
                data=msg_dict
            )

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
        delivered = msg.delivered_to if msg.delivered_to else []
        if username not in delivered:
            delivered.append(username)
            msg.delivered_to = delivered
            db.commit()
            if AUTONOMOUS_MODULES_AVAILABLE and websocket_manager:
                await websocket_manager.broadcast(
                    channel="chat", event="message_delivered",
                    data={"message_id": message_id, "delivered_to": delivered}
                )
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
                await websocket_manager.broadcast(
                    channel="chat", event="message_read",
                    data={"message_id": message_id, "read_by": read_list, "delivered_to": delivered}
                )
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
            await websocket_manager.broadcast(
                channel="chat", event="messages_read",
                data={"message_ids": updated, "read_by_user": username}
            )
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
        symbol_type = symbol.get("type", "marker")
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
                "lat": new_symbol.lat,
                "lng": new_symbol.lng,
                "type": new_symbol.type,
                "username": new_symbol.created_by,
                "source_page": source_page,
                "timestamp": timestamp,
                "label": new_symbol.name,
                "color": new_symbol.color,
                "icon": new_symbol.icon
            }
        
        # Broadcast to WebSocket clients using helper
        broadcast_websocket_update("symbols", "symbol_created", symbol_data)
        broadcast_websocket_update("markers", "marker_created", symbol_data)
        
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
            
            db.delete(marker)
            db.commit()
            
        # Broadcast to WebSocket clients using helper
        broadcast_websocket_update("symbols", "symbol_deleted", {"id": symbol_id})
        broadcast_websocket_update("markers", "marker_deleted", {"id": symbol_id})
        
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
                    # Relay camera frames to all clients subscribed to camera channel
                    logger.debug(f"Relaying camera frame from {connection_id}")
                    await websocket_manager.publish_to_channel('camera', {
                        'type': 'camera_frame',
                        'channel': 'camera',
                        'frame': data.get('frame'),
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
                    _active_stream_share = {
                        "active": data.get('active', False),
                        "stream_url": data.get('stream_url') or data.get('details'),
                        "stream_type": data.get('stream_type', 'mjpeg' if is_camera else 'video'),
                        "isCamera": is_camera,
                        "source": data.get('source'),
                        "timestamp": datetime.now(timezone.utc).isoformat()
                    }
                    await websocket_manager.publish_to_channel('camera', {
                        'type': 'stream_share',
                        'channel': 'camera',
                        'streamId': data.get('streamId', 'camera_main'),
                        'active': data.get('active', False),
                        'isCamera': is_camera,
                        'stream_url': _active_stream_share['stream_url'],
                        'stream_type': _active_stream_share['stream_type'],
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
        logger.info(f"WebSocket client disconnected: {connection_id}")
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
        websocket_manager.disconnect(connection_id)

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
    blocked_prefixes = ("api", "static", "assets", "_", "favicon.ico", "ws")
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
    raise HTTPException(status_code=404, detail=f"File {full_path} not found. Bitte .html Endung verwenden.")

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
    
    logger.info(f"  Server will bind to: 0.0.0.0:8001 (all interfaces)")
    
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
    logger.info(f"    - From this device: {protocol}://127.0.0.1:8001/landing.html")
    logger.info(f"    - From network:     {protocol}://{local_ip}:8001/landing.html")
    
    # Additional access URLs for each detected IP (avoid duplicates)
    if all_detected_ips:
        alternative_ips = [ip for ip in all_detected_ips if ip != local_ip]
        if alternative_ips:
            logger.info(f"  Alternative Network URLs:")
            for ip in alternative_ips:
                logger.info(f"    - {protocol}://{ip}:8001/landing.html")
    
    logger.info("="*60)
    
    # Run with or without SSL
    if use_ssl:
        uvicorn.run(
            app, 
            host="0.0.0.0", 
            port=8001, 
            log_level="info",
            ssl_certfile=cert_file,
            ssl_keyfile=key_file
        )
    else:
        uvicorn.run(app, host="0.0.0.0", port=8001, log_level="info")