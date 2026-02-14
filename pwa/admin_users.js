// admin_users.js - User management functionality for admin.html

// Get auth token from localStorage
function getAuthToken() {
    // First try the direct token storage (used by login)
    const directToken = localStorage.getItem('lpu5_token');
    if (directToken) {
        return directToken;
    }
    // Fallback to token in user object
    const user = JSON.parse(localStorage.getItem('lpu5_user') || '{}');
    return user.token || null;
}

// Get current user from localStorage
function getCurrentUser() {
    try {
        return JSON.parse(localStorage.getItem('lpu5_user') || '{}');
    } catch (e) {
        console.error('Failed to get current user:', e);
        return {};
    }
}

// Check if current user has admin or operator role (local check)
function hasAdminRole() {
    const user = getCurrentUser();
    const role = (user.role || '').toLowerCase();
    return role === 'admin' || role === 'operator';
}

// Check if current user has a specific permission
async function hasPermission(permission) {
    // First, check if user has admin role locally (fast check)
    if (hasAdminRole()) {
        return true;
    }
    
    const token = getAuthToken();
    if (!token) return false;
    
    try {
        const res = await fetch('/api/permissions/check', {
            method: 'POST',
            headers: {
                'Authorization': `Bearer ${token}`,
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({ permission })
        });
        
        if (res.ok) {
            const data = await res.json();
            return data.has_permission || false;
        }
    } catch (e) {
        console.error('Permission check failed:', e);
    }
    return false;
}

// Load and display all users in the table
async function loadUsers() {
    try {
        const res = await fetch('/api/users');
        if (!res.ok) {
            console.error('Failed to load users', res.status);
            updateUnitTable([]);
            return;
        }
        const users = await res.json();
        updateUnitTable(users);
    } catch (e) {
        console.error('loadUsers error', e);
        updateUnitTable([]);
    }
}

// Update the unit table with user data
async function updateUnitTable(users) {
    const tbody = document.getElementById('unitTable');
    if (!tbody) return;
    
    if (!users || users.length === 0) {
        tbody.innerHTML = '<tr><td colspan="6" class="empty-state"><i class="fas fa-inbox"></i> No users found</td></tr>';
        return;
    }
    
    // Check permissions for edit/delete actions
    const canUpdate = await hasPermission('users.update');
    const canDelete = await hasPermission('users.delete');
    
    tbody.innerHTML = '';
    users.forEach(user => {
        const tr = document.createElement('tr');
        
        // Name column
        const tdName = document.createElement('td');
        tdName.textContent = user.fullname || user.username || '-';
        tr.appendChild(tdName);
        
        // Callsign column
        const tdCallsign = document.createElement('td');
        tdCallsign.textContent = user.callsign || user.username || '-';
        tr.appendChild(tdCallsign);
        
        // Unit/Group column
        const tdUnit = document.createElement('td');
        tdUnit.textContent = user.unit || user.group || '-';
        tr.appendChild(tdUnit);
        
        // Account Status column (Active/Inactive)
        const tdStatus = document.createElement('td');
        const isActive = user.is_active !== false; // Default to true if not specified
        tdStatus.innerHTML = `<span style="color: ${isActive ? 'var(--accent-green)' : '#888'}">${isActive ? '✅ Active' : '❌ Inactive'}</span>`;
        tr.appendChild(tdStatus);
        
        // Type/Role column
        const tdType = document.createElement('td');
        const roleLabel = user.role || user.type || 'user';
        tdType.innerHTML = `<span class="role-badge role-${roleLabel}">${roleLabel}</span>`;
        tr.appendChild(tdType);
        
        // Actions column
        const tdActions = document.createElement('td');
        let actionsHTML = '';
        
        if (canUpdate) {
            actionsHTML += `
                <button class="btn-icon" onclick="openEditModal('${escapeAttr(user.username)}')" title="Edit">
                    <i class="fas fa-edit"></i>
                </button>
            `;
        }
        
        if (canDelete) {
            actionsHTML += `
                <button class="btn-icon" onclick="deleteUserFromTable('${escapeAttr(user.username)}')" title="Delete">
                    <i class="fas fa-trash"></i>
                </button>
            `;
        }
        
        if (!actionsHTML) {
            actionsHTML = '<span style="color: #666;">No actions</span>';
        }
        
        tdActions.innerHTML = actionsHTML;
        tr.appendChild(tdActions);
        
        tbody.appendChild(tr);
    });
}

// Helper to escape HTML attributes
function escapeAttr(str) {
    if (!str) return '';
    return String(str).replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

// Create a new user
async function createUser() {
    const username = document.getElementById('newUsername');
    const password = document.getElementById('newPassword');
    const role = document.getElementById('newRole');
    
    if (!username || !password) {
        alert('Input fields not found');
        return;
    }
    
    const usernameVal = username.value.trim();
    const passwordVal = password.value;
    const roleVal = role ? role.value : 'user';
    
    if (!usernameVal || !passwordVal) {
        alert('Username and password required');
        return;
    }
    
    // Check permission
    const canCreate = await hasPermission('users.create');
    if (!canCreate) {
        alert('No permission to create users');
        return;
    }
    
    try {
        const token = getAuthToken();
        const res = await fetch('/api/users/create', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'Authorization': token ? `Bearer ${token}` : ''
            },
            body: JSON.stringify({
                username: usernameVal,
                password: passwordVal,
                role: roleVal
            })
        });
        
        const body = await res.json().catch(() => null);
        
        if (!res.ok) {
            const msg = body && (body.message || body.detail) ? (body.message || body.detail) : `Status ${res.status}`;
            alert('User creation failed: ' + msg);
            return;
        }
        
        alert('User created successfully');
        username.value = '';
        password.value = '';
        if (role) role.value = 'user';
        
        // Reload user list
        await loadUsers();
    } catch (e) {
        console.error('createUser error', e);
        alert('Error creating user: ' + (e.message || e));
    }
}

