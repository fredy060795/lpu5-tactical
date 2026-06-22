#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
from typing import Any, Dict, Optional
import unittest
from unittest.mock import Mock

from opensky_proxy_utils import build_opensky_bbox_params

API_PATH = os.path.join(os.path.dirname(__file__), "api.py")
LPU5_PATH = os.path.join(os.path.dirname(__file__), "LPU5.py")

def _load_source(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def _load_opensky_fetcher():
    namespace = {
        "build_opensky_bbox_params": build_opensky_bbox_params,
        "requests": type("RequestsStub", (), {"get": Mock()})(),
        "OPENSKY_STATES_URL": "https://opensky-network.org/api/states/all",
        "OPENSKY_PROXY_TIMEOUT": 15,
        "Dict": Dict,
        "Any": Any,
        "Optional": Optional,
    }
    source = _load_source(API_PATH)
    start = source.index("def _fetch_opensky_states(")
    end = source.index("# JWT settings", start)
    exec(source[start:end], namespace)
    return namespace["_fetch_opensky_states"]


class TestOpenSkyProxyHelpers(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fetcher = _load_opensky_fetcher()
        cls.api_source = _load_source(API_PATH)
        with open(LPU5_PATH, "r", encoding="utf-8") as fh:
            cls.lpu5_source = fh.read()

    def test_build_opensky_bbox_params_clamps_and_orders_bounds(self):
        params = build_opensky_bbox_params(
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

        data = self.fetcher(lamin=1, lamax=2, requests_get=fake_get)

        self.assertEqual(data["states"], [])
        fake_get.assert_called_once()

    def test_fetch_opensky_states_rejects_invalid_states_payload(self):
        fake_response = Mock()
        fake_response.json.return_value = {"states": "bad"}
        fake_response.raise_for_status.return_value = None

        with self.assertRaises(ValueError):
            self.fetcher(requests_get=Mock(return_value=fake_response))

    def test_fetch_opensky_states_rejects_non_object_payload(self):
        fake_response = Mock()
        fake_response.json.return_value = []
        fake_response.raise_for_status.return_value = None

        with self.assertRaises(ValueError):
            self.fetcher(requests_get=Mock(return_value=fake_response))

    def test_api_declares_flights_proxy_route(self):
        self.assertIn('@app.get("/api/intel/flights")', self.api_source)
        self.assertIn("build_opensky_bbox_params", self.api_source)
        self.assertIn("build_opensky_bbox_params", self.lpu5_source)


if __name__ == "__main__":
    unittest.main()
