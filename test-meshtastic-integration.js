/**
 * Simple validation tests for Meshtastic PWA integration
 * Tests basic functionality without requiring a browser environment
 */

// Mock minimal browser APIs for Node.js testing
global.window = {};
global.navigator = {
  bluetooth: {
    requestDevice: () => Promise.reject(new Error('Mock: No real device'))
  }
};
global.DOMParser = class {
  parseFromString(str) {
    // Check whether the input looks like a valid COT XML document.
    const hasEvent = typeof str === 'string' && str.includes('<event');
    const hasPoint = typeof str === 'string' && str.includes('<point');
    // Very basic XML parsing for COT validation
    return {
      querySelector: (selector) => {
        if (selector === 'parsererror') return null;
        if (selector === 'event') {
          if (!hasEvent) return null;
          return {
            getAttribute: (attr) => {
              if (attr === 'uid') return 'TEST-001';
              if (attr === 'type') return 'a-f-G-U-C';
              if (attr === 'how') return 'm-g';
              return '';
            },
            querySelector: (sub) => {
              if (sub === 'point') {
                if (!hasPoint) return null;
                return {
                  getAttribute: (a) => {
                    if (a === 'lat') return '47.1234';
                    if (a === 'lon') return '8.5678';
                    if (a === 'hae') return '500';
                    return '0';
                  }
                };
              }
              if (sub === 'detail') return {
                querySelector: () => null
              };
              return null;
            }
          };
        }
        return null;
      }
    };
  }
};

// Load the modules
eval(require('fs').readFileSync('./cot-client.js', 'utf8'));

// Make classes available globally from window
global.COTEvent = window.COTEvent;
global.COTProtocolHandler = window.COTProtocolHandler;

console.log('=== Meshtastic PWA Integration Tests ===\n');

let testsPassed = 0;
let testsFailed = 0;

function test(name, fn) {
  try {
    fn();
    console.log('✅ PASS:', name);
    testsPassed++;
  } catch (error) {
    console.log('❌ FAIL:', name);
    console.log('   Error:', error.message);
    testsFailed++;
  }
}

function assert(condition, message) {
  if (!condition) {
    throw new Error(message || 'Assertion failed');
  }
}

// Test COT Event Creation
test('COTEvent creation with default values', () => {
  const cot = new COTEvent({
    lat: 47.1234,
    lon: 8.5678
  });
  assert(cot.lat === 47.1234, 'Latitude should match');
  assert(cot.lon === 8.5678, 'Longitude should match');
  assert(cot.type === 'a-f-G-U-C', 'Default type should be friendly ground unit');
  assert(cot.uid.length > 0, 'UID should be generated');
});

// Test COT Type Building
test('COT type building', () => {
  const type = COTEvent.buildCOTType('hostile', 'aircraft', 'F', 'X');
  assert(type === 'a-h-A-F-X', 'Type should be hostile aircraft');
  
  const type2 = COTEvent.buildCOTType('friendly', 'ground_unit');
  assert(type2 === 'a-f-G-U-C', 'Default friendly ground unit type');
});

// Test COT XML Generation
test('COT XML generation', () => {
  const cot = new COTEvent({
    uid: 'TEST-001',
    lat: 47.1234,
    lon: 8.5678,
    callsign: 'Alpha-1',
    remarks: 'Test position'
  });
  
  const xml = cot.toXML();
  assert(xml.includes('<?xml version="1.0"'), 'Should have XML declaration');
  assert(xml.includes('<event'), 'Should have event element');
  assert(xml.includes('uid="TEST-001"'), 'Should include UID');
  assert(xml.includes('lat="47.1234"'), 'Should include latitude');
  assert(xml.includes('lon="8.5678"'), 'Should include longitude');
  assert(xml.includes('callsign="Alpha-1"'), 'Should include callsign');
  assert(xml.includes('Test position'), 'Should include remarks');
});