// Delete user from table
async function deleteUserFromTable(username) {
    if (!confirm(`Really delete user "${username}"?`)) {
        return;
    }
    
    // Check permission
    const canDelete = await hasPermission('users.delete');
    if (!canDelete) {
        alert('No permission to delete users');
        return;
    }
    
    try {
        const token = getAuthToken();
        const res = await fetch(`/api/users/${encodeURIComponent(username)}`, {
            method: 'DELETE',
            headers: {
                'Authorization': token ? `Bearer ${token}` : ''
            }
        });
        
        if (!res.ok) {
            const body = await res.json().catch(() => null);
            const msg = body && (body.message || body.detail) ? (body.message || body.detail) : `Status ${res.status}`;
            alert('Deletion failed: ' + msg);
            return;
        }
        
        alert('User deleted successfully');
        
        // Reload user list
        await loadUsers();
    } catch (e) {
        console.error('deleteUserFromTable error', e);
        alert('Error deleting user: ' + (e.message || e));
    }
}

// Store current user being edited
let currentEditUser = null;

// Open edit modal with user data
async function openEditModal(username) {
    try {
        // Fetch user data
        const res = await fetch(`/api/users/${encodeURIComponent(username)}`);
        if (!res.ok) {
            alert('Could not load user');
            return;
        }
        
        const user = await res.json();
        currentEditUser = user;
        
        // Populate form fields
        document.getElementById('editUsername').value = user.username || '';
        document.getElementById('editFullname').value = user.fullname || '';
        document.getElementById('editCallsign').value = user.callsign || '';
        document.getElementById('editPassword').value = '';
        document.getElementById('editGroup').value = user.unit || user.group || '';
        document.getElementById('editStatus').value = user.is_active === false ? 'false' : 'true';
        document.getElementById('editRole').value = user.role || 'user';
        
        // Show modal
        const modal = document.getElementById('editModal');
        if (modal) modal.classList.add('active');
    } catch (e) {
        console.error('openEditModal error', e);
        alert('Error loading user: ' + (e.message || e));
    }
}

// Close edit modal
function closeEditModal() {
    const modal = document.getElementById('editModal');
    if (modal) modal.classList.remove('active');
    currentEditUser = null;
}

// Save user changes
async function saveUserChanges() {
    if (!currentEditUser) {
        alert('No user loaded');
        return;
    }
    
    const username = currentEditUser.username;
    const fullname = document.getElementById('editFullname').value.trim();
    const callsign = document.getElementById('editCallsign').value.trim();
    const password = document.getElementById('editPassword').value;
    const group = document.getElementById('editGroup').value.trim();
    const active = document.getElementById('editStatus').value === 'true';
    const role = document.getElementById('editRole').value;
    
    try {
        const payload = {
            fullname: fullname || undefined,
            callsign: callsign || undefined,
            unit: group || undefined,
            group: group || undefined,
            is_active: active,
            role: role
        };
        
        // Only include password if provided
        if (password) {
            payload.password = password;
        }
        
        const token = getAuthToken();
        const res = await fetch(`/api/users/${encodeURIComponent(username)}`, {
            method: 'PUT',
            headers: {
                'Content-Type': 'application/json',
                'Authorization': token ? `Bearer ${token}` : ''
            },
            body: JSON.stringify(payload)
        });
        
        if (!res.ok) {
            const body = await res.json().catch(() => null);
            const msg = body && (body.message || body.detail) ? (body.message || body.detail) : `Status ${res.status}`;
            alert('Save failed: ' + msg);
            return;
        }
        
        alert('User updated successfully');
        closeEditModal();
        
        // Reload user list
        await loadUsers();
    } catch (e) {
        console.error('saveUserChanges error', e);
        alert('Error saving: ' + (e.message || e));
    }
}

// Delete user from modal
async function deleteUserFromModal() {
    if (!currentEditUser) {
        alert('No user loaded');
        return;
    }
    
    const username = currentEditUser.username;
    if (!confirm(`Really delete user "${username}"?`)) {
        return;
    }
    
    try {
        const token = getAuthToken();
        const res = await fetch(`/api/users/${encodeURIComponent(username)}`, {
            method: 'DELETE',
            headers: {
                'Authorization': token ? `Bearer ${token}` : ''
            }
        });
        
        if (!res.ok) {
            const body = await res.json().catch(() => null);
            const msg = body && (body.message || body.detail) ? (body.message || body.detail) : `Status ${res.status}`;
            alert('Deletion failed: ' + msg);
            return;
        }
        
        alert('User deleted successfully');
        closeEditModal();
        
        // Reload user list
        await loadUsers();
    } catch (e) {
        console.error('deleteUser error', e);
        alert('Error deleting user: ' + (e.message || e));
    }
}

// Load users when page loads
document.addEventListener('DOMContentLoaded', () => {
    loadUsers();
});
