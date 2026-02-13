/**
 * API Client for LPU5 Tactical
 * Provides helper functions for API communication with error handling and retry logic
 */

// API Client namespace
const ApiClient = {
  // Queue for offline requests
  syncQueue: [],
  
  /**
   * Make an API request with automatic token inclusion
   * @param {string} endpoint - API endpoint (e.g., '/api/map_markers')
   * @param {Object} options - Fetch options (method, body, headers, etc.)
   * @returns {Promise<Response>}
   */
  async request(endpoint, options = {}) {
    const token = localStorage.getItem('token');
    const headers = {
      'Content-Type': 'application/json',
      ...options.headers
    };
    
    if (token) {
      headers['Authorization'] = `Bearer ${token}`;
    }
    
    const fetchOptions = {
      ...options,
      headers
    };
    
    try {
      const response = await fetch(endpoint, fetchOptions);
      return response;
    } catch (error) {
      console.error('[ApiClient] Request failed:', error);
      
      // If offline, queue the request
      if (!navigator.onLine && options.method && ['POST', 'PUT', 'DELETE', 'PATCH'].includes(options.method.toUpperCase())) {
        this.queueRequest(endpoint, fetchOptions);
      }
      
      throw error;
    }
  },
  
  /**
   * Queue a request for later when back online
   * @param {string} endpoint - API endpoint
   * @param {Object} options - Fetch options
   */
  queueRequest(endpoint, options) {
    const queueItem = {
      endpoint,
      options,
      timestamp: Date.now()
    };
    
    this.syncQueue.push(queueItem);
    console.log('[ApiClient] Request queued for sync:', endpoint);
    
    // Store in localStorage for persistence
    try {
      localStorage.setItem('lpu5_api_sync_queue', JSON.stringify(this.syncQueue));
    } catch (e) {
      console.warn('[ApiClient] Could not persist sync queue:', e);
    }
  },
  
  /**
   * Process queued requests when back online
   */
  async processSyncQueue() {
    if (!navigator.onLine || this.syncQueue.length === 0) {
      return;
    }
    
    console.log(`[ApiClient] Processing ${this.syncQueue.length} queued requests`);
    
    const queue = [...this.syncQueue];
    this.syncQueue = [];
    
    for (const item of queue) {
      try {
        const response = await fetch(item.endpoint, item.options);
        if (response.ok) {
          console.log('[ApiClient] Synced queued request:', item.endpoint);
        } else {
          console.warn('[ApiClient] Queued request failed:', item.endpoint, response.status);
          // Re-queue if not a client error
          if (response.status >= 500) {
            this.syncQueue.push(item);
          }
        }
      } catch (error) {
        console.error('[ApiClient] Error processing queued request:', error);
        // Re-queue on network error
        this.syncQueue.push(item);
      }
    }
    
    // Update localStorage
    try {
      if (this.syncQueue.length > 0) {
        localStorage.setItem('lpu5_api_sync_queue', JSON.stringify(this.syncQueue));
      } else {
        localStorage.removeItem('lpu5_api_sync_queue');
      }
    } catch (e) {
      console.warn('[ApiClient] Could not update sync queue:', e);
    }
  },
  
  /**
   * Load sync queue from localStorage on init
   */
  init() {
    try {
      const stored = localStorage.getItem('lpu5_api_sync_queue');
      if (stored) {
        this.syncQueue = JSON.parse(stored);
        console.log(`[ApiClient] Loaded ${this.syncQueue.length} queued requests from storage`);
      }
    } catch (e) {
      console.warn('[ApiClient] Could not load sync queue:', e);
    }
    
    // Process queue when online
    if (navigator.onLine) {
      this.processSyncQueue();
    }
  }
};

// Initialize on load
ApiClient.init();

// Export for use in overview.html
window.ApiClient = ApiClient;

console.log('[ApiClient] Initialized');
