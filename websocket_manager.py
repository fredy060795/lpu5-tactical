#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
websocket_manager.py - WebSocket Manager for Real-time Updates

Implements WebSocket support for real-time tactical updates:
- Position updates
- Status changes
- Messages
- Alerts
- CoT events
"""

import json
import logging
from typing import Dict, List, Set, Optional, Any
from datetime import datetime, timezone
import asyncio
from fastapi import WebSocket, WebSocketDisconnect

logger = logging.getLogger("lpu5-websocket")


class ConnectionManager:
    """Manages WebSocket connections with health monitoring and error recovery"""
    
    def __init__(self):
        """Initialize connection manager"""
        self.active_connections: Dict[str, WebSocket] = {}
        self.subscriptions: Dict[str, Set[str]] = {}  # channel -> set of connection_ids
        self.user_connections: Dict[str, str] = {}  # user_id -> connection_id
        self.connection_metadata: Dict[str, Dict[str, Any]] = {}  # connection_id -> metadata
        self.failed_send_attempts: Dict[str, int] = {}  # connection_id -> failed count
        self.max_failed_attempts = 3  # Max failed sends before disconnect
        
    async def connect(self, websocket: WebSocket, connection_id: str, user_id: Optional[str] = None):
        """
        Accept and register a new WebSocket connection
        
        Args:
            websocket: WebSocket connection
            connection_id: Unique connection identifier
            user_id: Optional user identifier
        """
        await websocket.accept()
        self.active_connections[connection_id] = websocket
        
        # Initialize connection metadata
        self.connection_metadata[connection_id] = {
            "connected_at": datetime.now(timezone.utc).isoformat(),
            "user_id": user_id,
            "last_activity": datetime.now(timezone.utc).isoformat(),
            "messages_sent": 0,
            "messages_received": 0
        }
        self.failed_send_attempts[connection_id] = 0
        
        if user_id:
            self.user_connections[user_id] = connection_id
        
        logger.info(f"WebSocket connected: {connection_id} (user: {user_id})")
        
        # Send welcome message
        await self.send_personal_message(connection_id, {
            "type": "connection_established",
            "connection_id": connection_id,
            "timestamp": datetime.now(timezone.utc).isoformat()
        })
    
    def disconnect(self, connection_id: str):
        """
        Disconnect and cleanup a WebSocket connection
        
        Args:
            connection_id: Connection identifier to remove
        """
        # Remove from active connections
        if connection_id in self.active_connections:
            del self.active_connections[connection_id]
        
        # Remove connection metadata
        if connection_id in self.connection_metadata:
            metadata = self.connection_metadata[connection_id]
            logger.info(f"WebSocket stats for {connection_id}: sent={metadata.get('messages_sent', 0)}, received={metadata.get('messages_received', 0)}")
            del self.connection_metadata[connection_id]
        
        # Remove failed send tracking
        if connection_id in self.failed_send_attempts:
            del self.failed_send_attempts[connection_id]
        
        # Remove from all subscriptions
        for channel in list(self.subscriptions.keys()):
            if connection_id in self.subscriptions[channel]:
                self.subscriptions[channel].remove(connection_id)
                if not self.subscriptions[channel]:
                    del self.subscriptions[channel]
        
        # Remove user mapping
        user_to_remove = None
        for user_id, conn_id in self.user_connections.items():
            if conn_id == connection_id:
                user_to_remove = user_id
                break
        if user_to_remove:
            del self.user_connections[user_to_remove]
        
        logger.info(f"WebSocket disconnected: {connection_id}")
    
    async def send_personal_message(self, connection_id: str, message: Dict[str, Any]):
        """
        Send a message to a specific connection with error handling
        
        Args:
            connection_id: Target connection ID
            message: Message dictionary to send
        """
        if connection_id not in self.active_connections:
            logger.warning(f"Attempted to send to non-existent connection: {connection_id}")
            return
        
        websocket = self.active_connections[connection_id]
        
        # Check WebSocket state before sending
        try:
            if hasattr(websocket, 'client_state'):
                from starlette.websockets import WebSocketState
                if websocket.client_state != WebSocketState.CONNECTED:
                    logger.warning(f"WebSocket {connection_id} not in CONNECTED state, disconnecting")
                    self.disconnect(connection_id)
                    return
        except Exception as e:
            logger.debug(f"Could not check WebSocket state for {connection_id}: {e}")
        
        try:
            await websocket.send_json(message)
            
            # Update metadata on successful send
            if connection_id in self.connection_metadata:
                self.connection_metadata[connection_id]["messages_sent"] += 1
                self.connection_metadata[connection_id]["last_activity"] = datetime.now(timezone.utc).isoformat()
            
            # Reset failed attempts counter on success
            self.failed_send_attempts[connection_id] = 0
            
        except RuntimeError as e:
            # RuntimeError is raised when WebSocket is closed
            error_msg = str(e) if str(e) else "WebSocket connection closed"
            logger.error(f"Failed to send message to {connection_id}: {error_msg}")
            
            # Track failed attempts
            self.failed_send_attempts[connection_id] = self.failed_send_attempts.get(connection_id, 0) + 1
            
            # Disconnect immediately on RuntimeError (connection is dead)
            logger.warning(f"Connection {connection_id} is dead (RuntimeError), disconnecting immediately")
            self.disconnect(connection_id)
            
        except Exception as e:
            # Catch all other exceptions
            error_msg = str(e) if str(e) else f"{type(e).__name__}"
            logger.error(f"Failed to send message to {connection_id}: {error_msg}")
            
            # Track failed attempts
            self.failed_send_attempts[connection_id] = self.failed_send_attempts.get(connection_id, 0) + 1
            
            # Disconnect if too many failures
            if self.failed_send_attempts[connection_id] >= self.max_failed_attempts:
                logger.warning(f"Connection {connection_id} exceeded max failed attempts, disconnecting")
                self.disconnect(connection_id)
    
    async def send_to_user(self, user_id: str, message: Dict[str, Any]):
        """
        Send a message to a specific user
        
        Args:
            user_id: Target user ID
            message: Message dictionary to send
        """
        connection_id = self.user_connections.get(user_id)
        if connection_id:
            await self.send_personal_message(connection_id, message)
    
    async def broadcast(self, message: Dict[str, Any], exclude: Optional[List[str]] = None):
        """
        Broadcast a message to all connected clients
        
        Args:
            message: Message dictionary to broadcast
            exclude: Optional list of connection IDs to exclude
        """
        exclude = exclude or []
        
        # Add timestamp if not present
        if "timestamp" not in message:
            message["timestamp"] = datetime.now(timezone.utc).isoformat()
        
        disconnected = []
        for connection_id, websocket in self.active_connections.items():
            if connection_id in exclude:
                continue
            
            # Check WebSocket state before sending
            try:
                if hasattr(websocket, 'client_state'):
                    from starlette.websockets import WebSocketState
                    if websocket.client_state != WebSocketState.CONNECTED:
                        logger.warning(f"WebSocket {connection_id} not in CONNECTED state during broadcast, marking for cleanup")
                        disconnected.append(connection_id)
                        continue
                
                await websocket.send_json(message)
            except RuntimeError as e:
                error_msg = str(e) if str(e) else "WebSocket connection closed"
                logger.error(f"Failed to broadcast to {connection_id}: {error_msg}")
                disconnected.append(connection_id)
            except Exception as e:
                error_msg = str(e) if str(e) else f"{type(e).__name__}"
                logger.error(f"Failed to broadcast to {connection_id}: {error_msg}")
                disconnected.append(connection_id)
        
        # Cleanup disconnected clients
        for connection_id in disconnected:
            self.disconnect(connection_id)
    
    def subscribe(self, connection_id: str, channel: str):
        """
        Subscribe a connection to a channel
        
        Args:
            connection_id: Connection to subscribe
            channel: Channel name
        """
        if channel not in self.subscriptions:
            self.subscriptions[channel] = set()
        
        self.subscriptions[channel].add(connection_id)
        logger.info(f"Connection {connection_id} subscribed to {channel}")
    
    def unsubscribe(self, connection_id: str, channel: str):
        """
        Unsubscribe a connection from a channel
        
        Args:
            connection_id: Connection to unsubscribe
            channel: Channel name
        """
        if channel in self.subscriptions:
            self.subscriptions[channel].discard(connection_id)
            if not self.subscriptions[channel]:
                del self.subscriptions[channel]
        
        logger.info(f"Connection {connection_id} unsubscribed from {channel}")
    
    async def publish_to_channel(self, channel: str, message: Dict[str, Any]):
        """
        Publish a message to all subscribers of a channel
        
        Args:
            channel: Channel name
            message: Message dictionary to publish
        """
        if channel not in self.subscriptions:
            return
        
        # Add timestamp if not present
        if "timestamp" not in message:
            message["timestamp"] = datetime.now(timezone.utc).isoformat()
        
        # Add channel to message
        message["channel"] = channel
        
        disconnected = []
        for connection_id in self.subscriptions[channel]:
            if connection_id not in self.active_connections:
                disconnected.append(connection_id)
                continue
            
            websocket = self.active_connections[connection_id]
            
            # Check WebSocket state before sending
            try:
                # Check if websocket has client_state attribute (Starlette WebSocket)
                if hasattr(websocket, 'client_state'):
                    from starlette.websockets import WebSocketState
                    if websocket.client_state != WebSocketState.CONNECTED:
                        logger.warning(f"WebSocket {connection_id} not in CONNECTED state, marking for cleanup")
                        disconnected.append(connection_id)
                        continue
                
                await websocket.send_json(message)
            except RuntimeError as e:
                # RuntimeError is raised when WebSocket is closed
                error_msg = str(e) if str(e) else "WebSocket connection closed"
                logger.error(f"Failed to publish to {connection_id}: {error_msg}")
                disconnected.append(connection_id)
            except Exception as e:
                # Catch all other exceptions
                error_msg = str(e) if str(e) else f"{type(e).__name__}"
                logger.error(f"Failed to publish to {connection_id}: {error_msg}")
                disconnected.append(connection_id)
        
        # Cleanup disconnected clients
        for connection_id in disconnected:
            if connection_id in self.subscriptions[channel]:
                self.subscriptions[channel].remove(connection_id)
    
    def get_connection_count(self) -> int:
        """Get number of active connections"""
        return len(self.active_connections)
    
    def get_channel_subscribers(self, channel: str) -> int:
        """Get number of subscribers to a channel"""
        return len(self.subscriptions.get(channel, set()))
    
    def list_channels(self) -> List[str]:
        """List all active channels"""
        return list(self.subscriptions.keys())
    
    def get_user_status(self, user_id: str) -> Dict[str, Any]:
        """
        Get connection status for a user
        
        Args:
            user_id: User ID to check
            
        Returns:
            Status dictionary
        """
        connection_id = self.user_connections.get(user_id)
        return {
            "user_id": user_id,
            "connected": connection_id is not None,
            "connection_id": connection_id
        }
    
    def update_connection_activity(self, connection_id: str):
        """
        Update last activity timestamp for a connection
        
        Args:
            connection_id: Connection to update
        """
        if connection_id in self.connection_metadata:
            self.connection_metadata[connection_id]["last_activity"] = datetime.now(timezone.utc).isoformat()
            self.connection_metadata[connection_id]["messages_received"] += 1
    
    def get_connection_health(self, connection_id: str) -> Dict[str, Any]:
        """
        Get health status of a connection
        
        Args:
            connection_id: Connection to check
            
        Returns:
            Health status dictionary
        """
        if connection_id not in self.active_connections:
            return {"healthy": False, "reason": "Connection not found"}
        
        metadata = self.connection_metadata.get(connection_id, {})
        failed_attempts = self.failed_send_attempts.get(connection_id, 0)
        
        # Calculate connection duration
        connected_at_str = metadata.get("connected_at")
        if connected_at_str:
            try:
                connected_at = datetime.fromisoformat(connected_at_str)
                duration_seconds = (datetime.now(timezone.utc) - connected_at).total_seconds()
            except Exception:
                duration_seconds = 0
        else:
            duration_seconds = 0
        
        healthy = failed_attempts < self.max_failed_attempts
        
        return {
            "healthy": healthy,
            "connection_id": connection_id,
            "failed_attempts": failed_attempts,
            "max_failed_attempts": self.max_failed_attempts,
            "duration_seconds": duration_seconds,
            "messages_sent": metadata.get("messages_sent", 0),
            "messages_received": metadata.get("messages_received", 0),
            "last_activity": metadata.get("last_activity"),
            "subscribed_channels": [ch for ch, subs in self.subscriptions.items() if connection_id in subs]
        }
    
    def get_all_connection_stats(self) -> Dict[str, Any]:
        """
        Get statistics for all connections
        
        Returns:
            Dictionary with overall connection statistics
        """
        total_connections = len(self.active_connections)
        total_channels = len(self.subscriptions)
        
        # Calculate total messages
        total_sent = sum(meta.get("messages_sent", 0) for meta in self.connection_metadata.values())
        total_received = sum(meta.get("messages_received", 0) for meta in self.connection_metadata.values())
        
        # Count unhealthy connections
        unhealthy = sum(1 for conn_id in self.active_connections 
                       if self.failed_send_attempts.get(conn_id, 0) > 0)
        
        return {
            "total_connections": total_connections,
            "total_channels": total_channels,
            "total_messages_sent": total_sent,
            "total_messages_received": total_received,
            "unhealthy_connections": unhealthy,
            "healthy_connections": total_connections - unhealthy
        }


# Predefined channel names for standardization
class Channels:
    """Standard channel names"""
    POSITIONS = "positions"          # Position updates
    STATUS = "status"                # Status changes
    MESSAGES = "messages"            # Chat messages
    ALERTS = "alerts"                # Alerts and warnings
    COT = "cot"                      # CoT events
    GEOFENCE = "geofence"           # Geofence events
    MISSIONS = "missions"            # Mission updates
    SYSTEM = "system"                # System notifications
    MARKERS = "markers"              # Map marker updates
    OVERLAYS = "overlays"            # Overlay updates
    DRAWINGS = "drawings"            # Drawing updates
    SYMBOLS = "symbols"              # Symbol updates
    CAMERA = "camera"                # Camera stream updates


class WebSocketEventHandler:
    """Handles WebSocket events and message routing"""
    
    def __init__(self, connection_manager: ConnectionManager):
        """
        Initialize event handler
        
        Args:
            connection_manager: ConnectionManager instance
        """
        self.manager = connection_manager
        self.message_handlers = {}
        self._register_default_handlers()
    
    def register_handler(self, message_type: str, handler):
        """
        Register a handler for a message type
        
        Args:
            message_type: Type of message
            handler: Async function to handle the message
        """
        self.message_handlers[message_type] = handler
        logger.info(f"Registered WebSocket handler: {message_type}")
    
    async def handle_message(self, connection_id: str, message: Dict[str, Any]):
        """
        Handle an incoming WebSocket message
        
        Args:
            connection_id: Connection that sent the message
            message: Message dictionary
        """
        message_type = message.get("type")
        
        if not message_type:
            await self.manager.send_personal_message(connection_id, {
                "type": "error",
                "error": "Missing message type"
            })
            return
        
        if message_type not in self.message_handlers:
            await self.manager.send_personal_message(connection_id, {
                "type": "error",
                "error": f"Unknown message type: {message_type}"
            })
            return
        
        handler = self.message_handlers[message_type]
        try:
            await handler(connection_id, message)
        except Exception as e:
            logger.error(f"Handler error for {message_type}: {e}")
            await self.manager.send_personal_message(connection_id, {
                "type": "error",
                "error": f"Handler error: {str(e)}"
            })
    
    def _register_default_handlers(self):
        """Register default message handlers"""
        
        # Subscribe to channel
        async def handle_subscribe(connection_id: str, message: Dict):
            channel = message.get("channel")
            if not channel:
                await self.manager.send_personal_message(connection_id, {
                    "type": "error",
                    "error": "Missing channel name"
                })
                return
            
            self.manager.subscribe(connection_id, channel)
            await self.manager.send_personal_message(connection_id, {
                "type": "subscribed",
                "channel": channel
            })
        
        self.register_handler("subscribe", handle_subscribe)
        
        # Unsubscribe from channel
        async def handle_unsubscribe(connection_id: str, message: Dict):
            channel = message.get("channel")
            if not channel:
                await self.manager.send_personal_message(connection_id, {
                    "type": "error",
                    "error": "Missing channel name"
                })
                return
            
            self.manager.unsubscribe(connection_id, channel)
            await self.manager.send_personal_message(connection_id, {
                "type": "unsubscribed",
                "channel": channel
            })
        
        self.register_handler("unsubscribe", handle_unsubscribe)
        
        # Ping/pong for keepalive
        async def handle_ping(connection_id: str, message: Dict):
            await self.manager.send_personal_message(connection_id, {
                "type": "pong",
                "timestamp": datetime.now(timezone.utc).isoformat()
            })
        
        self.register_handler("ping", handle_ping)
