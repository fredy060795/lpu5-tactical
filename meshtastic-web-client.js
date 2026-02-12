/**
 * Meshtastic Web Bluetooth Client
 * Provides direct Bluetooth connection to Meshtastic devices using Web Bluetooth API
 * Works completely offline without any backend dependencies
 */

class MeshtasticWebClient {
    constructor() {
        this.device = null;
        this.server = null;
        this.service = null;
        this.toRadioCharacteristic = null;
        this.fromRadioCharacteristic = null;
        this.fromNumCharacteristic = null;
        this.connected = false;
        this.nodes = new Map();
        this.messages = [];
        this.onMessageCallback = null;
        this.onNodeUpdateCallback = null;
        this.onStatusCallback = null;
        
        // Meshtastic BLE Service UUID
        this.SERVICE_UUID = '6ba1b218-15a8-461f-9fa8-5dcae273eafd';
        // New firmware characteristic UUIDs (Meshtastic 2.x+)
        this.FROMRADIO_UUID = '8ba2bcc2-ee02-4a55-a531-c525c5e454d5';
        this.TORADIO_UUID = 'f75c76d2-129e-4dad-a1dd-7866124401e7';
        this.FROMNUM_UUID = 'ed9da18c-a800-4f66-a670-aa7547e34453';
        // Legacy firmware characteristic UUIDs (fallback)
        this.LEGACY_FROMRADIO_UUID = '2c55e69e-4993-11ed-b878-0242ac120002';
        this.LEGACY_TORADIO_UUID = '29adaf70-4993-11ed-b878-0242ac120002';
    }

    /**
     * Check if Web Bluetooth is supported
     */
    isSupported() {
        return navigator.bluetooth !== undefined;
    }

    /**
     * Request and connect to a Meshtastic device
     */
    async connect() {
        if (!this.isSupported()) {
            throw new Error('Web Bluetooth is not supported in this browser');
        }

        try {
            this._updateStatus('Requesting Bluetooth device...');
            
            // Request device with Meshtastic service
            this.device = await navigator.bluetooth.requestDevice({
                filters: [{ services: [this.SERVICE_UUID] }],
                optionalServices: [this.SERVICE_UUID]
            });

            this._updateStatus('Connecting to device...');
            
            // Connect to GATT server
            this.server = await this.device.gatt.connect();
            
            this._updateStatus('Getting Meshtastic service...');
            
            // Get the Meshtastic service
            this.service = await this.server.getPrimaryService(this.SERVICE_UUID);
            
            // Get characteristics - try new UUIDs first, fall back to legacy
            try {
                this.toRadioCharacteristic = await this.service.getCharacteristic(this.TORADIO_UUID);
                this.fromRadioCharacteristic = await this.service.getCharacteristic(this.FROMRADIO_UUID);
            } catch (charError) {
                this._updateStatus('Trying legacy characteristic UUIDs...');
                this.toRadioCharacteristic = await this.service.getCharacteristic(this.LEGACY_TORADIO_UUID);
                this.fromRadioCharacteristic = await this.service.getCharacteristic(this.LEGACY_FROMRADIO_UUID);
            }
            
            try {
                this.fromNumCharacteristic = await this.service.getCharacteristic(this.FROMNUM_UUID);
            } catch (numError) {
                console.warn('[Meshtastic] fromNum characteristic not found, continuing without it');
                this.fromNumCharacteristic = null;
            }
            
            // Start notifications
            await this.fromRadioCharacteristic.startNotifications();
            this.fromRadioCharacteristic.addEventListener('characteristicvaluechanged', 
                this._handleIncomingData.bind(this));
            
            this.connected = true;
            this._updateStatus('Connected to ' + this.device.name);
            
            // Request initial data
            await this._requestConfig();
            
            return true;
        } catch (error) {
            this._updateStatus('Connection failed: ' + error.message);
            throw error;
        }
    }

