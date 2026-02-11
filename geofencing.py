#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
geofencing.py - Geofencing and Zone Monitoring System

Implements geofencing capabilities for tactical operations:
- Zone creation and management
- Entry/exit detection
- Alert triggers
- Distance calculations
"""

import json
import math
import os
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any, Tuple
import logging
from database import SessionLocal
from models import Geofence as GeofenceModel

logger = logging.getLogger("lpu5-geofencing")


class GeoFence:
    """Represents a geofence zone"""
    
    def __init__(self,
                 zone_id: str,
                 name: str,
                 center_lat: float,
                 center_lon: float,
                 radius_meters: float,
                 zone_type: str = "exclusion",
                 alert_on_entry: bool = True,
                 alert_on_exit: bool = False,
                 enabled: bool = True,
                 metadata: Optional[Dict] = None):
        """
        Initialize a geofence
        
        Args:
            zone_id: Unique identifier
            name: Zone name
            center_lat: Center latitude
            center_lon: Center longitude
            radius_meters: Radius in meters
            zone_type: Type (exclusion, inclusion, alert, safe)
            alert_on_entry: Trigger alert when entity enters
            alert_on_exit: Trigger alert when entity exits
            enabled: Whether zone is active
            metadata: Additional zone metadata
        """
        self.zone_id = zone_id
        self.name = name
        self.center_lat = center_lat
        self.center_lon = center_lon
        self.radius_meters = radius_meters
        self.zone_type = zone_type
        self.alert_on_entry = alert_on_entry
        self.alert_on_exit = alert_on_exit
        self.enabled = enabled
        self.metadata = metadata or {}
        self.created_at = datetime.now(timezone.utc).isoformat()
        
    def contains_point(self, lat: float, lon: float) -> bool:
        """
        Check if a point is within this geofence
        
        Args:
            lat: Latitude to check
            lon: Longitude to check
            
        Returns:
            True if point is within fence, False otherwise
        """
        distance = self.calculate_distance(lat, lon)
        return distance <= self.radius_meters
    
    def calculate_distance(self, lat: float, lon: float) -> float:
        """
        Calculate distance from center to point using Haversine formula
        
        Args:
            lat: Target latitude
            lon: Target longitude
            
        Returns:
            Distance in meters
        """
        return haversine_distance(self.center_lat, self.center_lon, lat, lon)
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary"""
        return {
            "zone_id": self.zone_id,
            "name": self.name,
            "center_lat": self.center_lat,
            "center_lon": self.center_lon,
            "radius_meters": self.radius_meters,
            "zone_type": self.zone_type,
            "alert_on_entry": self.alert_on_entry,
            "alert_on_exit": self.alert_on_exit,
            "enabled": self.enabled,
            "metadata": self.metadata,
            "created_at": self.created_at
        }
    
    @staticmethod
    def from_dict(data: Dict[str, Any]) -> 'GeoFence':
        """Create from dictionary"""
        fence = GeoFence(
            zone_id=data["zone_id"],
            name=data["name"],
            center_lat=data["center_lat"],
            center_lon=data["center_lon"],
            radius_meters=data["radius_meters"],
            zone_type=data.get("zone_type", "exclusion"),
            alert_on_entry=data.get("alert_on_entry", True),
            alert_on_exit=data.get("alert_on_exit", False),
            enabled=data.get("enabled", True),
            metadata=data.get("metadata", {})
        )
        fence.created_at = data.get("created_at", fence.created_at)
        return fence


