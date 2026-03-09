from sqlalchemy import Column, Integer, String, Float, Boolean, DateTime, ForeignKey, Text, JSON
from sqlalchemy.orm import relationship
from database import Base
from datetime import datetime, timezone
import uuid

def generate_uuid():
    return str(uuid.uuid4())

class Unit(Base):
    __tablename__ = "units"
    id = Column(String, primary_key=True, default=generate_uuid)
    name = Column(String, unique=True, nullable=False)
    description = Column(String, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

class User(Base):
    __tablename__ = "users"
    id = Column(String, primary_key=True, default=generate_uuid)
    username = Column(String, unique=True, index=True)
    password_hash = Column(String)
    email = Column(String, nullable=True)
    role = Column(String, default="user")
    group_id = Column(String, default="users")
    unit = Column(String, nullable=True)
    unit_id = Column(String, ForeignKey("units.id"), nullable=True)
    device = Column(String, nullable=True)
    rank = Column(String, nullable=True)
    fullname = Column(String, nullable=True)
    callsign = Column(String, nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    chat_channels = Column(JSON, nullable=True, default=lambda: ["all"])  # allowed chat channel IDs
    # ATAK / TAK interoperability fields — used when generating CoT SA beacons
    tak_team = Column(String, nullable=True, default="Cyan")          # team colour (e.g. "Cyan", "Red")
    tak_role = Column(String, nullable=True, default="Team Member")   # tactical role (e.g. "Team Member", "HQ")
    tak_display_type = Column(String, nullable=True, default="General Ground Unit")  # display type / CoT symbol
    data = Column(JSON, nullable=True) # Catch-all for extra legacy fields like history

class MapMarker(Base):
    __tablename__ = "map_markers"
    id = Column(String, primary_key=True, default=generate_uuid)
    name = Column(String)
    description = Column(String, nullable=True)
    lat = Column(Float)
    lng = Column(Float)
    type = Column(String)  # friendly, hostile, neutral, unknown
    color = Column(String, default="#ffffff")
    icon = Column(String, default="default")
    created_by = Column(String, ForeignKey("users.username"), nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    data = Column(JSON, nullable=True)  # Extra properties

class Mission(Base):
    __tablename__ = "missions"
    id = Column(String, primary_key=True, default=generate_uuid)
    name = Column(String)
    description = Column(String, nullable=True)
    status = Column(String, default="active")  # active, completed, archived
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    data = Column(JSON, nullable=True)

class MeshtasticNode(Base):
    __tablename__ = "meshtastic_nodes"
    id = Column(String, primary_key=True)  # Typically the node ID (e.g., !1234abcd)
    long_name = Column(String, nullable=True)
    short_name = Column(String, nullable=True)
    lat = Column(Float, nullable=True)
    lng = Column(Float, nullable=True)
    altitude = Column(Float, nullable=True)
    battery_level = Column(Integer, nullable=True)
    last_heard = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    is_online = Column(Boolean, default=False)
    hardware_model = Column(String, nullable=True)
    raw_data = Column(JSON, nullable=True)

class AutonomousRule(Base):
    __tablename__ = "autonomous_rules"
    id = Column(String, primary_key=True, default=generate_uuid)
    name = Column(String)
    description = Column(String, nullable=True)
    trigger_type = Column(String)  # geofence, time, etc.
    trigger_config = Column(JSON)
    conditions = Column(JSON)
    actions = Column(JSON)
    enabled = Column(Boolean, default=True)
    priority = Column(Integer, default=5)
    last_triggered = Column(DateTime, nullable=True)
    execution_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    data = Column(JSON, nullable=True) # Catch-all for extra fields

class Geofence(Base):
    __tablename__ = "geofences"
    id = Column(String, primary_key=True, default=generate_uuid)
    name = Column(String)
    center_lat = Column(Float, nullable=True)
    center_lon = Column(Float, nullable=True)
    radius_meters = Column(Float, nullable=True)
    points = Column(JSON, nullable=True)  # List of [lat, lng] for polygons
    zone_type = Column(String, default="exclusion")
    alert_on_entry = Column(Boolean, default=True)
    alert_on_exit = Column(Boolean, default=False)
    enabled = Column(Boolean, default=True)
    color = Column(String, default="#ff0000")
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    data = Column(JSON, nullable=True) # For metadata etc.

class ChatMessage(Base):
    __tablename__ = "chat_messages"
    id = Column(String, primary_key=True, default=generate_uuid)
    channel = Column(String, index=True)
    sender = Column(String)
    content = Column(Text)
    timestamp = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    type = Column(String, default="text")
    delivered_to = Column(JSON, nullable=True, default=lambda: [])  # list of usernames who received
    read_by = Column(JSON, nullable=True, default=lambda: [])       # list of usernames who read

class ChatChannel(Base):
    __tablename__ = "chat_channels"
    id = Column(String, primary_key=True, default=generate_uuid)
    name = Column(String, unique=True)
    description = Column(String, nullable=True)
    color = Column(String, default="#ffffff")
    created_by = Column(String, nullable=True)
    members = Column(JSON, nullable=True, default=lambda: [])  # list of usernames
    is_default = Column(Boolean, default=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

class AuditLog(Base):
    __tablename__ = "audit_log"
    id = Column(String, primary_key=True, default=generate_uuid)
    event_type = Column(String)
    user = Column(String, nullable=True)
    details = Column(String)
    ip_address = Column(String, nullable=True)
    timestamp = Column(DateTime, default=lambda: datetime.now(timezone.utc))

class Drawing(Base):
    __tablename__ = "drawings"
    id = Column(String, primary_key=True, default=generate_uuid)
    name = Column(String)
    type = Column(String, default="polyline")  # polyline, polygon, etc.
    coordinates = Column(JSON)  # List of [lat, lng]
    color = Column(String, default="#3388ff")
    weight = Column(Integer, default=3)
    created_by = Column(String, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    data = Column(JSON, nullable=True)

class Overlay(Base):
    __tablename__ = "overlays"
    id = Column(String, primary_key=True, default=generate_uuid)
    name = Column(String)
    image_url = Column(String)
    bounds = Column(JSON)  # {north, south, east, west}
    opacity = Column(Float, default=1.0)
    rotation = Column(Float, default=0.0)
    created_by = Column(String, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    data = Column(JSON, nullable=True)

class APISession(Base):
    __tablename__ = "api_sessions"
    id = Column(String, primary_key=True, default=generate_uuid)
    token = Column(String, index=True)
    user_id = Column(String, ForeignKey("users.id"))
    username = Column(String)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    expires_at = Column(DateTime)
    ip = Column(String, nullable=True)
    last_seen = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    data = Column(JSON, nullable=True)

class UserGroup(Base):
    __tablename__ = "user_groups"
    id = Column(String, primary_key=True, default=generate_uuid)
    name = Column(String, unique=True)
    description = Column(String, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    data = Column(JSON, nullable=True)

class QRCode(Base):
    __tablename__ = "qr_codes"
    id = Column(String, primary_key=True, default=generate_uuid)
    token = Column(String, unique=True, index=True)
    type = Column(String)  # login, registration, mission
    created_by = Column(String)
    expires_at = Column(DateTime, nullable=True)
    max_uses = Column(Integer, default=0)  # 0 = unlimited
    uses = Column(Integer, default=0)
    allowed_ips = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    data = Column(JSON, nullable=True)

class PendingRegistration(Base):
    __tablename__ = "pending_registrations"
    id = Column(String, primary_key=True, default=generate_uuid)
    token = Column(String, unique=True, index=True)
    username = Column(String)
    password_hash = Column(String)
    email = Column(String, nullable=True)
    fullname = Column(String, nullable=True)
    callsign = Column(String, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    data = Column(JSON, nullable=True)


class DeletedMarker(Base):
    """
    Persistent tombstone for every map marker that has been explicitly deleted
    in LPU5.  Rows in this table prevent CoT echo-backs from ATAK/WinTAK from
    silently recreating a marker that a user intentionally removed.

    The record lives forever (no automatic expiry) so that even after a server
    restart the deletion is honoured.  A row can be removed manually via the
    admin API if the same marker UID should be allowed to appear again.
    """
    __tablename__ = "deleted_markers"
    marker_id = Column(String, primary_key=True, index=True)
    deleted_by = Column(String, nullable=True)   # username of who deleted it
    deleted_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# Federation models
# ---------------------------------------------------------------------------

class FederatedServer(Base):
    """
    Registry of remote LPU5 servers participating in federation.
    Each entry holds the peer's RSA public key and trust state.
    A server becomes *trusted* only after a successful challenge/response
    handshake (mutual verification).
    """
    __tablename__ = "federated_servers"
    id = Column(String, primary_key=True, default=generate_uuid)
    name = Column(String, nullable=False)               # human-readable label
    url = Column(String, nullable=True)                 # base URL for REST calls (optional)
    server_id = Column(String, unique=True, index=True) # remote server's own ID (UUID)
    public_key_pem = Column(Text, nullable=False)       # RSA public key in PEM format
    fingerprint = Column(String, index=True)            # SHA-256 of DER-encoded public key
    trusted = Column(Boolean, default=False)            # True only after successful handshake
    registered_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    last_seen = Column(DateTime, nullable=True)
    meta = Column(JSON, nullable=True)                  # free-form peer metadata


class FederationChallenge(Base):
    """
    Pending challenge issued to a federated peer.
    The peer must sign the challenge bytes with its private key and return
    the signature.  The challenge expires after a short window to prevent
    replay attacks.
    """
    __tablename__ = "federation_challenges"
    id = Column(String, primary_key=True, default=generate_uuid)
    federated_server_id = Column(String, ForeignKey("federated_servers.id"), nullable=False)
    challenge_b64 = Column(String, nullable=False)      # base64-encoded random bytes
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    expires_at = Column(DateTime, nullable=False)
    used = Column(Boolean, default=False)
