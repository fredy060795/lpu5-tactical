#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cot_listener_service.py - CoT Socket Listener Service

Listens on TCP and/or UDP sockets for incoming Cursor-on-Target (CoT) XML
from ATAK / WinTAK / ITAK clients.  Each received event is forwarded to an
``ingest_callback`` for local processing (database upsert + WebSocket
broadcast), mirroring the approach used by meshtastic_gateway_service.py.

Usage alongside api.py:

    service = CoTListenerService(
        tcp_port=8088,
        udp_port=4242,
        ingest_callback=my_ingest_fn,
    )
    service.start()
    ...
    service.stop()

Standalone (for testing):

    python cot_listener_service.py --tcp-port 8088 --udp-port 4242
"""

import argparse
import http.server
import json
import logging
import os
import pathlib
import queue
import socket
import ssl
import struct
import sys
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s - %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("CoTListener")

# Maximum bytes buffered per TCP connection before the connection is reset.
_MAX_BUFFER = 65536  # 64 KB
# Receive chunk size for TCP reads.
_RECV_CHUNK = 4096
# Seconds a TCP client may be idle before the connection is closed.
# WinTAK/ATAK send SA beacons roughly every 30–60 s; use a generous timeout
# so connections stay alive during brief quiet periods.
_CONN_TIMEOUT = 120
# Maximum concurrent TCP handler threads.
_MAX_TCP_THREADS = 32


class CoTListenerService:
    """
    Listens on TCP and UDP sockets for incoming CoT XML from ATAK clients.

    For each complete ``<event …>…</event>`` block received the
    ``ingest_callback(xml_string)`` is invoked so the caller can parse and
    store the event without a dependency on this file.
    """

    # Default SA Multicast address and port used by WinTAK/ATAK for Situational Awareness.
    SA_MULTICAST_GROUP = "239.2.3.1"
    SA_MULTICAST_PORT  = 6969

    def __init__(
        self,
        tcp_port: int = 8088,
        udp_port: int = 4242,
        ingest_callback: Optional[Callable[[str], None]] = None,
        bind_address: str = "0.0.0.0",
        multicast_enabled: bool = False,
        multicast_group: str = SA_MULTICAST_GROUP,
        multicast_port: int = SA_MULTICAST_PORT,
        on_client_connect: Optional[Callable[["socket.socket", tuple], None]] = None,
    ):
        self.tcp_port = tcp_port
        self.udp_port = udp_port
        self.ingest_callback = ingest_callback
        self.bind_address = bind_address
        self.multicast_enabled = multicast_enabled
        self.multicast_group = multicast_group
        self.multicast_port = multicast_port
        # Called with (socket, addr) whenever a new TCP client connects.
        # Callers can use this to send an SA greeting or initial state dump.
        self.on_client_connect = on_client_connect

        self._stop = threading.Event()
        self._tcp_thread: Optional[threading.Thread] = None
        self._udp_thread: Optional[threading.Thread] = None
        self._multicast_thread: Optional[threading.Thread] = None
        self._handler_threads: List[threading.Thread] = []

        # Active TCP client sockets — used by send_to_clients() to push data
        # back to WinTAK/ATAK clients that are directly connected on port 8088.
        self._clients: Dict[int, "socket.socket"] = {}
        self._clients_lock = threading.Lock()

        self.stats: Dict = {
            "running": False,
            "started_at": None,
            "tcp_port": tcp_port,
            "udp_port": udp_port,
            "multicast_enabled": multicast_enabled,
            "multicast_group": multicast_group,
            "multicast_port": multicast_port,
            "events_received": 0,
            "events_ingested": 0,
            "tcp_connections": 0,
            "tcp_clients_active": 0,
            "udp_datagrams": 0,
            "multicast_datagrams": 0,
            "errors": 0,
        }

    # ------------------------------------------------------------------
    # CoT XML extraction helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_cot_events(data: str) -> List[str]:
        """
        Extract all complete ``<event …>…</event>`` blocks from *data*.

        Returns a list of XML strings, each containing exactly one CoT event.
        Handles multiple events packed into a single TCP segment and strips any
        TAK auth ``<auth>…</auth>`` preambles automatically.
        """
        events: List[str] = []
        search_from = 0
        while True:
            # Find the next opening tag (with or without attributes)
            idx_space = data.find("<event ", search_from)
            idx_plain = data.find("<event>", search_from)
            if idx_space == -1 and idx_plain == -1:
                break
            # Pick the earlier occurrence
            if idx_space == -1:
                start = idx_plain
            elif idx_plain == -1:
                start = idx_space
            else:
                start = min(idx_space, idx_plain)

            end = data.find("</event>", start)
            if end == -1:
                break  # incomplete – wait for more data
            end += len("</event>")
            events.append(data[start:end])
            search_from = end
        return events

    # ------------------------------------------------------------------
    # CoT ping-ack builder
    # ------------------------------------------------------------------

    @staticmethod
    def _build_pong_xml() -> str:
        """Build a minimal CoT t-x-c-t-r ping-ack to reply to a WinTAK/ATAK ping."""
        now = datetime.now(timezone.utc)
        stale = now + timedelta(seconds=30)
        fmt = lambda dt: dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"
        return (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<event version="2.0" uid="LPU5-GW" type="t-x-c-t-r" how="m-g"'
            f' time="{fmt(now)}" start="{fmt(now)}" stale="{fmt(stale)}">'
            '<point lat="0.0" lon="0.0" hae="0.0" ce="9999999.0" le="9999999.0"/>'
            '<detail/></event>'
        )

    # ------------------------------------------------------------------
    # Ingest helper
    # ------------------------------------------------------------------

    def _ingest(self, xml_string: str) -> None:
        """Forward a single CoT XML string to the registered callback."""
        self.stats["events_received"] += 1
        if not self.ingest_callback:
            logger.debug("CoT event received (no ingest_callback registered): %s…", xml_string[:80])
            return
        try:
            self.ingest_callback(xml_string)
            self.stats["events_ingested"] += 1
        except Exception as exc:
            logger.error("CoT ingest_callback error: %s", exc)
            self.stats["errors"] += 1

    # ------------------------------------------------------------------
    # TCP client push (bidirectional data exchange)
    # ------------------------------------------------------------------

    def send_to_clients(self, cot_xml: str) -> int:
        """
        Push a CoT XML string to all currently connected TCP clients
        (e.g. WinTAK/ATAK instances connected directly on port 8088).

        Returns the number of clients successfully reached.
        Dead connections are pruned automatically.
        """
        data = cot_xml.encode("utf-8")
        sent = 0
        with self._clients_lock:
            dead: List[int] = []
            for fd, sock in list(self._clients.items()):
                try:
                    sock.sendall(data)
                    sent += 1
                except OSError:
                    dead.append(fd)
            for fd in dead:
                self._clients.pop(fd, None)
        if sent:
            logger.debug("CoT pushed to %d TCP client(s) (%d bytes)", sent, len(data))
        return sent

    # ------------------------------------------------------------------
    # TCP handler (runs in a per-connection thread)
    # ------------------------------------------------------------------

    def _handle_tcp_connection(self, conn: socket.socket, addr) -> None:
        """Read CoT XML from a single accepted TCP connection."""
        self.stats["tcp_connections"] += 1
        logger.debug("CoT TCP connection from %s", addr)
        buf = b""
        conn.settimeout(_CONN_TIMEOUT)
        fd = conn.fileno()
        # Register so send_to_clients() can push data back to this client.
        with self._clients_lock:
            self._clients[fd] = conn
            self.stats["tcp_clients_active"] = len(self._clients)
        # Notify the caller (api.py) so it can send an SA greeting or initial
        # state dump to the newly connected WinTAK/ATAK client.
        if self.on_client_connect is not None:
            try:
                self.on_client_connect(conn, addr)
            except Exception as _connect_exc:
                logger.debug("CoT on_client_connect callback error for %s: %s", addr, _connect_exc)
        try:
            while not self._stop.is_set():
                try:
                    chunk = conn.recv(_RECV_CHUNK)
                except socket.timeout:
                    continue  # No data yet; check stop event and loop again
                if not chunk:
                    break
                buf += chunk
                if len(buf) > _MAX_BUFFER:
                    logger.warning(
                        "CoT TCP buffer overflow from %s (%d bytes) – discarding", addr, len(buf)
                    )
                    buf = b""
                    break
                text = buf.decode("utf-8", errors="replace")
                events = self._extract_cot_events(text)
                for ev in events:
                    # Respond to TAK ping (t-x-c-t) with a ping-ack (t-x-c-t-r)
                    # so that WinTAK/ATAK keeps the connection alive.
                    if (' type="t-x-c-t"' in ev or " type='t-x-c-t'" in ev) and "t-x-c-t-r" not in ev:
                        try:
                            conn.sendall(self._build_pong_xml().encode("utf-8"))
                            logger.debug("CoT TCP: sent ping-ack to %s", addr)
                        except OSError:
                            break
                    else:
                        self._ingest(ev)
                if events:
                    # Trim the consumed prefix from the buffer so we keep any
                    # bytes that arrived after the last </event>.
                    last_end = text.rfind("</event>") + len("</event>")
                    buf = text[last_end:].encode("utf-8")
        except OSError as exc:
            logger.debug("CoT TCP connection %s closed: %s", addr, exc)
        finally:
            # Unregister the client so it is no longer included in broadcasts.
            with self._clients_lock:
                self._clients.pop(fd, None)
                self.stats["tcp_clients_active"] = len(self._clients)
            try:
                conn.close()
            except OSError:
                pass

    # ------------------------------------------------------------------
    # TCP listener loop
    # ------------------------------------------------------------------

    def _tcp_listener(self) -> None:
        srv: Optional[socket.socket] = None
        try:
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.settimeout(1.0)  # allows the stop-event to be polled
            srv.bind((self.bind_address, self.tcp_port))
            srv.listen(16)
            logger.info("CoT TCP listener started on %s:%d", self.bind_address, self.tcp_port)
            while not self._stop.is_set():
                try:
                    conn, addr = srv.accept()
                except socket.timeout:
                    continue
                # Prune finished handler threads before spawning a new one
                self._handler_threads = [t for t in self._handler_threads if t.is_alive()]
                if len(self._handler_threads) >= _MAX_TCP_THREADS:
                    logger.warning("CoT TCP: max concurrent connections reached, rejecting %s", addr)
                    try:
                        conn.close()
                    except OSError:
                        pass
                    continue
                t = threading.Thread(
                    target=self._handle_tcp_connection,
                    args=(conn, addr),
                    daemon=True,
                    name=f"cot-tcp-{addr[0]}:{addr[1]}",
                )
                t.start()
                self._handler_threads.append(t)
        except OSError as exc:
            if not self._stop.is_set():
                logger.error("CoT TCP listener error on port %d: %s", self.tcp_port, exc)
                self.stats["errors"] += 1
        finally:
            if srv:
                try:
                    srv.close()
                except OSError:
                    pass
            logger.info("CoT TCP listener stopped")

    # ------------------------------------------------------------------
    # UDP listener loop
    # ------------------------------------------------------------------

    def _udp_listener(self) -> None:
        sock: Optional[socket.socket] = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.settimeout(1.0)
            sock.bind((self.bind_address, self.udp_port))
            logger.info("CoT UDP listener started on %s:%d", self.bind_address, self.udp_port)
            while not self._stop.is_set():
                try:
                    data, addr = sock.recvfrom(_MAX_BUFFER)
                except socket.timeout:
                    continue
                self.stats["udp_datagrams"] += 1
                text = data.decode("utf-8", errors="replace").strip()
                events = self._extract_cot_events(text)
                if events:
                    for ev in events:
                        self._ingest(ev)
                elif text.startswith("<event") or "<event " in text:
                    # The full datagram is a single event without </event> tail
                    self._ingest(text)
                else:
                    logger.debug("CoT UDP datagram from %s not a CoT event – ignored", addr)
        except OSError as exc:
            if not self._stop.is_set():
                logger.error("CoT UDP listener error on port %d: %s", self.udp_port, exc)
                self.stats["errors"] += 1
        finally:
            if sock:
                try:
                    sock.close()
                except OSError:
                    pass
            logger.info("CoT UDP listener stopped")

    # ------------------------------------------------------------------
    # SA Multicast listener loop (239.2.3.1:6969 by default)
    # ------------------------------------------------------------------

    def _multicast_listener(self) -> None:
        """Listen on the SA Multicast group for incoming CoT events from WinTAK/ATAK."""
        sock: Optional[socket.socket] = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            # On some platforms SO_REUSEPORT is needed to share the port with WinTAK
            if hasattr(socket, "SO_REUSEPORT"):
                try:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
                except OSError:
                    pass
            sock.settimeout(1.0)
            # Bind to the multicast port on all interfaces so we receive traffic
            # destined for the group regardless of the source interface.
            sock.bind(("", self.multicast_port))
            # Join the SA Multicast group
            # struct format: '4s' = 4-byte packed IPv4 address, 'L' = unsigned long (INADDR_ANY = all interfaces)
            mreq = struct.pack("4sL", socket.inet_aton(self.multicast_group), socket.INADDR_ANY)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
            logger.info(
                "CoT SA Multicast listener started on %s:%d",
                self.multicast_group,
                self.multicast_port,
            )
            while not self._stop.is_set():
                try:
                    data, addr = sock.recvfrom(_MAX_BUFFER)
                except socket.timeout:
                    continue
                self.stats["multicast_datagrams"] += 1
                text = data.decode("utf-8", errors="replace").strip()
                events = self._extract_cot_events(text)
                if events:
                    for ev in events:
                        self._ingest(ev)
                elif text.startswith("<event") or "<event " in text:
                    self._ingest(text)
                else:
                    logger.debug("SA Multicast datagram from %s not a CoT event – ignored", addr)
        except OSError as exc:
            if not self._stop.is_set():
                logger.error(
                    "CoT SA Multicast listener error on %s:%d: %s",
                    self.multicast_group,
                    self.multicast_port,
                    exc,
                )
                self.stats["errors"] += 1
        finally:
            if sock:
                try:
                    sock.close()
                except OSError:
                    pass
            logger.info("CoT SA Multicast listener stopped")

    # ------------------------------------------------------------------
    # SA Multicast send helper
    # ------------------------------------------------------------------

    def send_multicast(self, cot_xml: str) -> bool:
        """
        Send a CoT XML string to the SA Multicast group (e.g. 239.2.3.1:6969).

        This allows LPU5 to push marker updates to WinTAK/ATAK on the same
        LAN (or same machine) without requiring a dedicated TAK server.

        Returns True on success, False on error.
        """
        if not self.multicast_enabled:
            return False
        try:
            data = cot_xml.encode("utf-8")
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            # Set TTL=32 so multicast traffic stays within the local network.
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 32)
            sock.settimeout(2.0)
            try:
                sock.sendto(data, (self.multicast_group, self.multicast_port))
            finally:
                sock.close()
            logger.debug(
                "CoT sent to SA Multicast %s:%d (%d bytes)",
                self.multicast_group,
                self.multicast_port,
                len(data),
            )
            return True
        except Exception as exc:
            logger.warning(
                "CoT SA Multicast send to %s:%d failed: %s",
                self.multicast_group,
                self.multicast_port,
                exc,
            )
            return False


    def start(self) -> bool:
        """Start TCP, UDP, and (optionally) SA Multicast listener threads.  Returns True on success."""
        if self.stats["running"]:
            logger.warning("CoTListenerService is already running")
            return True

        self._stop.clear()

        self._tcp_thread = threading.Thread(
            target=self._tcp_listener,
            daemon=True,
            name="cot-listener-tcp",
        )
        self._udp_thread = threading.Thread(
            target=self._udp_listener,
            daemon=True,
            name="cot-listener-udp",
        )
        self._tcp_thread.start()
        self._udp_thread.start()

        if self.multicast_enabled:
            self._multicast_thread = threading.Thread(
                target=self._multicast_listener,
                daemon=True,
                name="cot-listener-multicast",
            )
            self._multicast_thread.start()

        self.stats["running"] = True
        self.stats["started_at"] = datetime.now(timezone.utc).isoformat()
        if self.multicast_enabled:
            logger.info(
                "✓ CoTListenerService started (TCP:%d, UDP:%d, SA Multicast:%s:%d)",
                self.tcp_port,
                self.udp_port,
                self.multicast_group,
                self.multicast_port,
            )
        else:
            logger.info(
                "✓ CoTListenerService started (TCP:%d, UDP:%d)",
                self.tcp_port,
                self.udp_port,
            )
        return True

    def stop(self) -> None:
        """Signal listener threads to stop and wait for them to finish."""
        logger.info("Stopping CoTListenerService…")
        self._stop.set()
        for t in [self._tcp_thread, self._udp_thread, self._multicast_thread]:
            if t and t.is_alive():
                t.join(timeout=5)
        self.stats["running"] = False
        logger.info("✓ CoTListenerService stopped")


    def get_status(self) -> Dict:
        """Return a copy of the current statistics dict."""
        status = dict(self.stats)
        if status.get("started_at"):
            try:
                started = datetime.fromisoformat(status["started_at"])
                status["uptime_seconds"] = int(
                    (datetime.now(timezone.utc) - started).total_seconds()
                )
            except Exception:
                status["uptime_seconds"] = 0
        return status


# ---------------------------------------------------------------------------
# CoT type → LPU5 type mapping (mirrors cot_protocol.py / cot_data_monitor.py)
# ---------------------------------------------------------------------------

# CoT type → LPU5 type mapping (mirrors cot_protocol.py / cot_data_monitor.py).
# Entries are checked in order; more specific prefixes must appear before
# general ones (e.g. "a-f-G-E-S-U-M" before "a-f-G-E" before "a-f").
_COT_TO_LPU5_TYPE: List[Tuple[str, str]] = [
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

_ATAK_TO_CBT_TYPE = {
    "hostile":  "cbt_hostile",
    "friendly": "cbt_friendly",
    "neutral":  "cbt_neutral",
    "unknown":  "cbt_unknown",
}


def _cot_type_to_lpu5(cot_type: str) -> str:
    """Map a CoT type string to the LPU5 internal type (prefix match)."""
    for prefix, lpu5 in _COT_TO_LPU5_TYPE:
        if cot_type.startswith(prefix):
            return lpu5
    return "unknown"


def _parse_cot_event(xml_str: str) -> Optional[Dict[str, Any]]:
    """
    Parse a CoT XML ``<event>`` into a flat dictionary with all fields
    relevant for displaying in the monitor UI.

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

    # ----- LPU5 type detection -----
    base_lpu5_type = _cot_type_to_lpu5(cot_type)
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
    elif base_lpu5_type == "friendly":
        detected_type = "meshtastic_node"
        detection_reason = f"friendly + how='{how}' → meshtastic_node (Meshtastic node relayed by ATAK)"
    else:
        cbt = _ATAK_TO_CBT_TYPE.get(base_lpu5_type)
        if cbt:
            detected_type = cbt
            detection_reason = f"ATAK CBT remapping: '{base_lpu5_type}' → '{cbt}'"

    # mesh- UID override
    if uid.startswith("mesh-"):
        detected_type = "node"
        detection_reason = "UID prefix 'mesh-' override → 'node'"

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
# Event store (shared between listener and web UI)
# ---------------------------------------------------------------------------

