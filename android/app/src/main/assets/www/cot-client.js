/**
 * COT (Cursor on Target) Client Library
 * JavaScript implementation for creating and parsing COT XML messages
 * Compatible with ATAK/WinTAK and based on cot_protocol.py
 */

class COTEvent {
    constructor(options = {}) {
        this.uid = options.uid || this._generateUID();
        this.type = options.type || 'a-f-G-U-C'; // friendly ground unit
        this.lat = options.lat || 0;
        this.lon = options.lon || 0;
        this.hae = options.hae || 0; // height above ellipsoid
        this.ce = options.ce || 9999999.0; // circular error
        this.le = options.le || 9999999.0; // linear error
        this.callsign = options.callsign || this.uid;
        this.remarks = options.remarks || '';
        this.teamName = options.teamName || '';
        this.teamRole = options.teamRole || '';
        this.how = options.how || 'm-g'; // machine-generated
        
        const now = new Date();
        this.time = options.time || now;
        this.start = options.start || now;
        this.stale = options.stale || new Date(now.getTime() + (5 * 60 * 1000)); // 5 minutes default
    }

    /**
     * Generate a unique ID for the event
     */
    _generateUID() {
        return 'MESH-' + Date.now() + '-' + Math.random().toString(36).substr(2, 9);
    }

    /**
     * Format datetime for COT XML
     */
    _formatTime(date) {
        const iso = date.toISOString();
        return iso.replace(/\.\d{3}Z$/, 'Z'); // Remove milliseconds
    }

    /**
     * Convert this COT event to XML string
     */
    toXML() {
        let xml = '<?xml version="1.0" encoding="UTF-8"?>';
        xml += '<event';
        xml += ` version="2.0"`;
        xml += ` uid="${this._escapeXML(this.uid)}"`;
        xml += ` type="${this._escapeXML(this.type)}"`;
        xml += ` how="${this._escapeXML(this.how)}"`;
        xml += ` time="${this._formatTime(this.time)}"`;
        xml += ` start="${this._formatTime(this.start)}"`;
        xml += ` stale="${this._formatTime(this.stale)}"`;
        xml += '>';
        
        // Point element
        xml += '<point';
        xml += ` lat="${this.lat}"`;
        xml += ` lon="${this.lon}"`;
        xml += ` hae="${this.hae}"`;
        xml += ` ce="${this.ce}"`;
        xml += ` le="${this.le}"`;
        xml += '/>';
        
        // Detail element
        xml += '<detail>';
        
        // Contact
        if (this.callsign) {
            xml += `<contact callsign="${this._escapeXML(this.callsign)}"/>`;
        }
        
        // Group/Team
        if (this.teamName || this.teamRole) {
            xml += '<__group';
            if (this.teamName) xml += ` name="${this._escapeXML(this.teamName)}"`;
            if (this.teamRole) xml += ` role="${this._escapeXML(this.teamRole)}"`;
            xml += '/>';
        }
        
        // Remarks
        if (this.remarks) {
            xml += `<remarks>${this._escapeXML(this.remarks)}</remarks>`;
        }
        
        // Track
        xml += '<track speed="0.0" course="0.0"/>';
        
        xml += '</detail>';
        xml += '</event>';
        
        return xml;
    }

    /**
     * Escape special XML characters
     */
    _escapeXML(str) {
        if (!str) return '';
        return String(str)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&apos;');
    }

    /**
     * Convert to dictionary for JSON serialization
     */
    toDict() {
        return {
            uid: this.uid,
            type: this.type,
            lat: this.lat,
            lon: this.lon,
            hae: this.hae,
            ce: this.ce,
            le: this.le,
            callsign: this.callsign,
            remarks: this.remarks,
            teamName: this.teamName,
            teamRole: this.teamRole,
            time: this._formatTime(this.time),
            start: this._formatTime(this.start),
            stale: this._formatTime(this.stale),
            how: this.how
        };
    }

