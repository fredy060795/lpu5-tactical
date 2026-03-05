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

    def test_gps_position_callsign_takes_priority_over_name(self):
        """callsign from user profile should take priority over name for GPS positions."""
        marker = {
            "id": "gps-cs1",
            "lat": 48.0,
            "lng": 11.0,
            "type": "gps_position",
            "name": "login_username",
            "callsign": "ALPHA-1",
        }
        evt = CoTProtocolHandler.marker_to_cot(marker)
        self.assertIsNotNone(evt)
        self.assertEqual(evt.callsign, "ALPHA-1")

    def test_gps_position_falls_back_to_name_when_no_callsign(self):
        """When no callsign is set, name is used as fallback for GPS positions."""
        marker = {
            "id": "gps-cs2",
            "lat": 48.0,
            "lng": 11.0,
            "type": "gps_position",
            "name": "login_username",
        }
        evt = CoTProtocolHandler.marker_to_cot(marker)
        self.assertIsNotNone(evt)
        self.assertEqual(evt.callsign, "login_username")

    def test_non_gps_marker_name_takes_priority_over_callsign(self):
        """For non-GPS markers, name still takes priority over callsign (existing behaviour)."""
        marker = {
            "id": "m-cs1",
            "lat": 48.0,
            "lng": 11.0,
            "type": "rechteck",
            "name": "Marker Alpha",
            "callsign": "ALPHA-1",
        }
        evt = CoTProtocolHandler.marker_to_cot(marker)
        self.assertIsNotNone(evt)
        self.assertEqual(evt.callsign, "Marker Alpha")


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

    def test_meshtastic_node_event_has_no_archive_element(self):
        # Meshtastic nodes use a-f-G-E-S-U-M with is_meshtastic_node=True and must NOT
        # carry <archive/> so ATAK treats them as live refreshing contacts.
        evt = CoTEvent(uid="mesh-arch-1", cot_type="a-f-G-E-S-U-M", lat=48.0, lon=11.0,
                       is_meshtastic_node=True)
        xml_str = evt.to_xml()
        root = ET.fromstring(xml_str.replace('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>', ''))
        detail = root.find("detail")
        self.assertIsNone(detail.find("archive"),
                          "Meshtastic node (a-f-G-E-S-U-M, is_meshtastic_node=True) must NOT include <archive/>")

    def test_meshtastic_node_marker_to_cot_has_no_archive(self):
        # End-to-end: a marker of type 'node' must produce CoT type a-f-G-E-S-U-M
        # without <archive/> so ATAK shows it as a live Meshtastic contact.
        marker = {"id": "mesh-456", "lat": 48.0, "lng": 11.0, "type": "node", "name": "Node1"}
        evt = CoTProtocolHandler.marker_to_cot(marker)
        self.assertIsNotNone(evt)
        self.assertEqual(evt.cot_type, "a-f-G-E-S-U-M",
                         "Meshtastic node must use a-f-G-E-S-U-M (Meshtastic equipment)")
        self.assertTrue(evt.is_meshtastic_node,
                        "marker_to_cot() must set is_meshtastic_node=True for type='node'")
        xml_str = evt.to_xml()
        root = ET.fromstring(xml_str.replace('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>', ''))
        detail = root.find("detail")
        self.assertIsNone(detail.find("archive"),
                          "Meshtastic node marker must produce CoT without <archive/>")

    def test_friendly_unit_still_has_archive_after_meshtastic_fix(self):
        # Regression: a-f-G-U-C (standard friendly unit, NOT a Meshtastic node)
        # must still include <archive/>.
        evt = CoTEvent(uid="reg-1", cot_type="a-f-G-U-C", lat=0.0, lon=0.0)
        xml_str = evt.to_xml()
        root = ET.fromstring(xml_str.replace('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>', ''))
        detail = root.find("detail")
        self.assertIsNotNone(detail.find("archive"),
                             "a-f-G-U-C (friendly unit) must still include <archive/>")

    def test_meshtastic_node_a_f_g_u_c_has_no_archive(self):
        # A CoTEvent with cot_type a-f-G-U-C AND is_meshtastic_node=True must NOT
        # receive <archive/> so ATAK treats it as a live PLI contact, not a static marker.
        evt = CoTEvent(uid="mesh-unit-1", cot_type="a-f-G-U-C", lat=48.0, lon=11.0,
                       is_meshtastic_node=True)
        xml_str = evt.to_xml()
        root = ET.fromstring(xml_str.replace('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>', ''))
        detail = root.find("detail")
        self.assertIsNone(detail.find("archive"),
                          "a-f-G-U-C with is_meshtastic_node=True must NOT include <archive/>")

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
        # It must map to a-f-G-E-S-U-M so ATAK displays each node as an individual
        # Meshtastic equipment contact rather than a generic unit marker.
        self.assertIn("node", CoTProtocolHandler.LPU5_TO_COT_TYPE)
        self.assertEqual(CoTProtocolHandler.LPU5_TO_COT_TYPE["node"], "a-f-G-E-S-U-M")

    def test_node_type_lpu5_to_cot_produces_meshtastic_equipment(self):
        self.assertEqual(CoTProtocolHandler.lpu5_type_to_cot("node"), "a-f-G-E-S-U-M")

    def test_meshtastic_node_in_lpu5_to_cot(self):
        self.assertIn("meshtastic_node", CoTProtocolHandler.LPU5_TO_COT_TYPE)
        self.assertEqual(CoTProtocolHandler.LPU5_TO_COT_TYPE["meshtastic_node"], "a-f-G-E-S-U-M")

    def test_tak_unit_in_lpu5_to_cot(self):
        self.assertIn("tak_unit", CoTProtocolHandler.LPU5_TO_COT_TYPE)
        self.assertEqual(CoTProtocolHandler.LPU5_TO_COT_TYPE["tak_unit"], "a-f-G-U-C")

    def test_node_marker_to_cot_produces_meshtastic_equipment_type(self):
        # "node" type must produce the a-f-G-E-S-U-M CoT type so ATAK
        # displays Meshtastic nodes as individual Meshtastic equipment contacts.
        node_name = "Büroturm"
        marker = {"id": "mesh-123", "lat": 48.0, "lng": 11.0, "type": "node",
                  "name": node_name, "callsign": node_name}
        evt = CoTProtocolHandler.marker_to_cot(marker)
        self.assertIsNotNone(evt)
        self.assertEqual(evt.cot_type, "a-f-G-E-S-U-M",
                         "Meshtastic node (type='node') must export as a-f-G-E-S-U-M (Meshtastic equipment)")
        self.assertTrue(evt.is_meshtastic_node,
                        "marker_to_cot() must set is_meshtastic_node=True for type='node'")

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


