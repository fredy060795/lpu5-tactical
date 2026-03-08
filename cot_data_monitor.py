#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cot_data_monitor.py – CoT Data-Flow Monitor for LPU5 ↔ ATAK

Standalone diagnostic tool that captures, parses, and logs every CoT XML
event exchanged between LPU5 and ATAK/WinTAK, including Meshtastic nodes.

Usage
-----
    # Monitor TAK server traffic (bidirectional):
    python cot_data_monitor.py --tak-host 192.168.1.100 --tak-port 8087

    # Monitor TAK server via SSL:
    python cot_data_monitor.py --tak-host 192.168.1.100 --tak-port 8089 --ssl

    # Listen for incoming CoT from ATAK clients on LAN (TCP + UDP + Multicast):
    python cot_data_monitor.py --listen --tcp-port 8088 --udp-port 4242

    # Both at the same time:
    python cot_data_monitor.py --tak-host 192.168.1.100 --tak-port 8087 --listen

    # Write to log file as well:
    python cot_data_monitor.py --tak-host 192.168.1.100 --log cot_traffic.log

    # Start with HTML web UI (graphical interface):
    python cot_data_monitor.py --tak-host 192.168.1.100 --web
    python cot_data_monitor.py --listen --web --web-port 9090

    # Monitor via the LPU5 API (when the API is already running):
    python cot_data_monitor.py --api-url http://127.0.0.1:8101
    python cot_data_monitor.py --api-url http://127.0.0.1:8101 --web

    # --web alone auto-detects a running API; falls back to --listen:
    python cot_data_monitor.py --web

Each captured CoT event is displayed with:
    • Direction  (LPU5 → ATAK  or  ATAK → LPU5)
    • Raw XML (optional, --show-xml)
    • Parsed fields: UID, CoT type, how, callsign, coordinates, team, role
    • LPU5 type mapping and detection rationale (marker / tak_maker /
      meshtastic_node / node / gateway / gps_position / cbt_* …)
    • Meshtastic detail flag, contact endpoint, color, remarks, stale window

The ``--web`` flag starts a built-in HTTP server that serves a graphical
HTML interface (``cot_monitor_ui.html``).  The web UI shows each captured
CoT event in a table, displays the LPU5-detected marker type, and provides
a dropdown to manually assign the correct marker type.  Annotated logs
(with corrections) can be saved as JSON for further development.

When the LPU5 API is already running, use ``--api-url`` to read CoT data
from the API's built-in monitor stream instead of opening local listeners
that would conflict with the running API.  The CoT monitor is also
available directly in the API at ``/api/cot/monitor/ui``.

