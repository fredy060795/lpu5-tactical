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
    // Very basic XML parsing for COT validation
    return {
      querySelector: (selector) => {
        if (selector === 'parsererror') return null;
        if (selector === 'event') return {
          getAttribute: (attr) => {
            if (attr === 'uid') return 'TEST-001';
            if (attr === 'type') return 'a-f-G-U-C';
            if (attr === 'how') return 'm-g';
            return '';
          },
          querySelector: (sub) => {
            if (sub === 'point') return {
              getAttribute: (a) => {
                if (a === 'lat') return '47.1234';
                if (a === 'lon') return '8.5678';
                if (a === 'hae') return '500';
                return '0';
              }
            };
            if (sub === 'detail') return {
              querySelector: () => null
            };
            return null;
          }
        };
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