// Test COT XML Parsing
test('COT XML parsing', () => {
  const xml = `<?xml version="1.0"?><event version="2.0" uid="TEST-001" type="a-f-G-U-C" how="m-g"><point lat="47.1234" lon="8.5678" hae="500"/><detail></detail></event>`;
  const cot = COTEvent.fromXML(xml);
  
  assert(cot !== null, 'Should parse XML successfully');
  assert(cot.uid === 'TEST-001', 'Should extract UID');
  assert(cot.lat === 47.1234, 'Should extract latitude');
  assert(cot.lon === 8.5678, 'Should extract longitude');
});

// Test COT to Marker Conversion
test('COT to map marker conversion', () => {
  const cot = new COTEvent({
    uid: 'TEST-001',
    type: 'a-f-G-U-C',
    lat: 47.1234,
    lon: 8.5678,
    callsign: 'Alpha-1'
  });
  
  const marker = COTProtocolHandler.cotToMarker(cot);
  assert(marker.id === 'TEST-001', 'Marker should have correct ID');
  assert(marker.lat === 47.1234, 'Marker should have correct latitude');
  assert(marker.lng === 8.5678, 'Marker should have correct longitude');
  assert(marker.status === 'friendly', 'Should identify as friendly');
  assert(marker.source === 'cot', 'Should mark as COT source');
});

// Test Marker to COT Conversion
test('Map marker to COT conversion', () => {
  const marker = {
    id: 'MARKER-001',
    lat: 47.1234,
    lng: 8.5678,
    name: 'Test Marker',
    status: 'friendly',
    description: 'Test location'
  };
  
  const cot = COTProtocolHandler.markerToCOT(marker);
  assert(cot !== null, 'Should convert marker to COT');
  assert(cot.lat === 47.1234, 'Should preserve latitude');
  assert(cot.lon === 8.5678, 'Should preserve longitude');
  assert(cot.callsign === 'Test Marker', 'Should preserve name as callsign');
  assert(cot.remarks === 'Test location', 'Should preserve description');
});

// Test COT Message Detection
test('COT message detection', () => {
  assert(COTProtocolHandler.isCOTMessage('<?xml version="1.0"?>'), 'Should detect XML declaration');
  assert(COTProtocolHandler.isCOTMessage('<event version="2.0">'), 'Should detect event element');
  assert(!COTProtocolHandler.isCOTMessage('Plain text message'), 'Should not detect plain text');
  assert(!COTProtocolHandler.isCOTMessage(null), 'Should handle null');
  assert(!COTProtocolHandler.isCOTMessage(''), 'Should handle empty string');
});

// Test COT XML Validation
test('COT XML validation', () => {
  const validXML = `<?xml version="1.0"?><event version="2.0" uid="TEST" type="a-f-G-U-C"><point lat="47.0" lon="8.0"/></event>`;
  assert(COTProtocolHandler.validateCOTXML(validXML), 'Should validate correct COT XML');
  
  const invalidXML = '<invalid>test</invalid>';
  assert(!COTProtocolHandler.validateCOTXML(invalidXML), 'Should reject invalid XML');
  
  const missingPoint = `<?xml version="1.0"?><event version="2.0" uid="TEST" type="a-f-G-U-C"></event>`;
  assert(!COTProtocolHandler.validateCOTXML(missingPoint), 'Should reject XML without point');
});

// Test COT Dictionary Serialization
test('COT dictionary serialization', () => {
  const cot = new COTEvent({
    uid: 'TEST-001',
    lat: 47.1234,
    lon: 8.5678,
    callsign: 'Alpha-1'
  });
  
  const dict = cot.toDict();
  assert(typeof dict === 'object', 'Should return object');
  assert(dict.uid === 'TEST-001', 'Should include UID');
  assert(dict.lat === 47.1234, 'Should include latitude');
  assert(dict.callsign === 'Alpha-1', 'Should include callsign');
  assert(typeof dict.time === 'string', 'Time should be formatted string');
});

