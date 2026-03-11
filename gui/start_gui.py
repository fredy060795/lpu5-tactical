"""
LPU5 Tactical – Desktop GUI
============================
Startet ein natives Desktop-Fenster (PyWebView) mit der lokalen
Verbindungsseite.  Von dort wird die vollständige SPA des LPU5-Servers
geladen und alle API-Calls gehen per HTTP/HTTPS direkt an den Backend-Server.

Starten:
    python start_gui.py [--server https://192.168.8.1:8101]

EXE bauen (Windows):
    pyinstaller --onefile --windowed --add-data "index.html;." start_gui.py
"""

import argparse
import os
import sys
import webview


def _asset(filename: str) -> str:
    """Gibt den absoluten Pfad zu einer Datei im gui/-Verzeichnis zurück.
    Funktioniert sowohl im Quellcode- als auch im PyInstaller-Bundle."""
    if getattr(sys, "frozen", False):
        base = sys._MEIPASS  # type: ignore[attr-defined]
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, filename)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="LPU5 Tactical Desktop GUI"
    )
    parser.add_argument(
        "--server",
        default="",
        help=(
            "URL des LPU5-Servers, z.B. https://192.168.8.1:8101 "
            "(leer = Eingabe über die Verbindungsseite)"
        ),
    )
    parser.add_argument(
        "--width", type=int, default=1280, help="Fensterbreite in Pixel"
    )
    parser.add_argument(
        "--height", type=int, default=800, help="Fensterhöhe in Pixel"
    )
    args = parser.parse_args()

    if args.server:
        # Direkt den Server laden, Verbindungsseite überspringen
        url = args.server.rstrip("/")
        window = webview.create_window(
            "LPU5 Tactical",
            url=url,
            width=args.width,
            height=args.height,
            resizable=True,
        )
    else:
        # Lokale Verbindungsseite anzeigen
        html_path = _asset("index.html")
        window = webview.create_window(
            "LPU5 Tactical – Verbinden",
            url=f"file://{html_path}",
            width=args.width,
            height=args.height,
            resizable=True,
        )

    webview.start(debug=False)


if __name__ == "__main__":
    main()
