#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cot_protocol.py - Cursor-on-Target (CoT) Protocol Implementation

Implements CoT XML parsing and generation for ATAK/WINTAK compatibility.
Supports standard CoT event types and bidirectional conversion.
"""

import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Any
import uuid
import logging

logger = logging.getLogger("lpu5-cot")


class CoTEvent:
    """Represents a Cursor-on-Target event"""
    
    # CoT Type Classifications
    ATOM_TYPES = {
        "friendly": "a-f",
        "hostile": "a-h",
        "neutral": "a-n",
        "unknown": "a-u",
        "pending": "a-p"
    }
    
    ENTITY_TYPES = {
        "ground_unit": "G",
        "aircraft": "A",
        "space": "P",
        "surface": "S",
        "subsurface": "U"
    }
    
    def __init__(self,
                 uid: str,
                 cot_type: str,
                 lat: float,
                 lon: float,
                 hae: float = 0.0,
                 ce: float = 9999999.0,
                 le: float = 9999999.0,
                 callsign: Optional[str] = None,
                 remarks: Optional[str] = None,
                 team_name: Optional[str] = None,
                 team_role: Optional[str] = None,
                 stale_minutes: int = 5,
                 how: str = "m-g",
                 color: Optional[int] = None,
                 has_meshtastic_detail: bool = False):
        """
        Initialize a CoT event
        
        Args:
            uid: Unique identifier for the entity
            cot_type: CoT type string (e.g., "a-f-G-U-C" for friendly ground unit)
            lat: Latitude in decimal degrees
            lon: Longitude in decimal degrees
            hae: Height above ellipsoid in meters
            ce: Circular error in meters
            le: Linear error in meters
            callsign: Entity callsign/name
            remarks: Additional remarks
            team_name: Team name
            team_role: Role within team
            stale_minutes: Minutes until event is stale
            how: How the event was generated (e.g. "m-g" machine-generated,
                 "h-g-i-g-o" human-placed, "h-e" human-entered coordinates)
            color: ATAK signed ARGB integer color value for spot-map markers
            has_meshtastic_detail: True when the raw CoT XML detail contained a
                <meshtastic> child element (set by from_xml(); indicates the
                event was forwarded by an ATAK Meshtastic plugin).
        """
        self.uid = uid
        self.cot_type = cot_type
        self.lat = lat
        self.lon = lon
        self.hae = hae
        self.ce = ce
        self.le = le
        self.callsign = callsign or uid
        self.remarks = remarks
        self.team_name = team_name
        self.team_role = team_role
        self.time = datetime.now(timezone.utc)
        self.start = self.time
        self.stale = self.time + timedelta(minutes=stale_minutes)
        self.how = how
        self.color = color
        self.has_meshtastic_detail = has_meshtastic_detail
        
    @staticmethod
    def build_cot_type(atom: str = "friendly",
                       entity: str = "ground_unit",
                       function: str = "U",  # Unit
                       detail: str = "C") -> str:  # Combat
        """
        Build a CoT type string from components
        
        Args:
            atom: Affiliation (friendly, hostile, neutral, unknown, pending)
            entity: Entity type (ground_unit, aircraft, surface, subsurface, space)
            function: Function code (default: U for Unit)
            detail: Detail code (default: C for Combat)
            
        Returns:
            Complete CoT type string (e.g., "a-f-G-U-C")
        """
        atom_code = CoTEvent.ATOM_TYPES.get(atom, "a-u")
        entity_code = CoTEvent.ENTITY_TYPES.get(entity, "G")
        return f"{atom_code}-{entity_code}-{function}-{detail}"
    
    def to_xml(self) -> str:
        """
        Convert this CoT event to XML string
        
        Returns:
            XML string representation of the CoT event
        """
        # Root event element
        event = ET.Element("event")
        event.set("version", "2.0")
        event.set("uid", self.uid)
        event.set("type", self.cot_type)
        event.set("how", self.how)
        event.set("time", self._format_time(self.time))
        event.set("start", self._format_time(self.start))
        event.set("stale", self._format_time(self.stale))
        
        # Point element with coordinates
        point = ET.SubElement(event, "point")
        point.set("lat", str(self.lat))
        point.set("lon", str(self.lon))
        point.set("hae", str(self.hae))
        point.set("ce", str(self.ce))
        point.set("le", str(self.le))
        
        # Detail element with additional info
        detail = ET.SubElement(event, "detail")
        
        # Contact information
        if self.callsign:
            contact = ET.SubElement(detail, "contact")
            contact.set("callsign", self.callsign)
        
        # Group/team information
        if self.team_name or self.team_role:
            group = ET.SubElement(detail, "__group")
            if self.team_name:
                group.set("name", self.team_name)
            if self.team_role:
                group.set("role", self.team_role)
        
        # Remarks
        if self.remarks:
            remarks_elem = ET.SubElement(detail, "remarks")
            remarks_elem.text = self.remarks

        # Spot-map and drawing markers need <archive/> so ATAK persists them on
        # the map after they go stale.  Without this element ATAK removes the
        # entity from its overlay once the stale timestamp is reached.
        # Military-affiliation markers (a-f/h/n/u) placed by LPU5 users are
        # also archived so they persist on the ATAK map like spot-map markers.
        if self.cot_type.startswith(("b-m", "u-d", "a-f", "a-h", "a-n", "a-u", "a-p")):
            ET.SubElement(detail, "archive")

        # Emit ATAK color element for spot-map markers so that the correct
        # color is rendered in ATAK/WinTAK.  The value is a signed ARGB int.
        if self.cot_type.startswith("b-m-p-s-m") and self.color is not None:
            color_elem = ET.SubElement(detail, "color")
            color_elem.set("argb", str(self.color))

        # Track information (for movement history)
        track = ET.SubElement(detail, "track")
        track.set("speed", "0.0")
        track.set("course", "0.0")
        
        return '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' + ET.tostring(event, encoding="unicode")
    
    @staticmethod
    def from_xml(xml_string: str) -> Optional['CoTEvent']:
        """
        Parse a CoT XML string into a CoTEvent object
        
        Args:
            xml_string: XML string to parse
            
        Returns:
            CoTEvent object or None if parsing fails
        """
        try:
            root = ET.fromstring(xml_string)
            
            # Extract required attributes
            uid = root.get("uid")
            cot_type = root.get("type")
            how = root.get("how", "m-g")
            
            # Extract point coordinates
            point = root.find("point")
            if point is None:
                logger.warning("CoT event missing point element")
                return None
                
            lat = float(point.get("lat", "0.0"))
            lon = float(point.get("lon", "0.0"))
            hae = float(point.get("hae", "0.0"))
            ce = float(point.get("ce", "9999999.0"))
            le = float(point.get("le", "9999999.0"))
            
            # Extract detail information
            detail = root.find("detail")
            callsign = None
            remarks = None
            team_name = None
            team_role = None
            
            if detail is not None:
                contact = detail.find("contact")
                if contact is not None:
                    callsign = contact.get("callsign")
                
                group = detail.find("__group")
                if group is not None:
                    team_name = group.get("name")
                    team_role = group.get("role")
                
                remarks_elem = detail.find("remarks")
                if remarks_elem is not None:
                    remarks = remarks_elem.text
            
            # Detect whether the CoT detail contains a <meshtastic> element,
            # which is added by ATAK Meshtastic plugins (e.g. atak-forwarder)
            # to identify Meshtastic node position events.
            has_meshtastic_detail = CoTProtocolHandler.detail_has_meshtastic(detail)

            # Calculate stale time (default 5 minutes from now)
            stale_str = root.get("stale")
            stale_minutes = 5
            if stale_str:
                try:
                    stale_time = datetime.strptime(stale_str, "%Y-%m-%dT%H:%M:%S.%fZ")
                    now = datetime.now(timezone.utc)
                    stale_delta = stale_time - now
                    stale_minutes = max(1, int(stale_delta.total_seconds() / 60))
                except Exception:
                    pass
            
            return CoTEvent(
                uid=uid,
                cot_type=cot_type,
                lat=lat,
                lon=lon,
                hae=hae,
                ce=ce,
                le=le,
                callsign=callsign,
                remarks=remarks,
                team_name=team_name,
                team_role=team_role,
                stale_minutes=stale_minutes,
                how=how,
                has_meshtastic_detail=has_meshtastic_detail,
            )
            
        except Exception as e:
            logger.error(f"Failed to parse CoT XML: {e}")
            return None
    
    @staticmethod
    def _format_time(dt: datetime) -> str:
        """Format datetime for CoT XML"""
        return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization"""
        return {
            "uid": self.uid,
            "type": self.cot_type,
            "lat": self.lat,
            "lon": self.lon,
            "hae": self.hae,
            "ce": self.ce,
            "le": self.le,
            "callsign": self.callsign,
            "remarks": self.remarks,
            "team_name": self.team_name,
            "team_role": self.team_role,
            "time": self._format_time(self.time),
            "start": self._format_time(self.start),
            "stale": self._format_time(self.stale),
            "how": self.how
        }


