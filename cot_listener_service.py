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
import logging
import socket
import struct
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional

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
# Standalone entry point (for testing / diagnostics)
# ---------------------------------------------------------------------------

def _standalone_ingest(xml_string: str) -> None:
    """Simple callback that prints received CoT events to stdout."""
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}] CoT event received ({len(xml_string)} bytes):")
    print(xml_string[:200] + ("…" if len(xml_string) > 200 else ""))
    print()


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
    print("=" * 60)
    print()

    service = CoTListenerService(
        tcp_port=args.tcp_port,
        udp_port=args.udp_port,
        ingest_callback=_standalone_ingest,
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
