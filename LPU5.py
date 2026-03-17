#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LPU5.py – Standalone LPU5 Tactical Desktop Client

Full 1:1 copy of the web UI (Dashboard, Admin, SDR, Map, etc.)
running as a native desktop window — no server required.

A lightweight local HTTP server serves the project files so that all
relative paths (JS, CSS, images, assets) work correctly.  On first
start the user is prompted to create a local admin account.  Use the
built-in Network view to connect to a remote LPU5 server for live
data synchronisation.

Requirements:
    pip install pywebview pyserial

Usage:
    python LPU5.py
    python LPU5.py --fullscreen
    python LPU5.py --debug
    python LPU5.py --width 1600 --height 1000
"""

import argparse
import functools
import hashlib
import json
import os
import secrets
import socket
import sys
import threading
import time
import uuid
from http.server import HTTPServer, SimpleHTTPRequestHandler

VERSION = "1.0.0"

# ── Optional: serial port scanning ──────────────────────────────
try:
    import serial.tools.list_ports as _serial_list_ports
except ImportError:
    _serial_list_ports = None

# ── Local user database ─────────────────────────────────────────
_USERS_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "lpu5_users.json"
)
_sessions: dict[str, dict] = {}


def _load_users() -> list[dict]:
    if os.path.exists(_USERS_FILE):
        try:
            with open(_USERS_FILE, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (json.JSONDecodeError, OSError):
            return []
    return []


def _save_users(users: list[dict]) -> None:
    with open(_USERS_FILE, "w", encoding="utf-8") as fh:
        json.dump(users, fh, indent=2, ensure_ascii=False)


def _hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    hashed = hashlib.sha256((salt + password).encode()).hexdigest()
    return f"{salt}:{hashed}"


def _verify_password(password: str, stored: str) -> bool:
    try:
        salt, hashed = stored.split(":", 1)
    except ValueError:
        return False
    return hashlib.sha256((salt + password).encode()).hexdigest() == hashed


def _generate_token(user: dict) -> str:
    token = secrets.token_hex(32)
    _sessions[token] = {
        "user_id": user["id"],
        "username": user["username"],
        "created_at": time.time(),
    }
    return token


def _user_from_token(token: str) -> dict | None:
    """Return the user dict for *token*, or ``None``."""
    session = _sessions.get(token)
    if session is None:
        return None
    users = _load_users()
    for u in users:
        if u["id"] == session["user_id"]:
            return u
    return None


def _user_info(user: dict) -> dict:
    """Return a safe subset of user fields (no password hash)."""
    return {
        "id": user["id"],
        "username": user["username"],
        "callsign": user.get("callsign", user["username"]),
        "role": user.get("role", "admin"),
        "fullname": user.get("fullname", user["username"]),
    }


def _scan_serial_ports() -> list[dict]:
    if _serial_list_ports is None:
        return []
    try:
        result = []
        for p in _serial_list_ports.comports():
            result.append(
                {
                    "device": getattr(p, "device", "") or "",
                    "description": getattr(p, "description", ""),
                    "hwid": getattr(p, "hwid", ""),
                    "manufacturer": getattr(p, "manufacturer", None),
                    "vid": getattr(p, "vid", None),
                    "pid": getattr(p, "pid", None),
                    "serial_number": getattr(p, "serial_number", None),
                }
            )
        return result
    except Exception:
        return []


# ── Local file server ───────────────────────────────────────────
# Serves all project files AND provides minimal stub API responses
# so the SPA doesn't break when running without the real backend.

# Minimal JSON stubs keyed by request path.
_API_STUBS: dict[str, tuple[int, dict | list]] = {
    "/api/health": (200, {"status": "ok", "standalone": True}),
    "/api/map_markers": (200, []),
    "/api/meshtastic/nodes": (200, []),
    "/api/missions": (200, []),
    "/api/users": (200, []),
    "/api/pending_registrations": (200, []),
    "/api/groups": (200, []),
    "/api/chat/channels": (200, []),
    "/api/drawings": (200, []),
    "/api/overlays": (200, []),
    "/api/symbols": (200, []),
    "/api/map_symbols": (200, []),
    "/api/geofences": (200, []),
    "/api/autonomous_rules": (200, []),
    "/api/sessions": (200, []),
    "/api/audit_log": (200, []),
    "/api/federation/servers": (200, []),
    "/api/federation/info": (
        200,
        {
            "server_name": "LPU5 Standalone",
            "version": VERSION,
            "standalone": True,
        },
    ),
    "/api/server_info": (
        200,
        {
            "server_name": "LPU5 Standalone",
            "version": VERSION,
            "base_url": "",
            "standalone": True,
        },
    ),
    "/api/config": (200, {}),
    "/api/dependencies/check": (200, {"standalone": True}),
}


def _read_request_body(handler) -> bytes:
    length = int(handler.headers.get("Content-Length", 0))
    return handler.rfile.read(length) if length else b""


def _send_json(handler, status: int, body) -> None:
    payload = json.dumps(body).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(payload)))
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.end_headers()
    handler.wfile.write(payload)


class _LPU5Handler(SimpleHTTPRequestHandler):
    """Serve static files + return stubs for /api/* requests."""

    # ── helpers ──────────────────────────────────────────────────

    def _bearer_user(self) -> dict | None:
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return _user_from_token(auth[7:])
        return None

    # ── GET ──────────────────────────────────────────────────────

    def do_GET(self):
        path = self.path.split("?")[0]

        # /api/me – return logged-in user
        if path == "/api/me":
            user = self._bearer_user()
            if user:
                _send_json(self, 200, _user_info(user))
            else:
                _send_json(self, 401, {"detail": "Not authenticated"})
            return

        # /api/scan_ports – COM / serial port enumeration
        if path == "/api/scan_ports":
            _send_json(self, 200, _scan_serial_ports())
            return

        # /api/standalone/has_users – check if any local users exist
        if path == "/api/standalone/has_users":
            _send_json(self, 200, {"has_users": len(_load_users()) > 0})
            return

        if path in _API_STUBS:
            status, body = _API_STUBS[path]
            _send_json(self, status, body)
            return

        # Catch-all for unknown /api/* endpoints → empty JSON
        if path.startswith("/api/"):
            _send_json(self, 200, [])
            return

        # Default: serve static file
        super().do_GET()

    # ── POST ─────────────────────────────────────────────────────

    def do_POST(self):
        path = self.path.split("?")[0]

        # Login – validate against local user store
        if path == "/api/login_user":
            try:
                data = json.loads(_read_request_body(self))
            except (json.JSONDecodeError, ValueError):
                _send_json(self, 400, {"detail": "Invalid JSON"})
                return
            username = (data.get("username") or "").strip()
            password = data.get("password") or ""
            if not username or not password:
                _send_json(self, 400, {"detail": "Username and password required"})
                return
            users = _load_users()
            found = None
            for u in users:
                if u["username"] == username:
                    found = u
                    break
            if not found or not _verify_password(password, found["password_hash"]):
                _send_json(self, 401, {"detail": "Invalid credentials"})
                return
            token = _generate_token(found)
            _send_json(
                self,
                200,
                {"status": "success", "token": token, "user": _user_info(found)},
            )
            return

        # Register – create a new local user (first user = admin)
        if path == "/api/register_user":
            try:
                data = json.loads(_read_request_body(self))
            except (json.JSONDecodeError, ValueError):
                _send_json(self, 400, {"detail": "Invalid JSON"})
                return
            username = (data.get("username") or "").strip()
            password = data.get("password") or ""
            fullname = (data.get("fullname") or "").strip() or username
            callsign = (data.get("callsign") or "").strip() or username
            if not username or not password:
                _send_json(self, 400, {"detail": "Username and password required"})
                return
            if len(password) < 4:
                _send_json(
                    self, 400, {"detail": "Password must be at least 4 characters"}
                )
                return
            users = _load_users()
            for u in users:
                if u["username"] == username:
                    _send_json(self, 409, {"detail": "Username already exists"})
                    return
            new_user = {
                "id": str(uuid.uuid4()),
                "username": username,
                "password_hash": _hash_password(password),
                "fullname": fullname,
                "callsign": callsign,
                "role": "admin",
            }
            users.append(new_user)
            _save_users(users)
            token = _generate_token(new_user)
            _send_json(
                self,
                200,
                {"status": "success", "token": token, "user": _user_info(new_user)},
            )
            return

        # Catch-all for other POST /api/* endpoints
        if path.startswith("/api/"):
            _send_json(self, 200, {"status": "ok", "standalone": True})
            return

        self.send_error(405)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,PUT,DELETE,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type,Authorization")
        self.end_headers()

    def do_PUT(self):
        self.do_POST()

    def do_DELETE(self):
        path = self.path.split("?")[0]
        if path.startswith("/api/"):
            _send_json(self, 200, {"status": "ok"})
            return
        self.send_error(405)

    # Suppress noisy access logs
    def log_message(self, format, *log_args):
        pass


def _find_free_port() -> int:
    """Find an available TCP port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start_local_server(directory: str, port: int) -> HTTPServer:
    """Start a background HTTP server rooted at *directory*."""
    handler = functools.partial(_LPU5Handler, directory=directory)
    server = HTTPServer(("127.0.0.1", port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


# ── Dependency check ────────────────────────────────────────────

def check_dependencies() -> bool:
    """Verify that pywebview is installed."""
    try:
        import webview  # noqa: F401
        return True
    except ImportError:
        print("[FEHLER] pywebview ist nicht installiert.")
        print("         Installieren mit: pip install pywebview pyserial")
        print("")
        print("  Windows:  pip install pywebview[cef] pyserial")
        print("  Linux:    pip install pywebview[gtk] pyserial")
        print("  macOS:    pip install pywebview pyserial")
        return False


# ── pywebview JS API bridge ─────────────────────────────────────

class LPU5Api:
    """Python-to-JS bridge exposed via pywebview js_api.

    Methods defined here are callable from JavaScript as:
        window.pywebview.api.method_name()
    """

    def __init__(self):
        self._window = None

    def set_window(self, window):
        self._window = window

    def get_version(self):
        """Return client version string."""
        return VERSION

    def get_platform(self):
        """Return the current platform identifier."""
        return sys.platform

    def get_app_info(self):
        """Return application metadata as a dict."""
        return {
            "name": "LPU5 Tactical Tracker",
            "version": VERSION,
            "platform": sys.platform,
            "python": sys.version,
        }


# ── Main ────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="LPU5 Tactical Tracker – Standalone Desktop Client",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Beispiele:\n"
            "  python LPU5.py                    Normaler Start\n"
            "  python LPU5.py --fullscreen        Vollbild\n"
            "  python LPU5.py --debug             Mit Entwickler-Tools\n"
        ),
    )
    parser.add_argument(
        "--fullscreen",
        action="store_true",
        help="Im Vollbildmodus starten",
    )
    parser.add_argument(
        "--width",
        type=int,
        default=1400,
        help="Fensterbreite in Pixel (Standard: 1400)",
    )
    parser.add_argument(
        "--height",
        type=int,
        default=900,
        help="Fensterhöhe in Pixel (Standard: 900)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Entwickler-Tools aktivieren",
    )
    args = parser.parse_args()

    # Check dependencies
    if not check_dependencies():
        sys.exit(1)

    import webview

    # Resolve project root (directory containing this script)
    project_dir = os.path.dirname(os.path.abspath(__file__))
    index_path = os.path.join(project_dir, "index.html")
    if not os.path.exists(index_path):
        print(f"[FEHLER] index.html nicht gefunden in: {project_dir}")
        sys.exit(1)

    # Start local HTTP server
    port = _find_free_port()
    server = _start_local_server(project_dir, port)
    local_url = f"http://127.0.0.1:{port}/index.html?standalone=1"

    print("[*] LPU5 Tactical Tracker – Standalone Client")
    print(f"[*] Version: {VERSION}")
    print(f"[*] Lokaler Server: http://127.0.0.1:{port}")
    print(f"[*] UI: {local_url}")
    users = _load_users()
    if users:
        print(f"[*] Lokale Benutzer: {len(users)}")
    else:
        print("[*] Kein Benutzer vorhanden – Erstregistrierung beim Start.")
    if _serial_list_ports:
        ports = _scan_serial_ports()
        print(f"[*] COM-Ports: {len(ports)} gefunden")
    else:
        print("[!] pyserial nicht installiert – COM-Port-Scan deaktiviert.")
        print("    Installieren mit: pip install pyserial")
    print("[*] Fenster schließen zum Beenden.")

    api = LPU5Api()

    # Create the main application window
    window = webview.create_window(
        "LPU5 Tactical Tracker",
        url=local_url,
        width=args.width,
        height=args.height,
        min_size=(800, 600),
        fullscreen=args.fullscreen,
        js_api=api,
        text_select=True,
    )
    api.set_window(window)

    # Select GUI backend based on platform
    gui = None
    if sys.platform == "win32":
        gui = "edgechromium"

    try:
        webview.start(debug=args.debug, gui=gui)
    finally:
        server.shutdown()


if __name__ == "__main__":
    main()