    /**
     * Disconnect from the device
     */
    async disconnect() {
        if (this.device && this.device.gatt.connected) {
            await this.device.gatt.disconnect();
        }
        this.connected = false;
        this.device = null;
        this.server = null;
        this.service = null;
        this._updateStatus('Disconnected');
    }

    /**
     * Send a text message
     */
    async sendText(text, channelIndex = 0) {
        if (!this.connected) {
            throw new Error('Not connected to device');
        }

        const packet = this._createTextPacket(text, channelIndex);
        await this._sendPacket(packet);
        
        return { success: true, text, timestamp: Date.now() };
    }

    /**
     * Send a COT (Cursor on Target) message
     */
    async sendCOT(cotXml, channelIndex = 0) {
        if (!this.connected) {
            throw new Error('Not connected to device');
        }

        const packet = this._createTextPacket(cotXml, channelIndex);
        await this._sendPacket(packet);
        
        return { success: true, cot: true, timestamp: Date.now() };
    }

    /**
     * Send position update
     */
    async sendPosition(lat, lon, alt = 0) {
        if (!this.connected) {
            throw new Error('Not connected to device');
        }

        const packet = this._createPositionPacket(lat, lon, alt);
        await this._sendPacket(packet);
        
        return { success: true, lat, lon, alt, timestamp: Date.now() };
    }

    /**
     * Get list of known nodes
     */
    getNodes() {
        return Array.from(this.nodes.values());
    }

    /**
     * Get message history
     */
    getMessages() {
        return this.messages;
    }

    /**
     * Set callback for new messages
     */
    onMessage(callback) {
        this.onMessageCallback = callback;
    }

    /**
     * Set callback for node updates
     */
    onNodeUpdate(callback) {
        this.onNodeUpdateCallback = callback;
    }

    /**
     * Set callback for status updates
     */
    onStatus(callback) {
        this.onStatusCallback = callback;
    }

    // Private methods

    _updateStatus(status) {
        console.log('[Meshtastic]', status);
        if (this.onStatusCallback) {
            this.onStatusCallback(status);
        }
    }

    async _requestConfig() {
        // Request device configuration
        const configRequest = new Uint8Array([0x00]); // Simple config request
        await this.toRadioCharacteristic.writeValue(configRequest);
    }

    async _sendPacket(packet) {
        // Split packet into chunks if needed (BLE has max packet size)
        const maxChunkSize = 512;
        const data = new Uint8Array(packet);
        
        for (let i = 0; i < data.length; i += maxChunkSize) {
            const chunk = data.slice(i, Math.min(i + maxChunkSize, data.length));
            await this.toRadioCharacteristic.writeValue(chunk);
        }
    }

    _handleIncomingData(event) {
        const value = event.target.value;
        const data = new Uint8Array(value.buffer);
        
        try {
            const packet = this._parsePacket(data);
            
            if (packet.type === 'text') {
                this._handleTextMessage(packet);
            } else if (packet.type === 'position') {
                this._handlePositionUpdate(packet);
            } else if (packet.type === 'nodeinfo') {
                this._handleNodeInfo(packet);
            }
        } catch (error) {
            console.error('[Meshtastic] Error parsing packet:', error);
        }
    }

    _parsePacket(data) {
        // Simplified packet parsing - real implementation would use protobuf
        // This is a basic example structure
        
        if (data.length < 4) {
            throw new Error('Packet too small');
        }

        const packet = {
            from: this._readUint32(data, 0),
            to: this._readUint32(data, 4),
            type: 'unknown',
            payload: null
        };

        // Determine packet type based on first bytes
        if (data[8] === 0x01) {
            packet.type = 'text';
            packet.payload = this._decodeText(data.slice(12));
        } else if (data[8] === 0x02) {
            packet.type = 'position';
            packet.payload = this._decodePosition(data.slice(12));
        } else if (data[8] === 0x03) {
            packet.type = 'nodeinfo';
            packet.payload = this._decodeNodeInfo(data.slice(12));
        }

        return packet;
    }

