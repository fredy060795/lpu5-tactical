/**
 * Global Navigation Loader
 * Loads the navigation from _global_nav.html and injects it into the page
 */
(function() {
    'use strict';
    
    // Fetch and load the navigation
    fetch('_global_nav.html')
        .then(response => {
            if (!response.ok) {
                throw new Error('Failed to load navigation');
            }
            return response.text();
        })
        .then(html => {
            // Parse the HTML to extract the navigation content
            const parser = new DOMParser();
            const doc = parser.parseFromString(html, 'text/html');
            
            // Extract the styles from head
            const styles = doc.querySelectorAll('style');
            styles.forEach(style => {
                // Only add if not already present
                const styleContent = style.textContent;
                if (!document.querySelector(`style[data-global-nav]`)) {
                    const newStyle = document.createElement('style');
                    newStyle.setAttribute('data-global-nav', 'true');
                    newStyle.textContent = styleContent;
                    document.head.appendChild(newStyle);
                }
            });
            
            // Extract the navigation element
            const nav = doc.querySelector('.tactical-sidebar') || doc.querySelector('nav');
            if (nav) {
                // Insert at the beginning of body
                document.body.insertBefore(nav.cloneNode(true), document.body.firstChild);
            }
            
            // Execute any inline scripts from the navigation document
            // Note: Only execute scripts from trusted navigation source (_global_nav.html)
            const inlineScripts = Array.from(doc.querySelectorAll('script:not([src])')).filter(s => s.textContent.trim());
            inlineScripts.forEach(script => {
                try {
                    // Create a new script element and append to body to execute in proper context
                    const newScript = document.createElement('script');
                    newScript.textContent = script.textContent;
                    document.body.appendChild(newScript);
                } catch (e) {
                    console.warn('Error executing navigation script:', e);
                }
            });
            
            // Load external scripts if any
            const scripts = doc.querySelectorAll('script[src]');
            scripts.forEach(script => {
                const newScript = document.createElement('script');
                newScript.src = script.src;
                document.body.appendChild(newScript);
            });
            
            // Update username from localStorage
            try {
                const userJson = localStorage.getItem('lpu5_user');
                if (userJson) {
                    const user = JSON.parse(userJson);
                    const displayName = user.fullname || user.username || user.callsign;
                    if (displayName) {
                        const userNameEl = document.getElementById('userName');
                        if (userNameEl) userNameEl.textContent = displayName;
                    }
                }
            } catch (e) {
                console.warn('Failed to update username in nav:', e);
            }
        })
        .catch(error => {
            console.error('Error loading global navigation:', error);
        });
})();
