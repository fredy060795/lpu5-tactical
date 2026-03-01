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
        # raute now maps to a-h-G-U-C (hostile); color element is not emitted
        # for military-affiliation types — ATAK uses affiliation colour instead.
        marker = {"id": "m7", "lat": 1.0, "lng": 2.0, "type": "raute", "color": "#ffff00"}
        evt = CoTProtocolHandler.marker_to_cot(marker)
        self.assertIsNotNone(evt)
        self.assertEqual(evt.cot_type, "a-h-G-U-C")
        xml_str = evt.to_xml()
        root = ET.fromstring(xml_str.replace('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>', ''))
        detail = root.find("detail")
        color_elem = detail.find("color")
        self.assertIsNone(color_elem, "No <color argb> element expected for military-affiliation type")

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


class TestAtakSymbolTypeMappings(unittest.TestCase):
    """Tests for the ATAK COT symbol type mappings.

    LPU5 shapes map to ATAK military-affiliation CoT types so that ATAK
    renders each shape with the correct colour:
      rechteck (blue rectangle) → a-f-G-U-C  (Friendly, blue,   F.1.…)
      blume    (yellow flower)  → a-u-G-U-C  (Unknown,  yellow, U.1.…)
      quadrat  (green square)   → a-n-G-U-C  (Neutral,  green,  N.1.…)
      raute    (red diamond)    → a-h-G-U-C  (Hostile,  red,    R.1.…)
    """

    # --- Forward mapping (LPU5 shape → ATAK CoT type) ---

    def test_rechteck_maps_to_friendly(self):
        self.assertEqual(CoTProtocolHandler.lpu5_type_to_cot("rechteck"), "a-f-G-U-C")

    def test_blume_maps_to_unknown(self):
        self.assertEqual(CoTProtocolHandler.lpu5_type_to_cot("blume"), "a-u-G-U-C")

    def test_quadrat_maps_to_neutral(self):
        self.assertEqual(CoTProtocolHandler.lpu5_type_to_cot("quadrat"), "a-n-G-U-C")

    def test_raute_maps_to_hostile(self):
        self.assertEqual(CoTProtocolHandler.lpu5_type_to_cot("raute"), "a-h-G-U-C")

    def test_rechteck_in_lpu5_to_cot_dict(self):
        self.assertEqual(CoTProtocolHandler.LPU5_TO_COT_TYPE["rechteck"], "a-f-G-U-C")

    def test_blume_in_lpu5_to_cot_dict(self):
        self.assertEqual(CoTProtocolHandler.LPU5_TO_COT_TYPE["blume"], "a-u-G-U-C")

    def test_quadrat_in_lpu5_to_cot_dict(self):
        self.assertEqual(CoTProtocolHandler.LPU5_TO_COT_TYPE["quadrat"], "a-n-G-U-C")

    def test_raute_in_lpu5_to_cot_dict(self):
        self.assertEqual(CoTProtocolHandler.LPU5_TO_COT_TYPE["raute"], "a-h-G-U-C")

    # --- Reverse mapping (ATAK CoT type → LPU5 shape) ---

    def test_friendly_cot_maps_to_rechteck(self):
        self.assertEqual(CoTProtocolHandler.cot_type_to_lpu5("a-f-G-U-C"), "rechteck")

    def test_unknown_cot_maps_to_blume(self):
        self.assertEqual(CoTProtocolHandler.cot_type_to_lpu5("a-u-G-U-C"), "blume")

    def test_neutral_cot_maps_to_quadrat(self):
        self.assertEqual(CoTProtocolHandler.cot_type_to_lpu5("a-n-G-U-C"), "quadrat")

    def test_hostile_cot_maps_to_raute(self):
        self.assertEqual(CoTProtocolHandler.cot_type_to_lpu5("a-h-G-U-C"), "raute")

    def test_friendly_subtype_resolves_to_rechteck(self):
        # Any a-f-* sub-type should resolve to rechteck
        self.assertEqual(CoTProtocolHandler.cot_type_to_lpu5("a-f-G-I-U-T-H"), "rechteck")

    def test_hostile_subtype_resolves_to_raute(self):
        self.assertEqual(CoTProtocolHandler.cot_type_to_lpu5("a-h-G-U-C-I"), "raute")

    # --- Archive element present for military-affiliation types ---

    def test_friendly_event_has_archive_element(self):
        evt = CoTEvent(uid="arch-1", cot_type="a-f-G-U-C", lat=0.0, lon=0.0)
        xml_str = evt.to_xml()
        root = ET.fromstring(xml_str.replace('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>', ''))
        detail = root.find("detail")
        self.assertIsNotNone(detail.find("archive"), "a-f type should include <archive/>")

    def test_hostile_event_has_archive_element(self):
        evt = CoTEvent(uid="arch-2", cot_type="a-h-G-U-C", lat=0.0, lon=0.0)
        xml_str = evt.to_xml()
        root = ET.fromstring(xml_str.replace('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>', ''))
        detail = root.find("detail")
        self.assertIsNotNone(detail.find("archive"), "a-h type should include <archive/>")

    def test_neutral_event_has_archive_element(self):
        evt = CoTEvent(uid="arch-3", cot_type="a-n-G-U-C", lat=0.0, lon=0.0)
        xml_str = evt.to_xml()
        root = ET.fromstring(xml_str.replace('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>', ''))
        detail = root.find("detail")
        self.assertIsNotNone(detail.find("archive"), "a-n type should include <archive/>")

    def test_unknown_event_has_archive_element(self):
        evt = CoTEvent(uid="arch-4", cot_type="a-u-G-U-C", lat=0.0, lon=0.0)
        xml_str = evt.to_xml()
        root = ET.fromstring(xml_str.replace('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>', ''))
        detail = root.find("detail")
        self.assertIsNotNone(detail.find("archive"), "a-u type should include <archive/>")

    # --- marker_to_cot() produces correct ATAK types for LPU5 shapes ---

    def test_marker_rechteck_produces_friendly_cot(self):
        marker = {"id": "s1", "lat": 1.0, "lng": 2.0, "type": "rechteck"}
        evt = CoTProtocolHandler.marker_to_cot(marker)
        self.assertIsNotNone(evt)
        self.assertEqual(evt.cot_type, "a-f-G-U-C")

    def test_marker_blume_produces_unknown_cot(self):
        marker = {"id": "s2", "lat": 1.0, "lng": 2.0, "type": "blume"}
        evt = CoTProtocolHandler.marker_to_cot(marker)
        self.assertIsNotNone(evt)
        self.assertEqual(evt.cot_type, "a-u-G-U-C")

    def test_marker_quadrat_produces_neutral_cot(self):
        marker = {"id": "s3", "lat": 1.0, "lng": 2.0, "type": "quadrat"}
        evt = CoTProtocolHandler.marker_to_cot(marker)
        self.assertIsNotNone(evt)
        self.assertEqual(evt.cot_type, "a-n-G-U-C")

    def test_marker_raute_produces_hostile_cot(self):
        marker = {"id": "s4", "lat": 1.0, "lng": 2.0, "type": "raute"}
        evt = CoTProtocolHandler.marker_to_cot(marker)
        self.assertIsNotNone(evt)
        self.assertEqual(evt.cot_type, "a-h-G-U-C")


