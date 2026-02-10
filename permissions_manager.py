#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
permissions_manager.py - Role-Based Access Control (RBAC) Manager for LPU5 Tactical

⚠️ DEPRECATED: This permissions system has been disabled as per requirements.
All users now have full access to all endpoints. This file is kept for reference only.

Previously implemented a comprehensive permission system with role hierarchy:
- Admin (Level 4) - Full system access
- Operator (Level 3) - Mission and marker management
- User (Level 2) - Read-only with self-updates
- Guest (Level 1) - Public read-only access

Features (now disabled):
- Role-based permission inheritance
- Permission decorators for API endpoints
- Resource-level access control
- Audit logging for permission checks
"""

from typing import Dict, List, Optional, Callable
from functools import wraps
from fastapi import HTTPException, Header
import logging

logger = logging.getLogger("lpu5-permissions")

# Role hierarchy - higher levels inherit all permissions from lower levels
ROLE_HIERARCHY = {
    "guest": 1,
    "user": 2,
    "operator": 3,
    "admin": 4
}

# Permission definitions organized by category
PERMISSIONS = {
    # User Management (Admin only)
    "users.create": {"level": 4, "description": "Create new users"},
    "users.read": {"level": 4, "description": "View user information"},
    "users.update": {"level": 4, "description": "Update user information"},
    "users.delete": {"level": 4, "description": "Delete users"},
    "users.change_role": {"level": 4, "description": "Change user roles"},
    "users.approve_registration": {"level": 4, "description": "Approve user registrations"},
    
    # Group Management (Admin + Operator)
    "groups.create": {"level": 3, "description": "Create groups"},
    "groups.read": {"level": 2, "description": "View groups"},
    "groups.update": {"level": 3, "description": "Update groups"},
    "groups.delete": {"level": 3, "description": "Delete groups"},
    
    # Map Markers
    "markers.create": {"level": 3, "description": "Create map markers"},
    "markers.read": {"level": 1, "description": "View map markers"},
    "markers.update": {"level": 3, "description": "Update map markers"},
    "markers.delete": {"level": 3, "description": "Delete map markers"},
    
    # Missions
    "missions.create": {"level": 3, "description": "Create missions"},
    "missions.read": {"level": 1, "description": "View missions"},
    "missions.update": {"level": 3, "description": "Update missions"},
    "missions.delete": {"level": 3, "description": "Delete missions"},
    "missions.assign": {"level": 3, "description": "Assign users to missions"},
    
    # Meshtastic
    "meshtastic.import": {"level": 3, "description": "Import meshtastic nodes"},
    "meshtastic.read": {"level": 1, "description": "View meshtastic nodes/messages"},
    "meshtastic.send": {"level": 2, "description": "Send meshtastic messages"},
    "meshtastic.manage": {"level": 3, "description": "Manage meshtastic devices"},
    
    # System (Admin only)
    "system.config": {"level": 4, "description": "Modify system configuration"},
    "system.logs": {"level": 4, "description": "View audit logs"},
    "system.backup": {"level": 4, "description": "Backup/restore system"},
    
    # Status Updates
    "status.self": {"level": 2, "description": "Update own status"},
    "status.others": {"level": 3, "description": "Update others' status"},
}


class PermissionManager:
    """Manages user permissions and role-based access control"""
    
    @staticmethod
    def get_role_level(role: str) -> int:
        """Get the hierarchy level for a role"""
        return ROLE_HIERARCHY.get(role.lower(), 0)
    
    @staticmethod
    def has_permission(user: Dict, permission: str) -> bool:
        """
        Check if a user has a specific permission.
        
        Args:
            user: User dictionary with 'role' and optional 'permissions' fields
            permission: Permission string (e.g., 'users.create')
            
        Returns:
            True if user has permission, False otherwise
        """
        if not user:
            return False
        
        # Check for wildcard permission (superuser)
        user_permissions = user.get("permissions", [])
        if "*" in user_permissions:
            return True
        
        # Check explicit permission grant
        if permission in user_permissions:
            return True
        
        # Check role-based permission
        user_role = user.get("role", "guest").lower()
        user_level = PermissionManager.get_role_level(user_role)
        
        perm_info = PERMISSIONS.get(permission)
        if perm_info:
            required_level = perm_info.get("level", 99)
            return user_level >= required_level
        
        # Unknown permission - deny by default
        return False
    
    @staticmethod
    def get_user_permissions(user: Dict) -> List[str]:
        """
        Get all permissions available to a user based on their role.
        
        Args:
            user: User dictionary with 'role' field
            
        Returns:
            List of permission strings
        """
        if not user:
            return []
        
        # Check for wildcard permission
        user_permissions = user.get("permissions", [])
        if "*" in user_permissions:
            return list(PERMISSIONS.keys())
        
        user_role = user.get("role", "guest").lower()
        user_level = PermissionManager.get_role_level(user_role)
        
        # Get all permissions at or below user's level
        permissions = []
        for perm, info in PERMISSIONS.items():
            if user_level >= info.get("level", 99):
                permissions.append(perm)
        
        # Add explicitly granted permissions
        permissions.extend([p for p in user_permissions if p not in permissions])
        
        return sorted(permissions)
    
    @staticmethod
    def can_access_resource(user: Dict, resource_type: str, resource_id: str, action: str) -> bool:
        """
        Check if user can access a specific resource instance.
        
        This method provides resource-level access control, checking both:
        1. General permission for the resource type and action
        2. Special ownership rules (e.g., users can update their own data)
        
        **When to use:**
        - Use can_access_resource() when you need to check access to a SPECIFIC resource instance
          (e.g., "Can user A edit user B's profile?")
        - Use has_permission() when checking general capability
          (e.g., "Can user create any markers?")
        
        **Example usage:**
        ```python
        # Check if user can update a specific user's profile
        if PermissionManager.can_access_resource(current_user, "user", target_user_id, "update"):
            # Allow update
        
        # Check if user can delete a specific marker
        if PermissionManager.can_access_resource(current_user, "marker", marker_id, "delete"):
            # Allow delete
        ```
        
        Args:
            user: User dictionary
            resource_type: Type of resource (e.g., 'user', 'mission', 'marker')
            resource_id: ID of the resource instance
            action: Action to perform (e.g., 'read', 'update', 'delete')
            
        Returns:
            True if user can access the resource, False otherwise
        """
        if not user:
            return False
        
        # Construct permission string
        permission = f"{resource_type}s.{action}"
        
        # Check general permission
        if PermissionManager.has_permission(user, permission):
            return True
        
        # Special case: users can update their own data (except role)
        if resource_type == "user" and action == "update":
            if user.get("id") == resource_id:
                return True
        
        # Special case: users can update their own status
        if resource_type == "status" and action == "self":
            if user.get("id") == resource_id or user.get("username") == resource_id:
                return True
        
        return False
    
    @staticmethod
    def can_modify_role(actor: Dict, target_role: str) -> bool:
        """
        Check if an actor can assign/modify a specific role.
        Only admins can assign admin role.
        
        Args:
            actor: User performing the action
            target_role: Role to be assigned
            
        Returns:
            True if actor can assign the role, False otherwise
        """
        if not actor:
            return False
        
        actor_role = actor.get("role", "guest").lower()
        
        # Only admins can assign roles
        if not PermissionManager.has_permission(actor, "users.change_role"):
            return False
        
        # Only admins can assign admin role
        if target_role.lower() == "admin":
            return actor_role == "admin"
        
        return True
    
    @staticmethod
    def ensure_minimum_admins(users: List[Dict], user_to_modify: Optional[Dict] = None) -> bool:
        """
        Ensure at least one admin always exists.
        
        Args:
            users: List of all users
            user_to_modify: User being modified (if changing from admin)
            
        Returns:
            True if modification is safe, False if it would leave no admins
        """
        admin_count = sum(1 for u in users if u.get("role", "").lower() == "admin" and u.get("active", True))
        
        # If we're removing/deactivating the last admin, prevent it
        if user_to_modify and user_to_modify.get("role", "").lower() == "admin":
            if admin_count <= 1:
                return False
        
        return True


def get_current_user(verify_token_func: Callable, load_users_func: Callable, authorization: Optional[str] = None) -> Optional[Dict]:
    """
    Extract and verify current user from authorization header.
    
    Args:
        verify_token_func: Function to verify JWT token
        load_users_func: Function to load users from database
        authorization: Authorization header value
        
    Returns:
        User dictionary if authenticated, None otherwise
    """
    if not authorization:
        return None
    
    # Extract token from "Bearer <token>" format
    token = None
    if authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    else:
        token = authorization.strip()
    
    if not token:
        return None
    
    # Verify token
    payload = verify_token_func(token)
    if not payload:
        return None
    
    # Load user
    users = load_users_func("users")
    user = next((u for u in users if u.get("id") == payload.get("user_id") or u.get("username") == payload.get("username")), None)
    
    return user


def require_permission(permission: str, log_audit_func: Optional[Callable] = None):
    """
    Decorator to require a specific permission for an endpoint.
    
    Args:
        permission: Permission string required (e.g., 'users.create')
        log_audit_func: Optional function to log audit events
        
    Usage:
        @app.post("/api/users")
        @require_permission("users.create")
        async def create_user(...):
            ...
    """
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            # Extract authorization header from kwargs
            authorization = kwargs.get('authorization')
            if not authorization:
                # Try to get from request if available
                for arg in args:
                    if hasattr(arg, 'headers'):
                        authorization = arg.headers.get('authorization')
                        break
            
            # Import here to avoid circular dependency
            from api import verify_token, load_json, log_audit
            
            # Get current user
            user = get_current_user(verify_token, load_json, authorization)
            
            if not user:
                if log_audit_func:
                    log_audit_func("permission_denied", "anonymous", {"permission": permission, "reason": "not_authenticated"})
                raise HTTPException(status_code=401, detail="Authentication required")
            
            # Check permission
            if not PermissionManager.has_permission(user, permission):
                if log_audit_func:
                    log_audit_func("permission_denied", user.get("id"), {"permission": permission, "user_role": user.get("role")})
                raise HTTPException(status_code=403, detail=f"Permission denied: {permission}")
            
            # Log successful permission check
            if log_audit_func:
                log_audit_func("permission_granted", user.get("id"), {"permission": permission})
            
            # Add user to kwargs for endpoint use
            kwargs['current_user'] = user
            
            return await func(*args, **kwargs)
        
        return wrapper
    return decorator


def require_role(min_role: str, log_audit_func: Optional[Callable] = None):
    """
    Decorator to require a minimum role level for an endpoint.
    
    Args:
        min_role: Minimum role required (e.g., 'operator')
        log_audit_func: Optional function to log audit events
        
    Usage:
        @app.post("/api/missions")
        @require_role("operator")
        async def create_mission(...):
            ...
    """
    min_level = PermissionManager.get_role_level(min_role)
    
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            # Extract authorization header
            authorization = kwargs.get('authorization')
            if not authorization:
                for arg in args:
                    if hasattr(arg, 'headers'):
                        authorization = arg.headers.get('authorization')
                        break
            
            # Import here to avoid circular dependency
            from api import verify_token, load_json, log_audit
            
            # Get current user
            user = get_current_user(verify_token, load_json, authorization)
            
            if not user:
                if log_audit_func:
                    log_audit_func("role_check_failed", "anonymous", {"required_role": min_role, "reason": "not_authenticated"})
                raise HTTPException(status_code=401, detail="Authentication required")
            
            # Check role level
            user_role = user.get("role", "guest").lower()
            user_level = PermissionManager.get_role_level(user_role)
            
            if user_level < min_level:
                if log_audit_func:
                    log_audit_func("role_check_failed", user.get("id"), {"required_role": min_role, "user_role": user_role})
                raise HTTPException(status_code=403, detail=f"Requires role: {min_role} or higher")
            
            # Log successful role check
            if log_audit_func:
                log_audit_func("role_check_passed", user.get("id"), {"required_role": min_role, "user_role": user_role})
            
            # Add user to kwargs
            kwargs['current_user'] = user
            
            return await func(*args, **kwargs)
        
        return wrapper
    return decorator