    /**
     * Parse COT XML string into a COTEvent object
     */
    static fromXML(xmlString) {
        try {
            const parser = new DOMParser();
            const xmlDoc = parser.parseFromString(xmlString, 'text/xml');
            
            // Check for parsing errors
            const parseError = xmlDoc.querySelector('parsererror');
            if (parseError) {
                throw new Error('XML parsing error: ' + parseError.textContent);
            }
            
            const event = xmlDoc.querySelector('event');
            if (!event) {
                throw new Error('No event element found');
            }
            
            // Extract basic attributes
            const uid = event.getAttribute('uid');
            const type = event.getAttribute('type');
            const how = event.getAttribute('how') || 'm-g';
            
            // Extract point
            const point = event.querySelector('point');
            if (!point) {
                throw new Error('No point element found');
            }
            
            const lat = parseFloat(point.getAttribute('lat'));
            const lon = parseFloat(point.getAttribute('lon'));
            const hae = parseFloat(point.getAttribute('hae') || '0');
            const ce = parseFloat(point.getAttribute('ce') || '9999999.0');
            const le = parseFloat(point.getAttribute('le') || '9999999.0');
            
            // Extract detail
            const detail = event.querySelector('detail');
            let callsign = uid;
            let remarks = '';
            let teamName = '';
            let teamRole = '';
            
            if (detail) {
                const contact = detail.querySelector('contact');
                if (contact) {
                    callsign = contact.getAttribute('callsign') || uid;
                }
                
                const group = detail.querySelector('__group');
                if (group) {
                    teamName = group.getAttribute('name') || '';
                    teamRole = group.getAttribute('role') || '';
                }
                
                const remarksElem = detail.querySelector('remarks');
                if (remarksElem) {
                    remarks = remarksElem.textContent || '';
                }
            }
            
            // Parse times
            const timeStr = event.getAttribute('time');
            const startStr = event.getAttribute('start');
            const staleStr = event.getAttribute('stale');
            
            return new COTEvent({
                uid,
                type,
                lat,
                lon,
                hae,
                ce,
                le,
                callsign,
                remarks,
                teamName,
                teamRole,
                how,
                time: timeStr ? new Date(timeStr) : new Date(),
                start: startStr ? new Date(startStr) : new Date(),
                stale: staleStr ? new Date(staleStr) : new Date(Date.now() + 5 * 60 * 1000)
            });
        } catch (error) {
            console.error('Failed to parse COT XML:', error);
            return null;
        }
    }

    // -----------------------------------------------------------------------
    // TAK compatibility: symbol-type mappings
    //
    // LPU5 uses German shape names ("raute", "rechteck", …) internally.
    // All TAK clients (ATAK, ITAK, WinTAK, XTAK) expect the CoT event
    // `type` attribute to carry the official TAK type code (e.g. "b-m-p-s-m"
    // for a spot-map marker).  These two tables must be kept in sync with
    // cot_protocol.py so that symbols have the same IDs on both sides and
    // server-side sync does not produce duplicates or errors.
    // -----------------------------------------------------------------------

    /** LPU5 type name → TAK CoT type code */
    static get LPU5_TO_COT_TYPE() {
        return {
            raute:    'b-m-p-s-m',
            quadrat:  'b-m-p-s-m',
            blume:    'b-m-p-s-m',
            rechteck: 'u-d-r',
            friendly: 'a-f-G-U-C',
            hostile:  'a-h-G-U-C',
            neutral:  'a-n-G-U-C',
            unknown:  'a-u-G-U-C',
            pending:  'a-p-G-U-C',
        };
    }

    /** TAK CoT type prefix → LPU5 type name.
     *  Ordered longest-prefix-first so that more-specific codes are matched
     *  before shorter prefix alternatives when iterating. */
    static get COT_TO_LPU5_TYPE() {
        return [
            ['b-m-p-s-m', 'raute'],    // TAK spot-map marker (all shapes)
            ['u-d-c-c',   'raute'],    // TAK drawing circle → diamond
            ['u-d-r',     'rechteck'], // TAK drawing rectangle
            ['u-d-f',     'raute'],    // TAK drawing freehand → diamond
            ['a-f',       'friendly'], // friendly (any sub-type)
            ['a-h',       'hostile'],
            ['a-n',       'neutral'],
            ['a-u',       'unknown'],
            ['a-p',       'pending'],
        ];
    }

    /** Convert a lowercase LPU5 type to the matching TAK CoT type string */
    static lpu5TypeToCot(lpu5Type) {
        return COTEvent.LPU5_TO_COT_TYPE[(lpu5Type || '').toLowerCase()] || 'a-u-G-U-C';
    }

    /** Convert a TAK CoT type string back to the LPU5 symbol type */
    static cotTypeToLpu5(cotType) {
        if (!cotType) return 'unknown';
        for (const [prefix, lpu5] of COTEvent.COT_TO_LPU5_TYPE) {
            if (cotType.startsWith(prefix)) return lpu5;
        }
        return 'unknown';
    }