class TestMeshtasticNodeAndTakUnit(unittest.TestCase):
    """Tests for ATAK Meshtastic node and GPS/SA position type detection."""

    # --- LPU5_TO_COT_TYPE contains new entries ---

    def test_node_type_in_lpu5_to_cot(self):
        # "node" is the internal LPU5 type for Meshtastic nodes stored in map_markers.
        # It must map to a-f-G-U-C (friendly unit) so that LPU5 does NOT forward
        # them to ATAK as unknown units (yellow flower / a-u-G-U-C).
        self.assertIn("node", CoTProtocolHandler.LPU5_TO_COT_TYPE)
        self.assertEqual(CoTProtocolHandler.LPU5_TO_COT_TYPE["node"], "a-f-G-U-C")

    def test_node_type_lpu5_to_cot_produces_friendly(self):
        self.assertEqual(CoTProtocolHandler.lpu5_type_to_cot("node"), "a-f-G-U-C")

    def test_meshtastic_node_in_lpu5_to_cot(self):
        self.assertIn("meshtastic_node", CoTProtocolHandler.LPU5_TO_COT_TYPE)
        self.assertEqual(CoTProtocolHandler.LPU5_TO_COT_TYPE["meshtastic_node"], "a-f-G-U-C")

    def test_tak_unit_in_lpu5_to_cot(self):
        self.assertIn("tak_unit", CoTProtocolHandler.LPU5_TO_COT_TYPE)
        self.assertEqual(CoTProtocolHandler.LPU5_TO_COT_TYPE["tak_unit"], "a-f-G-U-C")

    def test_node_marker_to_cot_produces_friendly_not_flower(self):
        # Regression: "node" type must NOT fall back to the unknown/flower (a-u-G-U-C).
        node_name = "Büroturm"
        marker = {"id": "mesh-123", "lat": 48.0, "lng": 11.0, "type": "node",
                  "name": node_name, "callsign": node_name}
        evt = CoTProtocolHandler.marker_to_cot(marker)
        self.assertIsNotNone(evt)
        self.assertEqual(evt.cot_type, "a-f-G-U-C",
                         "Meshtastic node (type='node') must export as friendly (a-f-G-U-C), not flower (a-u-G-U-C)")

    # --- CoTEvent.from_xml() detects <meshtastic> in <detail> ---

    def _make_cot_xml(self, how="m-g", extra_detail=""):
        return (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<event version="2.0" uid="TEST-1" type="a-f-G-U-C" '
            f'how="{how}" time="2024-01-01T00:00:00.000Z" '
            'start="2024-01-01T00:00:00.000Z" stale="2024-01-01T00:10:00.000Z">'
            '<point lat="48.0" lon="11.0" hae="250.0" ce="10.0" le="10.0"/>'
            f'<detail><contact callsign="Büroturm"/>{extra_detail}</detail>'
            '</event>'
        )

    def test_from_xml_detects_meshtastic_detail(self):
        xml = self._make_cot_xml(extra_detail='<meshtastic longName="Büroturm" shortName="BT"/>')
        evt = CoTEvent.from_xml(xml)
        self.assertIsNotNone(evt)
        self.assertTrue(evt.has_meshtastic_detail)

    def test_from_xml_no_meshtastic_detail(self):
        xml = self._make_cot_xml(extra_detail='<track speed="0" course="355"/>')
        evt = CoTEvent.from_xml(xml)
        self.assertIsNotNone(evt)
        self.assertFalse(evt.has_meshtastic_detail)

    def test_has_meshtastic_detail_defaults_to_false(self):
        evt = CoTEvent(uid="x", cot_type="a-f-G-U-C", lat=0.0, lon=0.0)
        self.assertFalse(evt.has_meshtastic_detail)

    # --- cot_to_marker() assigns meshtastic_node when <meshtastic> present ---

    def test_cot_to_marker_meshtastic_node_type(self):
        xml = self._make_cot_xml(
            how="m-g",
            extra_detail='<meshtastic longName="Büroturm" shortName="BT"/>'
        )
        evt = CoTEvent.from_xml(xml)
        marker = CoTProtocolHandler.cot_to_marker(evt)
        self.assertEqual(marker["type"], "meshtastic_node")

    def test_cot_to_marker_tak_unit_type_human_how(self):
        # "h-e" (human-entered) → ATAK SA / GPS position marker → tak_unit
        xml = self._make_cot_xml(how="h-e")
        evt = CoTEvent.from_xml(xml)
        marker = CoTProtocolHandler.cot_to_marker(evt)
        self.assertEqual(marker["type"], "tak_unit")

    def test_cot_to_marker_tak_unit_type_gps_how(self):
        # "h-g-i-g-o" (GPS-derived) → tak_unit
        xml = self._make_cot_xml(how="h-g-i-g-o")
        evt = CoTEvent.from_xml(xml)
        marker = CoTProtocolHandler.cot_to_marker(evt)
        self.assertEqual(marker["type"], "tak_unit")

    def test_cot_to_marker_rechteck_for_machine_generated(self):
        # "m-g" without <meshtastic> → original mapping (rechteck for a-f)
        xml = self._make_cot_xml(how="m-g")
        evt = CoTEvent.from_xml(xml)
        marker = CoTProtocolHandler.cot_to_marker(evt)
        self.assertEqual(marker["type"], "rechteck")

    def test_meshtastic_takes_precedence_over_human_how(self):
        # Even with how="h-e", <meshtastic> in detail takes precedence
        xml = self._make_cot_xml(
            how="h-e",
            extra_detail='<meshtastic longName="Tower" shortName="TW"/>'
        )
        evt = CoTEvent.from_xml(xml)
        marker = CoTProtocolHandler.cot_to_marker(evt)
        self.assertEqual(marker["type"], "meshtastic_node")

    def test_tak_unit_does_not_affect_hostile_type(self):
        # how="h-e" with a-h type should NOT produce tak_unit (only overrides a-f→rechteck)
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<event version="2.0" uid="TEST-2" type="a-h-G-U-C" '
            'how="h-e" time="2024-01-01T00:00:00.000Z" '
            'start="2024-01-01T00:00:00.000Z" stale="2024-01-01T00:10:00.000Z">'
            '<point lat="48.0" lon="11.0" hae="250.0" ce="10.0" le="10.0"/>'
            '<detail><contact callsign="Enemy"/></detail>'
            '</event>'
        )
        evt = CoTEvent.from_xml(xml)
        marker = CoTProtocolHandler.cot_to_marker(evt)
        # a-h maps to raute — tak_unit override only applies to rechteck (a-f)
        self.assertEqual(marker["type"], "raute")


if __name__ == "__main__":
    unittest.main()