class TestGatewayContactDisplay(unittest.TestCase):
    """Tests for Meshtastic gateway node ATAK display.

    Verifies that:
      - "gateway" LPU5 type maps to a-f-G-E-S-U-M (Meshtastic equipment)
      - CoTEvent.to_xml() includes the endpoint attribute in <contact> when set
      - marker_to_cot() passes contact_endpoint through to CoTEvent
      - Without an endpoint, the <contact> element is still emitted correctly
    """

    def test_gateway_type_in_lpu5_to_cot(self):
        """'gateway' LPU5 type must map to a-f-G-E-S-U-M (Meshtastic equipment)."""
        self.assertEqual(
            CoTProtocolHandler.lpu5_type_to_cot("gateway"),
            "a-f-G-E-S-U-M",
        )

    def test_gateway_marker_to_cot_produces_meshtastic_equipment(self):
        """A marker with type='gateway' must produce a CoT event of type a-f-G-E-S-U-M."""
        marker = {
            "id": "mesh-gw1",
            "lat": 48.1,
            "lng": 11.5,
            "name": "LPU5-GW",
            "callsign": "LPU5-GW",
            "type": "gateway",
        }
        evt = CoTProtocolHandler.marker_to_cot(marker)
        self.assertIsNotNone(evt)
        self.assertEqual(evt.cot_type, "a-f-G-E-S-U-M")
        self.assertTrue(evt.is_meshtastic_node)

    def test_contact_endpoint_in_xml(self):
        """CoTEvent.to_xml() must include endpoint in <contact> when contact_endpoint is set."""
        evt = CoTEvent(
            uid="LPU5-GW",
            cot_type="a-f-G-U-C",
            lat=0.0,
            lon=0.0,
            callsign="LPU5-GW",
            contact_endpoint="192.168.1.10:8088:tcp",
        )
        xml = evt.to_xml()
        root = ET.fromstring(xml.replace('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>', ""))
        contact = root.find("./detail/contact")
        self.assertIsNotNone(contact)
        self.assertEqual(contact.get("callsign"), "LPU5-GW")
        self.assertEqual(contact.get("endpoint"), "192.168.1.10:8088:tcp")

    def test_no_endpoint_when_contact_endpoint_is_none(self):
        """Without contact_endpoint the <contact> element must NOT have an endpoint attribute."""
        evt = CoTEvent(
            uid="LPU5-GW",
            cot_type="a-f-G-U-C",
            lat=0.0,
            lon=0.0,
            callsign="LPU5-GW",
        )
        xml = evt.to_xml()
        root = ET.fromstring(xml.replace('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>', ""))
        contact = root.find("./detail/contact")
        self.assertIsNotNone(contact)
        self.assertIsNone(contact.get("endpoint"),
                          "endpoint attribute must be absent when contact_endpoint is not set")

    def test_contact_endpoint_passed_through_marker_to_cot(self):
        """marker_to_cot() must forward contact_endpoint to CoTEvent."""
        marker = {
            "id": "mesh-gw1",
            "lat": 48.1,
            "lng": 11.5,
            "name": "GW-Node",
            "callsign": "GW-Node",
            "type": "gateway",
            "contact_endpoint": "10.0.0.5:8088:tcp",
        }
        evt = CoTProtocolHandler.marker_to_cot(marker)
        self.assertIsNotNone(evt)
        self.assertEqual(evt.contact_endpoint, "10.0.0.5:8088:tcp")
        xml = evt.to_xml()
        root = ET.fromstring(xml.replace('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>', ""))
        contact = root.find("./detail/contact")
        self.assertIsNotNone(contact)
        self.assertEqual(contact.get("endpoint"), "10.0.0.5:8088:tcp")

    def test_contact_endpoint_default_is_none(self):
        """CoTEvent contact_endpoint must default to None (backwards-compatible)."""
        evt = CoTEvent(uid="x", cot_type="a-f-G-U-C", lat=0.0, lon=0.0)
        self.assertIsNone(evt.contact_endpoint)

    def test_meshtastic_node_type_maps_to_equipment_type(self):
        """'meshtastic_node' type must map to a-f-G-E-S-U-M (Meshtastic equipment)."""
        self.assertEqual(
            CoTProtocolHandler.lpu5_type_to_cot("meshtastic_node"),
            "a-f-G-E-S-U-M",
        )

    def test_meshtastic_node_xml_contains_uid_droid(self):
        """CoTEvent with is_meshtastic_node=True must include <uid Droid="callsign"> in detail."""
        evt = CoTEvent(uid="mesh-pli-1", cot_type="a-f-G-U-C", lat=48.0, lon=11.0,
                       callsign="Alpha-1", is_meshtastic_node=True)
        xml = evt.to_xml()
        root = ET.fromstring(xml.replace('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>', ""))
        uid_elem = root.find("./detail/uid")
        self.assertIsNotNone(uid_elem, "<uid> element must be present in <detail> for Meshtastic node")
        self.assertEqual(uid_elem.get("Droid"), "Alpha-1",
                         "<uid Droid> must equal the callsign so WinTAK shows the unit name")

    def test_non_meshtastic_event_has_no_uid_droid(self):
        """A normal (non-Meshtastic) CoTEvent must NOT emit <uid Droid> in detail."""
        evt = CoTEvent(uid="unit-1", cot_type="a-f-G-U-C", lat=0.0, lon=0.0,
                       callsign="Bravo-2", is_meshtastic_node=False)
        xml = evt.to_xml()
        root = ET.fromstring(xml.replace('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>', ""))
        uid_elem = root.find("./detail/uid")
        self.assertIsNone(uid_elem, "<uid> must NOT appear in <detail> for non-Meshtastic events")

    def test_meshtastic_node_marker_to_cot_xml_has_uid_droid(self):
        """End-to-end: marker of type 'node' must produce XML with <uid Droid> in detail."""
        marker = {
            "id": "mesh-789",
            "lat": 48.0,
            "lng": 11.0,
            "type": "node",
            "name": "FieldUnit",
        }
        evt = CoTProtocolHandler.marker_to_cot(marker)
        self.assertIsNotNone(evt)
        xml = evt.to_xml()
        root = ET.fromstring(xml.replace('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>', ""))
        uid_elem = root.find("./detail/uid")
        self.assertIsNotNone(uid_elem, "<uid> must be present in <detail> for 'node' marker")
        self.assertEqual(uid_elem.get("Droid"), "FieldUnit")


