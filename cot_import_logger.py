#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
COT Import Diagnostic Logger
=============================
Monitors and logs all data flowing from ATAK to LPU5, including:
  - Raw incoming CoT XML
  - CoT type → LPU5 type mapping decisions
  - Meshtastic node detection (detail element, UID prefix, type prefix)
  - Marker create / update / skip actions
  - Gateway JSON import operations

Writes detailed entries to  logs/cot_import_debug.log  (rotating, max 5 MB × 3)
and keeps the last N entries in an in-memory ring buffer for quick API access.

Usage from api.py:
    from cot_import_logger import cot_diag

    cot_diag.log_incoming_cot(uid, event_type, ...)
    cot_diag.log_type_mapping(uid, cot_type, lpu5_type, ...)
    cot_diag.log_marker_action(uid, action, ...)
    cot_diag.log_gateway_import(node_id, ...)
"""

from __future__ import annotations

import collections
import json
import logging
import os
import threading
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
_LOG_FILE = os.path.join(_LOG_DIR, "cot_import_debug.log")
_MAX_BYTES = 5 * 1024 * 1024   # 5 MB per file
_BACKUP_COUNT = 3               # keep 3 rotated copies
_RING_SIZE = 500                # in-memory ring buffer size


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


# ---------------------------------------------------------------------------
# COTImportDiagnostics class
# ---------------------------------------------------------------------------

class COTImportDiagnostics:
    """Central diagnostic logger for all ATAK ↔ LPU5 data exchange."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ring: collections.deque = collections.deque(maxlen=_RING_SIZE)
        self._logger = self._setup_file_logger()
        self._enabled = True

    # ---- setup ------------------------------------------------------------

    @staticmethod
    def _setup_file_logger() -> logging.Logger:
        os.makedirs(_LOG_DIR, exist_ok=True)
        lg = logging.getLogger("cot-import-diag")
        lg.setLevel(logging.DEBUG)
        # Avoid duplicate handlers on reimport
        if not lg.handlers:
            fh = RotatingFileHandler(
                _LOG_FILE, maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT,
                encoding="utf-8",
            )
            fh.setLevel(logging.DEBUG)
            fmt = logging.Formatter(
                "%(asctime)s | %(levelname)-5s | %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
            fh.setFormatter(fmt)
            lg.addHandler(fh)
        lg.propagate = False
        return lg

    # ---- enable / disable -------------------------------------------------

    def enable(self) -> None:
        self._enabled = True

    def disable(self) -> None:
        self._enabled = False

    @property
    def is_enabled(self) -> bool:
        return self._enabled

    # ---- internal helpers -------------------------------------------------

    def _record(self, category: str, data: Dict[str, Any]) -> None:
        """Append an entry to both file and ring buffer."""
        if not self._enabled:
            return
        entry = {
            "ts": _now_iso(),
            "cat": category,
            **data,
        }
        with self._lock:
            self._ring.append(entry)
        # Build a compact one-line log string
        line = f"[{category}] {json.dumps(data, ensure_ascii=False, default=str)}"
        self._logger.info(line)

    # ---- public logging methods -------------------------------------------

    def log_incoming_cot(
        self,
        uid: str,
        event_type: str,
        how: str,
        lat: float,
        lng: float,
        callsign: str,
        has_meshtastic_detail: bool,
        raw_xml_snippet: Optional[str] = None,
    ) -> None:
        """Log a raw incoming CoT event (before type mapping)."""
        self._record("COT_INCOMING", {
            "uid": uid,
            "cot_type": event_type,
            "how": how,
            "lat": lat,
            "lng": lng,
            "callsign": callsign,
            "has_meshtastic_detail": has_meshtastic_detail,
            "raw_xml": (raw_xml_snippet or "")[:2000],
        })

    def log_type_mapping(
        self,
        uid: str,
        cot_type: str,
        initial_lpu5_type: str,
        final_lpu5_type: str,
        reason: str,
        has_meshtastic_detail: bool = False,
        uid_is_mesh: bool = False,
    ) -> None:
        """Log the CoT-type → LPU5-type mapping decision."""
        self._record("TYPE_MAPPING", {
            "uid": uid,
            "cot_type": cot_type,
            "initial_lpu5_type": initial_lpu5_type,
            "final_lpu5_type": final_lpu5_type,
            "reason": reason,
            "has_meshtastic_detail": has_meshtastic_detail,
            "uid_is_mesh_prefix": uid_is_mesh,
        })

    def log_marker_action(
        self,
        uid: str,
        action: str,
        effective_type: str,
        callsign: str,
        lat: float,
        lng: float,
        cot_type: str = "",
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Log a marker create / update / skip action in the DB."""
        self._record("MARKER_ACTION", {
            "uid": uid,
            "action": action,
            "effective_type": effective_type,
            "callsign": callsign,
            "lat": lat,
            "lng": lng,
            "cot_type": cot_type,
            **(extra or {}),
        })

    def log_cot_skipped(
        self,
        uid: str,
        reason: str,
        event_type: str = "",
    ) -> None:
        """Log when a CoT event is skipped (echo-back, dedup, etc.)."""
        self._record("COT_SKIPPED", {
            "uid": uid,
            "reason": reason,
            "cot_type": event_type,
        })

    def log_gateway_import(
        self,
        node_id: str,
        action: str,
        callsign: str = "",
        lat: float = 0.0,
        lng: float = 0.0,
        raw_node_snippet: Optional[str] = None,
        error: Optional[str] = None,
    ) -> None:
        """Log a single node from a Meshtastic Gateway JSON import."""
        data: Dict[str, Any] = {
            "node_id": node_id,
            "action": action,
            "callsign": callsign,
            "lat": lat,
            "lng": lng,
        }
        if raw_node_snippet:
            data["raw_node"] = raw_node_snippet[:2000]
        if error:
            data["error"] = error
        self._record("GATEWAY_IMPORT", data)

    def log_gateway_import_summary(
        self,
        total: int,
        imported: int,
        errors: int,
        source: str,
    ) -> None:
        """Log the final summary of a gateway import batch."""
        self._record("GATEWAY_IMPORT_SUMMARY", {
            "total_nodes": total,
            "imported": imported,
            "errors": errors,
            "source": source,
        })

    def log_meshtastic_mismatch(
        self,
        uid: str,
        cot_type: str,
        expected_lpu5_type: str,
        actual_lpu5_type: str,
        details: str = "",
    ) -> None:
        """Log a detected mismatch between ATAK and LPU5 Meshtastic node types."""
        self._record("MESHTASTIC_MISMATCH", {
            "uid": uid,
            "cot_type": cot_type,
            "expected_lpu5_type": expected_lpu5_type,
            "actual_lpu5_type": actual_lpu5_type,
            "details": details,
        })

    # ---- read / clear -----------------------------------------------------

    def get_entries(
        self,
        limit: int = 200,
        category: Optional[str] = None,
        uid_filter: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Return recent log entries from the in-memory ring buffer."""
        with self._lock:
            items = list(self._ring)
        # newest first
        items.reverse()
        if category:
            cat_upper = category.upper()
            items = [e for e in items if e.get("cat") == cat_upper]
        if uid_filter:
            items = [
                e for e in items
                if uid_filter in (e.get("uid", "") or e.get("node_id", ""))
            ]
        return items[:limit]

    def clear(self) -> int:
        """Clear the in-memory ring buffer. Returns number of entries cleared."""
        with self._lock:
            n = len(self._ring)
            self._ring.clear()
        self._logger.info("[ADMIN] Ring buffer cleared (%d entries removed)", n)
        return n

    def get_log_file_path(self) -> str:
        return _LOG_FILE

    def get_log_file_content(self, tail_lines: int = 500) -> str:
        """Read the last N lines from the log file."""
        if not os.path.exists(_LOG_FILE):
            return ""
        try:
            with open(_LOG_FILE, "r", encoding="utf-8") as f:
                lines = f.readlines()
            return "".join(lines[-tail_lines:])
        except Exception:
            return ""


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

cot_diag = COTImportDiagnostics()
