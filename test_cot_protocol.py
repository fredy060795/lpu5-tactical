#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_cot_protocol.py - Unit tests for cot_protocol.py

Tests the new color/type mapping additions:
  - hex_to_argb_int() helper
  - HEX_COLOR_TO_TEAM mapping
  - CoTEvent color parameter and <color argb> XML element
  - gps_position type in LPU5_TO_COT_TYPE
  - marker_to_cot() color and team derivation
"""

import unittest
import xml.etree.ElementTree as ET

from cot_protocol import CoTEvent, CoTProtocolHandler


class TestHexToArgbInt(unittest.TestCase):
    """Tests for CoTProtocolHandler.hex_to_argb_int()"""

    def test_red(self):
        # #FF0000 → alpha=0xFF, R=0xFF, G=0x00, B=0x00
        # unsigned: 0xFFFF0000 = 4278190080 → signed: -16776960... wait
        # 0xFF << 24 = 0xFF000000, | 0xFF << 16 = 0xFFFF0000
        # 0xFFFF0000 = 4278255616  no...
        # Let me recalculate: 0xFF000000 | 0x00FF0000 | 0x0000 | 0x00
        # = 0xFFFF0000 = 4278255616  -- wrong again
        # 0xFF << 24 = 4278190080 (0xFF000000)
        # 0xFF << 16 = 16711680 (0x00FF0000)
        # total = 4278190080 + 16711680 = 4294901760 (0xFFFF0000)
        # signed 32-bit: 4294901760 - 4294967296 = -65536
        result = CoTProtocolHandler.hex_to_argb_int("#ff0000")
        self.assertEqual(result, -65536)

    def test_green(self):
        # #00FF00 → 0xFF00FF00 unsigned = 4278255360 → signed = 4278255360 - 4294967296 = -16711936
        result = CoTProtocolHandler.hex_to_argb_int("#00ff00")
        self.assertEqual(result, -16711936)

    def test_blue(self):
        # #0000FF → 0xFF0000FF unsigned = 4278190335 → signed = 4278190335 - 4294967296 = -16776961
        result = CoTProtocolHandler.hex_to_argb_int("#0000ff")
        self.assertEqual(result, -16776961)

    def test_yellow(self):
        # #FFFF00 → 0xFFFFFF00 unsigned = 4294967040 → signed = 4294967040 - 4294967296 = -256
        result = CoTProtocolHandler.hex_to_argb_int("#ffff00")
        self.assertEqual(result, -256)

    def test_uppercase_input(self):
        result = CoTProtocolHandler.hex_to_argb_int("#FF0000")
        self.assertEqual(result, -65536)

    def test_without_hash(self):
        # Should accept colors without a leading '#'
        result = CoTProtocolHandler.hex_to_argb_int("ff0000")
        self.assertEqual(result, -65536)

    def test_eight_digit_argb(self):
        # #FFFF0000 (alpha=FF, R=FF, G=0, B=0)
        result = CoTProtocolHandler.hex_to_argb_int("#ffff0000")
        self.assertEqual(result, -65536)

    def test_invalid_returns_none(self):
        self.assertIsNone(CoTProtocolHandler.hex_to_argb_int("#ZZZZZZ"))

    def test_wrong_length_returns_none(self):
        self.assertIsNone(CoTProtocolHandler.hex_to_argb_int("#123"))


class TestHexColorToTeam(unittest.TestCase):
    """Tests for CoTProtocolHandler.hex_color_to_team()"""

    def test_yellow(self):
        self.assertEqual(CoTProtocolHandler.hex_color_to_team("#ffff00"), "Yellow")

    def test_blue(self):
        self.assertEqual(CoTProtocolHandler.hex_color_to_team("#0000ff"), "Blue")

    def test_green(self):
        self.assertEqual(CoTProtocolHandler.hex_color_to_team("#00ff00"), "Green")

    def test_red(self):
        self.assertEqual(CoTProtocolHandler.hex_color_to_team("#ff0000"), "Red")

    def test_uppercase_normalized(self):
        self.assertEqual(CoTProtocolHandler.hex_color_to_team("#FFFF00"), "Yellow")

    def test_unknown_color_returns_none(self):
        self.assertIsNone(CoTProtocolHandler.hex_color_to_team("#aabbcc"))

    def test_none_input_returns_none(self):
        self.assertIsNone(CoTProtocolHandler.hex_color_to_team(None))


class TestGpsPositionType(unittest.TestCase):
    """Tests that gps_position maps to the correct CoT type"""

    def test_gps_position_in_lpu5_to_cot(self):
        self.assertIn("gps_position", CoTProtocolHandler.LPU5_TO_COT_TYPE)
        self.assertEqual(CoTProtocolHandler.LPU5_TO_COT_TYPE["gps_position"], "a-f-G-U-C")

    def test_lpu5_type_to_cot_gps_position(self):
        self.assertEqual(CoTProtocolHandler.lpu5_type_to_cot("gps_position"), "a-f-G-U-C")

    def test_lpu5_type_to_cot_gps_position_case_insensitive(self):
        self.assertEqual(CoTProtocolHandler.lpu5_type_to_cot("GPS_POSITION"), "a-f-G-U-C")


class TestCoTEventColorParameter(unittest.TestCase):
    """Tests for the new color parameter in CoTEvent"""

    def test_color_defaults_to_none(self):
        evt = CoTEvent(uid="test-1", cot_type="a-f-G-U-C", lat=0.0, lon=0.0)
        self.assertIsNone(evt.color)

    def test_color_stored(self):
        evt = CoTEvent(uid="test-2", cot_type="b-m-p-s-m", lat=0.0, lon=0.0, color=-256)
        self.assertEqual(evt.color, -256)

    def test_color_element_emitted_for_spot_map(self):
        evt = CoTEvent(uid="test-3", cot_type="b-m-p-s-m", lat=1.0, lon=2.0, color=-256)
        xml_str = evt.to_xml()
        root = ET.fromstring(xml_str.replace('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>', ''))
        detail = root.find("detail")
        self.assertIsNotNone(detail)
        color_elem = detail.find("color")
        self.assertIsNotNone(color_elem, "Expected <color> element for b-m-p-s-m type")
        self.assertEqual(color_elem.get("argb"), "-256")

    def test_color_element_not_emitted_when_color_is_none(self):
        evt = CoTEvent(uid="test-4", cot_type="b-m-p-s-m", lat=1.0, lon=2.0)
        xml_str = evt.to_xml()
        root = ET.fromstring(xml_str.replace('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>', ''))
        detail = root.find("detail")
        color_elem = detail.find("color")
        self.assertIsNone(color_elem, "No <color> element expected when color is None")

    def test_color_element_not_emitted_for_non_spotmap_type(self):
        # For a friendly unit type (a-f-G-U-C), color element should not be emitted
        evt = CoTEvent(uid="test-5", cot_type="a-f-G-U-C", lat=1.0, lon=2.0, color=-256)
        xml_str = evt.to_xml()
        root = ET.fromstring(xml_str.replace('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>', ''))
        detail = root.find("detail")
        color_elem = detail.find("color")
        self.assertIsNone(color_elem, "No <color> element expected for non-spotmap type")


class TestMarkerToCotColorAndTeam(unittest.TestCase):
    """Tests for color/team derivation in marker_to_cot()"""

    def test_yellow_marker_gets_team_yellow(self):
        marker = {"id": "m1", "lat": 1.0, "lng": 2.0, "type": "raute", "color": "#ffff00"}
        evt = CoTProtocolHandler.marker_to_cot(marker)
        self.assertIsNotNone(evt)
        self.assertEqual(evt.team_name, "Yellow")

    def test_blue_marker_gets_team_blue(self):
        marker = {"id": "m2", "lat": 1.0, "lng": 2.0, "type": "raute", "color": "#0000ff"}
        evt = CoTProtocolHandler.marker_to_cot(marker)
        self.assertEqual(evt.team_name, "Blue")

    def test_green_marker_gets_team_green(self):
        marker = {"id": "m3", "lat": 1.0, "lng": 2.0, "type": "raute", "color": "#00ff00"}
        evt = CoTProtocolHandler.marker_to_cot(marker)
        self.assertEqual(evt.team_name, "Green")

    def test_red_marker_gets_team_red(self):
        marker = {"id": "m4", "lat": 1.0, "lng": 2.0, "type": "raute", "color": "#ff0000"}
        evt = CoTProtocolHandler.marker_to_cot(marker)
        self.assertEqual(evt.team_name, "Red")

    def test_unknown_color_no_team(self):
        marker = {"id": "m5", "lat": 1.0, "lng": 2.0, "type": "raute", "color": "#aabbcc"}
        evt = CoTProtocolHandler.marker_to_cot(marker)
        self.assertIsNone(evt.team_name)

    def test_explicit_team_not_overridden_by_color(self):
        marker = {"id": "m6", "lat": 1.0, "lng": 2.0, "type": "raute",
                  "color": "#ffff00", "team": "Cyan"}
        evt = CoTProtocolHandler.marker_to_cot(marker)
        self.assertEqual(evt.team_name, "Cyan")

    def test_spot_map_marker_color_in_xml(self):
        marker = {"id": "m7", "lat": 1.0, "lng": 2.0, "type": "raute", "color": "#ffff00"}
        evt = CoTProtocolHandler.marker_to_cot(marker)
        self.assertIsNotNone(evt)
        xml_str = evt.to_xml()
        root = ET.fromstring(xml_str.replace('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>', ''))
        detail = root.find("detail")
        color_elem = detail.find("color")
        self.assertIsNotNone(color_elem)
        self.assertEqual(color_elem.get("argb"), "-256")

    def test_no_color_field_no_team(self):
        marker = {"id": "m8", "lat": 1.0, "lng": 2.0, "type": "friendly"}
        evt = CoTProtocolHandler.marker_to_cot(marker)
        self.assertIsNone(evt.color)
        self.assertIsNone(evt.team_name)

    def test_gps_position_marker_maps_to_friendly_unit(self):
        marker = {"id": "gps-1", "lat": 48.0, "lng": 11.0, "type": "gps_position"}
        evt = CoTProtocolHandler.marker_to_cot(marker)
        self.assertIsNotNone(evt)
        self.assertEqual(evt.cot_type, "a-f-G-U-C")


if __name__ == "__main__":
    unittest.main()