The tool does **not** modify any data – it is read-only and safe for
production use.
"""

from __future__ import annotations

import argparse
import http.server
import json
import logging
import os
import pathlib
import queue
import re
import select
import signal
import socket
import ssl
import struct
import sys
import textwrap
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Colour helpers (ANSI)
# ---------------------------------------------------------------------------

_NO_COLOUR = os.environ.get("NO_COLOR") is not None

# Timeout (seconds) used for non-blocking socket recv loops throughout
# the capture threads.  Keep it short so the stop-event is checked often.
_SOCK_TIMEOUT = 1

def _c(code: str, text: str) -> str:
    """Wrap *text* in ANSI colour *code* unless colours are disabled."""
    if _NO_COLOUR:
        return text
    return f"\033[{code}m{text}\033[0m"

def _green(t: str) -> str:   return _c("32", t)
def _red(t: str) -> str:     return _c("31", t)
def _yellow(t: str) -> str:  return _c("33", t)
def _cyan(t: str) -> str:    return _c("36", t)
def _blue(t: str) -> str:    return _c("34", t)
def _magenta(t: str) -> str: return _c("35", t)
def _bold(t: str) -> str:    return _c("1", t)
def _dim(t: str) -> str:     return _c("2", t)

# ---------------------------------------------------------------------------
# CoT type → LPU5 type mapping (mirrors cot_protocol.py)
# ---------------------------------------------------------------------------

COT_TO_LPU5_TYPE: List[Tuple[str, str]] = [
    ("b-m-p-s-m", "hostile"),
    ("u-d-c-e",   "hostile"),
    ("u-d-c-c",   "hostile"),
    ("u-d-r",     "friendly"),
    ("u-d-f",     "hostile"),
    ("u-d-p",     "hostile"),
    ("a-f-G-E-S-U-M", "meshtastic_node"),
    ("a-f-G-E",   "meshtastic_node"),
    ("a-f",       "friendly"),
    ("a-h",       "hostile"),
    ("a-n",       "neutral"),
    ("a-u",       "unknown"),
    ("a-p",       "hostile"),
]

ATAK_TO_CBT_TYPE = {
    "hostile":  "cbt_hostile",
    "friendly": "cbt_friendly",
    "neutral":  "cbt_neutral",
    "unknown":  "cbt_unknown",
}

LPU5_TO_COT_TYPE = {
    "hostile":          "a-h-G-U-C",
    "neutral":          "a-n-G-U-C",
    "unknown":          "a-u-G-U-C",
    "friendly":         "a-f-G-U-C",
    "pending":          "a-p-G-U-C",
    "gps_position":     "a-f-G-E-S-U-M",
    "node":             "a-f-G-E-S-U-M",
    "meshtastic_node":  "a-f-G-E-S-U-M",
    "gateway":          "a-f-G-E-S-U-M",
    "tak_maker":        "a-f-G-U-C",
    "cbt_hostile":      "a-h-G-U-C",
    "cbt_friendly":     "a-f-G-U-C",
    "cbt_neutral":      "a-n-G-U-C",
    "cbt_unknown":      "a-u-G-U-C",
}


def cot_type_to_lpu5(cot_type: str) -> str:
    """Map a CoT type string to the LPU5 internal type (prefix match)."""
    for prefix, lpu5 in COT_TO_LPU5_TYPE:
        if cot_type.startswith(prefix):
            return lpu5
    return "unknown"


# ---------------------------------------------------------------------------
# CoT XML parser
# ---------------------------------------------------------------------------

def parse_cot_event(xml_str: str) -> Optional[Dict[str, Any]]:
    """
    Parse a CoT XML ``<event>`` into a flat dictionary with all fields
    relevant for debugging marker / Meshtastic display issues.

    Returns *None* when the XML is not a valid ``<event>``.
    """
    try:
        root = ET.fromstring(xml_str)
    except ET.ParseError:
        return None
    if root.tag != "event":
        return None

    uid        = root.get("uid", "")
    cot_type   = root.get("type", "")
    how        = root.get("how", "")
    evt_time   = root.get("time", "")
    evt_start  = root.get("start", "")
    evt_stale  = root.get("stale", "")

    lat = lon = hae = ce = le = None
    point = root.find("point")
    if point is not None:
        lat = point.get("lat")
        lon = point.get("lon")
        hae = point.get("hae")
        ce  = point.get("ce")
        le  = point.get("le")

    detail = root.find("detail")
    callsign = endpoint = team_name = team_role = remarks = None
    color_argb = None
    has_meshtastic = False
    mesh_long = mesh_short = None
    has_archive = False
    uid_droid = None
    speed = course = None

    if detail is not None:
        contact = detail.find("contact")
        if contact is not None:
            callsign = contact.get("callsign")
            endpoint = contact.get("endpoint")

        uid_el = detail.find("uid")
        if uid_el is not None:
            uid_droid = uid_el.get("Droid")

        group = detail.find("__group")
        if group is not None:
            team_name = group.get("name")
            team_role = group.get("role")

        remarks_el = detail.find("remarks")
        if remarks_el is not None and remarks_el.text:
            remarks = remarks_el.text

        color_el = detail.find("color")
        if color_el is not None:
            color_argb = color_el.get("argb")

        mesh_el = detail.find("meshtastic")
        if mesh_el is not None:
            has_meshtastic = True
            mesh_long  = mesh_el.get("longName")
            mesh_short = mesh_el.get("shortName")

        has_archive = detail.find("archive") is not None

        track = detail.find("track")
        if track is not None:
            speed  = track.get("speed")
            course = track.get("course")

    # ----- LPU5 type detection (mirrors cot_to_marker / _process_incoming_cot) -----
    base_lpu5_type = cot_type_to_lpu5(cot_type)
    detected_type  = base_lpu5_type
    detection_reason = f"COT_TO_LPU5_TYPE prefix match → '{base_lpu5_type}'"

    if has_meshtastic or base_lpu5_type == "meshtastic_node":
        detected_type = "meshtastic_node"
        detection_reason = (
            "<meshtastic> detail present" if has_meshtastic
            else f"CoT type '{cot_type}' prefix-matches meshtastic_node"
        )
    elif base_lpu5_type == "friendly" and how.startswith("h-g"):
        detected_type = "tak_maker"
        detection_reason = f"friendly + how='{how}' starts with 'h-g' → tak_maker (ATAK GPS SA)"
    elif base_lpu5_type == "friendly" and how.startswith("h"):
        detected_type = "meshtastic_node"
        detection_reason = f"friendly + how='{how}' starts with 'h' (not h-g) → meshtastic_node"
    else:
        cbt = ATAK_TO_CBT_TYPE.get(base_lpu5_type)
        if cbt:
            detected_type = cbt
            detection_reason = f"ATAK CBT remapping: '{base_lpu5_type}' → '{cbt}'"

    # mesh- UID override
    if uid.startswith("mesh-"):
        detected_type = "node"
        detection_reason = f"UID prefix 'mesh-' override → 'node'"

    # GPS- / LPU5-GW echo-back
    is_echo = uid.startswith("GPS-") or uid == "LPU5-GW"

    return {
        "uid":             uid,
        "cot_type":        cot_type,
        "how":             how,
        "time":            evt_time,
        "start":           evt_start,
        "stale":           evt_stale,
        "lat":             lat,
        "lon":             lon,
        "hae":             hae,
        "ce":              ce,
        "le":              le,
        "callsign":        callsign,
        "uid_droid":       uid_droid,
        "endpoint":        endpoint,
        "team":            team_name,
        "role":            team_role,
        "remarks":         remarks,
        "color_argb":      color_argb,
        "has_meshtastic":  has_meshtastic,
        "mesh_longName":   mesh_long,
        "mesh_shortName":  mesh_short,
        "has_archive":     has_archive,
        "speed":           speed,
        "course":          course,
        "base_lpu5_type":  base_lpu5_type,
        "detected_type":   detected_type,
        "detection_reason": detection_reason,
        "is_echo_back":    is_echo,
    }


# ---------------------------------------------------------------------------
# Pretty-printer
# ---------------------------------------------------------------------------

_TYPE_COLOURS = {
    "friendly":         _blue,
    "hostile":          _red,
    "neutral":          _green,
    "unknown":          _yellow,
    "pending":          _dim,
    "tak_maker":        _cyan,
    "meshtastic_node":  _magenta,
    "node":             _magenta,
    "gateway":          _magenta,
    "gps_position":     _cyan,
    "cbt_hostile":      _red,
    "cbt_friendly":     _blue,
    "cbt_neutral":      _green,
    "cbt_unknown":      _yellow,
}


def _type_label(t: str) -> str:
    fn = _TYPE_COLOURS.get(t, _dim)
    return fn(t)


def format_event(parsed: Dict[str, Any],
                 direction: str,
                 source_label: str,
                 show_xml: bool = False,
                 raw_xml: str = "") -> str:
    """
    Build a multi-line human-readable representation of a parsed CoT event.

    *direction* is ``">>>"`` (outgoing, LPU5→ATAK) or ``"<<<"`` (incoming, ATAK→LPU5).
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    if direction == ">>>":
        arrow = _green(">>> LPU5 → ATAK")
    else:
        arrow = _red("<<< ATAK → LPU5")

    lines: List[str] = []
    lines.append("")
    lines.append(f"{'═' * 80}")
    lines.append(f"  {arrow}   {_dim(now)}   {_dim(source_label)}")
    lines.append(f"{'─' * 80}")

    uid = parsed["uid"]
    callsign = parsed["callsign"] or uid
    cot_type = parsed["cot_type"]
    how      = parsed["how"]

    lines.append(f"  UID        : {_bold(uid)}")
    lines.append(f"  Callsign   : {_bold(callsign)}")
    if parsed.get("uid_droid") and parsed["uid_droid"] != callsign:
        lines.append(f"  UID Droid   : {parsed['uid_droid']}")
    lines.append(f"  CoT Type   : {cot_type}")
    lines.append(f"  How        : {how}")
    lines.append(f"  LPU5 Type  : {_type_label(parsed['detected_type'])}  "
                 f"({parsed['detection_reason']})")

    if parsed["is_echo_back"]:
        lines.append(f"  ⚠ ECHO-BACK: {_yellow('LPU5 would SKIP this event (own GPS or gateway SA)')}")

    lat, lon = parsed.get("lat"), parsed.get("lon")
    if lat is not None and lon is not None:
        lines.append(f"  Position   : lat={lat}  lon={lon}  hae={parsed.get('hae', '?')}")
    if parsed.get("ce") or parsed.get("le"):
        lines.append(f"  Accuracy   : CE={parsed.get('ce')}  LE={parsed.get('le')}")
    if parsed.get("speed") or parsed.get("course"):
        lines.append(f"  Track      : speed={parsed.get('speed')}  course={parsed.get('course')}")

    if parsed.get("team") or parsed.get("role"):
        lines.append(f"  Team/Role  : {parsed.get('team', '?')} / {parsed.get('role', '?')}")
    if parsed.get("endpoint"):
        lines.append(f"  Endpoint   : {parsed['endpoint']}")
    if parsed.get("color_argb"):
        lines.append(f"  Color ARGB : {parsed['color_argb']}")
    if parsed.get("remarks"):
        lines.append(f"  Remarks    : {parsed['remarks'][:120]}")

    if parsed["has_meshtastic"]:
        lines.append(f"  {_magenta('★ MESHTASTIC')} : "
                     f"longName={parsed.get('mesh_longName', '?')}  "
                     f"shortName={parsed.get('mesh_shortName', '?')}")
    lines.append(f"  Archive    : {'yes' if parsed['has_archive'] else 'no (live contact)'}")

    ts = parsed.get("time", "")
    stale = parsed.get("stale", "")
    if ts or stale:
        lines.append(f"  Time       : {ts}")
        lines.append(f"  Stale      : {stale}")

    if show_xml and raw_xml:
        lines.append(f"{'─' * 80}")
        lines.append(_dim("  Raw XML:"))
        for xline in raw_xml.strip().splitlines():
            lines.append(f"    {_dim(xline)}")

    lines.append(f"{'═' * 80}")
    return "\n".join(lines)