    _handleTextMessage(packet) {
        const message = {
            id: 'msg_' + Date.now(),
            from: packet.from,
            text: packet.payload,
            timestamp: Date.now(),
            isCOT: this._isCOTMessage(packet.payload)
        };

        this.messages.push(message);
        
        if (this.onMessageCallback) {
            this.onMessageCallback(message);
        }

        // Limit message history
        if (this.messages.length > 1000) {
            this.messages = this.messages.slice(-1000);
        }
    }

    _handlePositionUpdate(packet) {
        const node = {
            id: packet.from,
            lat: packet.payload.lat,
            lon: packet.payload.lon,
            alt: packet.payload.alt || 0,
            timestamp: Date.now()
        };

        this.nodes.set(packet.from, node);
        
        if (this.onNodeUpdateCallback) {
            this.onNodeUpdateCallback(node);
        }
    }

    _handleNodeInfo(packet) {
        const existingNode = this.nodes.get(packet.from) || { id: packet.from };
        
        existingNode.name = packet.payload.name || 'Node-' + packet.from.toString(16);
        existingNode.role = packet.payload.role || 'CLIENT';
        existingNode.timestamp = Date.now();
        
        this.nodes.set(packet.from, existingNode);
        
        if (this.onNodeUpdateCallback) {
            this.onNodeUpdateCallback(existingNode);
        }
    }

    _isCOTMessage(text) {
        return text && (text.startsWith('<?xml') || text.includes('<event'));
    }

    _createTextPacket(text, channelIndex = 0) {
        // Simplified packet creation - real implementation would use protobuf
        const textBytes = new TextEncoder().encode(text);
        const packet = new Uint8Array(12 + textBytes.length);
        
        // Header
        this._writeUint32(packet, 0, 0xFFFFFFFF); // to: broadcast
        this._writeUint32(packet, 4, 0); // from: self
        packet[8] = 0x01; // type: text
        
        // Payload
        packet.set(textBytes, 12);
        
        return packet;
    }

    _createPositionPacket(lat, lon, alt) {
        const packet = new Uint8Array(24);
        
        // Header
        this._writeUint32(packet, 0, 0xFFFFFFFF); // to: broadcast
        this._writeUint32(packet, 4, 0); // from: self
        packet[8] = 0x02; // type: position
        
        // Position data (simplified)
        this._writeFloat32(packet, 12, lat);
        this._writeFloat32(packet, 16, lon);
        this._writeFloat32(packet, 20, alt);
        
        return packet;
    }

    _decodeText(data) {
        return new TextDecoder().decode(data);
    }

    _decodePosition(data) {
        return {
            lat: this._readFloat32(data, 0),
            lon: this._readFloat32(data, 4),
            alt: this._readFloat32(data, 8)
        };
    }

    _decodeNodeInfo(data) {
        const name = new TextDecoder().decode(data.slice(0, 32)).replace(/\0/g, '');
        return {
            name: name || 'Unknown',
            role: 'CLIENT'
        };
    }

    // Utility methods for binary data
    _readUint32(data, offset) {
        return (data[offset] | (data[offset + 1] << 8) | 
                (data[offset + 2] << 16) | (data[offset + 3] << 24)) >>> 0;
    }

    _writeUint32(data, offset, value) {
        data[offset] = value & 0xFF;
        data[offset + 1] = (value >> 8) & 0xFF;
        data[offset + 2] = (value >> 16) & 0xFF;
        data[offset + 3] = (value >> 24) & 0xFF;
    }

    _readFloat32(data, offset) {
        const buffer = new ArrayBuffer(4);
        const view = new DataView(buffer);
        for (let i = 0; i < 4; i++) {
            view.setUint8(i, data[offset + i]);
        }
        return view.getFloat32(0, true);
    }

    _writeFloat32(data, offset, value) {
        const buffer = new ArrayBuffer(4);
        const view = new DataView(buffer);
        view.setFloat32(0, value, true);
        for (let i = 0; i < 4; i++) {
            data[offset + i] = view.getUint8(i);
        }
    }
}

// Make available globally
window.MeshtasticWebClient = MeshtasticWebClient;
