/**
 * Sidebar behaviour shared across all pages that load _global_nav.html.
 *
 * Provides:
 *  - logoutUser()              – clears auth storage and redirects to /
 *  - updateUserNameDisplay()   – fills #userName / #userStatus from localStorage
 *  - storage-event listener    – keeps the sidebar in sync across tabs
 */
(function () {
    'use strict';

    // --- logout -----------------------------------------------------------------

    if (typeof window.logoutUser !== 'function') {
        window.logoutUser = function logoutUser() {
            if (confirm('Really log out?')) {
                try {
                    localStorage.removeItem('lpu5_token');
                    localStorage.removeItem('lpu5_user');
                } catch (e) {}
                window.location.href = '/';
            }
        };
    }

    // --- username display -------------------------------------------------------

    async function updateUserNameDisplay() {
        const elName   = document.getElementById('userName');
        const elStatus = document.getElementById('userStatus');
        if (!elName && !elStatus) return;

        let displayName = null;

        // 1) lpu5_user JSON object
        try {
            const raw = localStorage.getItem('lpu5_user');
            if (raw) {
                const u = JSON.parse(raw);
                displayName = u.name || u.fullname || u.username || u.displayName || null;
            }
        } catch (e) {
            console.warn('sidebar.js: failed to parse lpu5_user', e);
        }

        // 2) lpu5_token as plain username (not a JWT)
        // Heuristic: JWTs contain dots and are typically long; a short token
        // without dots is likely a plain username stored directly in the key.
        if (!displayName) {
            const token = localStorage.getItem('lpu5_token');
            if (token && token.indexOf('.') === -1 && token.length < 128) {
                displayName = token;
            }
        }

        // 3) /api/me fallback
        if (!displayName) {
            try {
                const resp = await fetch('/api/me', { credentials: 'include' });
                if (resp.ok) {
                    const me = await resp.json();
                    displayName = me.name || me.fullname || me.username || null;
                }
            } catch (e) { /* ignore */ }
        }

        if (elName)   elName.textContent   = displayName || 'Guest';
        if (elStatus) elStatus.textContent = displayName ? 'Online' : 'Not logged in';

        // reflect login state on the nav element itself
        const nav = document.querySelector('.tactical-sidebar');
        if (nav) {
            nav.dataset.user = displayName || '';
        }
    }

    // run on load and keep in sync
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', function () {
            updateUserNameDisplay().catch(function (e) {
                console.warn('sidebar.js: updateUserNameDisplay error', e);
            });
        });
    } else {
        updateUserNameDisplay().catch(function (e) {
            console.warn('sidebar.js: updateUserNameDisplay error', e);
        });
    }

    window.addEventListener('storage', function (e) {
        if (e.key === 'lpu5_user' || e.key === 'lpu5_token') {
            updateUserNameDisplay().catch(function (err) { console.warn(err); });
        }
    });

    // expose for pages that call it directly
    window.updateUserNameDisplay = updateUserNameDisplay;
}());
