#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LPU5.py – Standalone LPU5 Tactical Desktop Client

Full 1:1 copy of the web UI (Dashboard, Admin, SDR, Map, etc.)
running as a native desktop window — no server required.

A lightweight local HTTP server serves the project files so that all
relative paths (JS, CSS, images, assets) work correctly.  The SPA
opens in standalone mode (login bypassed) with empty data.  Use the
built-in Network view to connect to a remote LPU5 server for live
data synchronisation.

Requirements:
    pip install pywebview

Usage:
    python LPU5.py
    python LPU5.py --fullscreen
    python LPU5.py --debug
    python LPU5.py --width 1600 --height 1000
"""

import argparse
import functools
import json
import os
import socket
import sys
import threading
from http.server import HTTPServer, SimpleHTTPRequestHandler

VERSION = "1.0.0"


# ── Local file server ───────────────────────────────────────────
# Serves all project files AND provides minimal stub API responses
# so the SPA doesn't break when running without the real backend.

# Minimal JSON stubs keyed by request path.
_API_STUBS: dict[str, tuple[int, dict | list]] = {
    "/api/me": (
        200,
        {
            "id": "standalone",
            "username": "offline",
            "callsign": "OFFLINE",
            "role": "admin",
            "fullname": "Standalone User",
        },
    ),
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
    "/api/scan_ports": (200, []),
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


class _LPU5Handler(SimpleHTTPRequestHandler):
    """Serve static files + return stubs for /api/* requests."""

    def do_GET(self):
        # Strip query string for path matching
        path = self.path.split("?")[0]

        if path in _API_STUBS:
            status, body = _API_STUBS[path]
            payload = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(payload)
            return

        # Catch-all for unknown /api/* endpoints → empty JSON
        if path.startswith("/api/"):
            payload = b"[]"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(payload)
            return

        # Default: serve static file
        super().do_GET()

    def do_POST(self):
        path = self.path.split("?")[0]

        # Stub login – always succeed in standalone mode
        if path == "/api/login_user":
            body = {
                "token": "standalone_token",
                "user": {
                    "id": "standalone",
                    "username": "standalone",
                    "callsign": "OFFLINE",
                    "role": "admin",
                    "fullname": "Standalone User",
                },
            }
            payload = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(payload)
            return

        # Catch-all for other POST /api/* endpoints
        if path.startswith("/api/"):
            payload = json.dumps({"status": "ok", "standalone": True}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(payload)
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
            payload = json.dumps({"status": "ok"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
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
        print("         Installieren mit: pip install pywebview")
        print("")
        print("  Windows:  pip install pywebview[cef]")
        print("  Linux:    pip install pywebview[gtk]")
        print("  macOS:    pip install pywebview")
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
