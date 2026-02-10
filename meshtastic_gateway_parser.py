#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Meshtastic Gateway Parser Module
Extracted from main_app.py for API integration
Parses Meshtastic node objects and extracts standardized data
"""

from typing import Dict, Any, Optional, Tuple


def parse_meshtastic_node(node: Dict[str, Any]) -> Dict[str, Any]:
    """
    Parse a meshtastic node object and extract:
    - UID, callsign, position (lat/lon), altitude
    - GPS validity status
    
    Args:
        node: Dictionary containing Meshtastic node data
        
    Returns:
        Dictionary with standardized node data:
        {
            'id': str,              # Unique identifier (e.g., 'ID-12345678')
            'callsign': str,        # Display name or short name
            'latitude': float,      # Latitude or None
            'longitude': float,     # Longitude or None
            'altitude': float,      # Altitude in meters
            'has_gps': bool,        # True if valid GPS coordinates
            'raw_data': dict        # Original node data for debugging
        }
    """
    user = node.get('user', {})
    pos = node.get('position', {})
    
    # Extract UID - prioritize user.id, fallback to node.num
    raw_uid = user.get('id')
    if not raw_uid:
        num = node.get('num')
        if num:
            raw_uid = f"!{num:08x}"
        else:
            raw_uid = "!unknown"
    
    # Format UID (remove '!' prefix and add 'ID-' prefix)
    uid = raw_uid.replace('!', 'ID-')
    
    # Extract callsign - prioritize longName, fallback to shortName, then UID
    callsign = user.get('longName') or user.get('shortName') or uid
    
    # Extract position data
    # Priority: integer telemetry (latitude_i/longitude_i with 1e-7 conversion) -> float -> None
    lat_i = pos.get('latitude_i')
    lon_i = pos.get('longitude_i')
    lat_f = pos.get('latitude')
    lon_f = pos.get('longitude')
    
    final_lat = None
    final_lon = None
    is_real = False
    
    # Check integer coordinates first (telemetry format - more precise)
    if lat_i is not None and lon_i is not None and lat_i != 0:
        final_lat = lat_i * 1e-7
        final_lon = lon_i * 1e-7
        is_real = True
    # Fallback to float coordinates
    elif lat_f is not None and lon_f is not None and lat_f != 0:
        final_lat = lat_f
        final_lon = lon_f
        is_real = True
    
    # Extract altitude
    alt = pos.get('altitude', 0) or 0
    
    return {
        'id': uid,
        'callsign': callsign,
        'latitude': final_lat,
        'longitude': final_lon,
        'altitude': alt,
        'has_gps': is_real,
        'raw_data': node  # Keep original for debugging
    }


def validate_node_for_import(parsed_node: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
    """
    Validate if a parsed node has sufficient data for import.
    
    Args:
        parsed_node: Output from parse_meshtastic_node()
        
    Returns:
        Tuple of (is_valid, error_reason)
        - is_valid: True if node can be imported
        - error_reason: String describing why node is invalid, or None if valid
    """
    if not parsed_node.get('has_gps'):
        return False, 'No valid GPS coordinates'
    
    if parsed_node.get('latitude') is None or parsed_node.get('longitude') is None:
        return False, 'Missing latitude or longitude'
    
    # Check for reasonable coordinate ranges
    lat = parsed_node.get('latitude')
    lon = parsed_node.get('longitude')
    
    if not (-90 <= lat <= 90):
        return False, f'Invalid latitude: {lat}'
    
    if not (-180 <= lon <= 180):
        return False, f'Invalid longitude: {lon}'
    
    return True, None


if __name__ == '__main__':
    # Unit tests
    print("Testing Meshtastic Gateway Parser...")
    
    # Test case 1: Node with integer coordinates
    test_node_1 = {
        'num': 123456789,
        'user': {
            'id': '!12345678',
            'longName': 'Test Node 1',
            'shortName': 'TN1'
        },
        'position': {
            'latitude_i': 473977854,  # 47.3977854
            'longitude_i': 84029310,  # 8.4029310
            'altitude': 500
        }
    }
    
    result_1 = parse_meshtastic_node(test_node_1)
    print(f"\nTest 1: {result_1}")
    assert result_1['id'] == 'ID-12345678'
    assert result_1['callsign'] == 'Test Node 1'
    assert abs(result_1['latitude'] - 47.3977854) < 0.0001
    assert result_1['has_gps'] is True
    
    # Test case 2: Node with float coordinates
    test_node_2 = {
        'num': 987654321,
        'user': {
            'id': '!87654321',
            'shortName': 'TN2'
        },
        'position': {
            'latitude': 51.5074,
            'longitude': -0.1278,
            'altitude': 100
        }
    }
    
    result_2 = parse_meshtastic_node(test_node_2)
    print(f"\nTest 2: {result_2}")
    assert result_2['id'] == 'ID-87654321'
    assert result_2['callsign'] == 'TN2'
    assert result_2['latitude'] == 51.5074
    assert result_2['has_gps'] is True
    
    # Test case 3: Node without GPS
    test_node_3 = {
        'num': 111111111,
        'user': {
            'longName': 'No GPS Node'
        },
        'position': {}
    }
    
    result_3 = parse_meshtastic_node(test_node_3)
    print(f"\nTest 3: {result_3}")
    assert result_3['has_gps'] is False
    assert result_3['latitude'] is None
    
    # Test validation
    is_valid, reason = validate_node_for_import(result_1)
    assert is_valid is True
    
    is_valid, reason = validate_node_for_import(result_3)
    assert is_valid is False
    assert 'GPS' in reason
    
    print("\n✅ All tests passed!")
