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
                 stale_minutes: int = 5):
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
        self.how = "m-g"  # machine-generated
        
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
        
        # Track information (for movement history)
        track = ET.SubElement(detail, "track")
        track.set("speed", "0.0")
        track.set("course", "0.0")
        
        return ET.tostring(event, encoding="unicode")
    
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
                stale_minutes=stale_minutes
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
            
            # Determine CoT type based on marker properties
            status = marker.get("status", "unknown").lower()
            affiliation = "unknown"
            if "friendly" in status or "active" in status or "aktiv" in status:
                affiliation = "friendly"
            elif "hostile" in status or "kia" in status:
                affiliation = "hostile"
            elif "neutral" in status:
                affiliation = "neutral"
            
            cot_type = CoTEvent.build_cot_type(atom=affiliation)
            
            return CoTEvent(
                uid=uid,
                cot_type=cot_type,
                lat=lat,
                lon=lon,
                callsign=marker.get("name") or marker.get("callsign"),
                remarks=marker.get("description") or marker.get("remarks"),
                team_name=marker.get("team"),
                team_role=marker.get("role")
            )
        except Exception as e:
            logger.error(f"Failed to convert marker to CoT: {e}")
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
        # Parse CoT type for affiliation
        cot_parts = cot_event.cot_type.split("-")
        affiliation = "unknown"
        if len(cot_parts) >= 2:
            atom = cot_parts[1]
            if atom == "f":
                affiliation = "friendly"
            elif atom == "h":
                affiliation = "hostile"
            elif atom == "n":
                affiliation = "neutral"
        
        return {
            "id": cot_event.uid,
            "name": cot_event.callsign,
            "callsign": cot_event.callsign,
            "lat": cot_event.lat,
            "lng": cot_event.lon,
            "altitude": cot_event.hae,
            "status": affiliation,
            "description": cot_event.remarks,
            "team": cot_event.team_name,
            "role": cot_event.team_role,
            "timestamp": cot_event._format_time(cot_event.time),
            "cot_type": cot_event.cot_type,
            "source": "cot"
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
