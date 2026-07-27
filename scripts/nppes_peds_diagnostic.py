#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path

import requests

BASE = "https://npiregistry.cms.hhs.gov/api/"
KNOWN_NPIS = [
    "1659722296", "1871574699", "1427340892", "1497016943",
    "1417948837", "1518985134", "1699091355", "1730529652",
]

s = requests.Session()
s.headers.update({"User-Agent": "AVCAI-NPPES-diagnostic/1.0"})


def request(params):
    params = {"version": "2.1", "pretty": "off", **params}
    r = s.get(BASE, params=params, timeout=60)
    return {
        "url": r.url,
        "status": r.status_code,
        "headers": dict(r.headers),
        "json": r.json() if r.headers.get("content-type", "").startswith("application/json") else None,
        "text_head": r.text[:1000],
    }


def compact_result(result):
    return {
        "number": result.get("number"),
        "basic": result.get("basic"),
        "taxonomies": result.get("taxonomies"),
        "addresses": result.get("addresses"),
        "practiceLocations": result.get("practiceLocations"),
    }


out = {"known": {}, "searches": {}}
for npi in KNOWN_NPIS:
    response = request({"number": npi})
    payload = response.get("json") or {}
    out["known"][npi] = {
        "status": response["status"],
        "result_count": payload.get("result_count"),
        "results": [compact_result(x) for x in payload.get("results", [])],
    }

for label, params in {
    "pediatrics_34232_location": {
        "enumeration_type": "NPI-1", "state": "FL", "postal_code": "34232",
        "taxonomy_description": "Pediatrics", "address_purpose": "LOCATION", "limit": 200,
    },
    "pediatrics_34232_primary": {
        "enumeration_type": "NPI-1", "state": "FL", "postal_code": "34232",
        "taxonomy_description": "Pediatrics", "address_purpose": "PRIMARY", "limit": 200,
    },
    "pediatrics_34232_no_purpose": {
        "enumeration_type": "NPI-1", "state": "FL", "postal_code": "34232",
        "taxonomy_description": "Pediatrics", "limit": 200,
    },
    "pediatrics_sarasota_city": {
        "enumeration_type": "NPI-1", "state": "FL", "city": "SARASOTA",
        "taxonomy_description": "Pediatrics", "address_purpose": "LOCATION", "limit": 200,
    },
    "taxonomy_wildcard_34232": {
        "enumeration_type": "NPI-1", "state": "FL", "postal_code": "34232",
        "taxonomy_description": "Pediatr*", "address_purpose": "LOCATION", "limit": 200,
    },
}.items():
    response = request(params)
    payload = response.get("json") or {}
    out["searches"][label] = {
        "status": response["status"],
        "url": response["url"],
        "result_count": payload.get("result_count"),
        "numbers": [x.get("number") for x in payload.get("results", [])],
        "results": [compact_result(x) for x in payload.get("results", [])],
        "text_head": response["text_head"],
    }

Path("output").mkdir(exist_ok=True)
Path("output/nppes_peds_diagnostic.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
print(json.dumps({
    "known_counts": {k: len(v["results"]) for k, v in out["known"].items()},
    "search_counts": {k: v["result_count"] for k, v in out["searches"].items()},
}, indent=2))
