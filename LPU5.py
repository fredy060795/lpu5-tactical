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

from __future__ import annotations

import argparse
import functools
import hashlib
import json
import os
import secrets
import signal
import socket
import subprocess
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
# In-memory session store – sessions are valid for the lifetime of the
# desktop application window and are cleared on restart.
_sessions: dict[str, dict] = {}

# ── Default admin credentials (auto-created on first start) ─────
_DEFAULT_ADMIN_USER = "admin"
_DEFAULT_ADMIN_PASS = "admin"

# ── Backend server subprocess management ────────────────────────
_SHUTDOWN_TIMEOUT_S = 10
_server_process: subprocess.Popen | None = None
_server_process_lock = threading.Lock()


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
    hashed = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100_000)
    return f"{salt}:{hashed.hex()}"


def _verify_password(password: str, stored: str) -> bool:
    try:
        salt, hashed = stored.split(":", 1)
    except ValueError:
        return False
    return (
        hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100_000).hex()
        == hashed
    )


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


def _ensure_default_admin() -> dict:
    """Ensure the default admin user exists.  Return the user dict."""
    users = _load_users()
    for u in users:
        if u["username"] == _DEFAULT_ADMIN_USER:
            return u
    admin = {
        "id": str(uuid.uuid4()),
        "username": _DEFAULT_ADMIN_USER,
        "password_hash": _hash_password(_DEFAULT_ADMIN_PASS),
        "fullname": "Administrator",
        "callsign": "ADMIN",
        "role": "admin",
    }
    users.append(admin)
    _save_users(users)
    return admin


def _auto_login_admin() -> tuple[str, dict]:
    """Create (or find) the default admin and return ``(token, user_info)``."""
    admin = _ensure_default_admin()
    token = _generate_token(admin)
    return token, _user_info(admin)


# ── Backend server (api.py) management ──────────────────────────

def _find_python_for_server() -> str:
    """Return the Python interpreter that should run api.py.

    Prefer the project's server virtualenv (``.venv``) when it exists.
    """
    project_dir = os.path.dirname(os.path.abspath(__file__))
    if sys.platform == "win32":
        candidates = [
            os.path.join(project_dir, ".venv", "Scripts", "python.exe"),
            os.path.join(project_dir, ".venv", "python.exe"),
        ]
    else:
        candidates = [
            os.path.join(project_dir, ".venv", "bin", "python"),
            os.path.join(project_dir, ".venv", "bin", "python3"),
        ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return sys.executable


def _start_backend_server() -> dict:
    """Start ``api.py`` as a subprocess and return status info."""
    global _server_process
    with _server_process_lock:
        if _server_process is not None and _server_process.poll() is None:
            return {"status": "already_running", "pid": _server_process.pid}

        project_dir = os.path.dirname(os.path.abspath(__file__))
        api_script = os.path.join(project_dir, "api.py")
        if not os.path.isfile(api_script):
            return {"status": "error", "detail": "api.py not found"}

        python = _find_python_for_server()
        log_file = os.path.join(project_dir, "server.log")
        fh = None
        try:
            kwargs: dict = {}
            if sys.platform == "win32":
                kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            fh = open(log_file, "a", encoding="utf-8")  # noqa: SIM115
            _server_process = subprocess.Popen(
                [python, api_script],
                cwd=project_dir,
                stdout=fh,
                stderr=subprocess.STDOUT,
                **kwargs,
            )
        except Exception as exc:
            if fh is not None:
                fh.close()
            return {"status": "error", "detail": str(exc)}

        print(f"[*] Backend-Server gestartet (PID {_server_process.pid}), Log: {log_file}")
        return {"status": "started", "pid": _server_process.pid}


def _stop_backend_server() -> dict:
    """Stop the backend server subprocess if it is running."""
    global _server_process
    with _server_process_lock:
        if _server_process is None or _server_process.poll() is not None:
            _server_process = None
            return {"status": "not_running"}
        pid = _server_process.pid
        try:
            if sys.platform == "win32":
                _server_process.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                _server_process.terminate()
            _server_process.wait(timeout=_SHUTDOWN_TIMEOUT_S)
        except Exception:
            try:
                _server_process.kill()
                _server_process.wait(timeout=5)
            except Exception:
                pass
        _server_process = None
        return {"status": "stopped", "pid": pid}


def _backend_server_status() -> dict:
    """Return the current status of the backend server subprocess."""
    with _server_process_lock:
        if _server_process is not None and _server_process.poll() is None:
            return {"running": True, "pid": _server_process.pid}
        return {"running": False}


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

        # /api/standalone/auto_login – auto-login with default admin
        if path == "/api/standalone/auto_login":
            token, user_info = _auto_login_admin()
            _send_json(self, 200, {
                "status": "success",
                "token": token,
                "user": user_info,
            })
            return

        # /api/standalone/server_status – backend server status
        if path == "/api/standalone/server_status":
            _send_json(self, 200, _backend_server_status())
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

        # Start backend server (api.py)
        if path == "/api/standalone/start_server":
            result = _start_backend_server()
            _send_json(self, 200, result)
            return

        # Stop backend server
        if path == "/api/standalone/stop_server":
            result = _stop_backend_server()
            _send_json(self, 200, result)
            return

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
    # ── Python version guard ────────────────────────────────────
    if sys.version_info < (3, 8):
        print("[FEHLER] Python 3.8 oder höher wird benötigt.")
        print(f"         Aktuelle Version: {sys.version}")
        sys.exit(1)

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

    # Ensure default admin account exists (auto-created on first start)
    admin = _ensure_default_admin()
    print(f"[*] Standard-Admin: {admin['username']} (Rolle: {admin['role']})")
    users = _load_users()
    print(f"[*] Lokale Benutzer: {len(users)}")

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

    # Let pywebview auto-detect the best available GUI backend.
    # Previously "edgechromium" was hard-coded on Windows which crashes
    # when the Edge WebView2 Runtime is not installed.
    try:
        webview.start(debug=args.debug)
    except Exception as exc:
        print(f"[FEHLER] Fenster konnte nicht gestartet werden: {exc}")
        print("")
        print("  Mögliche Ursachen:")
        print("  - Auf Windows: Edge WebView2 Runtime nicht installiert")
        print("    → https://developer.microsoft.com/en-us/microsoft-edge/webview2/")
        print("  - Auf Linux: GTK oder Qt mit Python-Bindings fehlt")
        print("    → sudo apt install python3-gi gir1.2-webkit2-4.0")
        sys.exit(1)
    finally:
        # Stop backend server if it was started from the UI
        _stop_backend_server()
        server.shutdown()


if __name__ == "__main__":
    main()