class CoTProtocolHandler:
    """Handles CoT protocol operations including conversion and validation"""

    # Mapping from LPU5 symbol type names to TAK-compatible CoT type codes.
    # These must match the identifiers used by ATAK/ITAK/WinTAK/XTAK exactly
    # so that symbols sync correctly across all TAK server implementations.
    #
    # LPU5 shapes are mapped to ATAK military-affiliation CoT types that share
    # the same colour convention so that ATAK/WinTAK renders the correct icon
    # colour for each shape:
    #   rechteck (blue rectangle)  → Friendly  (a-f)  → blue   in ATAK (F.1.…)
    #   blume    (yellow flower)   → Unknown   (a-u)  → yellow in ATAK (U.1.…)
    #   quadrat  (green square)    → Neutral   (a-n)  → green  in ATAK (N.1.…)
    #   raute    (red diamond)     → Hostile   (a-h)  → red    in ATAK (R.1.…)
    LPU5_TO_COT_TYPE: Dict[str, str] = {
        "raute":            "a-h-G-U-C",   # hostile ground unit (red diamond)
        "quadrat":          "a-n-G-U-C",   # neutral ground unit (green square)
        "blume":            "a-u-G-U-C",   # unknown ground unit (yellow flower)
        "rechteck":         "a-f-G-U-C",   # friendly ground unit (blue rectangle)
        "friendly":         "a-f-G-U-C",   # friendly ground unit
        "hostile":          "a-h-G-U-C",   # hostile ground unit
        "neutral":          "a-n-G-U-C",   # neutral ground unit
        "unknown":          "a-u-G-U-C",   # unknown ground unit
        "pending":          "a-p-G-U-C",   # pending ground unit
        "gps_position":     "a-f-G-U-C",   # live GPS position (friendly ground unit)
        "node":             "a-f-G-U-C",   # Meshtastic node (LPU5 internal type) → friendly unit
        "meshtastic_node":  "a-f-G-U-C",   # Meshtastic node forwarded by ATAK plugin
        "tak_unit":         "a-f-G-U-C",   # ATAK SA / GPS position marker
    }

    # Mapping from normalized lowercase hex color strings to ATAK team names.
    # ATAK uses team colors to visually group units on the situational-awareness
    # map.  Only the four most common LPU5 marker colors are mapped here;
    # unknown colors fall through to None (no team override).
    HEX_COLOR_TO_TEAM: Dict[str, str] = {
        "#ffff00": "Yellow",
        "#0000ff": "Blue",
        "#00ff00": "Green",
        "#ff0000": "Red",
    }

    # Reverse mapping: TAK CoT type prefix → LPU5 symbol type.
    # Stored as a list of (prefix, lpu5_type) tuples ordered longest-prefix
    # first so that more-specific codes are matched before shorter ones when
    # iterating with str.startswith().
    #
    # ATAK military-affiliation types map to the LPU5 shape that shares the
    # same colour convention:
    #   a-f (Friendly, blue)   → rechteck (blue rectangle)
    #   a-u (Unknown, yellow)  → blume    (yellow flower)
    #   a-n (Neutral, green)   → quadrat  (green square)
    #   a-h (Hostile, red)     → raute    (red diamond)
    COT_TO_LPU5_TYPE: List[tuple] = [
        ("b-m-p-s-m", "raute"),     # TAK spot-map marker (all shapes)
        ("u-d-c-e",   "raute"),     # TAK drawing ellipse → diamond
        ("u-d-c-c",   "raute"),     # TAK drawing circle → diamond
        ("u-d-r",     "rechteck"),  # TAK drawing rectangle
        ("u-d-f",     "raute"),     # TAK drawing freehand → diamond
        ("u-d-p",     "raute"),     # TAK drawing generic point → diamond
        ("a-f",       "rechteck"),  # friendly affiliation → blue rectangle
        ("a-h",       "raute"),     # hostile affiliation → red diamond
        ("a-n",       "quadrat"),   # neutral affiliation → green square
        ("a-u",       "blume"),     # unknown affiliation → yellow flower
        ("a-p",       "raute"),     # pending affiliation → red diamond
    ]

    @classmethod
    def lpu5_type_to_cot(cls, lpu5_type: str) -> str:
        """
        Convert a lowercase LPU5 symbol type to a TAK CoT type string.
        Falls back to the generic unknown ground-unit code if not found.
        """
        return cls.LPU5_TO_COT_TYPE.get(lpu5_type.lower(), "a-u-G-U-C")

    @classmethod
    def cot_type_to_lpu5(cls, cot_type: str) -> str:
        """
        Convert a TAK CoT type string to the corresponding LPU5 symbol type.
        Uses prefix matching (longest-first order) so sub-types resolve to
        their base category.  Falls back to "unknown".
        """
        for prefix, lpu5 in cls.COT_TO_LPU5_TYPE:
            if cot_type.startswith(prefix):
                return lpu5
        return "unknown"

    @staticmethod
    def detail_has_meshtastic(detail_elem: Optional[ET.Element]) -> bool:
        """
        Return True if the CoT ``<detail>`` element contains a ``<meshtastic>``
        child element.

        ATAK Meshtastic plugins (e.g. atak-forwarder) add this element to CoT
        events that originate from a Meshtastic node, making it the canonical
        way to distinguish Meshtastic-relayed positions from regular ATAK SA.

        Args:
            detail_elem: An ``xml.etree.ElementTree.Element`` for the
                         ``<detail>`` node, or ``None``.

        Returns:
            True if a ``<meshtastic>`` child is present, False otherwise.
        """
        return detail_elem is not None and detail_elem.find("meshtastic") is not None

    @staticmethod
    def hex_to_argb_int(hex_color: str) -> Optional[int]:
        """
        Convert an HTML hex color string to an ATAK signed 32-bit ARGB integer.

        ATAK encodes colors as signed 32-bit integers in ARGB byte order
        (alpha in the most-significant byte).  Full opacity (alpha=0xFF) is
        assumed when the input is a 6-digit ``#RRGGBB`` string.  8-digit
        ``#AARRGGBB`` strings are also supported.

        Args:
            hex_color: Color string in ``#RRGGBB`` or ``#AARRGGBB`` format.

        Returns:
            Signed 32-bit ARGB integer, or None if the input cannot be parsed.
        """
        try:
            h = hex_color.lstrip("#")
            if len(h) == 6:
                r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
                argb = (0xFF << 24) | (r << 16) | (g << 8) | b
            elif len(h) == 8:
                a, r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), int(h[6:8], 16)
                argb = (a << 24) | (r << 16) | (g << 8) | b
            else:
                return None
            # Reinterpret as signed 32-bit integer (ATAK expects a Java int)
            if argb >= 0x80000000:
                argb -= 0x100000000
            return argb
        except (ValueError, AttributeError):
            return None

    @classmethod
    def hex_color_to_team(cls, hex_color: str) -> Optional[str]:
        """
        Map a hex color string to an ATAK team name.

        Args:
            hex_color: Color string in ``#RRGGBB`` format.

        Returns:
            ATAK team name string, or None if the color is not mapped.
        """
        if not hex_color:
            return None
        return cls.HEX_COLOR_TO_TEAM.get(hex_color.lower())
    @staticmethod
    def marker_to_cot(marker: Dict[str, Any]) -> Optional[CoTEvent]:
        """
        Convert a map marker to CoT event

        Args:
            marker: Map marker dictionary

        Returns:
            CoTEvent object or None if conversion fails
        """
        try:
            uid = marker.get("id", str(uuid.uuid4()))
            lat = float(marker.get("lat", 0.0))
            lon = float(marker.get("lng", 0.0))

            # If the marker already carries a TAK-originated cot_type, reuse it
            # so that the exact symbol (including sub-type detail) is preserved
            # when re-broadcasting to other TAK clients.
            cot_type = marker.get("cot_type") or marker.get("cotType")
            if not cot_type:
                # Derive a TAK CoT type from the LPU5 symbol type field,
                # falling back to the status field for backwards compatibility
                # (matches the JS COTProtocolHandler.markerToCOT() logic).
                lpu5_type = (marker.get("type") or marker.get("status") or "unknown").lower()
                cot_type = CoTProtocolHandler.lpu5_type_to_cot(lpu5_type)

            # Preserve the original `how` attribute when re-broadcasting a
            # TAK-originated marker so that ATAK clients receive the correct
            # provenance.  Fall back to "m-g" (machine-generated) so that
            # meshtastic-node events and server-generated pings are still
            # marked correctly.
            how = marker.get("how") or marker.get("cot_how") or "m-g"

            # Derive ATAK color value and team name from the marker's hex color.
            # Team name takes precedence if explicitly set on the marker; the
            # color field is used as a fallback so that spot-map markers
            # created in LPU5 appear with the correct color in ATAK.
            hex_color = marker.get("color")
            argb_color: Optional[int] = None
            team_name: Optional[str] = marker.get("team")
            if hex_color:
                argb_color = CoTProtocolHandler.hex_to_argb_int(hex_color)
                if not team_name:
                    team_name = CoTProtocolHandler.hex_color_to_team(hex_color)

            return CoTEvent(
                uid=uid,
                cot_type=cot_type,
                lat=lat,
                lon=lon,
                callsign=marker.get("name") or marker.get("callsign"),
                remarks=marker.get("description") or marker.get("remarks"),
                team_name=team_name,
                team_role=marker.get("role"),
                how=how,
                color=argb_color
            )
        except Exception as e:
            logger.error(f"Failed to convert marker to CoT: {e}")
            return None

    @staticmethod
    def marker_to_cot_tombstone(marker: Dict[str, Any]) -> Optional[CoTEvent]:
        """
        Create a CoT tombstone (deletion) event for a map marker.

        TAK clients remove an entity from their map when they receive a CoT
        event whose ``stale`` timestamp is equal to (or earlier than) the
        event's ``time`` timestamp.  This method produces such an event so
        that deleting a marker in LPU5 propagates the deletion to ATAK/WinTAK.

        Args:
            marker: Map marker dictionary (must contain at least ``id``, ``lat``,
                    and ``lng`` / ``lon`` keys).

        Returns:
            CoTEvent configured as a tombstone, or None if conversion fails.
        """
        try:
            cot_event = CoTProtocolHandler.marker_to_cot(marker)
            if cot_event is None:
                return None
            # Make the event immediately stale so TAK clients remove the entity.
            now = datetime.now(timezone.utc)
            cot_event.time = now
            cot_event.start = now
            cot_event.stale = now
            return cot_event
        except Exception as e:
            logger.error("Failed to build CoT tombstone for marker %s: %s", marker.get("id"), e)
            return None

    @staticmethod
    def cot_to_marker(cot_event: CoTEvent) -> Dict[str, Any]:
        """
        Convert a CoT event to map marker

        Args:
            cot_event: CoTEvent object

        Returns:
            Map marker dictionary
        """
        # Map the TAK CoT type back to the LPU5 internal symbol type so the
        # correct icon is rendered in admin_map / overview.
        lpu5_type = CoTProtocolHandler.cot_type_to_lpu5(cot_event.cot_type)

        # For spot-map markers (b-m-p-s-m) the CoT type is the same for all
        # LPU5 shapes.  When the callsign matches a known LPU5 shape name use
        # it directly so that ATAK-placed markers labelled "quadrat" or "blume"
        # are rendered with the correct icon in the LPU5 web UI.
        if cot_event.cot_type == "b-m-p-s-m" and cot_event.callsign:
            callsign_lower = cot_event.callsign.lower()
            if callsign_lower in CoTProtocolHandler.LPU5_TO_COT_TYPE:
                lpu5_type = callsign_lower

        # Refine the type for ATAK-specific CoT sources so they render with
        # the correct icon rather than the generic blue-rectangle ("rechteck"):
        #   • Meshtastic nodes forwarded by an ATAK Meshtastic plugin carry a
        #     <meshtastic> element in their <detail> block.
        #   • ATAK SA / GPS position updates from devices with physical GPS
        #     (or manually placed positions) use a "h-*" how code.
        if cot_event.has_meshtastic_detail:
            lpu5_type = "meshtastic_node"
        elif lpu5_type == "rechteck" and cot_event.how.startswith("h"):
            lpu5_type = "tak_unit"

        return {
            "id": cot_event.uid,
            "name": cot_event.callsign,
            "callsign": cot_event.callsign,
            "lat": cot_event.lat,
            "lng": cot_event.lon,
            "altitude": cot_event.hae,
            "type": lpu5_type,
            "status": lpu5_type,
            "description": cot_event.remarks,
            "team": cot_event.team_name,
            "role": cot_event.team_role,
            "timestamp": cot_event._format_time(cot_event.time),
            "cot_type": cot_event.cot_type,
            "source": "cot",
            "how": cot_event.how
        }
    
    @staticmethod
    def validate_cot_xml(xml_string: str) -> bool:
        """
        Validate CoT XML structure
        
        Args:
            xml_string: XML string to validate
            
        Returns:
            True if valid CoT XML, False otherwise
        """
        try:
            root = ET.fromstring(xml_string)
            if root.tag != "event":
                return False
            
            # Check required attributes
            if not all(root.get(attr) for attr in ["version", "uid", "type"]):
                return False
            
            # Check for point element
            point = root.find("point")
            if point is None:
                return False
            
            # Validate coordinates
            lat = float(point.get("lat", "invalid"))
            lon = float(point.get("lon", "invalid"))
            
            if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
                return False
            
            return True
        except Exception:
            return False