    /**
     * Build a COT type string from components
     */
    static buildCOTType(atom = 'friendly', entity = 'ground_unit', func = 'U', detail = 'C') {
        const atomTypes = {
            'friendly': 'a-f',
            'hostile': 'a-h',
            'neutral': 'a-n',
            'unknown': 'a-u',
            'pending': 'a-p'
        };
        
        const entityTypes = {
            'ground_unit': 'G',
            'aircraft': 'A',
            'space': 'P',
            'surface': 'S',
            'subsurface': 'U'
        };
        
        const atomCode = atomTypes[atom] || 'a-u';
        const entityCode = entityTypes[entity] || 'G';
        
        return `${atomCode}-${entityCode}-${func}-${detail}`;
    }
}

/**
 * COT Protocol Handler
 * Provides utility functions for COT operations
 */
class COTProtocolHandler {
    /**
     * Convert a map marker to COT event
     */
    static markerToCOT(marker) {
        try {
            const uid = marker.id || COTEvent.prototype._generateUID();
            const lat = parseFloat(marker.lat || 0);
            const lon = parseFloat(marker.lng || marker.lon || 0);

            // If the marker already carries a TAK-originated cotType/cot_type,
            // reuse it exactly so that the symbol identity is preserved when
            // re-broadcasting to other TAK clients.
            let type = marker.cotType || marker.cot_type;
            if (!type) {
                // Derive the TAK CoT type from the LPU5 symbol type field.
                // Normalise to lowercase for consistent lookup.
                const lpu5Type = (marker.type || marker.status || 'unknown').toLowerCase();
                type = COTEvent.lpu5TypeToCot(lpu5Type);
            }

            return new COTEvent({
                uid,
                type,
                lat,
                lon,
                callsign: marker.name || marker.callsign || uid,
                remarks: marker.description || marker.remarks || '',
                teamName: marker.team || '',
                teamRole: marker.role || ''
            });
        } catch (error) {
            console.error('Failed to convert marker to COT:', error);
            return null;
        }
    }

    /**
     * Convert a COT event to map marker
     */
    static cotToMarker(cotEvent) {
        // Map the TAK CoT type back to the LPU5 internal symbol type so the
        // correct icon is rendered in admin_map / overview.
        const lpu5Type = COTEvent.cotTypeToLpu5(cotEvent.type);

        return {
            id: cotEvent.uid,
            name: cotEvent.callsign,
            callsign: cotEvent.callsign,
            lat: cotEvent.lat,
            lng: cotEvent.lon,
            lon: cotEvent.lon,
            altitude: cotEvent.hae,
            type: lpu5Type,
            status: lpu5Type,
            description: cotEvent.remarks,
            team: cotEvent.teamName,
            role: cotEvent.teamRole,
            timestamp: cotEvent._formatTime(cotEvent.time),
            cotType: cotEvent.type,
            source: 'cot'
        };
    }

    /**
     * Validate COT XML structure
     */
    static validateCOTXML(xmlString) {
        try {
            const parser = new DOMParser();
            const xmlDoc = parser.parseFromString(xmlString, 'text/xml');
            
            // Check for parsing errors
            const parseError = xmlDoc.querySelector('parsererror');
            if (parseError) return false;
            
            const event = xmlDoc.querySelector('event');
            if (!event) return false;
            
            // Check required attributes
            if (!event.getAttribute('uid') || !event.getAttribute('type')) {
                return false;
            }
            
            // Check for point element
            const point = event.querySelector('point');
            if (!point) return false;
            
            // Validate coordinates
            const lat = parseFloat(point.getAttribute('lat'));
            const lon = parseFloat(point.getAttribute('lon'));
            
            if (isNaN(lat) || isNaN(lon)) return false;
            if (lat < -90 || lat > 90 || lon < -180 || lon > 180) return false;
            
            return true;
        } catch (error) {
            return false;
        }
    }

    /**
     * Check if a string is COT XML
     */
    static isCOTMessage(text) {
        if (!text) return false;
        const trimmed = text.trim();
        return trimmed.startsWith('<?xml') || trimmed.startsWith('<event');
    }
}

// Make available globally
window.COTEvent = COTEvent;
window.COTProtocolHandler = COTProtocolHandler;