class TestMeshtasticCotTypeNotCorruptedByEcho(unittest.TestCase):
    """marker_to_cot must always use a-f-G-E-S-U-M for node/meshtastic_node/gateway
    types, even when a stale or wrong cot_type is stored in the marker's data field."""

    def test_node_marker_ignores_wrong_cot_type_in_data(self):
        """marker with type='node' and a wrong data.cot_type must still produce
        a-f-G-E-S-U-M — the stored cot_type must be ignored for Meshtastic types."""
        marker = {
            "id": "uuid-mesh-1",
            "lat": 48.0,
            "lng": 11.0,
            "name": "MeshNode Alpha",
            "type": "node",
            # Simulate what the CoT listener stores after ATAK echoes back with
            # a normalised/wrong type.
            "cot_type": "a-u-G-U-C",
        }
        evt = CoTProtocolHandler.marker_to_cot(marker)
        self.assertIsNotNone(evt)
        self.assertEqual(
            evt.cot_type,
            "a-f-G-E-S-U-M",
            "node marker must use a-f-G-E-S-U-M regardless of stored cot_type",
        )

    def test_meshtastic_node_marker_ignores_wrong_cot_type_in_data(self):
        """marker with type='meshtastic_node' and a wrong data.cot_type must
        produce a-f-G-E-S-U-M — the stored cot_type must be ignored."""
        marker = {
            "id": "uuid-mesh-2",
            "lat": 48.0,
            "lng": 11.0,
            "name": "MeshNode Beta",
            "type": "meshtastic_node",
            "cot_type": "a-u-G-U-C",  # wrong type stored from a previous echo
        }
        evt = CoTProtocolHandler.marker_to_cot(marker)
        self.assertIsNotNone(evt)
        self.assertEqual(
            evt.cot_type,
            "a-f-G-E-S-U-M",
            "meshtastic_node marker must use a-f-G-E-S-U-M regardless of stored cot_type",
        )

    def test_gateway_marker_ignores_wrong_cot_type_in_data(self):
        """marker with type='gateway' and a wrong data.cot_type must
        produce a-f-G-E-S-U-M."""
        marker = {
            "id": "uuid-gw-1",
            "lat": 0.0,
            "lng": 0.0,
            "name": "LPU5-Node",
            "type": "gateway",
            "cot_type": "a-u-G-U-C",  # wrong type stored from a previous echo
        }
        evt = CoTProtocolHandler.marker_to_cot(marker)
        self.assertIsNotNone(evt)
        self.assertEqual(
            evt.cot_type,
            "a-f-G-E-S-U-M",
            "gateway marker must use a-f-G-E-S-U-M regardless of stored cot_type",
        )

    def test_non_meshtastic_marker_still_uses_stored_cot_type(self):
        """Non-Meshtastic markers (e.g. 'rechteck') must still use the stored
        cot_type from data so that symbol detail is preserved on re-broadcast."""
        marker = {
            "id": "uuid-rect-1",
            "lat": 48.0,
            "lng": 11.0,
            "name": "Friendly Unit",
            "type": "rechteck",
            "cot_type": "a-f-G-U-C-I",  # specific sub-type from ATAK
        }
        evt = CoTProtocolHandler.marker_to_cot(marker)
        self.assertIsNotNone(evt)
        self.assertEqual(
            evt.cot_type,
            "a-f-G-U-C-I",
            "non-Meshtastic marker should preserve stored cot_type sub-type detail",
        )


