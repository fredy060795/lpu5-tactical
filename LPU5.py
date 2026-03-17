#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LPU5.py – Standalone LPU5 Tactical Map Client

Desktop application that runs independently from the LPU5 server.
Opens directly with the full tactical map UI — no login wall.
Connect to any LPU5 server via the built-in Network panel to sync
markers, positions, Meshtastic nodes, and more.

Requirements:
    pip install pywebview

Usage:
    python LPU5.py
    python LPU5.py --url https://192.168.1.10:8101
    python LPU5.py --fullscreen
    python LPU5.py --width 1600 --height 1000
"""

import argparse
import json
import os
import sys

VERSION = "1.0.0"


def check_dependencies():
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


def get_html_path():
    """Resolve path to LPU5_ui.html relative to this script."""
    base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "LPU5_ui.html")


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


def main():
    parser = argparse.ArgumentParser(
        description="LPU5 Tactical Map – Standalone Client",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Beispiele:\n"
            "  python LPU5.py                          Normaler Start\n"
            "  python LPU5.py --fullscreen              Vollbild\n"
            "  python LPU5.py --url https://10.0.0.1:8101  Auto-Verbindung\n"
        ),
    )
    parser.add_argument(
        "--url",
        type=str,
        default=None,
        help="Auto-Verbindung zu Server-URL (z.B. https://192.168.1.10:8101)",
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

    html_path = get_html_path()
    if not os.path.exists(html_path):
        print(f"[FEHLER] UI-Datei nicht gefunden: {html_path}")
        sys.exit(1)

    api = LPU5Api()

    # Create the main application window
    window = webview.create_window(
        "LPU5 Tactical Tracker",
        url=html_path,
        width=args.width,
        height=args.height,
        min_size=(800, 600),
        fullscreen=args.fullscreen,
        js_api=api,
        text_select=True,
    )
    api.set_window(window)

    # If --url provided, inject auto-connect call after page loads
    if args.url:
        server_url = args.url.rstrip("/")
        # Validate URL format
        from urllib.parse import urlparse

        parsed = urlparse(server_url)
        if parsed.scheme not in ("http", "https"):
            print(f"[FEHLER] Ungültiges URL-Schema: {parsed.scheme}")
            print("         Erlaubt: http, https")
            sys.exit(1)
        if not parsed.hostname:
            print(f"[FEHLER] Kein Hostname in URL: {server_url}")
            sys.exit(1)

        def on_loaded():
            safe_url = json.dumps(server_url)
            window.evaluate_js(f"autoConnectServer({safe_url})")

        window.events.loaded += on_loaded

    print("[*] LPU5 Tactical Tracker wird gestartet...")
    print(f"[*] UI: {html_path}")
    if args.url:
        print(f"[*] Auto-Verbindung: {args.url}")
    print("[*] Fenster schließen zum Beenden.")

    # Select GUI backend based on platform
    gui = None
    if sys.platform == "win32":
        gui = "edgechromium"

    webview.start(debug=args.debug, gui=gui)


if __name__ == "__main__":
    main()
