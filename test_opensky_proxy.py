#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import ast
import os
from typing import Any, Dict, Optional
import unittest
from unittest.mock import Mock


API_PATH = os.path.join(os.path.dirname(__file__), "api.py")


def _load_opensky_namespace():
    with open(API_PATH, "r", encoding="utf-8") as fh:
        source = fh.read()
    tree = ast.parse(source, filename=API_PATH)
    selected = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in {"OPENSKY_STATES_URL", "OPENSKY_PROXY_TIMEOUT"}:
                    selected.append(node)
                    break
        elif isinstance(node, ast.FunctionDef) and node.name in {"_build_opensky_bbox_params", "_fetch_opensky_states"}:
            selected.append(node)
    module = ast.Module(body=selected, type_ignores=[])
    namespace = {
        "Dict": Dict,
        "Any": Any,
        "Optional": Optional,
        "requests": type("RequestsStub", (), {"get": Mock()})(),
    }
    exec(compile(module, API_PATH, "exec"), namespace)
    return namespace, source


class TestOpenSkyProxyHelpers(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ns, cls.source = _load_opensky_namespace()

    def test_build_opensky_bbox_params_clamps_and_orders_bounds(self):
        params = self.ns["_build_opensky_bbox_params"](
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

        data = self.ns["_fetch_opensky_states"](lamin=1, lamax=2, requests_get=fake_get)

        self.assertEqual(data["states"], [])
        fake_get.assert_called_once()

    def test_api_declares_flights_proxy_route(self):
        self.assertIn('@app.get("/api/intel/flights")', self.source)


if __name__ == "__main__":
    unittest.main()