class TestMeshtasticCotParity(unittest.TestCase):
    """Verify that the CoT packets generated for Meshtastic nodes via the
    gateway path and the direct-import (Meshimporter) path are structurally
    identical: same CoT type, UID prefix, callsign handling, archive-free
    detail block, and <uid Droid> element.

    The gateway path constructs a marker_dict with:
        id        = f"mesh-{node_id}"   (node_id = raw Meshtastic ID, e.g. "!1234abcd")
        type      = "gateway"           (all nodes are forwarded as individual gateways)
    and then calls CoTProtocolHandler.marker_to_cot().

    The direct-import path (api.py  _forward_meshtastic_node_to_tak) builds
    an identical marker_dict structure and calls the same function.
    """

    def _make_person_node_marker(self, node_id="!aabbccdd", name="Alpha-1",
                                  lat=48.12, lng=11.57):
        """Helper: build a marker_dict as _forward_meshtastic_node_to_tak does."""
        return {
            "id": f"mesh-{node_id}",
            "name": name,
            "callsign": name,
            "lat": lat,
            "lng": lng,
            "type": "gateway",
            "meshtastic_node": True,
            "node_id": node_id,
            "source": "meshtastic",
        }

    def _make_gateway_marker(self, node_id="!00112233", name="LPU5-GW",
                              lat=48.0, lng=11.0, endpoint=None):
        """Helper: build a gateway marker_dict as _forward_meshtastic_node_to_tak does."""
        m = {
            "id": f"mesh-{node_id}",
            "name": name,
            "callsign": name,
            "lat": lat,
            "lng": lng,
            "type": "gateway",
            "meshtastic_node": True,
            "node_id": node_id,
            "source": "meshtastic",
        }
        if endpoint:
            m["contact_endpoint"] = endpoint
        return m

    # --- CoT type ---

    def test_person_node_cot_type_is_personnel(self):
        """All Meshtastic nodes (including person nodes) must produce a-f-G-E-S-U-M."""
        evt = CoTProtocolHandler.marker_to_cot(self._make_person_node_marker())
        self.assertIsNotNone(evt)
        self.assertEqual(evt.cot_type, "a-f-G-E-S-U-M",
                         "Meshtastic node must use a-f-G-E-S-U-M (Meshtastic equipment)")

    def test_gateway_node_cot_type_is_combat_unit(self):
        """Gateway node (type='gateway') must produce a-f-G-E-S-U-M."""
        evt = CoTProtocolHandler.marker_to_cot(self._make_gateway_marker())
        self.assertIsNotNone(evt)
        self.assertEqual(evt.cot_type, "a-f-G-E-S-U-M",
                         "Gateway must use a-f-G-E-S-U-M (Meshtastic equipment)")

    def test_person_and_gateway_have_same_cot_type(self):
        """Person node and gateway both use a-f-G-E-S-U-M (Meshtastic equipment)."""
        person_evt = CoTProtocolHandler.marker_to_cot(self._make_person_node_marker())
        gateway_evt = CoTProtocolHandler.marker_to_cot(self._make_gateway_marker())
        self.assertEqual(person_evt.cot_type, gateway_evt.cot_type,
                         "Person node and gateway both use a-f-G-E-S-U-M (Meshtastic equipment)")

    # --- UID ---

    def test_person_node_uid_uses_mesh_prefix(self):
        """UID must start with 'mesh-' so gateway and direct-import paths match."""
        node_id = "!aabbccdd"
        evt = CoTProtocolHandler.marker_to_cot(self._make_person_node_marker(node_id=node_id))
        self.assertEqual(evt.uid, f"mesh-{node_id}")

    def test_gateway_node_uid_uses_mesh_prefix(self):
        node_id = "!00112233"
        evt = CoTProtocolHandler.marker_to_cot(self._make_gateway_marker(node_id=node_id))
        self.assertEqual(evt.uid, f"mesh-{node_id}")

    # --- Callsign ---

    def test_person_node_callsign_preserved(self):
        evt = CoTProtocolHandler.marker_to_cot(
            self._make_person_node_marker(name="Bravo-2"))
        self.assertEqual(evt.callsign, "Bravo-2")

    def test_gateway_node_callsign_preserved(self):
        evt = CoTProtocolHandler.marker_to_cot(
            self._make_gateway_marker(name="GW-Node-1"))
        self.assertEqual(evt.callsign, "GW-Node-1")

    # --- is_meshtastic_node flag ---

    def test_person_node_sets_is_meshtastic_node(self):
        evt = CoTProtocolHandler.marker_to_cot(self._make_person_node_marker())
        self.assertTrue(evt.is_meshtastic_node)

    def test_gateway_sets_is_meshtastic_node(self):
        evt = CoTProtocolHandler.marker_to_cot(self._make_gateway_marker())
        self.assertTrue(evt.is_meshtastic_node)

    # --- No <archive/> element ---

    def test_person_node_xml_has_no_archive(self):
        """Person node CoT must NOT contain <archive/> so ATAK treats it as live."""
        evt = CoTProtocolHandler.marker_to_cot(self._make_person_node_marker())
        xml_str = evt.to_xml()
        root = ET.fromstring(
            xml_str.replace('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>', ""))
        detail = root.find("detail")
        self.assertIsNone(detail.find("archive"),
                          "Person node CoT must not include <archive/>")

    def test_gateway_xml_has_no_archive(self):
        """Gateway CoT must NOT contain <archive/> so ATAK treats it as live."""
        evt = CoTProtocolHandler.marker_to_cot(self._make_gateway_marker())
        xml_str = evt.to_xml()
        root = ET.fromstring(
            xml_str.replace('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>', ""))
        detail = root.find("detail")
        self.assertIsNone(detail.find("archive"),
                          "Gateway CoT must not include <archive/>")

    # --- <uid Droid> element ---

    def test_person_node_xml_has_uid_droid(self):
        """Person node CoT must include <uid Droid="callsign"> in <detail>."""
        evt = CoTProtocolHandler.marker_to_cot(
            self._make_person_node_marker(name="Charlie-3"))
        xml_str = evt.to_xml()
        root = ET.fromstring(
            xml_str.replace('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>', ""))
        uid_elem = root.find("./detail/uid")
        self.assertIsNotNone(uid_elem, "<uid> must be in <detail> for person node")
        self.assertEqual(uid_elem.get("Droid"), "Charlie-3")

    def test_gateway_xml_has_uid_droid(self):
        """Gateway CoT must include <uid Droid="callsign"> in <detail>."""
        evt = CoTProtocolHandler.marker_to_cot(
            self._make_gateway_marker(name="GW-Alpha"))
        xml_str = evt.to_xml()
        root = ET.fromstring(
            xml_str.replace('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>', ""))
        uid_elem = root.find("./detail/uid")
        self.assertIsNotNone(uid_elem, "<uid> must be in <detail> for gateway")
        self.assertEqual(uid_elem.get("Droid"), "GW-Alpha")

    # --- Contact element ---

    def test_person_node_contact_callsign(self):
        """<contact callsign> must equal the node name."""
        evt = CoTProtocolHandler.marker_to_cot(
            self._make_person_node_marker(name="Delta-4"))
        xml_str = evt.to_xml()
        root = ET.fromstring(
            xml_str.replace('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>', ""))
        contact = root.find("./detail/contact")
        self.assertIsNotNone(contact)
        self.assertEqual(contact.get("callsign"), "Delta-4")

    def test_gateway_contact_endpoint_present_when_set(self):
        """Gateway with contact_endpoint must emit endpoint attribute in <contact>."""
        evt = CoTProtocolHandler.marker_to_cot(
            self._make_gateway_marker(name="GW-1", endpoint="10.0.0.1:8088:tcp"))
        xml_str = evt.to_xml()
        root = ET.fromstring(
            xml_str.replace('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>', ""))
        contact = root.find("./detail/contact")
        self.assertIsNotNone(contact)
        self.assertEqual(contact.get("endpoint"), "10.0.0.1:8088:tcp")

    # --- XML structure parity: point element ---

    def test_person_node_point_lat_lon(self):
        """Point element must carry correct lat/lon."""
        evt = CoTProtocolHandler.marker_to_cot(
            self._make_person_node_marker(lat=47.5, lng=8.3))
        xml_str = evt.to_xml()
        root = ET.fromstring(
            xml_str.replace('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>', ""))
        point = root.find("point")
        self.assertIsNotNone(point)
        self.assertAlmostEqual(float(point.get("lat")), 47.5)
        self.assertAlmostEqual(float(point.get("lon")), 8.3)

    # --- Gateway path vs direct-import path produce same structure ---

    def test_gateway_path_same_structure_as_direct_import(self):
        """Simulate both paths for the same node and verify XML structure matches."""
        node_id = "!deadbeef"
        name = "FieldAgent"
        lat, lng = 48.5, 9.0

        # Gateway path: marker from gateway_node_update broadcast
        gateway_marker = {
            "id": f"mesh-{node_id}",
            "name": name,
            "callsign": name,
            "lat": lat,
            "lng": lng,
            "type": "gateway",
            "meshtastic_node": True,
            "node_id": node_id,
            "source": "meshtastic",
        }
        # Direct-import path: marker from _forward_meshtastic_node_to_tak
        direct_marker = {
            "id": f"mesh-{node_id}",
            "name": name,
            "callsign": name,
            "lat": lat,
            "lng": lng,
            "type": "gateway",
            "meshtastic_node": True,
            "node_id": node_id,
            "source": "meshtastic",
        }

        gw_evt = CoTProtocolHandler.marker_to_cot(gateway_marker)
        di_evt = CoTProtocolHandler.marker_to_cot(direct_marker)

        # Both must produce identical CoT type, UID, callsign, is_meshtastic_node
        self.assertEqual(gw_evt.cot_type, di_evt.cot_type,
                         "CoT type must match between gateway and direct-import paths")
        self.assertEqual(gw_evt.uid, di_evt.uid,
                         "UID must match between gateway and direct-import paths")
        self.assertEqual(gw_evt.callsign, di_evt.callsign,
                         "Callsign must match between gateway and direct-import paths")
        self.assertEqual(gw_evt.is_meshtastic_node, di_evt.is_meshtastic_node)
        self.assertEqual(gw_evt.lat, di_evt.lat)
        self.assertEqual(gw_evt.lon, di_evt.lon)

        # Verify both produce the same XML structural elements
        def _parse(evt):
            xml_str = evt.to_xml()
            return ET.fromstring(
                xml_str.replace('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>', ""))

        gw_root = _parse(gw_evt)
        di_root = _parse(di_evt)

        for root_elem in (gw_root, di_root):
            self.assertEqual(root_elem.get("type"), "a-f-G-E-S-U-M")
            self.assertEqual(root_elem.get("uid"), f"mesh-{node_id}")
            detail = root_elem.find("detail")
            self.assertIsNone(detail.find("archive"),
                              "Neither path should produce <archive/>")
            uid_d = detail.find("uid")
            self.assertIsNotNone(uid_d)
            self.assertEqual(uid_d.get("Droid"), name)