class _EventStore:
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
        """Return a summary of all events with corrections for export."""
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
        }


# ---------------------------------------------------------------------------
# Built-in HTTP server for the web UI (mirrors cot_data_monitor.py)
# ---------------------------------------------------------------------------

_event_store: Optional[_EventStore] = None
_html_dir: str = ""


class _MonitorHTTPHandler(http.server.BaseHTTPRequestHandler):
    """Serve cot_monitor_ui.html and provide API endpoints for the web UI."""

    def log_message(self, fmt, *a):
        pass  # suppress default request logging

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
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        filename = f"cot_monitor_log_{ts}.json"
        filepath = os.path.join(_html_dir, filename)
        try:
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(body, f, ensure_ascii=False, indent=2)
            logger.info("Log saved to %s", filepath)
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
                      store: _EventStore) -> threading.Thread:
    """Start the HTTP server for the web UI in a daemon thread."""
    global _event_store, _html_dir
    _event_store = store
    _html_dir = html_dir

    server = http.server.ThreadingHTTPServer(("0.0.0.0", port),
                                              _MonitorHTTPHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True,
                         name="cot-listener-web")
    t.start()
    logger.info("Web UI running on http://0.0.0.0:%d/", port)
    return t


# ---------------------------------------------------------------------------
# Standalone entry point (for testing / diagnostics)
# ---------------------------------------------------------------------------