def format_event_plain(parsed: Dict[str, Any],
                       direction: str,
                       source_label: str,
                       raw_xml: str = "") -> str:
    """Plain-text variant for log-file output (no ANSI colours)."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    arrow = ">>> LPU5 -> ATAK" if direction == ">>>" else "<<< ATAK -> LPU5"

    parts: List[str] = []
    parts.append(f"{'=' * 80}")
    parts.append(f"  {arrow}   {now}   {source_label}")
    parts.append(f"  UID={parsed['uid']}  Callsign={parsed.get('callsign', '?')}")
    parts.append(f"  CoT={parsed['cot_type']}  How={parsed['how']}")
    parts.append(f"  LPU5Type={parsed['detected_type']}  ({parsed['detection_reason']})")
    if parsed["is_echo_back"]:
        parts.append(f"  ** ECHO-BACK – LPU5 would skip **")
    lat, lon = parsed.get("lat"), parsed.get("lon")
    if lat is not None:
        parts.append(f"  Pos=({lat},{lon})  HAE={parsed.get('hae')}")
    if parsed.get("team"):
        parts.append(f"  Team={parsed.get('team')}  Role={parsed.get('role')}")
    if parsed.get("endpoint"):
        parts.append(f"  Endpoint={parsed['endpoint']}")
    if parsed["has_meshtastic"]:
        parts.append(f"  MESHTASTIC longName={parsed.get('mesh_longName')} "
                     f"shortName={parsed.get('mesh_shortName')}")
    parts.append(f"  Archive={parsed['has_archive']}  Stale={parsed.get('stale')}")
    if raw_xml:
        parts.append(f"  --- RAW XML ---")
        parts.append(raw_xml.strip())
    parts.append(f"{'=' * 80}")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# JSON-lines log writer (machine-readable)
# ---------------------------------------------------------------------------

def _jsonl_record(parsed: Dict[str, Any], direction: str,
                  source_label: str, raw_xml: str) -> str:
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "direction": "LPU5->ATAK" if direction == ">>>" else "ATAK->LPU5",
        "source": source_label,
        **parsed,
        "raw_xml": raw_xml.strip(),
    }
    return json.dumps(record, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Event extraction from TCP stream buffer
# ---------------------------------------------------------------------------

_EVENT_RE = re.compile(r"<event\b[^>]*>.*?</event>", re.DOTALL)


def extract_events(buf: str) -> Tuple[List[str], str]:
    """
    Extract complete ``<event>…</event>`` blocks from *buf*.

    Returns ``(events_list, remaining_buffer)``.
    """
    events: List[str] = []
    last_end = 0
    for m in _EVENT_RE.finditer(buf):
        events.append(m.group(0))
        last_end = m.end()
    return events, buf[last_end:]


# ---------------------------------------------------------------------------
# TAK server monitor (connects to a remote TAK server)
# ---------------------------------------------------------------------------

class TAKServerMonitor(threading.Thread):
    """
    Connect to a TAK server over TCP or SSL and passively capture all CoT
    traffic.  Both received events and the initial SA beacon sent by this
    monitor are logged.
    """

    def __init__(self, host: str, port: int, use_ssl: bool = False,
                 certfile: str | None = None, keyfile: str | None = None,
                 password: str | None = None,
                 on_event=None,
                 sa_callsign: str = "LPU5-MONITOR"):
        super().__init__(daemon=True)
        self.host = host
        self.port = port
        self.use_ssl = use_ssl
        self.certfile = certfile
        self.keyfile = keyfile
        self.password = password
        self.on_event = on_event
        self.sa_callsign = sa_callsign
        self._stop_event = threading.Event()

    def stop(self):
        self._stop_event.set()

    def _build_sa_beacon(self) -> str:
        """Minimal SA beacon so the TAK server registers us and relays traffic."""
        from datetime import timedelta
        now = datetime.now(timezone.utc)
        ts  = now.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        stale = (now + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        return (
            f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<event version="2.0" uid="{self.sa_callsign}" '
            f'type="a-f-G-U-C" how="m-g" '
            f'time="{ts}" start="{ts}" stale="{stale}">'
            f'<point lat="0.0" lon="0.0" hae="0.0" ce="9999999" le="9999999"/>'
            f'<detail>'
            f'<contact callsign="{self.sa_callsign}"/>'
            f'</detail></event>'
        )

    def run(self):
        while not self._stop_event.is_set():
            try:
                self._connect_and_receive()
            except Exception as exc:
                _log_stderr(f"TAK monitor connection error: {exc}")
            if not self._stop_event.is_set():
                _log_stderr("Reconnecting to TAK server in 5 s …")
                self._stop_event.wait(5)

    def _connect_and_receive(self):
        raw_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        raw_sock.settimeout(10)

        if self.use_ssl:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            # TAK servers typically use self-signed certificates;
            # hostname verification is disabled for compatibility.
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            if self.certfile:
                ctx.load_cert_chain(self.certfile,
                                    keyfile=self.keyfile,
                                    password=self.password)
            sock = ctx.wrap_socket(raw_sock, server_hostname=self.host)
        else:
            sock = raw_sock

        try:
            _log_stderr(f"Connecting to TAK server {self.host}:{self.port} "
                        f"({'SSL' if self.use_ssl else 'TCP'}) …")
            sock.connect((self.host, self.port))
            _log_stderr(f"Connected to TAK server {self.host}:{self.port}")

            # Send SA beacon so the server relays events to us
            sa_xml = self._build_sa_beacon()
            sock.sendall(sa_xml.encode("utf-8"))
            parsed = parse_cot_event(sa_xml)
            if parsed and self.on_event:
                self.on_event(parsed, ">>>", "SA beacon (monitor)", sa_xml)

            buf = ""
            sock.settimeout(_SOCK_TIMEOUT)
            last_sa = time.time()
            while not self._stop_event.is_set():
                # Refresh SA every 25 s to stay alive
                if time.time() - last_sa > 25:
                    sa_xml = self._build_sa_beacon()
                    try:
                        sock.sendall(sa_xml.encode("utf-8"))
                    except OSError:
                        break
                    last_sa = time.time()

                try:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    buf += chunk.decode("utf-8", errors="replace")
                    events, buf = extract_events(buf)
                    for ev_xml in events:
                        parsed = parse_cot_event(ev_xml)
                        if parsed and self.on_event:
                            self.on_event(parsed, "<<<", f"TAK {self.host}:{self.port}", ev_xml)
                except socket.timeout:
                    continue
                except OSError:
                    break
        finally:
            try:
                sock.close()
            except OSError:
                pass
            _log_stderr(f"Disconnected from TAK server {self.host}:{self.port}")


# ---------------------------------------------------------------------------
# Local CoT listener (accepts incoming CoT from ATAK clients on LAN)
# ---------------------------------------------------------------------------

class LocalCoTListener(threading.Thread):
    """
    Open TCP / UDP / Multicast listeners on the local machine – the same
    ports that the real LPU5 ``CoTListenerService`` would use – and capture
    any incoming CoT events from ATAK/WinTAK clients on the LAN.

    **Important**: This will conflict with a running LPU5 instance on the
    same ports.  Use ``--tcp-port`` / ``--udp-port`` to pick alternative
    ports, or stop LPU5 first.
    """

    def __init__(self, tcp_port: int = 8088, udp_port: int = 4242,
                 multicast_group: str = "239.2.3.1", multicast_port: int = 6969,
                 on_event=None):
        super().__init__(daemon=True)
        self.tcp_port = tcp_port
        self.udp_port = udp_port
        self.multicast_group = multicast_group
        self.multicast_port = multicast_port
        self.on_event = on_event
        self._stop_event = threading.Event()

    def stop(self):
        self._stop_event.set()

    def run(self):
        threads: List[threading.Thread] = []
        for fn, label in [
            (self._tcp_listener, "TCP"),
            (self._udp_listener, "UDP"),
            (self._multicast_listener, "Multicast"),
        ]:
            t = threading.Thread(target=fn, daemon=True, name=f"listener-{label}")
            t.start()
            threads.append(t)
        self._stop_event.wait()

    # -- TCP ----------------------------------------------------------------
    def _tcp_listener(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            srv.bind(("0.0.0.0", self.tcp_port))
        except OSError as exc:
            _log_stderr(f"Cannot bind TCP port {self.tcp_port}: {exc}")
            return
        srv.listen(5)
        srv.settimeout(_SOCK_TIMEOUT)
        _log_stderr(f"TCP listener on port {self.tcp_port}")
        while not self._stop_event.is_set():
            try:
                conn, addr = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle_tcp,
                             args=(conn, addr), daemon=True).start()
        srv.close()

    def _handle_tcp(self, conn: socket.socket, addr):
        _log_stderr(f"TCP connection from {addr}")
        buf = ""
        conn.settimeout(_SOCK_TIMEOUT)
        try:
            while not self._stop_event.is_set():
                try:
                    data = conn.recv(4096)
                    if not data:
                        break
                    buf += data.decode("utf-8", errors="replace")
                    events, buf = extract_events(buf)
                    for ev_xml in events:
                        parsed = parse_cot_event(ev_xml)
                        if parsed and self.on_event:
                            self.on_event(parsed, "<<<",
                                          f"TCP {addr[0]}:{addr[1]}", ev_xml)
                except socket.timeout:
                    continue
        except OSError:
            pass
        finally:
            conn.close()
            _log_stderr(f"TCP connection closed from {addr}")

    # -- UDP ----------------------------------------------------------------
    def _udp_listener(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("0.0.0.0", self.udp_port))
        except OSError as exc:
            _log_stderr(f"Cannot bind UDP port {self.udp_port}: {exc}")
            return
        sock.settimeout(_SOCK_TIMEOUT)
        _log_stderr(f"UDP listener on port {self.udp_port}")
        while not self._stop_event.is_set():
            try:
                data, addr = sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            text = data.decode("utf-8", errors="replace")
            events, _ = extract_events(text)
            for ev_xml in events:
                parsed = parse_cot_event(ev_xml)
                if parsed and self.on_event:
                    self.on_event(parsed, "<<<",
                                  f"UDP {addr[0]}:{addr[1]}", ev_xml)
        sock.close()

    # -- Multicast ----------------------------------------------------------
    def _multicast_listener(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM,
                             socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("", self.multicast_port))
        except OSError as exc:
            _log_stderr(f"Cannot bind multicast port {self.multicast_port}: {exc}")
            return
        mreq = struct.pack("4sL",
                           socket.inet_aton(self.multicast_group),
                           socket.INADDR_ANY)
        try:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        except OSError as exc:
            _log_stderr(f"Cannot join multicast group {self.multicast_group}: {exc}")
            sock.close()
            return
        sock.settimeout(_SOCK_TIMEOUT)
        _log_stderr(f"Multicast listener on {self.multicast_group}:{self.multicast_port}")
        while not self._stop_event.is_set():
            try:
                data, addr = sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            text = data.decode("utf-8", errors="replace")
            events, _ = extract_events(text)
            for ev_xml in events:
                parsed = parse_cot_event(ev_xml)
                if parsed and self.on_event:
                    self.on_event(parsed, "<<<",
                                  f"Multicast {addr[0]}:{addr[1]}", ev_xml)
        sock.close()


# ---------------------------------------------------------------------------
# LPU5 API WebSocket monitor
# ---------------------------------------------------------------------------

class LPU5WebSocketMonitor(threading.Thread):
    """
    Connect to the LPU5 API WebSocket endpoint (``/ws``) and capture
    marker / symbol events.  This shows processed events *after* LPU5 has
    applied its type-detection logic, which is useful for comparing with
    raw CoT data.
    """

    def __init__(self, api_url: str = "ws://127.0.0.1:8101/ws",
                 on_event=None):
        super().__init__(daemon=True)
        self.api_url = api_url
        self.on_event = on_event
        self._stop_event = threading.Event()

    def stop(self):
        self._stop_event.set()

    def run(self):
        try:
            import websockets
            import asyncio
        except ImportError:
            _log_stderr(
                "⚠  'websockets' package not installed – LPU5 WebSocket "
                "monitoring disabled.  Install with:  pip install websockets"
            )
            return

        async def _ws_loop():
            while not self._stop_event.is_set():
                try:
                    async with websockets.connect(self.api_url) as ws:
                        _log_stderr(f"Connected to LPU5 WebSocket at {self.api_url}")
                        # Subscribe to markers channel
                        await ws.send(json.dumps({"action": "subscribe",
                                                   "channel": "markers"}))
                        while not self._stop_event.is_set():
                            try:
                                msg = await asyncio.wait_for(ws.recv(), timeout=1.0)
                                self._handle_ws_message(msg)
                            except asyncio.TimeoutError:
                                continue
                except Exception as exc:
                    _log_stderr(f"LPU5 WebSocket error: {exc}")
                if not self._stop_event.is_set():
                    await asyncio.sleep(3)

        asyncio.run(_ws_loop())

    def _handle_ws_message(self, raw: str):
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return
        evt_type = msg.get("type", "")
        data = msg.get("data", {})
        if not data:
            return

        # Print processed marker data so we can compare with raw CoT
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        lines: List[str] = [
            "",
            f"{'─' * 80}",
            f"  {_cyan('◆ LPU5 WebSocket')}   {_dim(now)}   event={evt_type}",
            f"{'─' * 80}",
        ]
        for k, v in data.items():
            lines.append(f"  {k:16s}: {v}")
        lines.append(f"{'─' * 80}")
        output = "\n".join(lines)
        print(output, flush=True)
        if self.on_event:
            self.on_event(msg, evt_type)


# ---------------------------------------------------------------------------
# LPU5 API SSE monitor (connects to the API's built-in CoT monitor SSE)
# ---------------------------------------------------------------------------

class LPU5APIMonitor(threading.Thread):
    """
    Connect to the LPU5 API's ``/api/cot/monitor/stream`` SSE endpoint and
    forward every captured CoT event to the local ``on_event`` callback.

    This is the preferred data source when the LPU5 API is already running
    because it does **not** open any local TCP/UDP listeners and therefore
    never conflicts with the API's own ``CoTListenerService``.
    """

    def __init__(self, api_url: str = "http://127.0.0.1:8101",
                 on_event=None):
        super().__init__(daemon=True)
        self.api_url = api_url.rstrip("/")
        self.on_event = on_event
        self._stop_event = threading.Event()

    def stop(self):
        self._stop_event.set()

    def run(self):
        import urllib.request
        sse_url = self.api_url + "/api/cot/monitor/stream"
        while not self._stop_event.is_set():
            try:
                _log_stderr(f"Connecting to LPU5 API SSE at {sse_url}")
                req = urllib.request.Request(sse_url)
                resp = urllib.request.urlopen(req, timeout=30)
                _log_stderr(f"Connected to LPU5 API SSE stream")
                buf = ""
                event_name = ""
                while not self._stop_event.is_set():
                    try:
                        line = resp.readline()
                        if not line:
                            break
                        line = line.decode("utf-8", errors="replace").rstrip("\r\n")
                    except Exception:
                        break
                    if line.startswith("event:"):
                        event_name = line[6:].strip()
                    elif line.startswith("data:"):
                        buf = line[5:].strip()
                    elif line == "" and buf:
                        # End of SSE message
                        if event_name == "cot_event":
                            self._handle_sse_record(buf)
                        buf = ""
                        event_name = ""
                    elif line.startswith(":"):
                        # Comment / keep-alive
                        pass
            except Exception as exc:
                _log_stderr(f"LPU5 API SSE error: {exc}")
            if not self._stop_event.is_set():
                time.sleep(3)

    def _handle_sse_record(self, raw: str):
        try:
            record = json.loads(raw)
        except json.JSONDecodeError:
            return
        parsed = record.get("parsed", {})
        direction = record.get("direction", "<<<")
        source = record.get("source", "api")
        raw_xml = record.get("raw_xml", "")
        if parsed and self.on_event:
            self.on_event(parsed, direction, source, raw_xml)


# ---------------------------------------------------------------------------
# Statistics tracker
# ---------------------------------------------------------------------------

class Stats:
    def __init__(self):
        self._lock = threading.Lock()
        self.total = 0
        self.incoming = 0
        self.outgoing = 0
        self.by_type: Dict[str, int] = {}
        self.meshtastic = 0
        self.echo_backs = 0
        self.parse_errors = 0

    def record(self, parsed: Dict[str, Any], direction: str):
        with self._lock:
            self.total += 1
            if direction == "<<<":
                self.incoming += 1
            else:
                self.outgoing += 1
            t = parsed.get("detected_type", "?")
            self.by_type[t] = self.by_type.get(t, 0) + 1
            if parsed.get("has_meshtastic"):
                self.meshtastic += 1
            if parsed.get("is_echo_back"):
                self.echo_backs += 1

    def record_error(self):
        with self._lock:
            self.parse_errors += 1

    def summary(self) -> str:
        with self._lock:
            lines = [
                "",
                _bold("Session statistics"),
                f"  Total events       : {self.total}",
                f"  Incoming (ATAK→LPU5): {self.incoming}",
                f"  Outgoing (LPU5→ATAK): {self.outgoing}",
                f"  Meshtastic events  : {self.meshtastic}",
                f"  Echo-backs (skipped): {self.echo_backs}",
                f"  Parse errors       : {self.parse_errors}",
                f"  Events by LPU5 type:",
            ]
            for t, c in sorted(self.by_type.items(), key=lambda x: -x[1]):
                lines.append(f"    {t:20s}: {c}")
            return "\n".join(lines)


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def _log_stderr(msg: str):
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{_dim(ts)}] {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Filter
# ---------------------------------------------------------------------------

class EventFilter:
    """Optionally filter events by type, UID pattern, or Meshtastic-only."""

    def __init__(self, types: Optional[List[str]] = None,
                 uid_pattern: Optional[str] = None,
                 meshtastic_only: bool = False):
        self.types = set(types) if types else None
        self.uid_re = re.compile(uid_pattern) if uid_pattern else None
        self.meshtastic_only = meshtastic_only

    def match(self, parsed: Dict[str, Any]) -> bool:
        if self.meshtastic_only:
            if not (parsed.get("has_meshtastic") or
                    parsed.get("detected_type") in
                    ("meshtastic_node", "node", "gateway", "gps_position")):
                return False
        if self.types and parsed.get("detected_type") not in self.types:
            return False
        if self.uid_re and not self.uid_re.search(parsed.get("uid", "")):
            return False
        return True


# ---------------------------------------------------------------------------
# Event store (shared between capture threads and web server)
# ---------------------------------------------------------------------------

class EventStore:
    """Thread-safe store for captured CoT events with SSE broadcast."""

    def __init__(self, max_events: int = 10000):
        self._lock = threading.Lock()
        self._events: List[Dict[str, Any]] = []
        self._max = max_events
        self._sse_queues: List[queue.Queue] = []

    def add(self, parsed: Dict[str, Any], direction: str,
            source: str, raw_xml: str):
        record = {
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
            # Push to all SSE subscribers
            dead: List[int] = []
            for i, q in enumerate(self._sse_queues):
                try:
                    q.put_nowait(record)
                except queue.Full:
                    dead.append(i)
            for i in reversed(dead):
                self._sse_queues.pop(i)

    def get_all(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._events)

    def set_correction(self, idx: int, correction: str, notes: str):
        with self._lock:
            if 0 <= idx < len(self._events):
                self._events[idx]["correction"] = correction
                self._events[idx]["notes"] = notes

    def clear(self):
        with self._lock:
            self._events.clear()

    def subscribe_sse(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=500)
        with self._lock:
            self._sse_queues.append(q)
        return q

    def unsubscribe_sse(self, q: queue.Queue):
        with self._lock:
            try:
                self._sse_queues.remove(q)
            except ValueError:
                pass

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


# ---------------------------------------------------------------------------
# Built-in HTTP server for the web UI
# ---------------------------------------------------------------------------

# Module-level reference – set in main() before the server starts.
_event_store: Optional[EventStore] = None
_html_dir: str = ""


class MonitorHTTPHandler(http.server.BaseHTTPRequestHandler):
    """Handle HTTP requests for the CoT monitor web UI."""

    # Suppress default request logging to stderr
    def log_message(self, fmt, *a):
        pass

    def _send_json(self, obj: Any, status: int = 200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html_bytes: bytes, status: int = 200):
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html_bytes)))
        self.end_headers()
        self.wfile.write(html_bytes)

    # ── Routes ──

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            self._serve_html()
        elif self.path == "/api/events":
            self._api_get_events()
        elif self.path == "/api/events/stream":
            self._api_sse_stream()
        elif self.path == "/api/export":
            self._api_export_get()
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path == "/api/events/clear":
            self._api_clear()
        elif self.path.startswith("/api/events/") and self.path.endswith("/correction"):
            self._api_set_correction()
        elif self.path == "/api/export":
            self._api_export_post()
        else:
            self.send_error(404)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    # ── Handlers ──

    def _serve_html(self):
        html_path = os.path.join(_html_dir, "cot_monitor_ui.html")
        try:
            with open(html_path, "rb") as f:
                data = f.read()
            self._send_html(data)
        except FileNotFoundError:
            self.send_error(404, f"cot_monitor_ui.html not found in {_html_dir}")

    def _api_get_events(self):
        if _event_store is None:
            self._send_json({"events": []})
            return
        self._send_json({"events": _event_store.get_all()})

    def _api_sse_stream(self):
        """Server-Sent Events stream for real-time updates."""
        if _event_store is None:
            self.send_error(503)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        q = _event_store.subscribe_sse()
        try:
            while True:
                try:
                    record = q.get(timeout=15)
                    payload = json.dumps(record, ensure_ascii=False)
                    self.wfile.write(f"event: cot_event\ndata: {payload}\n\n"
                                     .encode("utf-8"))
                    self.wfile.flush()
                except queue.Empty:
                    # Send keep-alive comment
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            _event_store.unsubscribe_sse(q)

    def _api_clear(self):
        if _event_store:
            _event_store.clear()
        self._send_json({"ok": True})

    def _api_set_correction(self):
        # Path: /api/events/{idx}/correction
        try:
            parts = self.path.split("/")
            idx = int(parts[3])
        except (IndexError, ValueError):
            self.send_error(400)
            return
        body = self._read_body()
        if body is None:
            return
        if _event_store:
            _event_store.set_correction(
                idx,
                body.get("correction", ""),
                body.get("notes", ""),
            )
        self._send_json({"ok": True})

    def _api_export_get(self):
        if _event_store is None:
            self._send_json({})
            return
        self._send_json(_event_store.export_log())

    def _api_export_post(self):
        body = self._read_body()
        if body is None:
            return
        # Save server-side copy
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        filename = f"cot_monitor_log_{ts}.json"
        filepath = os.path.join(_html_dir, filename)
        try:
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(body, f, ensure_ascii=False, indent=2)
            _log_stderr(f"Log saved to {filepath}")
            self._send_json({"ok": True, "file": filepath})
        except OSError as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=500)

    def _read_body(self) -> Optional[Dict]:
        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length)
            return json.loads(raw)
        except (ValueError, json.JSONDecodeError):
            self.send_error(400, "Invalid JSON")
            return None


def _start_web_server(port: int, html_dir: str,
                      store: EventStore,
                      max_retries: int = 10) -> Optional[threading.Thread]:
    """Start the HTTP server for the web UI in a daemon thread.

    If *port* is already in use the function tries up to *max_retries*
    consecutive ports (port+1, port+2, …) before giving up.  Returns
    ``None`` when no port could be bound so the caller can continue
    without the web UI.
    """
    global _event_store, _html_dir
    _event_store = store
    _html_dir = html_dir

    for attempt in range(max_retries):
        try_port = port + attempt
        try:
            server = http.server.ThreadingHTTPServer(
                ("0.0.0.0", try_port), MonitorHTTPHandler)
        except OSError as exc:
            _log_stderr(f"Cannot bind web UI port {try_port}: {exc}")
            continue
        t = threading.Thread(target=server.serve_forever, daemon=True,
                             name="web-server")
        t.start()
        _log_stderr(f"Web UI running on http://0.0.0.0:{try_port}/")
        return t

    _log_stderr("⚠  Could not start web UI – all attempted ports are in use.")
    return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="CoT Data-Flow Monitor for LPU5 ↔ ATAK",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Examples:
              # Monitor TAK server traffic:
              %(prog)s --tak-host 192.168.1.100 --tak-port 8087

              # Monitor TAK server via SSL with client cert:
              %(prog)s --tak-host 192.168.1.100 --tak-port 8089 --ssl \\
                       --certfile client.pem --keyfile client.key

              # Listen for incoming CoT from ATAK clients:
              %(prog)s --listen --tcp-port 8088 --udp-port 4242

              # Both at the same time, show raw XML, write log:
              %(prog)s --tak-host 192.168.1.100 --listen --show-xml \\
                       --log cot_traffic.log

              # Only show Meshtastic-related events:
              %(prog)s --tak-host 192.168.1.100 --meshtastic-only

              # Filter by detected LPU5 type:
              %(prog)s --tak-host 192.168.1.100 --types meshtastic_node,node,gateway

              # Monitor via LPU5 WebSocket (processed events):
              %(prog)s --ws ws://127.0.0.1:8101/ws

              # Monitor via running LPU5 API (no port conflicts):
              %(prog)s --api-url http://127.0.0.1:8101
              %(prog)s --api-url http://127.0.0.1:8101 --web

              # Start with web UI for graphical monitoring & correction:
              %(prog)s --tak-host 192.168.1.100 --web
              %(prog)s --listen --web --web-port 9090
        """),
    )

    g_tak = parser.add_argument_group("TAK server connection")
    g_tak.add_argument("--tak-host", metavar="HOST",
                       help="TAK server hostname or IP")
    g_tak.add_argument("--tak-port", metavar="PORT", type=int, default=8087,
                       help="TAK server port (default: 8087)")
    g_tak.add_argument("--ssl", action="store_true",
                       help="Use SSL/TLS for the TAK connection")
    g_tak.add_argument("--certfile", metavar="FILE",
                       help="Client certificate file (PEM) for SSL")
    g_tak.add_argument("--keyfile", metavar="FILE",
                       help="Client key file for SSL")
    g_tak.add_argument("--certpass", metavar="PASS",
                       help="Password for the client key")

    g_listen = parser.add_argument_group("Local CoT listener")
    g_listen.add_argument("--listen", action="store_true",
                          help="Open local TCP/UDP/Multicast listeners for "
                               "incoming CoT from ATAK clients")
    g_listen.add_argument("--tcp-port", metavar="PORT", type=int, default=8088,
                          help="TCP listener port (default: 8088)")
    g_listen.add_argument("--udp-port", metavar="PORT", type=int, default=4242,
                          help="UDP listener port (default: 4242)")

    g_ws = parser.add_argument_group("LPU5 WebSocket monitor")
    g_ws.add_argument("--ws", metavar="URL",
                      help="LPU5 WebSocket URL (e.g. ws://127.0.0.1:8101/ws)")

    g_api = parser.add_argument_group("LPU5 API monitor (use when API is already running)")
    g_api.add_argument("--api-url", metavar="URL",
                       help="LPU5 API base URL (e.g. http://127.0.0.1:8101). "
                            "Connects to the API's built-in CoT monitor SSE "
                            "stream instead of opening local listeners that "
                            "would conflict with the running API.")

    g_web = parser.add_argument_group("Web UI (graphical interface)")
    g_web.add_argument("--web", action="store_true",
                       help="Start the built-in web UI for graphical monitoring "
                            "and marker-type correction")
    g_web.add_argument("--web-port", metavar="PORT", type=int, default=8888,
                       help="HTTP port for the web UI (default: 8888)")

    g_filter = parser.add_argument_group("Filtering")
    g_filter.add_argument("--meshtastic-only", action="store_true",
                          help="Only show Meshtastic-related events")
    g_filter.add_argument("--types", metavar="LIST",
                          help="Comma-separated list of LPU5 types to show "
                               "(e.g. meshtastic_node,node,tak_maker)")
    g_filter.add_argument("--uid", metavar="REGEX",
                          help="Only show events whose UID matches this regex")

    g_out = parser.add_argument_group("Output")
    g_out.add_argument("--show-xml", action="store_true",
                       help="Include the raw CoT XML in console output")
    g_out.add_argument("--log", metavar="FILE",
                       help="Write plain-text log to FILE")
    g_out.add_argument("--jsonl", metavar="FILE",
                       help="Write JSON-lines log to FILE (machine-readable)")

    args = parser.parse_args()

    if not args.tak_host and not args.listen and not args.ws and not args.api_url:
        if args.web:
            # --web alone: try to auto-detect a running LPU5 API and use its
            # SSE stream.  This avoids the port conflict that would otherwise
            # crash the monitor when the API already occupies TCP 8088 / UDP 4242.
            _default_api = "http://127.0.0.1:8101"
            try:
                import urllib.request
                urllib.request.urlopen(_default_api + "/api/cot/monitor/stats",
                                       timeout=2)
                args.api_url = _default_api
                _log_stderr(f"Auto-detected running LPU5 API at {_default_api}")
            except Exception:
                # API not reachable – fall back to local listener
                args.listen = True
        else:
            parser.error(
                "Specify at least one of: --tak-host, --listen, --ws, or --api-url"
            )

    # ----- Set up output channels -----
    log_fh: Optional[Any] = None
    jsonl_fh: Optional[Any] = None
    if args.log:
        log_fh = open(args.log, "a", encoding="utf-8")
        _log_stderr(f"Logging to {args.log}")
    if args.jsonl:
        jsonl_fh = open(args.jsonl, "a", encoding="utf-8")
        _log_stderr(f"JSON-lines log to {args.jsonl}")

    stats = Stats()
    evt_filter = EventFilter(
        types=args.types.split(",") if args.types else None,
        uid_pattern=args.uid,
        meshtastic_only=args.meshtastic_only,
    )

    # ----- Event store for the web UI -----
    event_store = EventStore() if args.web else None

    output_lock = threading.Lock()

    def on_cot_event(parsed: Dict[str, Any], direction: str,
                     source: str, raw_xml: str):
        """Central handler for every captured CoT event."""
        stats.record(parsed, direction)

        # Always push to web UI store (before filtering – the web UI has
        # its own client-side filters).
        if event_store is not None:
            event_store.add(parsed, direction, source, raw_xml)

        if not evt_filter.match(parsed):
            return

        console = format_event(parsed, direction, source,
                               show_xml=args.show_xml, raw_xml=raw_xml)
        with output_lock:
            print(console, flush=True)
            if log_fh:
                log_fh.write(format_event_plain(parsed, direction, source,
                                                raw_xml=raw_xml) + "\n")
                log_fh.flush()
            if jsonl_fh:
                jsonl_fh.write(_jsonl_record(parsed, direction,
                                             source, raw_xml) + "\n")
                jsonl_fh.flush()

    # ----- Print header -----
    print(_bold("\n╔══════════════════════════════════════════════════════════════╗"))
    print(_bold("║       CoT Data-Flow Monitor – LPU5 ↔ ATAK                  ║"))
    print(_bold("╚══════════════════════════════════════════════════════════════╝"))
    print(f"  Started : {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    if args.tak_host:
        print(f"  TAK     : {args.tak_host}:{args.tak_port} "
              f"({'SSL' if args.ssl else 'TCP'})")
    if args.listen:
        print(f"  Listen  : TCP={args.tcp_port}  UDP={args.udp_port}  "
              f"Multicast=239.2.3.1:6969")
    if args.ws:
        print(f"  WS      : {args.ws}")
    if args.api_url:
        print(f"  API     : {args.api_url} (SSE monitor stream)")
    if args.web:
        print(f"  Web UI  : http://0.0.0.0:{args.web_port}/")
    if args.meshtastic_only:
        print(f"  Filter  : Meshtastic-only")
    if args.types:
        print(f"  Filter  : types={args.types}")
    if args.uid:
        print(f"  Filter  : uid=/{args.uid}/")
    print(f"  Ctrl+C to stop and show summary\n")

    # ----- Start web server -----
    if args.web and event_store is not None:
        html_dir = str(pathlib.Path(__file__).resolve().parent)
        _start_web_server(args.web_port, html_dir, event_store)

    # ----- Start monitors -----
    monitors: List[threading.Thread] = []

    if args.tak_host:
        tak = TAKServerMonitor(
            host=args.tak_host, port=args.tak_port,
            use_ssl=args.ssl,
            certfile=args.certfile, keyfile=args.keyfile,
            password=args.certpass,
            on_event=on_cot_event,
        )
        tak.start()
        monitors.append(tak)

    if args.listen:
        listener = LocalCoTListener(
            tcp_port=args.tcp_port, udp_port=args.udp_port,
            on_event=on_cot_event,
        )
        listener.start()
        monitors.append(listener)

    if args.ws:
        ws_mon = LPU5WebSocketMonitor(api_url=args.ws,
                                       on_event=lambda msg, evt: None)
        ws_mon.start()
        monitors.append(ws_mon)

    if args.api_url:
        api_mon = LPU5APIMonitor(
            api_url=args.api_url,
            on_event=on_cot_event,
        )
        api_mon.start()
        monitors.append(api_mon)

    # ----- Wait for Ctrl+C -----
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass

    print(stats.summary())

    for m in monitors:
        if hasattr(m, "stop"):
            m.stop()

    if log_fh:
        log_fh.close()
    if jsonl_fh:
        jsonl_fh.close()

    print(f"\n{_dim('Done.')}")


if __name__ == "__main__":
    main()
