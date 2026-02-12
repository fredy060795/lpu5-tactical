/**
 * Offline Message Queue Manager
 * Uses IndexedDB to store and manage messages when offline
 * Ensures reliable message delivery via Meshtastic LoRa
 */

class MessageQueueManager {
    constructor() {
        this.db = null;
        this.dbName = 'MeshtasticOfflineDB';
        this.dbVersion = 1;
        this.initialized = false;
        this.MAX_RETRIES = 3; // Configurable maximum retry attempts
    }

    /**
     * Initialize the database
     */
    async init() {
        if (this.initialized) return;

        return new Promise((resolve, reject) => {
            const request = indexedDB.open(this.dbName, this.dbVersion);

            request.onerror = () => reject(request.error);
            
            request.onsuccess = () => {
                this.db = request.result;
                this.initialized = true;
                console.log('[MessageQueue] Database initialized');
                resolve();
            };

            request.onupgradeneeded = (event) => {
                const db = event.target.result;

                // Create object stores if they don't exist
                if (!db.objectStoreNames.contains('pendingMessages')) {
                    const store = db.createObjectStore('pendingMessages', { keyPath: 'id', autoIncrement: true });
                    store.createIndex('timestamp', 'timestamp', { unique: false });
                    store.createIndex('status', 'status', { unique: false });
                }

                if (!db.objectStoreNames.contains('sentMessages')) {
                    const store = db.createObjectStore('sentMessages', { keyPath: 'id', autoIncrement: true });
                    store.createIndex('timestamp', 'timestamp', { unique: false });
                }

                if (!db.objectStoreNames.contains('receivedMessages')) {
                    const store = db.createObjectStore('receivedMessages', { keyPath: 'id', autoIncrement: true });
                    store.createIndex('timestamp', 'timestamp', { unique: false });
                    store.createIndex('from', 'from', { unique: false });
                }

                if (!db.objectStoreNames.contains('nodes')) {
                    const store = db.createObjectStore('nodes', { keyPath: 'id' });
                    store.createIndex('timestamp', 'timestamp', { unique: false });
                }

                console.log('[MessageQueue] Database schema created');
            };
        });
    }

    /**
     * Add a message to the pending queue
     */
    async addPendingMessage(message) {
        await this.init();

        return new Promise((resolve, reject) => {
            const transaction = this.db.transaction(['pendingMessages'], 'readwrite');
            const store = transaction.objectStore('pendingMessages');

            const messageData = {
                ...message,
                timestamp: message.timestamp || Date.now(),
                status: 'pending',
                retryCount: 0,
                maxRetries: this.MAX_RETRIES
            };

            const request = store.add(messageData);

            request.onsuccess = () => {
                console.log('[MessageQueue] Message queued:', request.result);
                resolve(request.result);
            };

            request.onerror = () => reject(request.error);
        });
    }

    /**
     * Get all pending messages
     */
    async getPendingMessages() {
        await this.init();

        return new Promise((resolve, reject) => {
            const transaction = this.db.transaction(['pendingMessages'], 'readonly');
            const store = transaction.objectStore('pendingMessages');
            const request = store.getAll();

            request.onsuccess = () => resolve(request.result);
            request.onerror = () => reject(request.error);
        });
    }

    /**
     * Mark a message as sent
     */
    async markAsSent(messageId) {
        await this.init();

        return new Promise((resolve, reject) => {
            const transaction = this.db.transaction(['pendingMessages', 'sentMessages'], 'readwrite');
            const pendingStore = transaction.objectStore('pendingMessages');
            const sentStore = transaction.objectStore('sentMessages');

            // Get the message from pending
            const getRequest = pendingStore.get(messageId);

            getRequest.onsuccess = () => {
                const message = getRequest.result;
                if (!message) {
                    resolve();
                    return;
                }

                // Add to sent messages
                message.sentTimestamp = Date.now();
                delete message.id; // Let it auto-generate new ID
                sentStore.add(message);

                // Remove from pending
                pendingStore.delete(messageId);

                console.log('[MessageQueue] Message marked as sent:', messageId);
                resolve();
            };

            getRequest.onerror = () => reject(getRequest.error);
        });
    }

    /**
     * Increment retry count for a message
     */
    async incrementRetry(messageId) {
        await this.init();

        return new Promise((resolve, reject) => {
            const transaction = this.db.transaction(['pendingMessages'], 'readwrite');
            const store = transaction.objectStore('pendingMessages');

            const request = store.get(messageId);

            request.onsuccess = () => {
                const message = request.result;
                if (!message) {
                    resolve(false);
                    return;
                }

                message.retryCount = (message.retryCount || 0) + 1;
                message.lastRetryTimestamp = Date.now();

                // If max retries exceeded, mark as failed
                if (message.retryCount >= message.maxRetries) {
                    message.status = 'failed';
                }

                const updateRequest = store.put(message);
                updateRequest.onsuccess = () => {
                    console.log('[MessageQueue] Retry count updated:', messageId, message.retryCount);
                    resolve(message.retryCount < message.maxRetries);
                };
                updateRequest.onerror = () => reject(updateRequest.error);
            };

            request.onerror = () => reject(request.error);
        });
    }