def _standalone_ingest(xml_string: str) -> None:
    """Simple callback that prints received CoT events to stdout."""
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}] CoT event received ({len(xml_string)} bytes):")
    print(xml_string[:200] + ("…" if len(xml_string) > 200 else ""))
    print()


def _make_web_ingest(store: _EventStore):
    """Return an ingest callback that parses CoT XML and stores it in the EventStore."""
    def _ingest(xml_string: str) -> None:
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        print(f"[{ts}] CoT event received ({len(xml_string)} bytes)")
        parsed = _parse_cot_event(xml_string)
        if parsed:
            store.add(parsed, "<<<", "CoT Listener", xml_string)
        else:
            logger.debug("Could not parse CoT event: %s…", xml_string[:80])
    return _ingest


# ---------------------------------------------------------------------------
# iTAK CoT Bridge Server  (SSL TCP on 127.0.0.1:8089)
# ---------------------------------------------------------------------------
# Meshtastic's iOS app includes a built-in TAK server that bridges CoT between
# iTAK and the LoRa mesh (default 127.0.0.1:4242).  The iTAKBridgeServer
# replicates this pattern on port 8089 with TLS so the LPU5 ecosystem can
# serve the same role — iTAK connects to 127.0.0.1:8089 (SSL) and CoT events
# flow bidirectionally through the existing ingest pipeline into the mesh.
# ---------------------------------------------------------------------------

