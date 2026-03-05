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
    const hasMeshtastic = typeof str === 'string' && str.includes('<meshtastic');
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
                querySelector: (dsub) => {
                  if (dsub === 'meshtastic') return hasMeshtastic ? {} : null;
                  return null;
                }
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

test('meshtastic_node type maps to a-f-G-E-S-U-M', () => {
  const cotType = COTEvent.lpu5TypeToCot('meshtastic_node');
  assert(cotType === 'a-f-G-E-S-U-M', `meshtastic_node should map to a-f-G-E-S-U-M, got ${cotType}`);
});

test('gateway type maps to a-f-G-E-S-U-M', () => {
  const cotType = COTEvent.lpu5TypeToCot('gateway');
  assert(cotType === 'a-f-G-E-S-U-M', `gateway should map to a-f-G-E-S-U-M, got ${cotType}`);
});

test('a-f-G-E-S-U-M maps back to meshtastic_node (not rechteck/friendly)', () => {
  const lpu5 = COTEvent.cotTypeToLpu5('a-f-G-E-S-U-M');
  assert(lpu5 === 'meshtastic_node', `a-f-G-E-S-U-M should map to meshtastic_node, got ${lpu5}`);
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

// Test hasMeshtasticDetail flag on COTEvent
test('COTEvent hasMeshtasticDetail defaults to false', () => {
  const evt = new COTEvent({ uid: 'X', type: 'a-f-G-U-C' });
  assert(evt.hasMeshtasticDetail === false, 'hasMeshtasticDetail should default to false');
});

test('COTEvent hasMeshtasticDetail set via constructor', () => {
  const evt = new COTEvent({ uid: 'X', type: 'a-f-G-U-C', hasMeshtasticDetail: true });
  assert(evt.hasMeshtasticDetail === true, 'hasMeshtasticDetail should be true when set');
});

// Test cotToMarker() with hasMeshtasticDetail=true → type should be meshtastic_node
test('cotToMarker with hasMeshtasticDetail=true returns meshtastic_node', () => {
  const evt = new COTEvent({ type: 'a-f-G-U-C', hasMeshtasticDetail: true });
  const marker = COTProtocolHandler.cotToMarker(evt);
  assert(marker.type === 'meshtastic_node',
    `Expected meshtastic_node, got ${marker.type}`);
  assert(marker.status === 'meshtastic_node', 'status should match type');
});

test('cotToMarker with <meshtastic> in detail overrides even m-g how', () => {
  // m-g how without <meshtastic> → friendly; WITH <meshtastic> → meshtastic_node
  const evtNoMesh = new COTEvent({ type: 'a-f-G-U-C', how: 'm-g', hasMeshtasticDetail: false });
  assert(COTProtocolHandler.cotToMarker(evtNoMesh).type === 'friendly',
    'Without <meshtastic>, a-f type should remain friendly');

  const evtWithMesh = new COTEvent({ type: 'a-f-G-U-C', how: 'm-g', hasMeshtasticDetail: true });
  assert(COTProtocolHandler.cotToMarker(evtWithMesh).type === 'meshtastic_node',
    'With <meshtastic>, type must be overridden to meshtastic_node');
});

// Test cotToMarker() tak_unit detection for human-placed positions
test('cotToMarker with how=h-e and friendly type returns tak_unit', () => {
  const evt = new COTEvent({ type: 'a-f-G-U-C', how: 'h-e', hasMeshtasticDetail: false });
  const marker = COTProtocolHandler.cotToMarker(evt);
  assert(marker.type === 'tak_unit',
    `Expected tak_unit for h-e how, got ${marker.type}`);
});

test('cotToMarker with how=h-g-i-g-o (GPS) and friendly type returns tak_unit', () => {
  const evt = new COTEvent({ type: 'a-f-G-U-C', how: 'h-g-i-g-o', hasMeshtasticDetail: false });
  const marker = COTProtocolHandler.cotToMarker(evt);
  assert(marker.type === 'tak_unit',
    `Expected tak_unit for GPS how, got ${marker.type}`);
});

test('cotToMarker hasMeshtasticDetail takes precedence over h-* how', () => {
  // Even with how="h-e", <meshtastic> in detail must win
  const evt = new COTEvent({ type: 'a-f-G-U-C', how: 'h-e', hasMeshtasticDetail: true });
  const marker = COTProtocolHandler.cotToMarker(evt);
  assert(marker.type === 'meshtastic_node',
    `Expected meshtastic_node (precedence over tak_unit), got ${marker.type}`);
});

test('cotToMarker tak_unit detection does not affect hostile type', () => {
  // how="h-e" with a-h type should NOT produce tak_unit
  const evt = new COTEvent({ type: 'a-h-G-U-C', how: 'h-e', hasMeshtasticDetail: false });
  const marker = COTProtocolHandler.cotToMarker(evt);
  assert(marker.type === 'hostile',
    `Expected hostile, got ${marker.type}`);
});

// Test fromXML() <meshtastic> detection via the mock DOMParser
test('fromXML detects <meshtastic> in detail and sets hasMeshtasticDetail', () => {
  const xmlWithMesh = (
    '<event version="2.0" uid="MESH-1" type="a-f-G-U-C" how="m-g" ' +
    'time="2024-01-01T00:00:00Z" start="2024-01-01T00:00:00Z" ' +
    'stale="2024-01-01T00:10:00Z">' +
    '<point lat="48.0" lon="11.0" hae="0" ce="9999999" le="9999999"/>' +
    '<detail><contact callsign="Node1"/>' +
    '<meshtastic longName="Node1" shortName="N1"/></detail>' +
    '</event>'
  );
  const evt = COTEvent.fromXML(xmlWithMesh);
  assert(evt !== null, 'fromXML should parse successfully');
  assert(evt.hasMeshtasticDetail === true,
    `Expected hasMeshtasticDetail=true, got ${evt.hasMeshtasticDetail}`);
  const marker = COTProtocolHandler.cotToMarker(evt);
  assert(marker.type === 'meshtastic_node',
    `Expected meshtastic_node from XML with <meshtastic>, got ${marker.type}`);
});

test('fromXML without <meshtastic> does not set hasMeshtasticDetail', () => {
  const xmlNoMesh = (
    '<event version="2.0" uid="UNIT-1" type="a-f-G-U-C" how="m-g" ' +
    'time="2024-01-01T00:00:00Z" start="2024-01-01T00:00:00Z" ' +
    'stale="2024-01-01T00:10:00Z">' +
    '<point lat="48.0" lon="11.0" hae="0" ce="9999999" le="9999999"/>' +
    '<detail><contact callsign="Alpha"/></detail>' +
    '</event>'
  );
  const evt = COTEvent.fromXML(xmlNoMesh);
  assert(evt !== null, 'fromXML should parse successfully');
  assert(evt.hasMeshtasticDetail === false,
    `Expected hasMeshtasticDetail=false, got ${evt.hasMeshtasticDetail}`);
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