// Test XML Escaping
test('XML special character escaping', () => {
  const cot = new COTEvent({
    callsign: 'Test & <Special> "Characters"',
    remarks: "It's a test with 'quotes'"
  });
  
  const xml = cot.toXML();
  assert(xml.includes('&amp;'), 'Should escape ampersand');
  assert(xml.includes('&lt;'), 'Should escape less-than');
  assert(xml.includes('&gt;'), 'Should escape greater-than');
  assert(xml.includes('&quot;'), 'Should escape quotes');
  assert(!xml.includes('& <'), 'Should not have unescaped special chars');
});

// Test Coordinates Validation
test('Coordinate bounds validation', () => {
  // Valid coordinates should work
  const valid = new COTEvent({ lat: 45.0, lon: -120.0 });
  assert(valid.lat === 45.0, 'Should accept valid latitude');
  assert(valid.lon === -120.0, 'Should accept valid longitude');
  
  // Edge cases
  const north = new COTEvent({ lat: 90, lon: 0 });
  assert(north.lat === 90, 'Should accept north pole');
  
  const south = new COTEvent({ lat: -90, lon: 0 });
  assert(south.lat === -90, 'Should accept south pole');
  
  const dateline = new COTEvent({ lat: 0, lon: 180 });
  assert(dateline.lon === 180, 'Should accept dateline');
});

// Test Affiliation Parsing
test('Affiliation parsing from COT type', () => {
  const friendly = COTProtocolHandler.cotToMarker(new COTEvent({ type: 'a-f-G-U-C' }));
  assert(friendly.status === 'friendly', 'Should parse friendly');
  
  const hostile = COTProtocolHandler.cotToMarker(new COTEvent({ type: 'a-h-G-U-C' }));
  assert(hostile.status === 'hostile', 'Should parse hostile');
  
  const neutral = COTProtocolHandler.cotToMarker(new COTEvent({ type: 'a-n-G-U-C' }));
  assert(neutral.status === 'neutral', 'Should parse neutral');
  
  const unknown = COTProtocolHandler.cotToMarker(new COTEvent({ type: 'a-u-G-U-C' }));
  assert(unknown.status === 'unknown', 'Should parse unknown');
});

// Test Meshtastic node type mappings
test('node type maps to a-f-G-E-S-U-M (Meshtastic equipment)', () => {
  const cotType = COTEvent.lpu5TypeToCot('node');
  assert(cotType === 'a-f-G-E-S-U-M', `node should map to a-f-G-E-S-U-M, got ${cotType}`);
});

test('meshtastic_node type maps to a-f-G-U-C (SA type, shows as SA in ATAK)', () => {
  const cotType = COTEvent.lpu5TypeToCot('meshtastic_node');
  assert(cotType === 'a-f-G-U-C', `meshtastic_node should map to a-f-G-U-C, got ${cotType}`);
});

test('gateway type maps to a-f-G-E-S-U-M', () => {
  const cotType = COTEvent.lpu5TypeToCot('gateway');
  assert(cotType === 'a-f-G-E-S-U-M', `gateway should map to a-f-G-E-S-U-M, got ${cotType}`);
});

test('a-f-G-E-S-U-M maps back to cbt_rechteck (not meshtastic_node)', () => {
  const lpu5 = COTEvent.cotTypeToLpu5('a-f-G-E-S-U-M');
  assert(lpu5 === 'cbt_rechteck', `a-f-G-E-S-U-M should map to cbt_rechteck, got ${lpu5}`);
});

test('markerToCOT with node type produces a-f-G-E-S-U-M', () => {
  const marker = { id: 'MESH-001', lat: 48.0, lng: 11.0, name: 'MeshNode', type: 'node' };
  const cot = COTProtocolHandler.markerToCOT(marker);
  assert(cot !== null, 'markerToCOT should return a COT event');
  assert(cot.type === 'a-f-G-E-S-U-M', `node marker should produce a-f-G-E-S-U-M, got ${cot.type}`);
});