class iTAKBridgeServer:
    """
    Local SSL TCP server that bridges CoT XML between iTAK on iPhone
    and the Meshtastic mesh network.

    Binds to ``127.0.0.1:8089`` (SSL/TLS) by default.  iTAK connects as a
    regular TAK server client and exchanges CoT XML.  Received events are
    forwarded to *ingest_callback* (the same pipeline as the main CoT
    listener), and outgoing CoT can be pushed to all connected iTAK
    clients via :meth:`send_to_clients`.

    The server auto-generates a self-signed certificate when
    *cert_path*/*key_path* do not exist, using :func:`generate_self_signed_cert`
    from ``generate_cert.py``.
    """

    DEFAULT_PORT = 8089
    DEFAULT_BIND = "127.0.0.1"

    def __init__(
        self,
        port: int = DEFAULT_PORT,
        bind_address: str = DEFAULT_BIND,
        cert_path: str = "cert.pem",
        key_path: str = "key.pem",
        ingest_callback: Optional[Callable[[str], None]] = None,
        on_client_connect: Optional[Callable[["socket.socket", tuple], None]] = None,
    ):
        self.port = port
        self.bind_address = bind_address
        self.cert_path = cert_path
        self.key_path = key_path
        self.ingest_callback = ingest_callback
        self.on_client_connect = on_client_connect

        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._handler_threads: List[threading.Thread] = []

        self._clients: Dict[int, "socket.socket"] = {}
        self._clients_lock = threading.Lock()

        self.stats: Dict[str, Any] = {
            "running": False,
            "started_at": None,
            "port": port,
            "bind_address": bind_address,
            "ssl": True,
            "events_received": 0,
            "events_ingested": 0,
            "connections": 0,
            "clients_active": 0,
            "errors": 0,
        }

    # ------------------------------------------------------------------
    # SSL context
    # ------------------------------------------------------------------

    def _get_ssl_context(self) -> ssl.SSLContext:
        """Create a server-side TLS context, generating certs if missing."""
        if not os.path.exists(self.cert_path) or not os.path.exists(self.key_path):
            logger.info("iTAK bridge: generating self-signed certificate …")
            try:
                from generate_cert import generate_self_signed_cert
                if not generate_self_signed_cert(self.cert_path, self.key_path, "127.0.0.1"):
                    raise RuntimeError("Certificate generation returned False")
            except Exception as exc:
                logger.error("iTAK bridge: cert generation failed: %s", exc)
                raise

        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(self.cert_path, self.key_path)
        return ctx

    # ------------------------------------------------------------------
    # Ingest / send helpers (reuse CoTListenerService statics)
    # ------------------------------------------------------------------

    def _ingest(self, xml_string: str) -> None:
        self.stats["events_received"] += 1
        if not self.ingest_callback:
            logger.debug("iTAK bridge event (no callback): %s…", xml_string[:80])
            return
        try:
            self.ingest_callback(xml_string)
            self.stats["events_ingested"] += 1
        except Exception as exc:
            logger.error("iTAK bridge ingest error: %s", exc)
            self.stats["errors"] += 1

    def send_to_clients(self, cot_xml: str) -> int:
        """Push CoT XML to all connected iTAK clients.  Returns count of clients reached."""
        data = cot_xml.encode("utf-8")
        sent = 0
        with self._clients_lock:
            dead: List[int] = []
            for fd, sock in list(self._clients.items()):
                try:
                    sock.sendall(data)
                    sent += 1
                except OSError:
                    dead.append(fd)
            for fd in dead:
                self._clients.pop(fd, None)
                self.stats["clients_active"] = len(self._clients)
        if sent:
            logger.debug("iTAK bridge: pushed CoT to %d client(s)", sent)
        return sent

    # ------------------------------------------------------------------
    # Per-connection handler
    # ------------------------------------------------------------------

    def _handle_connection(self, conn: socket.socket, addr: tuple) -> None:
        self.stats["connections"] += 1
        logger.info("iTAK bridge: connection from %s", addr)
        buf = b""
        conn.settimeout(_CONN_TIMEOUT)
        fd = conn.fileno()
        with self._clients_lock:
            self._clients[fd] = conn
            self.stats["clients_active"] = len(self._clients)
        if self.on_client_connect is not None:
            try:
                self.on_client_connect(conn, addr)
            except Exception:
                pass
        try:
            while not self._stop.is_set():
                try:
                    chunk = conn.recv(_RECV_CHUNK)
                except socket.timeout:
                    continue
                except ssl.SSLError as e:
                    if "timed out" in str(e):
                        continue
                    raise
                if not chunk:
                    break
                buf += chunk
                if len(buf) > _MAX_BUFFER:
                    logger.warning("iTAK bridge: buffer overflow from %s – discarding", addr)
                    buf = b""
                    break
                text = buf.decode("utf-8", errors="replace")
                events = CoTListenerService._extract_cot_events(text)
                for ev in events:
                    if (' type="t-x-c-t"' in ev or " type='t-x-c-t'" in ev) and "t-x-c-t-r" not in ev:
                        try:
                            conn.sendall(CoTListenerService._build_pong_xml().encode("utf-8"))
                        except OSError:
                            break
                    else:
                        self._ingest(ev)
                if events:
                    last_end = text.rfind("</event>") + len("</event>")
                    buf = text[last_end:].encode("utf-8")
        except (OSError, ssl.SSLError) as exc:
            logger.debug("iTAK bridge: connection %s closed: %s", addr, exc)
        finally:
            with self._clients_lock:
                self._clients.pop(fd, None)
                self.stats["clients_active"] = len(self._clients)
            try:
                conn.close()
            except OSError:
                pass

    # ------------------------------------------------------------------
    # Listener loop
    # ------------------------------------------------------------------

    def _listener(self) -> None:
        srv: Optional[socket.socket] = None
        try:
            ssl_ctx = self._get_ssl_context()
            raw_srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            raw_srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            raw_srv.settimeout(1.0)
            raw_srv.bind((self.bind_address, self.port))
            raw_srv.listen(8)
            srv = ssl_ctx.wrap_socket(raw_srv, server_side=True)
            srv.settimeout(1.0)
            logger.info(
                "✓ iTAK CoT Bridge started on %s:%d (SSL/TLS)",
                self.bind_address, self.port,
            )
            while not self._stop.is_set():
                try:
                    conn, addr = srv.accept()
                except socket.timeout:
                    continue
                except ssl.SSLError as e:
                    logger.debug("iTAK bridge: SSL accept error: %s", e)
                    continue
                self._handler_threads = [t for t in self._handler_threads if t.is_alive()]
                if len(self._handler_threads) >= _MAX_TCP_THREADS:
                    logger.warning("iTAK bridge: max connections reached, rejecting %s", addr)
                    try:
                        conn.close()
                    except OSError:
                        pass
                    continue
                t = threading.Thread(
                    target=self._handle_connection,
                    args=(conn, addr),
                    daemon=True,
                    name=f"itak-bridge-{addr[0]}:{addr[1]}",
                )
                t.start()
                self._handler_threads.append(t)
        except OSError as exc:
            if not self._stop.is_set():
                logger.error("iTAK bridge listener error on port %d: %s", self.port, exc)
                self.stats["errors"] += 1
        finally:
            if srv:
                try:
                    srv.close()
                except OSError:
                    pass
            logger.info("iTAK bridge listener stopped")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> bool:
        """Start the SSL listener thread.  Returns True on success."""
        if self.stats["running"]:
            logger.warning("iTAK bridge is already running")
            return True
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._listener, daemon=True, name="itak-bridge-ssl",
        )
        self._thread.start()
        self.stats["running"] = True
        self.stats["started_at"] = datetime.now(timezone.utc).isoformat()
        return True

    def stop(self) -> None:
        """Signal the listener to stop and wait for threads to finish."""
        logger.info("Stopping iTAK CoT Bridge …")
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        for t in self._handler_threads:
            if t.is_alive():
                t.join(timeout=2)
        self._handler_threads.clear()
        with self._clients_lock:
            for sock in self._clients.values():
                try:
                    sock.close()
                except OSError:
                    pass
            self._clients.clear()
        self.stats["running"] = False
        self.stats["clients_active"] = 0
        logger.info("✓ iTAK CoT Bridge stopped")

    def get_status(self) -> Dict[str, Any]:
        """Return a copy of the current statistics dict."""
        status = dict(self.stats)
        if status.get("started_at"):
            try:
                started = datetime.fromisoformat(status["started_at"])
                status["uptime_seconds"] = int(
                    (datetime.now(timezone.utc) - started).total_seconds()
                )
            except Exception:
                status["uptime_seconds"] = 0
        return status