    /**
     * Store a received message
     */
    async addReceivedMessage(message) {
        await this.init();

        return new Promise((resolve, reject) => {
            const transaction = this.db.transaction(['receivedMessages'], 'readwrite');
            const store = transaction.objectStore('receivedMessages');

            const messageData = {
                ...message,
                timestamp: message.timestamp || Date.now(),
                read: false
            };

            const request = store.add(messageData);

            request.onsuccess = () => {
                console.log('[MessageQueue] Received message stored:', request.result);
                resolve(request.result);
            };

            request.onerror = () => reject(request.error);
        });
    }

    /**
     * Get received messages
     */
    async getReceivedMessages(limit = 100) {
        await this.init();

        return new Promise((resolve, reject) => {
            const transaction = this.db.transaction(['receivedMessages'], 'readonly');
            const store = transaction.objectStore('receivedMessages');
            const index = store.index('timestamp');
            
            const request = index.openCursor(null, 'prev'); // Most recent first
            const results = [];

            request.onsuccess = (event) => {
                const cursor = event.target.result;
                if (cursor && results.length < limit) {
                    results.push(cursor.value);
                    cursor.continue();
                } else {
                    resolve(results);
                }
            };

            request.onerror = () => reject(request.error);
        });
    }

    /**
     * Store or update a node
     */
    async updateNode(node) {
        await this.init();

        return new Promise((resolve, reject) => {
            const transaction = this.db.transaction(['nodes'], 'readwrite');
            const store = transaction.objectStore('nodes');

            const nodeData = {
                ...node,
                timestamp: Date.now()
            };

            const request = store.put(nodeData);

            request.onsuccess = () => {
                console.log('[MessageQueue] Node updated:', node.id);
                resolve();
            };

            request.onerror = () => reject(request.error);
        });
    }

    /**
     * Get all nodes
     */
    async getNodes() {
        await this.init();

        return new Promise((resolve, reject) => {
            const transaction = this.db.transaction(['nodes'], 'readonly');
            const store = transaction.objectStore('nodes');
            const request = store.getAll();

            request.onsuccess = () => resolve(request.result);
            request.onerror = () => reject(request.error);
        });
    }

    /**
     * Clear old messages (keep last N days)
     */
    async clearOldMessages(daysToKeep = 7) {
        await this.init();

        const cutoffTime = Date.now() - (daysToKeep * 24 * 60 * 60 * 1000);

        return new Promise((resolve, reject) => {
            const transaction = this.db.transaction(['receivedMessages', 'sentMessages'], 'readwrite');
            
            ['receivedMessages', 'sentMessages'].forEach(storeName => {
                const store = transaction.objectStore(storeName);
                const index = store.index('timestamp');
                const range = IDBKeyRange.upperBound(cutoffTime);
                
                index.openCursor(range).onsuccess = (event) => {
                    const cursor = event.target.result;
                    if (cursor) {
                        cursor.delete();
                        cursor.continue();
                    }
                };
            });

            transaction.oncomplete = () => {
                console.log('[MessageQueue] Old messages cleared');
                resolve();
            };

            transaction.onerror = () => reject(transaction.error);
        });
    }

    /**
     * Get statistics
     */
    async getStats() {
        await this.init();

        const pending = await this.getPendingMessages();
        const received = await this.getReceivedMessages(1000);
        const nodes = await this.getNodes();
        
        // Get sent messages count
        const sentCount = await new Promise((resolve, reject) => {
            const transaction = this.db.transaction(['sentMessages'], 'readonly');
            const store = transaction.objectStore('sentMessages');
            const request = store.count();
            request.onsuccess = () => resolve(request.result);
            request.onerror = () => reject(request.error);
        });

        return {
            pendingCount: pending.length,
            sentCount: sentCount,
            receivedCount: received.length,
            nodeCount: nodes.length,
            failedCount: pending.filter(m => m.status === 'failed').length
        };
    }

    /**
     * Export all data (for backup)
     */
    async exportData() {
        await this.init();

        const [pending, received, nodes] = await Promise.all([
            this.getPendingMessages(),
            this.getReceivedMessages(1000),
            this.getNodes()
        ]);

        return {
            version: this.dbVersion,
            exported: new Date().toISOString(),
            pendingMessages: pending,
            receivedMessages: received,
            nodes: nodes
        };
    }

    /**
     * Import data (for restore)
     */
    async importData(data) {
        await this.init();

        const transaction = this.db.transaction(['pendingMessages', 'receivedMessages', 'nodes'], 'readwrite');

        // Import pending messages
        if (data.pendingMessages) {
            const pendingStore = transaction.objectStore('pendingMessages');
            data.pendingMessages.forEach(msg => {
                delete msg.id; // Let it auto-generate
                pendingStore.add(msg);
            });
        }

        // Import received messages
        if (data.receivedMessages) {
            const receivedStore = transaction.objectStore('receivedMessages');
            data.receivedMessages.forEach(msg => {
                delete msg.id; // Let it auto-generate
                receivedStore.add(msg);
            });
        }

        // Import nodes
        if (data.nodes) {
            const nodesStore = transaction.objectStore('nodes');
            data.nodes.forEach(node => {
                nodesStore.put(node);
            });
        }

        return new Promise((resolve, reject) => {
            transaction.oncomplete = () => {
                console.log('[MessageQueue] Data imported successfully');
                resolve();
            };
            transaction.onerror = () => reject(transaction.error);
        });
    }
}

// Make available globally
window.MessageQueueManager = MessageQueueManager;