class TestMeshtasticNodeNameNotPrefixed(unittest.TestCase):
    """Meshtastic node callsigns must use the plain node name (longName / shortName)
    without any hardware-model or device-port prefix.

    Regression tests for the bug where markers were created with names like
    ``"Maker = Alice"`` causing ATAK/WinTAK to display units as "Maker UNK"
    instead of their real callsign.
    """

    def _make_node_marker(self, name: str, marker_type: str = "meshtastic_node") -> dict:
        """Build a minimal Meshtastic node marker as created by the sync worker."""
        return {
            "id": "mesh-abc123",
            "lat": 48.1,
            "lng": 11.6,
            "type": marker_type,
            "name": name,
            "callsign": name,
        }

    def test_plain_name_preserved_as_callsign(self):
        """marker_to_cot must forward the plain node name as the CoT callsign."""
        marker = self._make_node_marker("Alice")
        evt = CoTProtocolHandler.marker_to_cot(marker)
        self.assertIsNotNone(evt)
        self.assertEqual(evt.callsign, "Alice",
                         "Callsign must be the plain node name, not a hardware-model prefixed string")

    def test_name_without_hardware_model_prefix(self):
        """Callsign must NOT contain 'Maker =' or any hardware-model prefix."""
        marker = self._make_node_marker("Alice")
        evt = CoTProtocolHandler.marker_to_cot(marker)
        self.assertIsNotNone(evt)
        self.assertNotIn("=", evt.callsign,
                         "Callsign must not contain '=' from a hardware-model prefix")
        self.assertNotIn("Maker", evt.callsign,
                         "Callsign must not contain hardware model name")

    def test_uid_droid_uses_plain_name(self):
        """<uid Droid> in the CoT XML must equal the plain node name."""
        marker = self._make_node_marker("Bob")
        evt = CoTProtocolHandler.marker_to_cot(marker)
        self.assertIsNotNone(evt)
        xml_str = evt.to_xml()
        root = ET.fromstring(
            xml_str.replace('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>', ""))
        uid_elem = root.find("./detail/uid")
        self.assertIsNotNone(uid_elem)
        self.assertEqual(uid_elem.get("Droid"), "Bob")

    def test_contact_callsign_uses_plain_name(self):
        """<contact callsign> in the CoT XML must equal the plain node name."""
        marker = self._make_node_marker("Charlie")
        evt = CoTProtocolHandler.marker_to_cot(marker)
        self.assertIsNotNone(evt)
        xml_str = evt.to_xml()
        root = ET.fromstring(
            xml_str.replace('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>', ""))
        contact = root.find("./detail/contact")
        self.assertIsNotNone(contact)
        self.assertEqual(contact.get("callsign"), "Charlie")

    def test_gateway_plain_name_preserved(self):
        """Gateway nodes must also use their plain name (no hardware-model prefix)."""
        marker = self._make_node_marker("GW-Node-1", marker_type="gateway")
        evt = CoTProtocolHandler.marker_to_cot(marker)
        self.assertIsNotNone(evt)
        self.assertEqual(evt.callsign, "GW-Node-1")
        self.assertNotIn("=", evt.callsign)