def main() -> None:
    parser = argparse.ArgumentParser(description="CoT Socket Listener Service (standalone)")
    parser.add_argument("--tcp-port", type=int, default=8088, help="TCP listen port (default 8088)")
    parser.add_argument("--udp-port", type=int, default=4242, help="UDP listen port (default 4242)")
    parser.add_argument("--bind", default="0.0.0.0", help="Bind address (default 0.0.0.0)")
    parser.add_argument(
        "--multicast", action="store_true", default=False,
        help="Enable SA Multicast listener (default: disabled)"
    )
    parser.add_argument(
        "--multicast-group", default=CoTListenerService.SA_MULTICAST_GROUP,
        help=f"SA Multicast group (default {CoTListenerService.SA_MULTICAST_GROUP})"
    )
    parser.add_argument(
        "--multicast-port", type=int, default=CoTListenerService.SA_MULTICAST_PORT,
        help=f"SA Multicast port (default {CoTListenerService.SA_MULTICAST_PORT})"
    )
    parser.add_argument(
        "--web", action="store_true", default=False,
        help="Start the built-in web UI (cot_monitor_ui.html) for graphical monitoring"
    )
    parser.add_argument(
        "--web-port", type=int, default=8888,
        help="HTTP port for the web UI (default 8888)"
    )
    args = parser.parse_args()

    print()
    print("=" * 60)
    print("  COT LISTENER SERVICE (standalone)")
    print("=" * 60)
    print(f"  TCP port : {args.tcp_port}")
    print(f"  UDP port : {args.udp_port}")
    print(f"  Bind     : {args.bind}")
    if args.multicast:
        print(f"  SA Mcast : {args.multicast_group}:{args.multicast_port} (enabled)")
    else:
        print(f"  SA Mcast : disabled (use --multicast to enable)")
    if args.web:
        print(f"  Web UI   : http://0.0.0.0:{args.web_port}/")
    print("=" * 60)
    print()

    # Set up web UI if requested
    event_store: Optional[_EventStore] = None
    if args.web:
        event_store = _EventStore()
        html_dir = str(pathlib.Path(__file__).resolve().parent)
        _start_web_server(args.web_port, html_dir, event_store)

    # Choose ingest callback based on web UI mode
    if event_store is not None:
        ingest_cb = _make_web_ingest(event_store)
    else:
        ingest_cb = _standalone_ingest

    service = CoTListenerService(
        tcp_port=args.tcp_port,
        udp_port=args.udp_port,
        ingest_callback=ingest_cb,
        bind_address=args.bind,
        multicast_enabled=args.multicast,
        multicast_group=args.multicast_group,
        multicast_port=args.multicast_port,
    )
    service.start()

    print("Listener running.  Press Ctrl+C to stop.\n")
    try:
        while True:
            time.sleep(5)
            s = service.get_status()
            print(
                f"[stats] events_received={s['events_received']}  "
                f"tcp_conn={s['tcp_connections']}  "
                f"udp_dgram={s['udp_datagrams']}  "
                f"mcast_dgram={s.get('multicast_datagrams', 0)}  "
                f"errors={s['errors']}"
            )
    except KeyboardInterrupt:
        print("\nShutdown signal received…")
        service.stop()
        print("✓ Service stopped cleanly")
        sys.exit(0)


if __name__ == "__main__":
    main()
