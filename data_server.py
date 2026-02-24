#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
data_server.py - LPU5 Tactical Tracker Data Distribution Server

Separate process for managing real-time data distribution via WebSocket.
This server handles:
- WebSocket connections from clients
- Broadcasting of map markers, drawings, overlays
- Distribution of messages and status updates
- Real-time tactical data synchronization

The data server runs independently from the main API server,
allowing for better scalability and separation of concerns.
"""

import asyncio
import json
import logging
import signal
import sys
from datetime import datetime, timezone
from typing import Dict, Set, Any, Optional

# Fix Windows asyncio ProactorEventLoop issue
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

# -------------------------
# Logging setup
# -------------------------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("lpu5-data-server")

# -------------------------
# Data Server Configuration
# -------------------------
DATA_SERVER_PORT = 8102
DATA_SERVER_HOST = "0.0.0.0"

# -------------------------
# WebSocket Connection Manager
# -------------------------
class DataServerConnectionManager:
    """
    Manages WebSocket connections for data distribution with multicast group routing.

    Architecture overview
    ---------------------
    Each WebSocket client (EUD / stream.html) connects to /ws and receives a unique
    connection_id.  Clients join logical groups – called *units* – by sending a
    ``subscribe`` or ``join_group`` message.  Internally every unit is stored as a
    channel named ``unit:<unit_name>``.  A plain ``camera`` channel is kept for
    backward-compatible global broadcasts.

    Group membership is maintained in ``self.subscriptions``:
        ``{ "unit:alpha": {"conn-1", "conn-3"}, "camera": {"conn-1", "conn-2"}, ... }``

    How to add a new group
    ----------------------
    No server-side registration is required.  A group is created automatically the
    first time any client subscribes to it.  Remove a group by having all members
    unsubscribe; the empty set is cleaned up automatically.

    How a client joins a group
    --------------------------
    Send over the WebSocket::

        { "type": "join_group", "group": "alpha" }

    This subscribes the connection to the ``unit:alpha`` channel.

    How to send a stream to a specific group
    ----------------------------------------
    Include ``target_units`` in any stream-related message::

        { "type": "stream_share", "streamId": "...", "active": true,
          "target_units": ["alpha", "bravo"], ... }

    The server will relay the message exclusively to subscribers of
    ``unit:alpha`` and ``unit:bravo``.  If ``target_units`` is empty or absent,
    the message is broadcast to the global ``camera`` channel (backward compat).
    """

    def __init__(self):
        self.active_connections: Dict[str, WebSocket] = {}
        self.subscriptions: Dict[str, Set[str]] = {}  # channel -> set of connection_ids
        self.connection_metadata: Dict[str, Dict[str, Any]] = {}
        
    async def connect(self, websocket: WebSocket, connection_id: str):
        """Accept and register a new WebSocket connection"""
        await websocket.accept()
        self.active_connections[connection_id] = websocket
        self.connection_metadata[connection_id] = {
            "connected_at": datetime.now(timezone.utc).isoformat(),
            "last_activity": datetime.now(timezone.utc).isoformat(),
            "messages_sent": 0,
            "subscriptions": set()
        }
        logger.info(f"Client connected: {connection_id}")
        
        # Send welcome message
        await self.send_to_connection(connection_id, {
            "type": "connection_established",
            "connection_id": connection_id,
            "timestamp": datetime.now(timezone.utc).isoformat()
        })
    
    def disconnect(self, connection_id: str):
        """Disconnect and cleanup a connection"""
        if connection_id in self.active_connections:
            del self.active_connections[connection_id]
            
        # Remove from all channel subscriptions
        for channel, subscribers in self.subscriptions.items():
            subscribers.discard(connection_id)
            
        if connection_id in self.connection_metadata:
            del self.connection_metadata[connection_id]
            
        logger.info(f"Client disconnected: {connection_id}")
    
    async def subscribe(self, connection_id: str, channel: str):
        """Subscribe a connection to a channel"""
        if channel not in self.subscriptions:
            self.subscriptions[channel] = set()
        self.subscriptions[channel].add(connection_id)
        
        if connection_id in self.connection_metadata:
            self.connection_metadata[connection_id]["subscriptions"].add(channel)
        
        logger.info(f"Connection {connection_id} subscribed to channel: {channel}")
        
        # Send confirmation
        await self.send_to_connection(connection_id, {
            "type": "subscribed",
            "channel": channel,
            "timestamp": datetime.now(timezone.utc).isoformat()
        })
    
    async def unsubscribe(self, connection_id: str, channel: str):
        """Unsubscribe a connection from a channel"""
        if channel in self.subscriptions:
            self.subscriptions[channel].discard(connection_id)
            
        if connection_id in self.connection_metadata:
            self.connection_metadata[connection_id]["subscriptions"].discard(channel)
        
        logger.info(f"Connection {connection_id} unsubscribed from channel: {channel}")
        
        # Send confirmation
        await self.send_to_connection(connection_id, {
            "type": "unsubscribed",
            "channel": channel,
            "timestamp": datetime.now(timezone.utc).isoformat()
        })
    
    async def send_to_connection(self, connection_id: str, message: dict):
        """Send a message to a specific connection"""
        if connection_id in self.active_connections:
            try:
                await self.active_connections[connection_id].send_json(message)
                if connection_id in self.connection_metadata:
                    self.connection_metadata[connection_id]["messages_sent"] += 1
                    self.connection_metadata[connection_id]["last_activity"] = datetime.now(timezone.utc).isoformat()
            except Exception as e:
                logger.error(f"Failed to send message to {connection_id}: {e}")
                # Connection may be dead, will be cleaned up on next interaction
    
    async def broadcast_to_channel(self, channel: str, message: dict, exclude: Optional[str] = None):
        """Broadcast a message to all subscribers of a channel"""
        if channel not in self.subscriptions:
            return
        
        subscribers = list(self.subscriptions[channel])
        logger.debug(f"Broadcasting to channel '{channel}': {len(subscribers)} subscribers")
        
        # Add metadata to message
        message["channel"] = channel
        if "timestamp" not in message:
            message["timestamp"] = datetime.now(timezone.utc).isoformat()
        
        # Send to all subscribers (optionally excluding the sender)
        for connection_id in subscribers:
            if exclude and connection_id == exclude:
                continue
            await self.send_to_connection(connection_id, message)
    
    async def broadcast_to_all(self, message: dict):
        """Broadcast a message to all connected clients"""
        logger.debug(f"Broadcasting to all: {len(self.active_connections)} connections")
        
        if "timestamp" not in message:
            message["timestamp"] = datetime.now(timezone.utc).isoformat()
        
        for connection_id in list(self.active_connections.keys()):
            await self.send_to_connection(connection_id, message)
    
    async def join_group(self, connection_id: str, group_name: str):
        """
        Subscribe a connection to a unit group.

        Internally this subscribes the connection to the ``unit:<group_name>``
        channel.  Creates the channel entry if it does not exist yet.

        Args:
            connection_id: The WebSocket connection that wants to join.
            group_name:    The logical group / unit name (e.g. "alpha").
        """
        channel = f"unit:{group_name}"
        await self.subscribe(connection_id, channel)
        await self.send_to_connection(connection_id, {
            "type": "joined_group",
            "group": group_name,
            "channel": channel,
            "timestamp": datetime.now(timezone.utc).isoformat()
        })

    async def leave_group(self, connection_id: str, group_name: str):
        """
        Unsubscribe a connection from a unit group.

        Args:
            connection_id: The WebSocket connection that wants to leave.
            group_name:    The logical group / unit name.
        """
        channel = f"unit:{group_name}"
        await self.unsubscribe(connection_id, channel)
        await self.send_to_connection(connection_id, {
            "type": "left_group",
            "group": group_name,
            "channel": channel,
            "timestamp": datetime.now(timezone.utc).isoformat()
        })

    def list_groups(self) -> Dict[str, Any]:
        """
        Return all active unit groups with their subscriber counts.

        Returns a dict keyed by group name (without the ``unit:`` prefix)::

            { "alpha": 3, "bravo": 1 }
        """
        groups: Dict[str, Any] = {}
        for channel, members in self.subscriptions.items():
            if channel.startswith("unit:"):
                group_name = channel[len("unit:"):]
                groups[group_name] = len(members)
        return groups

    def get_stats(self) -> dict:
        """Get connection statistics"""
        return {
            "active_connections": len(self.active_connections),
            "channels": {
                channel: len(subscribers)
                for channel, subscribers in self.subscriptions.items()
            },
            "total_subscriptions": sum(len(s) for s in self.subscriptions.values()),
            "groups": self.list_groups()
        }

# -------------------------
# FastAPI Application
# -------------------------
app = FastAPI(
    title="LPU5 Data Distribution Server",
    description="Real-time data distribution via WebSocket",
    version="1.0.0"
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Global connection manager
connection_manager = DataServerConnectionManager()

# -------------------------
# WebSocket Endpoint
# -------------------------
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """
    WebSocket endpoint for real-time data distribution with multicast group routing.

    Supported client → server messages
    -----------------------------------
    subscribe        { "type": "subscribe",   "channel": "<channel>" }
        Low-level channel subscription (e.g. "camera", "unit:alpha").

    unsubscribe      { "type": "unsubscribe", "channel": "<channel>" }
        Remove the connection from the given channel.

    join_group       { "type": "join_group",  "group": "<unit_name>" }
        Join a named unit group.  Equivalent to subscribing to "unit:<unit_name>".
        The server replies with { "type": "joined_group", "group": "...", "channel": "..." }.

    leave_group      { "type": "leave_group", "group": "<unit_name>" }
        Leave a named unit group.
        The server replies with { "type": "left_group", "group": "...", "channel": "..." }.

    ping             { "type": "ping" }
        Keep-alive; server replies with { "type": "pong" }.

    camera_frame     { "type": "camera_frame", "frame": "<base64>", "streamId": "...",
                        "target_units": ["alpha", "bravo"] }
        Relay a camera frame.  When target_units is non-empty, the frame is sent
        exclusively to subscribers of those unit channels.  When target_units is
        empty or absent, the frame is broadcast to the global "camera" channel.

    stream_available { "type": "stream_available", "streamId": "...", "active": true,
                        "isCamera": true, "source": "...", "details": "...",
                        "target_units": ["alpha"] }
        Announce that a local camera stream is available (or has stopped) without
        immediately broadcasting it.  Used by overview.html to notify stream.html
        that a feed can be selected for broadcast.  Routing follows the same
        target_units logic as camera_frame.

    stream_share     { "type": "stream_share", "streamId": "...", "active": true,
                        "isCamera": true, "source": "...", "details": "...",
                        "stream_url": null, "target_units": ["alpha"] }
        Announce or end a stream.  Routing follows the same target_units logic as
        camera_frame.

    broadcast_selected  { "type": "broadcast_selected", "streamId": "...",
                           "source": "...", "details": "...",
                           "target_units": ["alpha"] }
        Signal EUDs that a stream has been selected for broadcast.  Routing follows
        the same target_units logic.
    """
    import uuid
    connection_id = str(uuid.uuid4())
    
    try:
        await connection_manager.connect(websocket, connection_id)
        
        while True:
            try:
                # Receive message from client
                data = await websocket.receive_json()
                message_type = data.get("type")
                
                # Handle low-level channel subscription
                if message_type == "subscribe":
                    channel = data.get("channel")
                    if channel:
                        await connection_manager.subscribe(connection_id, channel)
                
                # Handle low-level channel unsubscription
                elif message_type == "unsubscribe":
                    channel = data.get("channel")
                    if channel:
                        await connection_manager.unsubscribe(connection_id, channel)
                
                # Semantic group join – subscribes to unit:<group> channel
                elif message_type == "join_group":
                    group = data.get("group")
                    if group:
                        await connection_manager.join_group(connection_id, group)
                    else:
                        await connection_manager.send_to_connection(connection_id, {
                            "type": "error",
                            "error": "join_group requires a 'group' field"
                        })
                
                # Semantic group leave – unsubscribes from unit:<group> channel
                elif message_type == "leave_group":
                    group = data.get("group")
                    if group:
                        await connection_manager.leave_group(connection_id, group)
                    else:
                        await connection_manager.send_to_connection(connection_id, {
                            "type": "error",
                            "error": "leave_group requires a 'group' field"
                        })
                
                # Handle ping
                elif message_type == "ping":
                    await connection_manager.send_to_connection(connection_id, {
                        "type": "pong",
                        "timestamp": datetime.now(timezone.utc).isoformat()
                    })
                
                # Relay camera frames
                # If target_units is specified → route only to those unit channels (multicast).
                # If target_units is empty / absent → broadcast to the global "camera" channel.
                elif message_type == "camera_frame":
                    target_units = data.get("target_units") or []
                    frame_msg = {
                        "type": "camera_frame",
                        "frame": data.get("frame"),
                        "streamId": data.get("streamId"),
                        "source_connection": connection_id
                    }
                    if target_units:
                        for unit_name in target_units:
                            await connection_manager.broadcast_to_channel(
                                f"unit:{unit_name}", frame_msg, exclude=connection_id
                            )
                    else:
                        # No specific target → global camera channel
                        await connection_manager.broadcast_to_channel("camera", frame_msg, exclude=connection_id)
                
                # Relay stream-available announcements from EUDs (e.g. overview.html)
                # so that stream.html can list them as incoming streams.
                # If target_units is specified → route only to those unit channels.
                # If target_units is empty / absent → broadcast to the global "camera" channel.
                elif message_type == "stream_available":
                    target_units = data.get("target_units") or []
                    avail_msg = {
                        "type": "stream_available",
                        "streamId": data.get("streamId"),
                        "active": data.get("active", False),
                        "isCamera": data.get("isCamera", False),
                        "source": data.get("source"),
                        "details": data.get("details"),
                        "stream_url": data.get("stream_url"),
                        "timestamp": data.get("timestamp"),
                        "target_units": target_units,
                        "source_connection": connection_id
                    }
                    if target_units:
                        for unit_name in target_units:
                            await connection_manager.broadcast_to_channel(
                                f"unit:{unit_name}", avail_msg, exclude=connection_id
                            )
                    else:
                        await connection_manager.broadcast_to_channel("camera", avail_msg, exclude=connection_id)

                # Relay stream sharing
                # If target_units is specified → route only to those unit channels (multicast).
                # If target_units is empty / absent → broadcast to the global "camera" channel.
                elif message_type == "stream_share":
                    target_units = data.get("target_units") or []
                    share_msg = {
                        "type": "stream_share",
                        "streamId": data.get("streamId", "camera_main"),
                        "active": data.get("active", False),
                        "isCamera": data.get("isCamera", False),
                        "source": data.get("source"),
                        "details": data.get("details"),
                        "stream_url": data.get("stream_url"),
                        "timestamp": data.get("timestamp"),
                        "target_units": target_units,
                        "source_connection": connection_id
                    }
                    if target_units:
                        for unit_name in target_units:
                            await connection_manager.broadcast_to_channel(
                                f"unit:{unit_name}", share_msg, exclude=connection_id
                            )
                    else:
                        await connection_manager.broadcast_to_channel("camera", share_msg, exclude=connection_id)
                
                # Relay broadcast selection (stream.html selects which stream to forward to EUDs)
                # If target_units is specified → route only to those unit channels (multicast).
                # If target_units is empty / absent → broadcast to the global "camera" channel.
                elif message_type == "broadcast_selected":
                    target_units = data.get("target_units") or []
                    sel_msg = {
                        "type": "broadcast_selected",
                        "streamId": data.get("streamId"),
                        "source": data.get("source"),
                        "details": data.get("details"),
                        "timestamp": data.get("timestamp"),
                        "target_units": target_units,
                        "source_connection": connection_id
                    }
                    if target_units:
                        for unit_name in target_units:
                            await connection_manager.broadcast_to_channel(
                                f"unit:{unit_name}", sel_msg, exclude=connection_id
                            )
                    else:
                        await connection_manager.broadcast_to_channel("camera", sel_msg, exclude=connection_id)
                
                # Unknown message type
                else:
                    logger.warning(f"Unknown message type from {connection_id}: {message_type}")
                    
            except WebSocketDisconnect:
                break
            except Exception as e:
                logger.error(f"Error processing message from {connection_id}: {e}")
                break
                
    except Exception as e:
        logger.error(f"WebSocket error for {connection_id}: {e}")
    finally:
        connection_manager.disconnect(connection_id)

# -------------------------
# HTTP API for data distribution (called by main API server)
# -------------------------
@app.post("/api/broadcast")
async def broadcast_data(data: dict):
    """
    Broadcast data to clients via WebSocket.
    Called by the main API server to distribute data.
    
    Request body:
    {
        "channel": "markers|drawings|overlays|messages|alerts|...",
        "type": "marker_created|marker_updated|...",
        "data": { ... }
    }
    """
    channel = data.get("channel", "general")
    message_type = data.get("type", "update")
    message_data = data.get("data", {})
    
    # Construct broadcast message
    message = {
        "type": message_type,
        **message_data
    }
    
    # Broadcast to channel
    await connection_manager.broadcast_to_channel(channel, message)
    
    return {
        "status": "success",
        "message": f"Data broadcast to channel '{channel}'",
        "subscribers": len(connection_manager.subscriptions.get(channel, set()))
    }

# -------------------------
# Group management endpoints
# -------------------------
@app.get("/api/groups")
def list_groups():
    """
    List all active multicast groups (units) and their subscriber counts.

    Returns a JSON object where each key is a unit/group name and the value
    is the number of currently subscribed connections.

    Example response::

        {
          "alpha": 3,
          "bravo": 1
        }

    A group is created automatically when the first client joins it and
    removed automatically when the last client leaves.
    """
    return connection_manager.list_groups()


@app.get("/api/groups/{group_name}/members")
def get_group_members(group_name: str):
    """
    Return the number of connections currently subscribed to a unit group.

    Args:
        group_name: The unit/group name (without the ``unit:`` prefix).

    Returns::

        { "group": "alpha", "members": 3, "channel": "unit:alpha" }
    """
    channel = f"unit:{group_name}"
    count = len(connection_manager.subscriptions.get(channel, set()))
    return {
        "group": group_name,
        "channel": channel,
        "members": count
    }


@app.post("/api/groups/{group_name}/broadcast")
async def broadcast_to_group(group_name: str, data: dict):
    """
    Broadcast a message directly to all members of a unit group.

    This endpoint is for server-to-group messaging (e.g. from other services).
    Clients can also target groups by including ``target_units`` in WebSocket
    stream messages.

    Request body::

        {
          "type": "my_message_type",
          "data": { ... }
        }

    Path param:
        group_name: The unit/group name (e.g. "alpha").
    """
    channel = f"unit:{group_name}"
    message_type = data.get("type", "update")
    message_data = data.get("data", {})
    message = {
        "type": message_type,
        **message_data
    }
    await connection_manager.broadcast_to_channel(channel, message)
    return {
        "status": "success",
        "group": group_name,
        "channel": channel,
        "message": f"Message broadcast to group '{group_name}'",
        "subscribers": len(connection_manager.subscriptions.get(channel, set()))
    }

# -------------------------
# Status and health endpoints
# -------------------------
@app.get("/api/status")
def get_status():
    """Get data server status"""
    stats = connection_manager.get_stats()
    return {
        "status": "running",
        "server": "LPU5 Data Distribution Server",
        "version": "1.0.0",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        **stats
    }

@app.get("/api/health")
def health_check():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "timestamp": datetime.now(timezone.utc).isoformat()
    }

# -------------------------
# Shutdown handler
# -------------------------
shutdown_event = asyncio.Event()

def signal_handler(sig, frame):
    """Handle shutdown signals"""
    logger.info("Shutdown signal received, stopping data server...")
    shutdown_event.set()

# -------------------------
# Main entry point
# -------------------------
if __name__ == "__main__":
    # Register signal handlers
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    logger.info("="*60)
    logger.info("  LPU5 DATA DISTRIBUTION SERVER")
    logger.info("="*60)
    logger.info(f"  Starting on: {DATA_SERVER_HOST}:{DATA_SERVER_PORT}")
    logger.info(f"  WebSocket:   ws://{DATA_SERVER_HOST}:{DATA_SERVER_PORT}/ws")
    logger.info(f"  HTTP API:    http://{DATA_SERVER_HOST}:{DATA_SERVER_PORT}/api/")
    logger.info("="*60)
    
    # Run server
    try:
        uvicorn.run(
            app,
            host=DATA_SERVER_HOST,
            port=DATA_SERVER_PORT,
            log_level="info"
        )
    except KeyboardInterrupt:
        logger.info("Server stopped by user")
    except Exception as e:
        logger.error(f"Server error: {e}")
        sys.exit(1)