class GeofencingManager:
    """Manages geofencing operations and monitoring"""
    
    def __init__(self, db_path: str = "geofences_db.json"):
        """
        Initialize geofencing manager
        
        Args:
            db_path: Path to geofences database file
        """
        self.db_path = db_path
        self.geofences: Dict[str, GeoFence] = {}
        self.entity_states: Dict[str, Dict[str, bool]] = {}  # entity_id -> {zone_id -> inside}
        self.load_geofences()
        
    def load_geofences(self):
        """Load geofences from SQLAlchemy database"""
        db = SessionLocal()
        try:
            db_fences = db.query(GeofenceModel).all()
            self.geofences = {}
            for db_fence in db_fences:
                # Convert DB model to legacy class for runtime logic
                fence = GeoFence(
                    zone_id=db_fence.id,
                    name=db_fence.name,
                    center_lat=db_fence.center_lat or 0.0,
                    center_lon=db_fence.center_lon or 0.0,
                    radius_meters=db_fence.radius_meters or 0.0,
                    zone_type=db_fence.zone_type,
                    alert_on_entry=db_fence.alert_on_entry,
                    alert_on_exit=db_fence.alert_on_exit,
                    enabled=db_fence.enabled,
                    metadata=db_fence.data or {}
                )
                fence.created_at = db_fence.created_at.isoformat() if db_fence.created_at else fence.created_at
                self.geofences[fence.zone_id] = fence
            logger.info(f"Loaded {len(self.geofences)} geofences from database")
        except Exception as e:
            logger.error(f"Failed to load geofences from DB: {e}")
        finally:
            db.close()
    
    def save_geofences(self):
        """Not needed for DB version, changes are committed individually. Re-syncing for safety."""
        self.load_geofences()
    
    def create_geofence(self, fence: GeoFence) -> GeoFence:
        """
        Create a new geofence (DB-backed)
        """
        db = SessionLocal()
        try:
            db_fence = GeofenceModel(
                id=fence.zone_id,
                name=fence.name,
                center_lat=fence.center_lat,
                center_lon=fence.center_lon,
                radius_meters=fence.radius_meters,
                zone_type=fence.zone_type,
                alert_on_entry=fence.alert_on_entry,
                alert_on_exit=fence.alert_on_exit,
                enabled=fence.enabled,
                data=fence.metadata
            )
            db.add(db_fence)
            db.commit()
            
            # Update local cache
            self.geofences[fence.zone_id] = fence
            logger.info(f"Created geofence in DB: {fence.name} ({fence.zone_id})")
            return fence
        except Exception as e:
            db.rollback()
            logger.error(f"Failed to create geofence in DB: {e}")
            raise
        finally:
            db.close()
    
    def update_geofence(self, zone_id: str, updates: Dict[str, Any]) -> Optional[GeoFence]:
        """
        Update an existing geofence (DB-backed)
        """
        db = SessionLocal()
        try:
            db_fence = db.query(GeofenceModel).filter(GeofenceModel.id == zone_id).first()
            if not db_fence:
                return None
            
            # Update allowed fields
            for key, value in updates.items():
                if hasattr(db_fence, key) and key != 'id':
                    setattr(db_fence, key, value)
                elif key == 'metadata':
                    db_fence.data = value
            
            db.commit()
            
            # Re-read to refresh cache
            self.load_geofences()
            return self.geofences.get(zone_id)
        except Exception as e:
            db.rollback()
            logger.error(f"Failed to update geofence in DB: {e}")
            return None
        finally:
            db.close()
    
    def delete_geofence(self, zone_id: str) -> bool:
        """
        Delete a geofence (DB-backed)
        """
        db = SessionLocal()
        try:
            db_fence = db.query(GeofenceModel).filter(GeofenceModel.id == zone_id).first()
            if db_fence:
                db.delete(db_fence)
                db.commit()
                if zone_id in self.geofences:
                    self.geofences.pop(zone_id)
                logger.info(f"Deleted geofence from DB: {zone_id}")
                return True
            return False
        except Exception as e:
            db.rollback()
            logger.error(f"Failed to delete geofence from DB: {e}")
            return False
        finally:
            db.close()
    
    def get_geofence(self, zone_id: str) -> Optional[GeoFence]:
        """Get a geofence by ID"""
        return self.geofences.get(zone_id)
    
    def list_geofences(self, enabled_only: bool = False) -> List[GeoFence]:
        """
        List all geofences
        
        Args:
            enabled_only: Only return enabled geofences
            
        Returns:
            List of GeoFence objects
        """
        fences = list(self.geofences.values())
        if enabled_only:
            fences = [f for f in fences if f.enabled]
        return fences
    
    def check_position(self, entity_id: str, lat: float, lon: float) -> List[Dict[str, Any]]:
        """
        Check entity position against all geofences
        
        Args:
            entity_id: Entity identifier
            lat: Current latitude
            lon: Current longitude
            
        Returns:
            List of alerts/events triggered
        """
        alerts = []
        
        # Initialize entity state if new
        if entity_id not in self.entity_states:
            self.entity_states[entity_id] = {}
        
        # Check against all enabled geofences
        for fence in self.geofences.values():
            if not fence.enabled:
                continue
            
            currently_inside = fence.contains_point(lat, lon)
            previously_inside = self.entity_states[entity_id].get(fence.zone_id, False)
            
            # Detect entry
            if currently_inside and not previously_inside and fence.alert_on_entry:
                distance = fence.calculate_distance(lat, lon)
                alerts.append({
                    "type": "geofence_entry",
                    "entity_id": entity_id,
                    "zone_id": fence.zone_id,
                    "zone_name": fence.name,
                    "zone_type": fence.zone_type,
                    "lat": lat,
                    "lon": lon,
                    "distance_from_center": distance,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "message": f"Entity {entity_id} entered zone {fence.name}"
                })
            
            # Detect exit
            elif not currently_inside and previously_inside and fence.alert_on_exit:
                distance = fence.calculate_distance(lat, lon)
                alerts.append({
                    "type": "geofence_exit",
                    "entity_id": entity_id,
                    "zone_id": fence.zone_id,
                    "zone_name": fence.name,
                    "zone_type": fence.zone_type,
                    "lat": lat,
                    "lon": lon,
                    "distance_from_center": distance,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "message": f"Entity {entity_id} exited zone {fence.name}"
                })
            
            # Update state
            self.entity_states[entity_id][fence.zone_id] = currently_inside
        
        return alerts
    
    def get_zones_containing(self, lat: float, lon: float) -> List[GeoFence]:
        """
        Get all zones containing a point
        
        Args:
            lat: Latitude
            lon: Longitude
            
        Returns:
            List of GeoFence objects containing the point
        """
        return [
            fence for fence in self.geofences.values()
            if fence.enabled and fence.contains_point(lat, lon)
        ]
    
    def get_nearest_zones(self, lat: float, lon: float, limit: int = 5) -> List[Tuple[GeoFence, float]]:
        """
        Get nearest zones to a point
        
        Args:
            lat: Latitude
            lon: Longitude
            limit: Maximum number of zones to return
            
        Returns:
            List of (GeoFence, distance_in_meters) tuples, sorted by distance
        """
        zones_with_distance = [
            (fence, fence.calculate_distance(lat, lon))
            for fence in self.geofences.values()
            if fence.enabled
        ]
        
        # Sort by distance
        zones_with_distance.sort(key=lambda x: x[1])
        
        return zones_with_distance[:limit]


def haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """
    Calculate the great circle distance between two points on Earth
    using the Haversine formula
    
    Args:
        lat1: Latitude of first point
        lon1: Longitude of first point
        lat2: Latitude of second point
        lon2: Longitude of second point
        
    Returns:
        Distance in meters
    """
    # Earth radius in meters
    R = 6371000
    
    # Convert to radians
    lat1_rad = math.radians(lat1)
    lat2_rad = math.radians(lat2)
    delta_lat = math.radians(lat2 - lat1)
    delta_lon = math.radians(lon2 - lon1)
    
    # Haversine formula
    a = (math.sin(delta_lat / 2) ** 2 +
         math.cos(lat1_rad) * math.cos(lat2_rad) *
         math.sin(delta_lon / 2) ** 2)
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    
    distance = R * c
    return distance


def calculate_bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """
    Calculate bearing from point 1 to point 2
    
    Args:
        lat1: Latitude of first point
        lon1: Longitude of first point
        lat2: Latitude of second point
        lon2: Longitude of second point
        
    Returns:
        Bearing in degrees (0-360)
    """
    lat1_rad = math.radians(lat1)
    lat2_rad = math.radians(lat2)
    delta_lon = math.radians(lon2 - lon1)
    
    x = math.sin(delta_lon) * math.cos(lat2_rad)
    y = (math.cos(lat1_rad) * math.sin(lat2_rad) -
         math.sin(lat1_rad) * math.cos(lat2_rad) * math.cos(delta_lon))
    
    bearing = math.atan2(x, y)
    bearing = math.degrees(bearing)
    bearing = (bearing + 360) % 360
    
    return bearing
