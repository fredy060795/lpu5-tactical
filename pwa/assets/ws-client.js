/**
 * WebSocket Client for LPU5 Tactical
 * Handles real-time communication with the server
 */

const WsClient = {
  ws: null,
  reconnectAttempts: 0,
  maxReconnectAttempts: 5,
  reconnectDelay: 3000,
  isConnecting: false,
  messageHandlers: [],
  
  /**
   * Initialize WebSocket connection
   * @param {string} url - WebSocket URL (optional, defaults to current host)
   */
  connect(url = null) {
    if (this.isConnecting || (this.ws && this.ws.readyState === WebSocket.OPEN)) {
      console.log('[WsClient] Already connected or connecting');
      return;
    }
    
    this.isConnecting = true;
    
    // Construct WebSocket URL
    if (!url) {
      const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
      url = `${protocol}//${window.location.host}/ws`;
    }
    
    console.log('[WsClient] Connecting to:', url);
    
    try {
      this.ws = new WebSocket(url);
      
      this.ws.onopen = () => {
        console.log('[WsClient] Connected');
        this.isConnecting = false;
        this.reconnectAttempts = 0;
        this.onOpen();
      };
      
      this.ws.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data);
          this.handleMessage(data);
        } catch (e) {
          console.error('[WsClient] Failed to parse message:', e);
        }
      };
      
      this.ws.onerror = (error) => {
        console.error('[WsClient] Error:', error);
        this.isConnecting = false;
      };
      
      this.ws.onclose = () => {
        console.log('[WsClient] Disconnected');
        this.isConnecting = false;
        this.onClose();
        this.scheduleReconnect();
      };
    } catch (error) {
      console.error('[WsClient] Failed to create WebSocket:', error);
      this.isConnecting = false;
      this.scheduleReconnect();
    }
  },
  
  /**
   * Schedule reconnection attempt
   */
  scheduleReconnect() {
    if (this.reconnectAttempts >= this.maxReconnectAttempts) {
      console.warn('[WsClient] Max reconnect attempts reached');
      return;
    }
    
    this.reconnectAttempts++;
    console.log(`[WsClient] Reconnecting in ${this.reconnectDelay}ms (attempt ${this.reconnectAttempts}/${this.maxReconnectAttempts})`);
    
    setTimeout(() => {
      this.connect();
    }, this.reconnectDelay);
  },
  
  /**
   * Send message to server
   * @param {Object} data - Data to send
   */
  send(data) {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
      console.warn('[WsClient] Cannot send - not connected');
      return false;
    }
    
    try {
      this.ws.send(JSON.stringify(data));
      return true;
    } catch (error) {
      console.error('[WsClient] Failed to send message:', error);
      return false;
    }
  },
  
  /**
   * Register a message handler
   * @param {Function} handler - Function to call when message received
   */
  onMessage(handler) {
    if (typeof handler === 'function') {
      this.messageHandlers.push(handler);
    }
  },
  
  /**
   * Handle incoming message
   * @param {Object} data - Parsed message data
   */
  handleMessage(data) {
    // Call all registered handlers
    this.messageHandlers.forEach(handler => {
      try {
        handler(data);
      } catch (e) {
        console.error('[WsClient] Handler error:', e);
      }
    });
  },
  
  /**
   * Called when connection opens
   */
  onOpen() {
    // Can be overridden
  },
  
  /**
   * Called when connection closes
   */
  onClose() {
    // Can be overridden
  },
  
  /**
   * Disconnect from server
   */
  disconnect() {
    if (this.ws) {
      this.reconnectAttempts = this.maxReconnectAttempts; // Prevent reconnect
      this.ws.close();
      this.ws = null;
    }
  },
  
  /**
   * Check if connected
   */
  isConnected() {
    return this.ws && this.ws.readyState === WebSocket.OPEN;
  }
};

// Export for use in overview.html
window.WsClient = WsClient;

console.log('[WsClient] Initialized');
