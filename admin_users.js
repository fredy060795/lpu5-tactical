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

// Fetch all chat channels from API
async function fetchChatChannels() {
    try {
        const token = getAuthToken();
        const res = await fetch('/api/chat/channels', {
            headers: token ? { 'Authorization': `Bearer ${token}` } : {}
        });
        if (!res.ok) return [];
        const data = await res.json();
        return (data && data.channels) ? data.channels : [];
    } catch (e) {
        console.error('fetchChatChannels error', e);
        return [];
    }
}

// Populate chat channel checkboxes in the edit modal
async function populateChatChannelCheckboxes(container, selectedChannels) {
    // Admin/operator roles bypass channel filtering on the server side, so all channels are returned
    const token = getAuthToken();
    let channels = [];
    try {
        const res = await fetch('/api/chat/channels', {
            headers: token ? { 'Authorization': `Bearer ${token}` } : {}
        });
        if (res.ok) {
            const data = await res.json();
            channels = (data && data.channels) ? data.channels : [];
        }
    } catch (e) {
        console.error('populateChatChannelCheckboxes error', e);
    }

    container.innerHTML = '';
    if (channels.length === 0) {
        container.innerHTML = '<span style="color:#666;font-size:0.85em;">No channels available</span>';
        return;
    }

    channels.forEach(ch => {
        const isChecked = ch.id === 'all' || (selectedChannels && selectedChannels.includes(ch.id));
        const label = document.createElement('label');
        label.style.cssText = 'display:inline-flex;align-items:center;gap:5px;background:#1a1a1a;border:1px solid #333;border-radius:5px;padding:4px 8px;cursor:pointer;font-size:0.85em;';
        const cb = document.createElement('input');
        cb.type = 'checkbox';
        cb.value = ch.id;
        cb.checked = isChecked;
        if (ch.id === 'all') cb.disabled = true; // "all" is always required
        cb.style.accentColor = ch.color || '#28a745';
        label.appendChild(cb);
        const span = document.createElement('span');
        span.textContent = ch.name || ch.id;
        if (ch.color) span.style.color = ch.color;
        label.appendChild(span);
        container.appendChild(label);
    });
}



// Fetch all units from API
async function fetchUnits() {
    try {
        const res = await fetch('/api/units');
        if (!res.ok) return [];
        return await res.json();
    } catch (e) {
        console.error('fetchUnits error', e);
        return [];
    }
}

// Populate a <select> element with units
async function populateUnitDropdown(selectEl, selectedName) {
    const units = await fetchUnits();
    selectEl.innerHTML = '';
    units.forEach(u => {
        const opt = document.createElement('option');
        opt.value = u.name;
        opt.textContent = u.name;
        if (u.name === selectedName) opt.selected = true;
        selectEl.appendChild(opt);
    });
    if (units.length === 0) {
        const opt = document.createElement('option');
        opt.value = '';
        opt.textContent = '(No units)';
        selectEl.appendChild(opt);
    }
}

// Render the units management list
async function loadUnitsAdmin() {
    const container = document.getElementById('unitsList');
    if (!container) return;
    const units = await fetchUnits();
    if (!units.length) {
        container.innerHTML = '<span style="color:#666;">No units found</span>';
        return;
    }
    container.innerHTML = '';
    units.forEach(u => {
        const span = document.createElement('span');
        span.style.cssText = 'display:inline-flex;align-items:center;gap:6px;background:#222;border:1px solid #444;border-radius:6px;padding:5px 10px;';
        span.innerHTML = `<i class="fas fa-shield-alt" style="color:#28a745;"></i> ${escapeAttr(u.name)}`;
        if (u.name !== 'General') {
            const btn = document.createElement('button');
            btn.title = 'Delete unit';
            btn.style.cssText = 'background:none;border:none;color:#dc3545;cursor:pointer;padding:0 2px;';
            btn.innerHTML = '<i class="fas fa-times"></i>';
            btn.onclick = () => deleteUnit(u.id, u.name);
            span.appendChild(btn);
        }
        container.appendChild(span);
    });
}

// Create a new unit
async function createUnitFromAdmin() {
    const input = document.getElementById('newUnitName');
    if (!input) return;
    const name = input.value.trim();
    if (!name) { alert('Unit name required'); return; }
    
    const token = getAuthToken();
    try {
        const res = await fetch('/api/units', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'Authorization': token ? `Bearer ${token}` : ''
            },
            body: JSON.stringify({ name })
        });
        const body = await res.json().catch(() => null);
        if (!res.ok) {
            alert('Failed to create unit: ' + ((body && (body.detail || body.message)) || res.status));
            return;
        }
        input.value = '';
        await loadUnitsAdmin();
    } catch (e) {
        alert('Error creating unit: ' + e.message);
    }
}

// Delete a unit
async function deleteUnit(unitId, unitName) {
    if (!confirm(`Delete unit "${unitName}"?\n\nAll users in this unit will be moved to "General".`)) return;
    
    const token = getAuthToken();
    try {
        const res = await fetch(`/api/units/${encodeURIComponent(unitId)}`, {
            method: 'DELETE',
            headers: { 'Authorization': token ? `Bearer ${token}` : '' }
        });
        const body = await res.json().catch(() => null);
        if (!res.ok) {
            alert('Failed to delete unit: ' + ((body && (body.detail || body.message)) || res.status));
            return;
        }
        await loadUnitsAdmin();
        await loadUsers();
    } catch (e) {
        alert('Error deleting unit: ' + e.message);
    }
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
        
        // Unit column
        const tdUnit = document.createElement('td');
        tdUnit.textContent = user.unit || 'General';
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
        document.getElementById('editStatus').value = user.is_active === false ? 'false' : 'true';
        document.getElementById('editRole').value = user.role || 'user';
        
        // Populate unit dropdown
        const unitSelect = document.getElementById('editGroup');
        if (unitSelect) {
            await populateUnitDropdown(unitSelect, user.unit || 'General');
        }
        
        // Populate chat channel checkboxes
        const chatChannelsContainer = document.getElementById('editChatChannels');
        if (chatChannelsContainer) {
            await populateChatChannelCheckboxes(chatChannelsContainer, user.chat_channels || ['all']);
        }
        
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
    const unitSelect = document.getElementById('editGroup');
    const unit = unitSelect ? unitSelect.value : '';
    const active = document.getElementById('editStatus').value === 'true';
    const role = document.getElementById('editRole').value;
    
    // Collect selected chat channels
    const chatChannelsContainer = document.getElementById('editChatChannels');
    const selectedChatChannels = chatChannelsContainer
        ? Array.from(chatChannelsContainer.querySelectorAll('input[type="checkbox"]'))
              .filter(cb => cb.checked)
              .map(cb => cb.value)
        : ['all'];
    
    try {
        const payload = {
            fullname: fullname || undefined,
            callsign: callsign || undefined,
            unit: unit || undefined,
            is_active: active,
            role: role,
            chat_channels: selectedChatChannels.length > 0 ? selectedChatChannels : ['all']
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

// Load users and units when page loads
document.addEventListener('DOMContentLoaded', () => {
    loadUsers();
    loadUnitsAdmin();
});