class TestMeshtasticNodeGroupElement(unittest.TestCase):
    """Tests verifying that Meshtastic node CoT events include <__group> so
    WinTAK/ATAK displays each mesh node as an individual SA contact rather
    than a static map marker.

    Requirement: every mesh node CoT event must contain
        <__group name="Cyan" role="Team Member"/>
    in its <detail> section (matching the LPU5-GW SA beacon format) unless
    an explicit team / role is configured on the marker.
    """

    def _parse_xml(self, xml_str: str):
        return ET.fromstring(
            xml_str.replace('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>', "")
        )

    # ------------------------------------------------------------------
    # CoTEvent.to_xml() directly
    # ------------------------------------------------------------------

    def test_meshtastic_node_xml_has_group_element(self):
        """CoTEvent with is_meshtastic_node=True must include <__group> in <detail>."""
        evt = CoTEvent(
            uid="mesh-!12345678",
            cot_type="a-f-G-E-S-U-M",
            lat=47.1,
            lon=8.5,
            callsign="Node-1",
            is_meshtastic_node=True,
        )
        root = self._parse_xml(evt.to_xml())
        group = root.find("./detail/__group")
        self.assertIsNotNone(group, "<__group> element must be present in <detail> for Meshtastic nodes")

    def test_meshtastic_node_group_default_name_cyan(self):
        """Default __group name for mesh nodes must be 'Cyan' (matches LPU5-GW beacon)."""
        evt = CoTEvent(
            uid="mesh-!12345678",
            cot_type="a-f-G-E-S-U-M",
            lat=47.1,
            lon=8.5,
            callsign="Node-1",
            is_meshtastic_node=True,
        )
        root = self._parse_xml(evt.to_xml())
        group = root.find("./detail/__group")
        self.assertIsNotNone(group)
        self.assertEqual(group.get("name"), "Cyan",
                         "__group name must default to 'Cyan' for Meshtastic nodes")

    def test_meshtastic_node_group_default_role_team_member(self):
        """Default __group role for mesh nodes must be 'Team Member'."""
        evt = CoTEvent(
            uid="mesh-!12345678",
            cot_type="a-f-G-E-S-U-M",
            lat=47.1,
            lon=8.5,
            callsign="Node-1",
            is_meshtastic_node=True,
        )
        root = self._parse_xml(evt.to_xml())
        group = root.find("./detail/__group")
        self.assertIsNotNone(group)
        self.assertEqual(group.get("role"), "Team Member",
                         "__group role must default to 'Team Member' for Meshtastic nodes")

    def test_meshtastic_node_explicit_team_not_overridden(self):
        """If team_name is already set on a Meshtastic node, it must not be replaced by 'Cyan'."""
        evt = CoTEvent(
            uid="mesh-!abc",
            cot_type="a-f-G-E-S-U-M",
            lat=0.0,
            lon=0.0,
            callsign="Node-2",
            team_name="Magenta",
            team_role="HQ",
            is_meshtastic_node=True,
        )
        root = self._parse_xml(evt.to_xml())
        group = root.find("./detail/__group")
        self.assertIsNotNone(group)
        self.assertEqual(group.get("name"), "Magenta", "Explicit team_name must be preserved")
        self.assertEqual(group.get("role"), "HQ", "Explicit team_role must be preserved")

    def test_non_meshtastic_event_without_team_has_no_group(self):
        """A non-Meshtastic event without a team must NOT have <__group>."""
        evt = CoTEvent(
            uid="unit-99",
            cot_type="a-f-G-U-C",
            lat=0.0,
            lon=0.0,
            callsign="Alpha",
            is_meshtastic_node=False,
        )
        root = self._parse_xml(evt.to_xml())
        group = root.find("./detail/__group")
        self.assertIsNone(group, "<__group> must NOT be added to non-Meshtastic events without a team")

    def test_non_meshtastic_event_with_team_still_gets_group(self):
        """A non-Meshtastic event with an explicit team must still get <__group>."""
        evt = CoTEvent(
            uid="unit-77",
            cot_type="a-f-G-U-C",
            lat=0.0,
            lon=0.0,
            callsign="Bravo",
            team_name="Green",
            team_role="Team Leader",
            is_meshtastic_node=False,
        )
        root = self._parse_xml(evt.to_xml())
        group = root.find("./detail/__group")
        self.assertIsNotNone(group)
        self.assertEqual(group.get("name"), "Green")
        self.assertEqual(group.get("role"), "Team Leader")

    # ------------------------------------------------------------------
    # End-to-end via marker_to_cot()
    # ------------------------------------------------------------------

    def test_gateway_marker_to_cot_xml_has_group(self):
        """End-to-end: type='gateway' marker must produce XML with <__group> in <detail>."""
        marker = {
            "id": "mesh-gw1",
            "lat": 48.1,
            "lng": 11.5,
            "name": "GW-Node",
            "callsign": "GW-Node",
            "type": "gateway",
        }
        evt = CoTProtocolHandler.marker_to_cot(marker)
        self.assertIsNotNone(evt)
        root = self._parse_xml(evt.to_xml())
        group = root.find("./detail/__group")
        self.assertIsNotNone(group, "<__group> must be present in CoT XML for gateway markers")
        self.assertEqual(group.get("name"), "Cyan")
        self.assertEqual(group.get("role"), "Team Member")

    def test_node_marker_to_cot_xml_has_group(self):
        """End-to-end: type='node' marker must produce XML with <__group> in <detail>."""
        marker = {
            "id": "mesh-node1",
            "lat": 47.5,
            "lng": 9.2,
            "name": "Node-Alpha",
            "type": "node",
        }
        evt = CoTProtocolHandler.marker_to_cot(marker)
        self.assertIsNotNone(evt)
        root = self._parse_xml(evt.to_xml())
        group = root.find("./detail/__group")
        self.assertIsNotNone(group, "<__group> must be present in CoT XML for node markers")
        self.assertEqual(group.get("name"), "Cyan")

    def test_meshtastic_node_marker_to_cot_xml_has_group(self):
        """End-to-end: type='meshtastic_node' marker must produce XML with <__group>."""
        marker = {
            "id": "mesh-pli1",
            "lat": 47.0,
            "lng": 8.0,
            "name": "Mesh-PLI",
            "type": "meshtastic_node",
        }
        evt = CoTProtocolHandler.marker_to_cot(marker)
        self.assertIsNotNone(evt)
        root = self._parse_xml(evt.to_xml())
        group = root.find("./detail/__group")
        self.assertIsNotNone(group, "<__group> must be present in CoT XML for meshtastic_node markers")
        self.assertEqual(group.get("name"), "Cyan")

    def test_each_node_has_unique_uid_and_individual_group(self):
        """Multiple mesh nodes must each produce a distinct UID and their own <__group>."""
        nodes = [
            {"id": "mesh-!aaa111", "lat": 47.1, "lng": 8.1, "name": "Node-A", "type": "node"},
            {"id": "mesh-!bbb222", "lat": 47.2, "lng": 8.2, "name": "Node-B", "type": "node"},
            {"id": "mesh-!ccc333", "lat": 47.3, "lng": 8.3, "name": "Node-C", "type": "gateway"},
        ]
        uids = []
        for marker in nodes:
            evt = CoTProtocolHandler.marker_to_cot(marker)
            self.assertIsNotNone(evt)
            uids.append(evt.uid)
            root = self._parse_xml(evt.to_xml())
            group = root.find("./detail/__group")
            self.assertIsNotNone(
                group,
                f"<__group> must be present for node {marker['name']}"
            )
        # All UIDs must be distinct
        self.assertEqual(len(uids), len(set(uids)),
                         "Each mesh node must have a unique CoT UID")


