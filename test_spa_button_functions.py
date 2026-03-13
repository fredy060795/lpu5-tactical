#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_spa_button_functions.py - Verify SPA inline event handlers have their
functions exposed to the global (window) scope.

When individual pages were merged into the SPA (index.html), functions defined
inside _spaInit_* closures must be explicitly exposed via `window.fn = fn;`
for inline onclick/onchange/oninput handlers to reach them.

This test parses index.html and checks that every function called from an
inline event handler is either:
  (a) assigned to window.* somewhere in a <script> block, OR
  (b) loaded from an external <script src="..."> file that defines it globally.
"""

import re
import os
import unittest

INDEX_PATH = os.path.join(os.path.dirname(__file__), "index.html")

# Functions loaded from external scripts (e.g. admin_users.js) that are
# defined at global scope and therefore accessible without window.* exposure.
EXTERNAL_GLOBAL_FUNCTIONS = {
    "createUser", "createUnitFromAdmin", "saveUserChanges",
    "closeEditModal", "deleteUserFromModal",
    "loadAllUsers", "loadUnits",
}

# Built-in JS / well-known global identifiers that are not custom functions.
# Note: 'map' is the global Leaflet map instance (used as map.setView() etc.)
BUILTIN_NAMES = {
    "event", "this", "alert", "confirm", "console", "document", "window",
    "parseInt", "parseFloat", "setTimeout", "clearTimeout", "setInterval",
    "Math", "Date", "JSON", "Array", "Object", "String", "Number",
    "navigator", "location", "history", "fetch", "map",
    "true", "false", "null", "undefined", "NaN", "Infinity",
    "if", "else", "for", "while", "do", "switch", "case", "break",
    "continue", "return", "throw", "try", "catch", "finally", "new",
    "delete", "typeof", "void", "in", "instanceof", "with",
}


def _load_index():
    with open(INDEX_PATH, "r", encoding="utf-8") as f:
        return f.read()


def _extract_inline_handler_functions(html):
    """Return set of function names referenced in inline event handlers."""
    # Match inline event handler attributes
    handler_re = re.compile(
        r'\bon(?:click|change|input|submit|keypress|keydown|keyup|focus|blur|load)'
        r'\s*=\s*"([^"]+)"',
        re.IGNORECASE,
    )
    # Match the first function-call identifier in the handler value
    # Handles patterns like: func1(); func2(); condition && func3()
    func_call_re = re.compile(r"(?:^|[;&|,\s])([a-zA-Z_$][a-zA-Z0-9_$]*)\s*\(")

    functions = set()
    for m in handler_re.finditer(html):
        handler_body = m.group(1)
        for fc in func_call_re.finditer(handler_body):
            name = fc.group(1)
            if name not in BUILTIN_NAMES and not name.startswith("event."):
                functions.add(name)
    return functions


def _extract_dynamic_onclick_functions(html):
    """Return set of function names from dynamically generated onclick/onchange."""
    # Match onclick="functionName(...)" inside template literals and string concatenation
    dynamic_re = re.compile(
        r"""on(?:click|change|input)\s*=\s*(?:\\?["']|&quot;)\s*([a-zA-Z_$][a-zA-Z0-9_$]*)\s*\(""",
    )
    functions = set()
    # Search inside <script> blocks for dynamic HTML generation
    script_re = re.compile(r"<script[^>]*>(.*?)</script[^>]*>", re.DOTALL | re.IGNORECASE)
    for sm in script_re.finditer(html):
        script_body = sm.group(1)
        for m in dynamic_re.finditer(script_body):
            name = m.group(1)
            if name not in BUILTIN_NAMES:
                functions.add(name)
    return functions


def _extract_window_exposed_functions(html):
    """Return set of function names exposed via window.name = ..."""
    # Match window.functionName = functionName; or window.functionName = function...
    exposed_re = re.compile(r"window\.([a-zA-Z_$][a-zA-Z0-9_$]*)\s*=\s*")
    return {m.group(1) for m in exposed_re.finditer(html)}


def _extract_global_function_defs(html):
    """Return set of functions defined at top-level (outside _spaInit_ closures)."""
    # These are global functions accessible without window exposure
    # We look for function definitions not inside _spaInit_ blocks
    global_re = re.compile(r"^\s*(?:async\s+)?function\s+([a-zA-Z_$][a-zA-Z0-9_$]*)\s*\(", re.MULTILINE)
    return {m.group(1) for m in global_re.finditer(html)}


class TestSPAButtonFunctions(unittest.TestCase):
    """Verify that all inline event handler functions are globally accessible."""

    @classmethod
    def setUpClass(cls):
        cls.html = _load_index()
        cls.inline_fns = _extract_inline_handler_functions(cls.html)
        cls.dynamic_fns = _extract_dynamic_onclick_functions(cls.html)
        cls.window_fns = _extract_window_exposed_functions(cls.html)
        cls.global_fns = _extract_global_function_defs(cls.html)

    def test_inline_handler_functions_are_accessible(self):
        """Every function called from an inline event handler must be reachable."""
        all_handler_fns = self.inline_fns | self.dynamic_fns
        accessible = self.window_fns | self.global_fns | EXTERNAL_GLOBAL_FUNCTIONS

        missing = set()
        for fn in all_handler_fns:
            if fn not in accessible:
                missing.add(fn)

        if missing:
            self.fail(
                f"{len(missing)} function(s) called from inline event handlers "
                f"but NOT accessible in global scope:\n  "
                + "\n  ".join(sorted(missing))
            )

    def test_statistics_view_exposures(self):
        """Statistics view must expose terminateMission and toggleHistory."""
        self.assertIn("terminateMission", self.window_fns,
                       "terminateMission must be exposed via window.terminateMission")
        self.assertIn("toggleHistory", self.window_fns,
                       "toggleHistory must be exposed via window.toggleHistory")

    def test_global_intel_view_exposures(self):
        """Global Intel view must expose map/layer/panel toggle functions."""
        required = {
            "togglePanel", "switchTile", "toggleLayer", "toggleGroupShare",
            "refreshAll", "closeWebcamModal", "loadWebcams",
            "shareCurrentZone", "openInWindy",
        }
        for fn in required:
            self.assertIn(fn, self.window_fns,
                           f"{fn} must be exposed via window.{fn}")

    def test_cot_monitor_view_exposures(self):
        """COT Monitor view must expose showDetail, onCorrection, onNotes."""
        for fn in ("showDetail", "onCorrection", "onNotes"):
            self.assertIn(fn, self.window_fns,
                           f"{fn} must be exposed via window.{fn}")

    def test_network_view_exposures(self):
        """Network view must expose TAK/federation inline handler functions."""
        for fn in ("_cleanTakHost", "fedDecodeQrFile", "_onTakConnectionTypeChange"):
            self.assertIn(fn, self.window_fns,
                           f"{fn} must be exposed via window.{fn}")

    def test_admin_map_flyout_exposures(self):
        """Admin map flyout toolbar functions must be exposed."""
        required = {
            "gpsRefresh", "gpsFix",
            "selectSymbol", "startSymbolPlace",
            "startDrawing", "finishDrawing",
            "onOverlayFile", "applyRotation",
            "saveOverlayConfig", "resetOverlayInputs", "scheduleReapply",
        }
        for fn in required:
            self.assertIn(fn, self.window_fns,
                           f"{fn} must be exposed via window.{fn}")

    def test_admin_view_exposures(self):
        """Admin view must expose switchTab, QR code and registration functions."""
        required = {
            "switchTab", "createQRCode", "copyQRData", "createCOTMeshQR",
            "approveSelected", "rejectSelected",
        }
        for fn in required:
            self.assertIn(fn, self.window_fns,
                           f"{fn} must be exposed via window.{fn}")

    def test_mission_view_exposures(self):
        """Mission view must expose mission CRUD functions."""
        required = {
            "createMission", "saveGesamtbefehl", "openOrderView",
            "uploadAttachment", "deleteMission",
        }
        for fn in required:
            self.assertIn(fn, self.window_fns,
                           f"{fn} must be exposed via window.{fn}")

    def test_stream_view_exposures(self):
        """Stream view must expose camera/stream control functions."""
        required = {
            "connectCamera", "stopCamera", "connectScreenShare",
            "connectSSHStream", "stopStream", "toggleMulticast",
            "selectStreamToBroadcast", "deselectStream",
            "viewStream", "removeStream",
        }
        for fn in required:
            self.assertIn(fn, self.window_fns,
                           f"{fn} must be exposed via window.{fn}")

    def test_meshtastic_view_exposures(self):
        """Meshtastic view must expose channel management functions."""
        required = {
            "openCreateChannelModal", "closeCreateChannelModal", "createChannel",
            "openChannelAdminModal", "closeChannelAdminModal",
            "openChannelInfoModal", "closeChannelInfoModal",
            "confirmDeleteChannel", "closeDeleteChannelModal",
            "addChannelAdminMember", "removeChannelAdminMember", "setActiveChannel",
        }
        for fn in required:
            self.assertIn(fn, self.window_fns,
                           f"{fn} must be exposed via window.{fn}")

    def test_language_view_exposures(self):
        """Language view must expose saveLanguage."""
        self.assertIn("saveLanguage", self.window_fns,
                       "saveLanguage must be exposed via window.saveLanguage")


if __name__ == "__main__":
    unittest.main()