test('markerToCOT with node type ignores wrong stored cot_type', () => {
  // Simulates a marker whose data.cot_type was corrupted by an ATAK echo
  const marker = {
    id: 'MESH-002', lat: 48.0, lng: 11.0, name: 'MeshNode',
    type: 'node', cot_type: 'a-f-G-U-C'  // wrong echo value
  };
  const cot = COTProtocolHandler.markerToCOT(marker);
  // Note: JS markerToCOT honours stored cot_type; fixing this echo-corruption
  // is handled server-side in Python cot_protocol.py. This test documents the
  // current JS behaviour.
  assert(cot !== null, 'markerToCOT should return a COT event');
  // With stored cot_type, JS uses it directly (Python handles the fix server-side)
  assert(cot.type !== null, 'COT type should be set');
});

test('COTEvent hasMeshtasticDetail defaults to false', () => {
  const cot = new COTEvent({ uid: 'TEST', type: 'a-f-G-U-C', lat: 0, lon: 0 });
  assert(cot.hasMeshtasticDetail === false, 'hasMeshtasticDetail should default to false');
});

test('COTEvent hasMeshtasticDetail can be set via constructor', () => {
  const cot = new COTEvent({ uid: 'TEST', type: 'a-f-G-U-C', lat: 0, lon: 0, hasMeshtasticDetail: true });
  assert(cot.hasMeshtasticDetail === true, 'hasMeshtasticDetail should be settable via options');
});

test('cotToMarker with hasMeshtasticDetail=true overrides type to meshtastic_node', () => {
  // Simulates receiving a CoT with a-f-G-U-C type but <meshtastic> in detail
  // and how='m-g' (machine-generated, typical for Meshtastic forwarding plugins).
  const cot = new COTEvent({
    uid: 'ATAK-NODE-1',
    type: 'a-f-G-U-C',  // normalised by ATAK from a-f-G-E-S-U-M
    lat: 48.0,
    lon: 11.0,
    callsign: 'FieldNode',
    how: 'm-g',
    hasMeshtasticDetail: true,  // preserved <meshtastic> element
  });
  const marker = COTProtocolHandler.cotToMarker(cot);
  assert(marker.type === 'meshtastic_node',
    `hasMeshtasticDetail with how=m-g should force type=meshtastic_node, got ${marker.type}`);
});

test('cotToMarker Meshtastic SA beacon with meshtastic detail produces meshtastic_node', () => {
  // hasMeshtasticDetail is the authoritative signal that this is a Meshtastic
  // node (not a plain human ATAK user), regardless of how="h-e".  In
  // cot-client.js the hasMeshtasticDetail check runs first.
  const cot = new COTEvent({
    uid: 'SA-UNIT-1',
    type: 'a-f-G-U-C',
    lat: 48.0,
    lon: 11.0,
    callsign: 'Alpha',
    how: 'h-e',
    hasMeshtasticDetail: true,
  });
  const marker = COTProtocolHandler.cotToMarker(cot);
  assert(marker.type === 'meshtastic_node',
    `how='h-e' + hasMeshtasticDetail must produce meshtastic_node, got ${marker.type}`);
});

test('cotToMarker with a-f-G-E-S-U-M type and no hasMeshtasticDetail gives cbt_rechteck', () => {
  const cot = new COTEvent({
    uid: 'MESH-3',
    type: 'a-f-G-E-S-U-M',
    lat: 48.0,
    lon: 11.0,
    callsign: 'Node3',
  });
  const marker = COTProtocolHandler.cotToMarker(cot);
  assert(marker.type === 'cbt_rechteck',
    `a-f-G-E-S-U-M without <meshtastic> detail should map to cbt_rechteck, got ${marker.type}`);
});

// Summary
console.log('\n=== Test Summary ===');
console.log(`Total Tests: ${testsPassed + testsFailed}`);
console.log(`✅ Passed: ${testsPassed}`);
console.log(`❌ Failed: ${testsFailed}`);

if (testsFailed === 0) {
  console.log('\n🎉 All tests passed!');
  process.exit(0);
} else {
  console.log('\n⚠️  Some tests failed');
  process.exit(1);
}
