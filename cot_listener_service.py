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
import sys
import threading
import time
from datetime import datetime, timezone
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
_CONN_TIMEOUT = 30
# Maximum concurrent TCP handler threads.
_MAX_TCP_THREADS = 32


class CoTListenerService:
    """
    Listens on TCP and UDP sockets for incoming CoT XML from ATAK clients.

    For each complete ``<event …>…</event>`` block received the
    ``ingest_callback(xml_string)`` is invoked so the caller can parse and
    store the event without a dependency on this file.
    """

    def __init__(
        self,
        tcp_port: int = 8088,
        udp_port: int = 4242,
        ingest_callback: Optional[Callable[[str], None]] = None,
        bind_address: str = "0.0.0.0",
    ):
        self.tcp_port = tcp_port
        self.udp_port = udp_port
        self.ingest_callback = ingest_callback
        self.bind_address = bind_address

        self._stop = threading.Event()
        self._tcp_thread: Optional[threading.Thread] = None
        self._udp_thread: Optional[threading.Thread] = None
        self._handler_threads: List[threading.Thread] = []

        self.stats: Dict = {
            "running": False,
            "started_at": None,
            "tcp_port": tcp_port,
            "udp_port": udp_port,
            "events_received": 0,
            "events_ingested": 0,
            "tcp_connections": 0,
            "udp_datagrams": 0,
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
    # TCP handler (runs in a per-connection thread)
    # ------------------------------------------------------------------

    def _handle_tcp_connection(self, conn: socket.socket, addr) -> None:
        """Read CoT XML from a single accepted TCP connection."""
        self.stats["tcp_connections"] += 1
        logger.debug("CoT TCP connection from %s", addr)
        buf = b""
        conn.settimeout(_CONN_TIMEOUT)
        try:
            while not self._stop.is_set():
                try:
                    chunk = conn.recv(_RECV_CHUNK)
                except socket.timeout:
                    break
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
                    self._ingest(ev)
                if events:
                    # Trim the consumed prefix from the buffer so we keep any
                    # bytes that arrived after the last </event>.
                    last_end = text.rfind("</event>") + len("</event>")
                    buf = text[last_end:].encode("utf-8")
        except OSError as exc:
            logger.debug("CoT TCP connection %s closed: %s", addr, exc)
        finally:
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
    # Public interface
    # ------------------------------------------------------------------

    def start(self) -> bool:
        """Start TCP and UDP listener threads.  Returns True on success."""
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

        self.stats["running"] = True
        self.stats["started_at"] = datetime.now(timezone.utc).isoformat()
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
        for t in [self._tcp_thread, self._udp_thread]:
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
    args = parser.parse_args()

    print()
    print("=" * 60)
    print("  COT LISTENER SERVICE (standalone)")
    print("=" * 60)
    print(f"  TCP port : {args.tcp_port}")
    print(f"  UDP port : {args.udp_port}")
    print(f"  Bind     : {args.bind}")
    print("=" * 60)
    print()

    service = CoTListenerService(
        tcp_port=args.tcp_port,
        udp_port=args.udp_port,
        ingest_callback=_standalone_ingest,
        bind_address=args.bind,
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
                f"errors={s['errors']}"
            )
    except KeyboardInterrupt:
        print("\nShutdown signal received…")
        service.stop()
        print("✓ Service stopped cleanly")
        sys.exit(0)


if __name__ == "__main__":
    main()
