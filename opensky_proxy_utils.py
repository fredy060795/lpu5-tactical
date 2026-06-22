#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from typing import Dict, Optional


def build_opensky_bbox_params(
    lamin: Optional[float] = None,
    lomin: Optional[float] = None,
    lamax: Optional[float] = None,
    lomax: Optional[float] = None,
) -> Dict[str, str]:
    params: Dict[str, str] = {}
    bounds = {
        "lamin": (-90.0, 90.0, lamin),
        "lamax": (-90.0, 90.0, lamax),
        "lomin": (-180.0, 180.0, lomin),
        "lomax": (-180.0, 180.0, lomax),
    }
    for key, (lower, upper, value) in bounds.items():
        if value is None:
            continue
        params[key] = f"{max(lower, min(upper, float(value))):.4f}"
    if "lamin" in params and "lamax" in params and float(params["lamin"]) > float(params["lamax"]):
        params["lamin"], params["lamax"] = params["lamax"], params["lamin"]
    if "lomin" in params and "lomax" in params and float(params["lomin"]) > float(params["lomax"]):
        params["lomin"], params["lomax"] = params["lomax"], params["lomin"]
    return params
