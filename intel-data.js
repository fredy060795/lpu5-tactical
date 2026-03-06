// ─── LPU5 Tactical – Shared Intel Data Module ────────────────────────────────
// Provides static intel data and API fetch helpers so that admin_map.html and
// overview.html can display intel layers directly without requiring a user to
// manually share data from global_Intel.html.
(function () {
  'use strict';

  var I = {};

  // ── Static Data ─────────────────────────────────────────────────────────────
  I.CONFLICT_ZONES = [
    { region: 'Ukraine',                   lat: 49.0,  lon:  32.0, type: 'War',       detail: 'Russia-Ukraine War \u2013 active front lines',              intensity: 'high' },
    { region: 'Gaza / Israel',             lat: 31.4,  lon:  34.3, type: 'War',       detail: 'Gaza conflict \u2013 humanitarian crisis',                  intensity: 'high' },
    { region: 'Sudan',                     lat: 15.6,  lon:  32.5, type: 'Civil War', detail: 'SAF vs. RSF \u2013 humanitarian emergency',                 intensity: 'high' },
    { region: 'Myanmar',                   lat: 19.7,  lon:  96.1, type: 'Civil War', detail: 'Military junta vs. resistance groups',                       intensity: 'high' },
    { region: 'Sahel (Mali/Niger/Burkina)',lat: 14.0,  lon:  -2.0, type: 'Terrorism', detail: 'JNIM/ISGS activity \u2013 unstable region',                 intensity: 'mid'  },
    { region: 'Syria',                     lat: 35.0,  lon:  38.0, type: 'Conflict',  detail: 'Fragmented conflict \u2013 multiple armed actors',          intensity: 'mid'  },
    { region: 'Somalia',                   lat:  5.1,  lon:  46.2, type: 'Terrorism', detail: 'Al-Shabaab activity \u2013 ongoing AMISOM ops',             intensity: 'mid'  },
    { region: 'Haiti',                     lat: 18.9,  lon: -72.3, type: 'Crisis',    detail: 'Gang control \u2013 state collapse \u2013 Kenyan MSS',     intensity: 'mid'  },
    { region: 'Iraq / Syria',              lat: 34.0,  lon:  43.0, type: 'Terrorism', detail: 'ISIS remnants \u2013 active cells',                         intensity: 'low'  },
    { region: 'Ethiopia (Amhara)',         lat: 10.5,  lon:  37.5, type: 'Conflict',  detail: 'Government vs. Amhara militias \u2013 Fano forces',         intensity: 'mid'  },
    { region: 'DR Congo',                  lat: -1.5,  lon:  29.5, type: 'Conflict',  detail: 'M23 / FDLR operations \u2013 eastern DRC',                  intensity: 'high' },
    { region: 'Taiwan Strait',             lat: 24.0,  lon: 120.5, type: 'Tensions',  detail: 'PLA military exercises / grey-zone operations',             intensity: 'low'  },
    { region: 'Red Sea / Houthi',          lat: 14.5,  lon:  43.5, type: 'Conflict',  detail: 'Houthi missile/drone attacks on shipping lanes',            intensity: 'high' },
    { region: 'Kosovo',                    lat: 42.6,  lon:  21.0, type: 'Tensions',  detail: 'Serbia-Kosovo tensions \u2013 KFOR presence',               intensity: 'low'  },
    { region: 'Venezuela / Guyana',        lat:  6.8,  lon: -61.5, type: 'Tensions',  detail: 'Essequibo territorial dispute \u2013 military buildup',     intensity: 'mid'  }
  ];

  I.MIL_BASES = [
    { name: 'Ramstein AB',          country: 'Germany',     lat:  49.44, lon:   7.60 },
    { name: 'Diego Garcia',         country: 'UK/US',       lat:  -7.31, lon:  72.41 },
    { name: 'Kadena AB',            country: 'Japan',       lat:  26.35, lon: 127.77 },
    { name: 'Al Udeid AB',          country: 'Qatar',       lat:  25.12, lon:  51.31 },
    { name: 'Naval Station Rota',   country: 'Spain',       lat:  36.65, lon:  -6.33 },
    { name: 'Aviano AB',            country: 'Italy',       lat:  46.03, lon:  12.60 },
    { name: 'Incirlik AB',          country: 'Turkey',      lat:  37.00, lon:  35.43 },
    { name: 'Camp Lemonnier',       country: 'Djibouti',    lat:  11.55, lon:  43.15 },
    { name: 'Andersen AFB',         country: 'Guam',        lat:  13.58, lon: 144.93 },
    { name: 'Yokota AB',            country: 'Japan',       lat:  35.75, lon: 139.35 },
    { name: 'Osan AB',              country: 'South Korea', lat:  37.08, lon: 127.03 },
    { name: 'RAF Lakenheath',       country: 'UK',          lat:  52.41, lon:   0.56 },
    { name: 'Spangdahlem AB',       country: 'Germany',     lat:  49.97, lon:   6.69 },
    { name: 'NAS Norfolk',          country: 'USA',         lat:  36.95, lon: -76.33 },
    { name: 'Pearl Harbor-Hickam',  country: 'USA',         lat:  21.35, lon:-157.96 },
    { name: 'NAS Sigonella',        country: 'Italy',       lat:  37.40, lon:  14.92 },
    { name: 'Camp Humphreys',       country: 'South Korea', lat:  36.96, lon: 127.02 },
    { name: 'Al-Dhafra AB',         country: 'UAE',         lat:  24.24, lon:  54.55 },
    { name: 'Ali Al Salem AB',      country: 'Kuwait',      lat:  29.46, lon:  47.52 },
    { name: 'MK AB Romania',        country: 'Romania',     lat:  44.36, lon:  28.49 },
    { name: 'Lajes Field (Azores)', country: 'Portugal',    lat:  38.76, lon: -27.09 },
    { name: 'Darwin Military Area', country: 'Australia',   lat: -12.43, lon: 130.87 },
    { name: 'Grafenwoehr Training', country: 'Germany',     lat:  49.70, lon:  11.92 },
    { name: 'RAF Mildenhall',       country: 'UK',          lat:  52.36, lon:   0.49 },
    { name: 'Tyndall AFB',          country: 'USA',         lat:  30.07, lon: -85.58 }
  ];

  I.NAVAL_UNITS = [
    { id: 'cvn78',  name: 'USS Gerald R. Ford (CVN-78)',        cls: 'Aircraft Carrier',   lat:  47.0, lon:  -30.0 },
    { id: 'cvn69',  name: 'USS Dwight D. Eisenhower (CVN-69)', cls: 'Aircraft Carrier',   lat:  15.0, lon:   43.5 },
    { id: 'cvn70',  name: 'USS Carl Vinson (CVN-70)',           cls: 'Aircraft Carrier',   lat:  20.0, lon:  150.0 },
    { id: 'r08',    name: 'HMS Queen Elizabeth (R08)',           cls: 'Aircraft Carrier',   lat:  56.0, lon:    2.0 },
    { id: 'r91',    name: 'Charles de Gaulle (R91)',             cls: 'Aircraft Carrier',   lat:  40.0, lon:   10.0 },
    { id: 'cv16',   name: 'Liaoning (CV-16)',                   cls: 'Aircraft Carrier',   lat:  18.0, lon:  115.0 },
    { id: 'cv17',   name: 'Shandong (CV-17)',                   cls: 'Aircraft Carrier',   lat:  32.0, lon:  123.0 },
    { id: 'cv18',   name: 'Fujian (CV-18)',                     cls: 'Aircraft Carrier',   lat:  27.0, lon:  122.0 },
    { id: 'ddh183', name: 'JS Izumo (DDH-183)',                 cls: 'Helicopter Carrier', lat:  28.0, lon:  135.0 },
    { id: 'r11',    name: 'INS Vikrant (R11)',                  cls: 'Aircraft Carrier',   lat:  15.0, lon:   68.0 },
    { id: 'l02',    name: 'HMAS Canberra (L02)',                cls: 'Amphibious',         lat: -22.0, lon:  155.0 },
    { id: 'lha6',   name: 'USS America (LHA-6)',                cls: 'Amphibious',         lat:  38.0, lon:   20.0 },
    { id: 'lhd5',   name: 'USS Bataan (LHD-5)',                 cls: 'Amphibious',         lat:  15.0, lon:  -70.0 },
    { id: 'cvn76',  name: 'USS Ronald Reagan (CVN-76)',         cls: 'Aircraft Carrier',   lat:  36.0, lon:  132.0 },
    { id: 'l9014',  name: 'FS Tonnerre (L9014)',                cls: 'Amphibious',         lat:  45.0, lon:  -10.0 }
  ];

  // ── API configuration ─────────────────────────────────────────────────────────
  I.MIL_REGEX   = /^(REACH|SENTRY|DARK|DUKE|NAVY|GAF|RRR|FAF|NATO|BOXER|COBRA|VIPER|GHOST|HAWK|EAGLE|TALON|REAPER|RANGER|SHADOW|IRON|STEEL|ATLAS|TOPGUN|MAGIC|HAVOC|STORM|BLADE|NOBLE|ASCOT|TARTAN|RCH|CNV|VENUS|MULE)/i;
  I.EQ_URL      = 'https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_day.geojson';
  I.FL_URL_BASE = 'https://opensky-network.org/api/states/all';

  // ── Helpers ───────────────────────────────────────────────────────────────────
  function _magColor(mag) {
    return mag >= 6 ? '#e74c3c' : mag >= 4.5 ? '#e67e22' : mag >= 2.5 ? '#f39c12' : '#2ecc71';
  }
  function _intensityColor(i) {
    return i === 'high' ? '#e74c3c' : i === 'mid' ? '#e67e22' : '#f1c40f';
  }

  // ── API fetch functions ───────────────────────────────────────────────────────

  // Fetches earthquake markers from USGS.
  // onSuccess(markers [])   onError(err) – both optional
  I.fetchEarthquakes = function (onSuccess, onError) {
    fetch(I.EQ_URL)
      .then(function (r) { return r.json(); })
      .then(function (data) {
        var markers = (data.features || []).map(function (f) {
          var p = f.properties, coords = f.geometry && f.geometry.coordinates;
          if (!coords || p.mag == null) return null;
          return {
            type: 'earthquake',
            title: 'M' + p.mag.toFixed(1) + ' \u2013 ' + (p.place || 'Unknown'),
            lat: coords[1], lon: coords[0],
            color: _magColor(p.mag), icon: 'fa-circle-exclamation'
          };
        }).filter(Boolean);
        if (onSuccess) onSuccess(markers);
      })
      .catch(function (e) { if (onError) onError(e); });
  };

  // Fetches global flights from OpenSky and splits into civil and military.
  // onSuccess(civilMarkers [], milMarkers [])   onError(err) – both optional
  I.fetchFlights = function (onSuccess, onError) {
    fetch(I.FL_URL_BASE, { signal: AbortSignal.timeout(30000) })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        var fl = [], mil = [];
        (data.states || []).forEach(function (s) {
          var cs = (s[1] || s[0] || '?').trim(), origin = s[2], lon = s[5], lat = s[6];
          if (lat == null || lon == null) return;
          var title = cs + (origin ? ' \u2013 ' + origin : '');
          if (I.MIL_REGEX.test(cs)) {
            mil.push({ type: 'milaircraft', title: title, lat: lat, lon: lon, color: '#e74c3c', icon: 'fa-fighter-jet' });
          } else {
            fl.push({ type: 'flight', title: title, lat: lat, lon: lon, color: '#f1c40f', icon: 'fa-plane' });
          }
        });
        if (onSuccess) onSuccess(fl, mil);
      })
      .catch(function (e) { if (onError) onError(e); });
  };

  // ── Static source helpers ─────────────────────────────────────────────────────
  I.getConflictMarkers = function () {
    return I.CONFLICT_ZONES.map(function (z) {
      return { type: 'conflict', title: z.region, lat: z.lat, lon: z.lon,
               color: _intensityColor(z.intensity), icon: 'fa-fire', detail: z.type };
    });
  };

  I.getMilBaseMarkers = function () {
    return I.MIL_BASES.map(function (b) {
      return { type: 'milbase', title: b.name, lat: b.lat, lon: b.lon,
               color: '#e67e22', icon: 'fa-shield-halved', detail: b.country };
    });
  };

  I.getNavalMarkers = function () {
    return I.NAVAL_UNITS.map(function (u) {
      return { type: 'naval', title: u.name, lat: u.lat, lon: u.lon,
               color: '#1abc9c', icon: 'fa-anchor', detail: u.cls };
    });
  };

  window.LPU5Intel = I;
}());
