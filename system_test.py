import requests
import time
import json
import logging
import sys

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("system-test")

BASE_URL = "https://127.0.0.1:8001"
USERNAME = "administrator"
PASSWORD = "password"

# Disable SSL verification for self-signed certs
requests.packages.urllib3.disable_warnings()

def run_test():
    logger.info("Starting System Test...")
    
    # 1. Health Check
    try:
        resp = requests.get(f"{BASE_URL}/api/health", verify=False, timeout=5)
        if resp.status_code == 200:
            logger.info("✅ Health check passed")
        else:
            logger.error(f"❌ Health check failed: {resp.status_code}")
            return False
    except Exception as e:
        logger.error(f"❌ Health check error: {e}")
        return False

    # 2. Login
    token = None
    try:
        login_data = {"username": USERNAME, "password": PASSWORD}
        resp = requests.post(f"{BASE_URL}/api/login_user", json=login_data, verify=False, timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            token = data.get("token")
            logger.info("✅ Login successful")
        else:
            logger.error(f"❌ Login failed: {resp.status_code} - {resp.text}")
            return False
    except Exception as e:
        logger.error(f"❌ Login error: {e}")
        return False

    headers = {"Authorization": f"Bearer {token}"}

    # 3. Get User Info
    try:
        resp = requests.get(f"{BASE_URL}/api/me", headers=headers, verify=False, timeout=5)
        if resp.status_code == 200:
            logger.info("✅ Get /api/me successful")
        else:
            logger.error(f"❌ Get /api/me failed: {resp.status_code}")
            return False
    except Exception as e:
        logger.error(f"❌ Get /api/me error: {e}")
        return False

    # 4. Marker Data Exchange
    marker_id = None
    try:
        # Create Marker
        marker_data = {
            "lat": 52.52,
            "lng": 13.40,
            "name": "Test Marker System Test",
            "type": "friendly",
            "color": "#00ff00"
        }
        resp = requests.post(f"{BASE_URL}/api/map_markers", json=marker_data, headers=headers, verify=False, timeout=5)
        if resp.status_code == 200:
            res_data = resp.json()
            marker_id = res_data.get("marker", {}).get("id")
            logger.info(f"✅ Marker created: {marker_id}")
        else:
            logger.error(f"❌ Marker creation failed: {resp.status_code} - {resp.text}")
            return False

        # Fetch Markers
        resp = requests.get(f"{BASE_URL}/api/map_markers", headers=headers, verify=False, timeout=5)
        if resp.status_code == 200:
            markers = resp.json()
            if any(m.get("id") == marker_id for m in markers if isinstance(m, dict)):
                logger.info("✅ Marker found in list")
            else:
                # Might be a list or a dict with 'markers' key depending on endpoint
                logger.warning(f"Marker {marker_id} not found in list immediately, retrying...")
                time.sleep(1)
                resp = requests.get(f"{BASE_URL}/api/map_markers", headers=headers, verify=False, timeout=5)
                markers = resp.json()
                # Check if it's a list or a dict
                marker_list = markers if isinstance(markers, list) else markers.get("markers", [])
                if any(m.get("id") == marker_id for m in marker_list):
                    logger.info("✅ Marker found in list on retry")
                else:
                    logger.error("❌ Marker not found in list")
                    return False
        else:
            logger.error(f"❌ Fetch markers failed: {resp.status_code}")
            return False

    except Exception as e:
        logger.error(f"❌ Data exchange error: {e}")
        return False

    # 5. Stability Monitoring (Wait 30 seconds)
    logger.info("Monitoring stability for 30 seconds...")
    start_time = time.time()
    while time.time() - start_time < 30:
        try:
            resp = requests.get(f"{BASE_URL}/api/health", verify=False, timeout=2)
            if resp.status_code != 200:
                logger.error(f"❌ Health check failed during monitoring: {resp.status_code}")
                return False
        except Exception as e:
            logger.error(f"❌ Connection lost during monitoring: {e}")
            return False
        time.sleep(5)
    
    logger.info("✅ Stability monitor passed (30s)")

    # 6. Cleanup
    if marker_id:
        try:
            resp = requests.delete(f"{BASE_URL}/api/map_markers/{marker_id}", headers=headers, verify=False, timeout=5)
            if resp.status_code == 200:
                logger.info("✅ Test marker deleted")
            else:
                logger.warning(f"⚠️ Failed to delete test marker: {resp.status_code}")
        except Exception as e:
            logger.error(f"❌ Cleanup error: {e}")

    logger.info("System Test COMPLETED SUCCESSFULLY")
    return True

if __name__ == "__main__":
    success = run_test()
    if not success:
        sys.exit(1)
    sys.exit(0)
