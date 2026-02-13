/**
 * permissions.js - Frontend Permission Helper for LPU5 Tactical RBAC
 * 
 * Provides client-side permission checking and UI visibility management.
 * Note: Frontend checks are for UX only - security is enforced on backend.
 */

// Cache for user permissions
let _userPermissionsCache = null;
let _currentUserCache = null;

/**
 * Get the current user's token from localStorage
 * @returns {string|null} JWT token
 */
function getAuthToken() {
    const user = JSON.parse(localStorage.getItem('lpu5_user') || '{}');
    return user.token || null;
}

/**
 * Get current user info from localStorage
 * @returns {Object|null} User object
 */
function getCurrentUser() {
    if (_currentUserCache) {
        return _currentUserCache;
    }
    
    const user = JSON.parse(localStorage.getItem('lpu5_user') || '{}');
    if (user && user.username) {
        _currentUserCache = user;
        return user;
    }
    return null;
}

/**
 * Fetch user permissions from the API
 * @returns {Promise<Array<string>>} Array of permission strings
 */
async function fetchUserPermissions() {
    const token = getAuthToken();
    if (!token) {
        return [];
    }
    
    try {
        const response = await fetch('/api/permissions/user', {
            method: 'GET',
            headers: {
                'Authorization': `Bearer ${token}`,
                'Content-Type': 'application/json'
            }
        });
        
        if (response.ok) {
            const data = await response.json();
            _userPermissionsCache = data.permissions || [];
            return _userPermissionsCache;
        }
    } catch (e) {
        console.error('Failed to fetch user permissions:', e);
    }
    
    return [];
}

/**
 * Check if the current user has a specific permission
 * @param {string} permission - Permission to check (e.g., 'users.create')
 * @returns {Promise<boolean>} True if user has permission
 */
async function hasPermission(permission) {
    // Get permissions from cache or fetch
    if (!_userPermissionsCache) {
        await fetchUserPermissions();
    }
    
    // Check for wildcard permission
    if (_userPermissionsCache && _userPermissionsCache.includes('*')) {
        return true;
    }
    
    // Check for specific permission
    return _userPermissionsCache && _userPermissionsCache.includes(permission);
}

/**
 * Check if the current user has a specific permission (via API call)
 * Use this when you need real-time verification
 * @param {string} permission - Permission to check
 * @returns {Promise<boolean>} True if user has permission
 */
async function checkPermissionAPI(permission) {
    const token = getAuthToken();
    if (!token) {
        return false;
    }
    
    try {
        const response = await fetch('/api/permissions/check', {
            method: 'POST',
            headers: {
                'Authorization': `Bearer ${token}`,
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({ permission })
        });
        
        if (response.ok) {
            const data = await response.json();
            return data.has_permission || false;
        }
    } catch (e) {
        console.error('Failed to check permission:', e);
    }
    
    return false;
}

/**
 * Get the current user's role
 * @returns {string} Role name (admin, operator, user, guest)
 */
function getUserRole() {
    const user = getCurrentUser();
    return user?.role || 'guest';
}

/**
 * Check if user has minimum role level
 * @param {string} minRole - Minimum role required (guest, user, operator, admin)
 * @returns {boolean} True if user has required role level or higher
 */
function hasMinRole(minRole) {
    const roleHierarchy = {
        'guest': 1,
        'user': 2,
        'operator': 3,
        'admin': 4
    };
    
    const userRole = getUserRole();
    const userLevel = roleHierarchy[userRole] || 0;
    const requiredLevel = roleHierarchy[minRole] || 0;
    
    return userLevel >= requiredLevel;
}

/**
 * Show or hide an element based on permission
 * @param {string|HTMLElement} elementOrSelector - Element or CSS selector
 * @param {string} permission - Permission required to show element
 * @param {boolean} hide - If true, hide when permission is missing; if false, disable
 */
async function showIfPermission(elementOrSelector, permission, hide = true) {
    const element = typeof elementOrSelector === 'string' 
        ? document.querySelector(elementOrSelector) 
        : elementOrSelector;
    
    if (!element) return;
    
    const hasPerm = await hasPermission(permission);
    
    if (hasPerm) {
        element.style.display = '';
        element.disabled = false;
    } else {
        if (hide) {
            element.style.display = 'none';
        } else {
            element.disabled = true;
        }
    }
}

/**
 * Show or hide an element based on role
 * @param {string|HTMLElement} elementOrSelector - Element or CSS selector
 * @param {string} minRole - Minimum role required
 * @param {boolean} hide - If true, hide when role insufficient; if false, disable
 */
function showIfRole(elementOrSelector, minRole, hide = true) {
    const element = typeof elementOrSelector === 'string' 
        ? document.querySelector(elementOrSelector) 
        : elementOrSelector;
    
    if (!element) return;
    
    const hasRole = hasMinRole(minRole);
    
    if (hasRole) {
        element.style.display = '';
        element.disabled = false;
    } else {
        if (hide) {
            element.style.display = 'none';
        } else {
            element.disabled = true;
        }
    }
}

/**
 * Initialize permissions on page load
 * Fetches and caches user permissions
 */
async function initializePermissions() {
    await fetchUserPermissions();
    console.log('Permissions initialized:', _userPermissionsCache);
}

/**
 * Clear permissions cache (e.g., after logout)
 */
function clearPermissionsCache() {
    _userPermissionsCache = null;
    _currentUserCache = null;
}

/**
 * Check multiple permissions at once
 * @param {Array<string>} permissions - Array of permissions to check
 * @returns {Promise<Object>} Object with permission names as keys and boolean values
 */
async function checkMultiplePermissions(permissions) {
    if (!_userPermissionsCache) {
        await fetchUserPermissions();
    }
    
    const results = {};
    for (const perm of permissions) {
        results[perm] = await hasPermission(perm);
    }
    return results;
}

/**
 * Apply permission-based visibility to multiple elements
 * Elements should have data-permission or data-role attributes
 * 
 * Example usage:
 *   <button data-permission="users.create">Create User</button>
 *   <button data-role="operator">Create Mission</button>
 */
async function applyPermissionAttributes() {
    // Handle data-permission attributes
    const permissionElements = document.querySelectorAll('[data-permission]');
    for (const element of permissionElements) {
        const permission = element.getAttribute('data-permission');
        const hideMode = element.getAttribute('data-permission-hide') !== 'false';
        await showIfPermission(element, permission, hideMode);
    }
    
    // Handle data-role attributes
    const roleElements = document.querySelectorAll('[data-role]');
    for (const element of roleElements) {
        const role = element.getAttribute('data-role');
        const hideMode = element.getAttribute('data-role-hide') !== 'false';
        showIfRole(element, role, hideMode);
    }
}

// Auto-initialize on page load if user is logged in
if (typeof window !== 'undefined') {
    window.addEventListener('DOMContentLoaded', () => {
        const user = getCurrentUser();
        if (user && user.token) {
            initializePermissions().then(() => {
                applyPermissionAttributes();
            });
        }
    });
}

// Export functions for use in other scripts
if (typeof module !== 'undefined' && module.exports) {
    module.exports = {
        hasPermission,
        checkPermissionAPI,
        getUserRole,
        hasMinRole,
        showIfPermission,
        showIfRole,
        initializePermissions,
        clearPermissionsCache,
        checkMultiplePermissions,
        applyPermissionAttributes,
        getCurrentUser,
        getAuthToken,
        fetchUserPermissions
    };
}