class TestMeshtasticDetailInOutgoingCoT(unittest.TestCase):
    """Tests that verify outgoing CoT for Meshtastic nodes includes a <meshtastic>
    element in <detail>.

    Adding <meshtastic longName="..." shortName="..."/> to outgoing CoT solves the
    round-trip problem: when ATAK/WinTAK loads LPU5-generated Meshtastic nodes and
    re-broadcasts them, the TAK client preserves the <meshtastic> element.  LPU5
    can then identify the forwarded event as a Meshtastic node via
    detail_has_meshtastic() even if the TAK client normalises the CoT type code
    (e.g. strips "-E-S-U-M" → "a-f-G-U-C").
    """

    def _parse_xml(self, xml_str: str) -> ET.Element:
        return ET.fromstring(
            xml_str.replace('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>', "")
        )

    def test_to_xml_includes_meshtastic_element_for_node(self):
        """to_xml() must include <meshtastic> in <detail> when is_meshtastic_node=True."""
        marker = {
            "id": "mesh-!aabb",
            "lat": 48.0,
            "lng": 11.0,
            "name": "FieldNode",
            "type": "node",
        }
        evt = CoTProtocolHandler.marker_to_cot(marker)
        self.assertIsNotNone(evt)
        root = self._parse_xml(evt.to_xml())
        mesh_elem = root.find("./detail/meshtastic")
        self.assertIsNotNone(mesh_elem,
                             "<meshtastic> must be present in <detail> for type='node' markers")

    def test_to_xml_includes_meshtastic_element_for_meshtastic_node(self):
        """to_xml() must include <meshtastic> for type='meshtastic_node'."""
        marker = {
            "id": "mesh-!ccdd",
            "lat": 47.5,
            "lng": 9.2,
            "name": "MeshUnit",
            "type": "meshtastic_node",
        }
        evt = CoTProtocolHandler.marker_to_cot(marker)
        self.assertIsNotNone(evt)
        root = self._parse_xml(evt.to_xml())
        self.assertIsNotNone(root.find("./detail/meshtastic"),
                             "<meshtastic> must be present for type='meshtastic_node'")

    def test_to_xml_includes_meshtastic_element_for_gateway(self):
        """to_xml() must include <meshtastic> for type='gateway'."""
        marker = {
            "id": "mesh-!0011",
            "lat": 48.1,
            "lng": 11.5,
            "name": "GW-Node",
            "type": "gateway",
        }
        evt = CoTProtocolHandler.marker_to_cot(marker)
        self.assertIsNotNone(evt)
        root = self._parse_xml(evt.to_xml())
        self.assertIsNotNone(root.find("./detail/meshtastic"),
                             "<meshtastic> must be present for type='gateway'")

    def test_meshtastic_element_long_name_equals_callsign(self):
        """<meshtastic longName> must equal the node's callsign."""
        name = "TowerAlpha"
        marker = {"id": "mesh-!ff00", "lat": 0.0, "lng": 0.0, "name": name, "type": "node"}
        evt = CoTProtocolHandler.marker_to_cot(marker)
        root = self._parse_xml(evt.to_xml())
        mesh_elem = root.find("./detail/meshtastic")
        self.assertIsNotNone(mesh_elem)
        self.assertEqual(mesh_elem.get("longName"), name)

    def test_meshtastic_element_short_name_is_first_two_chars(self):
        """<meshtastic shortName> must be the first two characters of the callsign."""
        name = "Bravo"
        marker = {"id": "mesh-!aa11", "lat": 0.0, "lng": 0.0, "name": name, "type": "node"}
        evt = CoTProtocolHandler.marker_to_cot(marker)
        root = self._parse_xml(evt.to_xml())
        mesh_elem = root.find("./detail/meshtastic")
        self.assertIsNotNone(mesh_elem)
        self.assertEqual(mesh_elem.get("shortName"), "Br")

    def test_non_meshtastic_to_xml_has_no_meshtastic_element(self):
        """to_xml() must NOT include <meshtastic> for non-Meshtastic events."""
        evt = CoTEvent(uid="unit-1", cot_type="a-f-G-U-C", lat=0.0, lon=0.0,
                       callsign="Alpha", is_meshtastic_node=False)
        root = self._parse_xml(evt.to_xml())
        self.assertIsNone(root.find("./detail/meshtastic"),
                          "<meshtastic> must NOT appear in non-Meshtastic CoT")

    def test_round_trip_cot_from_lpu5_echoed_by_atak_with_normalised_type(self):
        """Full round-trip: LPU5 generates CoT → ATAK normalises type to a-f-G-U-C
        and echoes back → LPU5 must still identify the marker as meshtastic_node
        via the preserved <meshtastic> element.
        """
        # 1. LPU5 generates CoT for a Meshtastic node (with <meshtastic> in detail)
        marker = {"id": "mesh-!deadbeef", "lat": 48.0, "lng": 11.0,
                  "name": "Relay-1", "type": "node"}
        lpu5_evt = CoTProtocolHandler.marker_to_cot(marker)
        lpu5_xml = lpu5_evt.to_xml()

        # Verify the outgoing XML contains <meshtastic>
        root = self._parse_xml(lpu5_xml)
        self.assertIsNotNone(root.find("./detail/meshtastic"),
                             "LPU5 outgoing CoT must include <meshtastic>")

        # 2. Simulate ATAK normalising the CoT type to a-f-G-U-C but preserving
        #    the <detail> block (including <meshtastic>).
        atak_echo_xml = lpu5_xml.replace('type="a-f-G-E-S-U-M"', 'type="a-f-G-U-C"')

        # 3. LPU5 parses the echoed CoT
        echo_evt = CoTEvent.from_xml(atak_echo_xml)
        self.assertIsNotNone(echo_evt)
        self.assertTrue(echo_evt.has_meshtastic_detail,
                        "Parsed echo must have has_meshtastic_detail=True")

        # 4. Convert to marker → must be meshtastic_node, not rechteck
        echo_marker = CoTProtocolHandler.cot_to_marker(echo_evt)
        self.assertEqual(echo_marker["type"], "meshtastic_node",
                         "ATAK-echoed node must map to meshtastic_node via <meshtastic> detail")

    def test_round_trip_preserves_meshtastic_type_when_cot_type_preserved(self):
        """If ATAK preserves a-f-G-E-S-U-M type AND <meshtastic> detail, the result
        must still be meshtastic_node (both mechanisms agree).
        """
        marker = {"id": "mesh-!11223344", "lat": 47.8, "lng": 10.1,
                  "name": "Scout-2", "type": "node"}
        lpu5_evt = CoTProtocolHandler.marker_to_cot(marker)
        lpu5_xml = lpu5_evt.to_xml()

        # ATAK keeps the type unchanged
        echo_evt = CoTEvent.from_xml(lpu5_xml)
        self.assertIsNotNone(echo_evt)
        self.assertEqual(echo_evt.cot_type, "a-f-G-E-S-U-M")
        self.assertTrue(echo_evt.has_meshtastic_detail)

        echo_marker = CoTProtocolHandler.cot_to_marker(echo_evt)
        self.assertEqual(echo_marker["type"], "meshtastic_node")


if __name__ == "__main__":
    unittest.main()
