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
    """Manages WebSocket connections for data distribution"""
    
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
    
    async def broadcast_to_channel(self, channel: str, message: dict):
        """Broadcast a message to all subscribers of a channel"""
        if channel not in self.subscriptions:
            return
        
        subscribers = list(self.subscriptions[channel])
        logger.debug(f"Broadcasting to channel '{channel}': {len(subscribers)} subscribers")
        
        # Add metadata to message
        message["channel"] = channel
        if "timestamp" not in message:
            message["timestamp"] = datetime.now(timezone.utc).isoformat()
        
        # Send to all subscribers
        for connection_id in subscribers:
            await self.send_to_connection(connection_id, message)
    
    async def broadcast_to_all(self, message: dict):
        """Broadcast a message to all connected clients"""
        logger.debug(f"Broadcasting to all: {len(self.active_connections)} connections")
        
        if "timestamp" not in message:
            message["timestamp"] = datetime.now(timezone.utc).isoformat()
        
        for connection_id in list(self.active_connections.keys()):
            await self.send_to_connection(connection_id, message)
    
    def get_stats(self) -> dict:
        """Get connection statistics"""
        return {
            "active_connections": len(self.active_connections),
            "channels": {
                channel: len(subscribers)
                for channel, subscribers in self.subscriptions.items()
            },
            "total_subscriptions": sum(len(s) for s in self.subscriptions.values())
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
    WebSocket endpoint for real-time data distribution.
    Clients connect here to receive real-time updates.
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
                
                # Handle subscription requests
                if message_type == "subscribe":
                    channel = data.get("channel")
                    if channel:
                        await connection_manager.subscribe(connection_id, channel)
                
                # Handle unsubscription requests
                elif message_type == "unsubscribe":
                    channel = data.get("channel")
                    if channel:
                        await connection_manager.unsubscribe(connection_id, channel)
                
                # Handle ping
                elif message_type == "ping":
                    await connection_manager.send_to_connection(connection_id, {
                        "type": "pong",
                        "timestamp": datetime.now(timezone.utc).isoformat()
                    })
                
                # Relay camera frames (server-side relay)
                elif message_type == "camera_frame":
                    await connection_manager.broadcast_to_channel("camera", {
                        "type": "camera_frame",
                        "frame": data.get("frame"),
                        "source_connection": connection_id
                    })
                
                # Relay stream sharing
                elif message_type == "stream_share":
                    await connection_manager.broadcast_to_channel("camera", {
                        "type": "stream_share",
                        "streamId": data.get("streamId", "camera_main"),
                        "active": data.get("active", False),
                        "isCamera": data.get("isCamera", False),
                        "source_connection": connection_id
                    })
                
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
