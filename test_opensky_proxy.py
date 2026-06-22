#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import unittest
from unittest.mock import Mock

from fastapi.testclient import TestClient

import api


class TestOpenSkyProxyHelpers(unittest.TestCase):
    def test_build_opensky_bbox_params_clamps_and_orders_bounds(self):
        params = api._build_opensky_bbox_params(
            lamin=95,
            lomin=20,
            lamax=-95,
            lomax=-220,
        )
        self.assertEqual(
            params,
            {
                "lamin": "-90.0000",
                "lamax": "90.0000",
                "lomin": "-180.0000",
                "lomax": "20.0000",
            },
        )

    def test_fetch_opensky_states_normalizes_missing_states(self):
        fake_response = Mock()
        fake_response.json.return_value = {"time": 123}
        fake_response.raise_for_status.return_value = None
        fake_get = Mock(return_value=fake_response)

        data = api._fetch_opensky_states(lamin=1, lamax=2, requests_get=fake_get)

        self.assertEqual(data["states"], [])
        fake_get.assert_called_once()


class TestOpenSkyProxyEndpoint(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(api.app)

    def test_intel_flights_endpoint_proxies_upstream_payload(self):
        fake_payload = {"time": 42, "states": [["icao", "TEST123", "DE", None, None, 7.0, 51.0]]}
        original = api._fetch_opensky_states
        api._fetch_opensky_states = Mock(return_value=fake_payload)
        try:
            response = self.client.get("/api/intel/flights?lamin=10&lomin=20&lamax=30&lomax=40")
        finally:
            api._fetch_opensky_states = original

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), fake_payload)


if __name__ == "__main__":
    unittest.main()
