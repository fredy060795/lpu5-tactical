import json
import os
import sys
from datetime import datetime
from database import SessionLocal, engine, Base
from models import User, MapMarker, Mission, MeshtasticNode, AutonomousRule, Geofence, ChatMessage, AuditLog

# Create tables
Base.metadata.create_all(bind=engine)

def load_json_file(filename):
    if not os.path.exists(filename):
        print(f"File not found: {filename}")
        return []
    try:
        with open(filename, 'r', encoding='utf-8') as f:
            data = json.load(f)
            # Some files mimic a DB structure with a root key, others are lists
            if isinstance(data, dict):
                # Try to find the list inside
                for key in data.keys():
                     if isinstance(data[key], list):
                         return data[key]
                return [] # Fallback
            return data
    except Exception as e:
        print(f"Error loading {filename}: {e}")
        return []

def migrate():
    db = SessionLocal()
    print("Starting migration...")

    # migrate Users
    users_data = load_json_file("users_db.json")
    for u in users_data:
        existing = db.query(User).filter(User.username == u.get("username")).first()
        if existing:
            existing.password_hash = u.get("password_hash")
            existing.email = u.get("email")
            existing.role = u.get("role", "user")
            existing.group_id = u.get("group_id", "users")
            existing.unit = u.get("unit")
            existing.device = u.get("device")
            existing.rank = u.get("rank")
            existing.fullname = u.get("fullname")
            existing.callsign = u.get("callsign")
            existing.is_active = u.get("active", True)
            existing.data = u
        else:
            user = User(
                id=u.get("id"),
                username=u.get("username"),
                password_hash=u.get("password_hash"),
                email=u.get("email"),
                role=u.get("role", "user"),
                group_id=u.get("group_id", "users"),
                unit=u.get("unit"),
                device=u.get("device"),
                rank=u.get("rank"),
                fullname=u.get("fullname"),
                callsign=u.get("callsign"),
                is_active=u.get("active", True),
                data=u
            )
            db.add(user)
    print(f"Processed {len(users_data)} users.")

    # migrate Map Markers & Symbols
    markers_data = load_json_file("map_markers_db.json")
    # Handle api.py wrapping: {"map_markers": [...]}
    if isinstance(markers_data, dict) and "map_markers" in markers_data:
        markers_data = markers_data["map_markers"]

    # Also load symbols
    symbols_data = load_json_file("symbols_db.json")
    if isinstance(symbols_data, dict) and "symbols" in symbols_data:
        symbols_data = symbols_data["symbols"]
    
    # Combine lists
    if isinstance(symbols_data, list):
        if isinstance(markers_data, list):
            markers_data.extend(symbols_data)
        else:
            markers_data = symbols_data
            
    for m in markers_data:
        if not db.query(MapMarker).filter(MapMarker.id == m.get("id")).first():
            marker = MapMarker(
                id=m.get("id"),
                name=m.get("name") or m.get("label"),
                description=m.get("description"),
                lat=m.get("lat"),
                lng=m.get("lng"),
                type=m.get("type", "unknown"),
                color=m.get("color", "#ffffff"),
                icon=m.get("icon", "default"),
                created_by=m.get("created_by") or m.get("username"),
                data=m # Store full object as JSON payload for flexibility
            )
            db.add(marker)
    print(f"Migrated {len(markers_data)} markers.")

    # migrate Meshtastic Nodes
    mesh_nodes_data = load_json_file("meshtastic_nodes_db.json")
    for n in mesh_nodes_data:
        if not db.query(MeshtasticNode).filter(MeshtasticNode.id == n.get("id")).first():
            # Parse timestamp if exists
            last_heard = None
            if n.get("last_heard"):
                try:
                    last_heard = datetime.fromisoformat(n.get("last_heard").replace('Z', '+00:00'))
                except:
                    pass

            node = MeshtasticNode(
                id=n.get("id"),
                long_name=n.get("longName"),
                short_name=n.get("shortName"),
                lat=n.get("latitude"),
                lng=n.get("longitude"),
                altitude=n.get("altitude"),
                battery_level=n.get("battery_level"),
                last_heard=last_heard,
                is_online=False, # Default
                hardware_model=n.get("hardware_model"),
                raw_data=n
            )
            db.add(node)
    print(f"Migrated {len(mesh_nodes_data)} mesh nodes.")

    # migrate Missions
    missions_data = load_json_file("missions_db.json")
    for m in missions_data:
        if not db.query(Mission).filter(Mission.id == m.get("id")).first():
             mission = Mission(
                 id=m.get("id"),
                 name=m.get("name"),
                 description=m.get("description"),
                 status=m.get("status", "active"),
                 data=m
             )
             db.add(mission)
    print(f"Migrated {len(missions_data)} missions.")

    # migrate Autonomous Rules
    rules_data = load_json_file("autonomous_rules_db.json")
    for r in rules_data:
        if not db.query(AutonomousRule).filter(AutonomousRule.id == r.get("rule_id")).first():
            rule = AutonomousRule(
                id=r.get("rule_id"),
                name=r.get("name"),
                description=r.get("description"),
                trigger_type=r.get("trigger_type"),
                trigger_config=r.get("trigger_config"),
                conditions=r.get("conditions"),
                actions=r.get("actions"),
                enabled=r.get("enabled", True),
                priority=r.get("priority", 5),
                execution_count=r.get("execution_count", 0),
                data=r
            )
            db.add(rule)
    print(f"Migrated {len(rules_data)} rules.")

    # migrate Geofences
    geofences_data = load_json_file("geofences_db.json")
    if not geofences_data:
        geofences_data = load_json_file("example_geofences.json") # Fallback for dev
        
    for g in geofences_data:
        if not db.query(Geofence).filter(Geofence.id == g.get("zone_id")).first():
            fence = Geofence(
                id=g.get("zone_id"),
                name=g.get("name"),
                center_lat=g.get("center_lat"),
                center_lon=g.get("center_lon"),
                radius_meters=g.get("radius_meters"),
                zone_type=g.get("zone_type", "exclusion"),
                alert_on_entry=g.get("alert_on_entry", True),
                alert_on_exit=g.get("alert_on_exit", False),
                enabled=g.get("enabled", True),
                color=g.get("color", "#ff0000"),
                data=g
            )
            db.add(fence)
    print(f"Migrated {len(geofences_data)} geofences.")

    try:
        db.commit()
        print("Migration committed successfully.")
    except Exception as e:
        db.rollback()
        print(f"Migration failed during commit: {e}")
    finally:
        db.close()

if __name__ == "__main__":
    migrate()
