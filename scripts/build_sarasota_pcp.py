#!/usr/bin/env python3
"""Build a public-data directory of primary-care physicians in Sarasota County, Florida.

Sources:
- NPPES/NPI Registry API v2.1 (provider identity, taxonomy, practice address, phone, NPI)
- CMS Doctors and Clinicians national data API (Medicare listing, facility/group, group size)
- U.S. Census Geocoder (county verification)

This script deliberately distinguishes public payer indicators from confirmed current insurance-network participation.
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import requests

NPPES_API = "https://npiregistry.cms.hhs.gov/api/"
CMS_API = "https://data.cms.gov/provider-data/api/1/datastore/query/mj5m-pzi6/0"
CENSUS_BATCH = "https://geocoding.geo.census.gov/geocoder/geographies/addressbatch"
OUTPUT_DIR = Path("output")

# Physician taxonomy codes included in the requested primary-care scope.
PRIMARY_TAXONOMIES = {
    "207Q00000X": ("Family Medicine", "Adult & All-Ages Primary Care"),
    "207R00000X": ("Internal Medicine", "Adult Primary Care"),
    "208000000X": ("Pediatrics", "Pediatric Primary Care"),
    "208D00000X": ("General Practice", "Adult & All-Ages Primary Care"),
    "207QG0300X": ("Family Medicine - Geriatric Medicine", "Adult/Geriatric Primary Care"),
    "207RG0300X": ("Internal Medicine - Geriatric Medicine", "Adult/Geriatric Primary Care"),
    "207QA0505X": ("Family Medicine - Adult Medicine", "Adult Primary Care"),
    "207QA0000X": ("Family Medicine - Adolescent Medicine", "Mixed/Adolescent Primary Care"),
    "2080A0000X": ("Pediatrics - Adolescent Medicine", "Pediatric/Adolescent Primary Care"),
    "207RA0000X": ("Internal Medicine - Adolescent Medicine", "Mixed/Adolescent Primary Care"),
}

NPPES_SEARCH_TERMS = [
    "Family Medicine",
    "Internal Medicine",
    "Pediatrics",
    "General Practice",
    "Geriatric Medicine",
    "Adolescent Medicine",
]

CMS_CITIES = [
    "SARASOTA",
    "VENICE",
    "NORTH PORT",
    "NOKOMIS",
    "OSPREY",
    "ENGLEWOOD",
    "LONGBOAT KEY",
    "LAUREL",
    "LAKEWOOD RANCH",
    "WELLEN PARK",
]

CMS_PRIMARY_SPECIALTIES = {
    "FAMILY PRACTICE",
    "FAMILY MEDICINE",
    "INTERNAL MEDICINE",
    "GENERAL PRACTICE",
    "PEDIATRIC MEDICINE",
    "PEDIATRICS",
    "GERIATRIC MEDICINE",
}

# ZIPs treated as Sarasota-only fallbacks when the Census address geocoder cannot match.
# Cross-county ZIPs (e.g., 34223, 34224, 34228, 34240, 34243, 34251) require an address match.
SARASOTA_ONLY_ZIPS = {
    "34229",
    "34230",
    "34231",
    "34232",
    "34233",
    "34234",
    "34235",
    "34236",
    "34237",
    "34238",
    "34239",
    "34241",
    "34242",
    "34249",
    "34272",
    "34274",
    "34275",
    "34276",
    "34277",
    "34278",
    "34284",
    "34285",
    "34286",
    "34287",
    "34288",
    "34289",
    "34290",
    "34291",
    "34292",
    "34293",
    "34295",
}

S = requests.Session()
S.headers.update(
    {
        "User-Agent": "AVCAI-Sarasota-PCP-Public-Data-Directory/1.0 (public-data research)",
        "Accept": "application/json,text/plain,*/*",
    }
)


def log(message: str) -> None:
    print(f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] {message}", flush=True)


def get_json(url: str, params: dict[str, Any] | None = None, *, attempts: int = 6) -> Any:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            response = S.get(url, params=params, timeout=90)
            if response.status_code in {429, 500, 502, 503, 504}:
                raise requests.HTTPError(f"transient HTTP {response.status_code}", response=response)
            response.raise_for_status()
            return response.json()
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt == attempts:
                break
            delay = min(30, 2**attempt)
            log(f"Retrying {url} after {exc!r} ({delay}s)")
            time.sleep(delay)
    raise RuntimeError(f"Failed to retrieve {url}: {last_error}")


def post_with_retry(url: str, *, files: dict[str, Any], data: dict[str, Any], attempts: int = 5) -> requests.Response:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            response = S.post(url, files=files, data=data, timeout=180)
            if response.status_code in {429, 500, 502, 503, 504}:
                raise requests.HTTPError(f"transient HTTP {response.status_code}", response=response)
            response.raise_for_status()
            return response
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt == attempts:
                break
            delay = min(30, 2**attempt)
            log(f"Retrying Census geocoder after {exc!r} ({delay}s)")
            time.sleep(delay)
    raise RuntimeError(f"Failed to retrieve Census geocoder response: {last_error}")


def clean(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def upper_clean(value: Any) -> str:
    return clean(value).upper()


def zip5(value: Any) -> str:
    match = re.search(r"\d{5}", clean(value))
    return match.group(0) if match else ""


def normalize_text(value: Any) -> str:
    value = upper_clean(value)
    value = value.replace("&", " AND ")
    value = re.sub(r"[^A-Z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def normalize_phone(value: Any) -> str:
    digits = re.sub(r"\D", "", clean(value))
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) == 10:
        return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    return clean(value)


def epoch_to_date(value: Any) -> str:
    try:
        seconds = int(value)
        # NPPES epochs are milliseconds in some responses.
        if seconds > 10_000_000_000:
            seconds //= 1000
        return datetime.fromtimestamp(seconds, tz=timezone.utc).date().isoformat()
    except Exception:  # noqa: BLE001
        return ""


def make_full_name(basic: dict[str, Any]) -> str:
    parts = [
        clean(basic.get("first_name")),
        clean(basic.get("middle_name")),
        clean(basic.get("last_name")),
        clean(basic.get("name_suffix")),
    ]
    return " ".join(part for part in parts if part)


def standardize_credential(value: Any) -> str:
    text = upper_clean(value).replace(".", "")
    if re.search(r"(^|[, /])DO($|[, /])", text):
        return "DO"
    if re.search(r"(^|[, /])MD($|[, /])", text) or "MBBS" in text or "MBCHB" in text:
        return "MD"
    return clean(value)


def address_key(row: dict[str, Any]) -> str:
    return "|".join(
        [
            normalize_text(row.get("address_1")),
            normalize_text(row.get("address_2")),
            normalize_text(row.get("city")),
            upper_clean(row.get("state")),
            zip5(row.get("zip")),
        ]
    )


def loose_address_key(row: dict[str, Any]) -> str:
    return "|".join(
        [
            normalize_text(row.get("address_1")),
            normalize_text(row.get("city")),
            zip5(row.get("zip")),
        ]
    )


def nppes_page(term: str, skip: int) -> dict[str, Any]:
    params = {
        "version": "2.1",
        "enumeration_type": "NPI-1",
        "state": "FL",
        "postal_code": "342*",
        "taxonomy_description": term,
        "address_purpose": "LOCATION",
        "limit": 200,
        "skip": skip,
        "pretty": "off",
    }
    return get_json(NPPES_API, params=params)


def extract_identifiers(result: dict[str, Any]) -> tuple[str, str]:
    medicaid_states: set[str] = set()
    other: set[str] = set()
    identifiers = result.get("identifiers") or []
    if not isinstance(identifiers, list):
        return "", ""
    for item in identifiers:
        if not isinstance(item, dict):
            continue
        desc = upper_clean(item.get("desc") or item.get("identifier_type_desc"))
        state = upper_clean(item.get("state"))
        issuer = clean(item.get("issuer"))
        code = clean(item.get("identifier"))
        if "MEDICAID" in desc:
            medicaid_states.add(state or "Unspecified state")
        elif any(token in desc for token in ("BLUE CROSS", "BLUE SHIELD", "OTHER")) or issuer:
            label = " / ".join(part for part in [desc.title(), issuer, code] if part)
            if label:
                other.add(label)
    return "; ".join(sorted(medicaid_states)), "; ".join(sorted(other))


def extract_practice_addresses(result: dict[str, Any]) -> list[tuple[dict[str, Any], str]]:
    extracted: list[tuple[dict[str, Any], str]] = []
    for addr in result.get("addresses") or []:
        if not isinstance(addr, dict):
            continue
        if upper_clean(addr.get("address_purpose")) == "LOCATION":
            extracted.append((addr, "NPPES Primary Practice Location"))
    secondary = result.get("practiceLocations") or result.get("practice_locations") or []
    for addr in secondary:
        if isinstance(addr, dict):
            extracted.append((addr, "NPPES Secondary Practice Location"))
    return extracted


def fetch_nppes() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    raw_seen: set[tuple[str, str]] = set()
    npi_seen: set[str] = set()

    for term in NPPES_SEARCH_TERMS:
        log(f"NPPES search: {term}")
        skip = 0
        while skip <= 1000:
            payload = nppes_page(term, skip)
            results = payload.get("results") or []
            if not isinstance(results, list):
                results = []
            log(f"  skip={skip}: {len(results)} results")
            for result in results:
                if not isinstance(result, dict):
                    continue
                npi = clean(result.get("number"))
                basic = result.get("basic") or {}
                status = upper_clean(basic.get("status"))
                if status and status != "A":
                    continue
                taxonomies = result.get("taxonomies") or []
                primary = next(
                    (
                        item
                        for item in taxonomies
                        if isinstance(item, dict)
                        and str(item.get("primary")).lower() in {"true", "1", "yes"}
                    ),
                    None,
                )
                if not primary and taxonomies:
                    primary = taxonomies[0] if isinstance(taxonomies[0], dict) else None
                primary_code = upper_clean((primary or {}).get("code"))
                if primary_code not in PRIMARY_TAXONOMIES:
                    continue

                all_codes = {
                    upper_clean(item.get("code"))
                    for item in taxonomies
                    if isinstance(item, dict) and upper_clean(item.get("code"))
                }
                specialty, scope = PRIMARY_TAXONOMIES[primary_code]
                if "207R00000X" in all_codes and "208000000X" in all_codes:
                    scope = "Mixed Adult & Pediatric (Med-Peds indicator)"
                other_pcp = [
                    f"{code} - {PRIMARY_TAXONOMIES[code][0]}"
                    for code in sorted(all_codes & set(PRIMARY_TAXONOMIES))
                    if code != primary_code
                ]
                medicaid_states, other_payer_ids = extract_identifiers(result)
                addresses = extract_practice_addresses(result)
                for addr, location_type in addresses:
                    state = upper_clean(addr.get("state"))
                    z5 = zip5(addr.get("postal_code"))
                    if state != "FL" or not z5.startswith("342"):
                        continue
                    row = {
                        "provider_name": make_full_name(basic),
                        "credential": standardize_credential(basic.get("credential")),
                        "npi": npi,
                        "scope": scope,
                        "primary_specialty": specialty,
                        "primary_taxonomy_code": primary_code,
                        "other_pcp_taxonomies": "; ".join(other_pcp),
                        "practice_facility_name": "",
                        "address_1": clean(addr.get("address_1")),
                        "address_2": clean(addr.get("address_2")),
                        "city": clean(addr.get("city")),
                        "state": state,
                        "zip": z5,
                        "phone": normalize_phone(addr.get("telephone_number")),
                        "fax": normalize_phone(addr.get("fax_number")),
                        "practice_location_type": location_type,
                        "nppes_status": status or "A",
                        "nppes_last_updated": epoch_to_date(result.get("last_updated_epoch")),
                        "sole_proprietor": clean(basic.get("sole_proprietor")),
                        "medicare_care_compare_listed": "No public CMS match found",
                        "medicare_individual_assignment": "",
                        "medicare_group_assignment": "",
                        "medicaid_identifier_states": medicaid_states,
                        "other_payer_identifiers": other_payer_ids,
                        "cms_reported_group_size": "",
                        "organization_pac_id": "",
                        "individual_pac_id": "",
                        "cms_address_id": "",
                        "county_verification": "",
                        "census_county": "",
                        "census_county_fips": "",
                        "geocoder_match": "",
                        "source_nppes": f"https://npiregistry.cms.hhs.gov/provider-view/{npi}",
                        "source_cms": "",
                        "verification_notes": "",
                    }
                    dedupe = (npi, address_key(row))
                    if dedupe in raw_seen:
                        continue
                    raw_seen.add(dedupe)
                    rows.append(row)
                    npi_seen.add(npi)

            if len(results) < 200:
                break
            skip += 200
            if skip > 1000:
                log(f"  Reached NPPES skip cap for {term}")
                break
    log(f"NPPES produced {len(rows)} unique physician-location records across {len(npi_seen)} NPIs before county verification")
    return rows


def extract_cms_records(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("results", "data", "records", "items"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def fetch_cms() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for city in CMS_CITIES:
        offset = 0
        while True:
            params = {
                "conditions[0][property]": "state",
                "conditions[0][value]": "FL",
                "conditions[0][operator]": "=",
                "conditions[1][property]": "citytown",
                "conditions[1][value]": city,
                "conditions[1][operator]": "=",
                "limit": 1500,
                "offset": offset,
            }
            try:
                payload = get_json(CMS_API, params=params)
            except Exception as exc:  # noqa: BLE001
                log(f"CMS query failed for {city}: {exc!r}")
                break
            records = extract_cms_records(payload)
            log(f"CMS city={city}, offset={offset}: {len(records)} rows")
            for record in records:
                specialty = upper_clean(record.get("pri_spec"))
                if specialty not in CMS_PRIMARY_SPECIALTIES:
                    continue
                npi = clean(record.get("npi"))
                cms_row = {
                    "npi": npi,
                    "credential": standardize_credential(record.get("cred")),
                    "provider_name": " ".join(
                        part
                        for part in [
                            clean(record.get("frst_nm")),
                            clean(record.get("mid_nm")),
                            clean(record.get("lst_nm")),
                            clean(record.get("suff")),
                        ]
                        if part
                    ),
                    "cms_primary_specialty": clean(record.get("pri_spec")),
                    "practice_facility_name": clean(record.get("facility_name")),
                    "address_1": clean(record.get("adr_ln_1")),
                    "address_2": clean(record.get("adr_ln_2")),
                    "city": clean(record.get("citytown")),
                    "state": upper_clean(record.get("state")),
                    "zip": zip5(record.get("zip_code")),
                    "phone": normalize_phone(record.get("telephone_number")),
                    "medicare_individual_assignment": clean(record.get("ind_assgn")),
                    "medicare_group_assignment": clean(record.get("grp_assgn")),
                    "cms_reported_group_size": clean(record.get("num_org_mem")),
                    "organization_pac_id": clean(record.get("org_pac_id")),
                    "individual_pac_id": clean(record.get("ind_pac_id")),
                    "cms_address_id": clean(record.get("adrs_id")),
                    "source_cms": "https://data.cms.gov/provider-data/dataset/mj5m-pzi6",
                }
                key = (npi, address_key(cms_row))
                if key not in seen:
                    seen.add(key)
                    rows.append(cms_row)
            if len(records) < 1500:
                break
            offset += 1500
            if offset >= 15000:
                log(f"CMS safety pagination cap reached for {city}")
                break
    log(f"CMS supplement produced {len(rows)} primary-care rows")
    return rows


def merge_cms(nppes_rows: list[dict[str, Any]], cms_rows: list[dict[str, Any]]) -> None:
    exact: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    loose: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    by_npi: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in cms_rows:
        npi = row["npi"]
        exact[(npi, address_key(row))].append(row)
        loose[(npi, loose_address_key(row))].append(row)
        by_npi[npi].append(row)

    for row in nppes_rows:
        npi = row["npi"]
        candidates = exact.get((npi, address_key(row)), [])
        match_note = "Exact NPI + practice address match"
        if not candidates:
            candidates = loose.get((npi, loose_address_key(row)), [])
            match_note = "NPI + normalized street/city/ZIP match"
        if not candidates and len(by_npi.get(npi, [])) == 1:
            # Use a unique NPI match only when it is in the same city and ZIP.
            single = by_npi[npi][0]
            if normalize_text(single.get("city")) == normalize_text(row.get("city")) and zip5(single.get("zip")) == zip5(row.get("zip")):
                candidates = [single]
                match_note = "Unique NPI match in same city and ZIP"
        if not candidates:
            continue
        # Prefer a record with a facility and the largest reported group size.
        def rank(c: dict[str, Any]) -> tuple[int, int]:
            try:
                size = int(float(clean(c.get("cms_reported_group_size")) or 0))
            except ValueError:
                size = 0
            return (1 if c.get("practice_facility_name") else 0, size)

        cms = sorted(candidates, key=rank, reverse=True)[0]
        row["medicare_care_compare_listed"] = "Yes"
        for field in (
            "practice_facility_name",
            "medicare_individual_assignment",
            "medicare_group_assignment",
            "cms_reported_group_size",
            "organization_pac_id",
            "individual_pac_id",
            "cms_address_id",
            "source_cms",
        ):
            if clean(cms.get(field)):
                row[field] = cms[field]
        if not row.get("phone") and cms.get("phone"):
            row["phone"] = cms["phone"]
        row["verification_notes"] = match_note


def geocode_addresses(rows: list[dict[str, Any]]) -> dict[str, dict[str, str]]:
    unique: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = address_key(row)
        if key and key not in unique:
            unique[key] = row

    output: dict[str, dict[str, str]] = {}
    items = list(unique.items())
    for chunk_start in range(0, len(items), 9000):
        chunk = items[chunk_start : chunk_start + 9000]
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\n")
        id_to_key: dict[str, str] = {}
        for index, (key, row) in enumerate(chunk, start=chunk_start + 1):
            ident = str(index)
            id_to_key[ident] = key
            street = " ".join(part for part in [clean(row.get("address_1")), clean(row.get("address_2"))] if part)
            writer.writerow([ident, street, clean(row.get("city")), upper_clean(row.get("state")), zip5(row.get("zip"))])
        payload_bytes = buffer.getvalue().encode("utf-8")
        response = post_with_retry(
            CENSUS_BATCH,
            files={"addressFile": ("addresses.csv", payload_bytes, "text/csv")},
            data={"benchmark": "Public_AR_Current", "vintage": "Current_Current"},
        )
        reader = csv.reader(io.StringIO(response.text))
        matched_count = 0
        for fields in reader:
            if not fields:
                continue
            ident = clean(fields[0])
            key = id_to_key.get(ident)
            if not key:
                continue
            # Expected columns: id, input address, match, match type, matched address,
            # coordinates, tigerline id, side, state fips, county fips, tract, block.
            match_flag = upper_clean(fields[2] if len(fields) > 2 else "")
            match_type = clean(fields[3] if len(fields) > 3 else "")
            matched_address = clean(fields[4] if len(fields) > 4 else "")
            state_fips = clean(fields[8] if len(fields) > 8 else "")
            county_fips = clean(fields[9] if len(fields) > 9 else "")
            full_fips = f"{state_fips.zfill(2)}{county_fips.zfill(3)}" if state_fips and county_fips else ""
            is_match = match_flag in {"MATCH", "EXACT", "TIE"} or bool(full_fips)
            if is_match:
                matched_count += 1
            output[key] = {
                "matched": "Yes" if is_match else "No",
                "match_type": match_type,
                "matched_address": matched_address,
                "state_fips": state_fips,
                "county_fips": full_fips,
            }
        log(f"Census geocoder chunk: {matched_count}/{len(chunk)} address matches")
    return output


def verify_county(rows: list[dict[str, Any]], geocoded: dict[str, dict[str, str]]) -> list[dict[str, Any]]:
    kept: list[dict[str, Any]] = []
    for row in rows:
        geo = geocoded.get(address_key(row), {})
        fips = clean(geo.get("county_fips"))
        z5 = zip5(row.get("zip"))
        row["geocoder_match"] = clean(geo.get("matched"))
        row["census_county_fips"] = fips
        if fips == "12115":
            row["county_verification"] = "Verified by U.S. Census address geocoder"
            row["census_county"] = "Sarasota County, Florida"
            kept.append(row)
        elif fips:
            # Definitively geocoded outside Sarasota County.
            continue
        elif z5 in SARASOTA_ONLY_ZIPS:
            row["county_verification"] = "Included by Sarasota-only ZIP fallback; address geocoder unmatched"
            row["census_county"] = "Sarasota County, Florida (ZIP fallback)"
            row["verification_notes"] = "; ".join(
                part
                for part in [row.get("verification_notes", ""), "County based on Sarasota-only ZIP fallback"]
                if part
            )
            kept.append(row)
        else:
            # Cross-county or uncertain ZIP and no address match: exclude conservatively.
            continue
    log(f"County verification retained {len(kept)} Sarasota County physician-location rows")
    return kept


def safe_int(value: Any) -> int:
    try:
        return int(float(clean(value) or 0))
    except ValueError:
        return 0


def assign_practice_metrics(rows: list[dict[str, Any]]) -> None:
    address_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    org_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        address_groups[address_key(row)].append(row)
        org_id = clean(row.get("organization_pac_id"))
        if org_id:
            org_groups[org_id].append(row)

    for row in rows:
        at_address = address_groups[address_key(row)]
        known_pcps = len({item["npi"] for item in at_address})
        row["known_pcps_at_address"] = known_pcps
        cms_size = safe_int(row.get("cms_reported_group_size"))
        basis_size = max(cms_size, known_pcps)
        if cms_size >= 100:
            tier = "Enterprise / Health System (100+)"
        elif cms_size >= 50:
            tier = "Large Group (50-99)"
        elif cms_size >= 10:
            tier = "Mid-size Group (10-49)"
        elif cms_size >= 2:
            tier = "Small Group (2-9)"
        elif cms_size == 1:
            tier = "Solo / Single-member Group"
        elif known_pcps >= 10:
            tier = "Mid-size PCP Cluster (10+ known at address)"
        elif known_pcps >= 2:
            tier = "Small PCP Cluster (2-9 known at address)"
        elif upper_clean(row.get("sole_proprietor")) == "YES":
            tier = "Solo / Sole Proprietor"
        else:
            tier = "Unknown / Unverified"
        row["practice_size_tier"] = tier
        row["practice_size_sort"] = basis_size
        if not clean(row.get("practice_facility_name")):
            if known_pcps > 1:
                row["practice_facility_name"] = f"Address-based PCP cluster ({known_pcps} physicians)"
            else:
                row["practice_facility_name"] = "Practice name not identified in CMS public file"


def add_final_fields(rows: list[dict[str, Any]], extracted_at: str) -> None:
    for row in rows:
        assignment = upper_clean(row.get("medicare_individual_assignment"))
        if assignment == "Y":
            row["medicare_assignment_indicator"] = "Yes - public CMS assignment indicator"
        elif assignment == "N":
            row["medicare_assignment_indicator"] = "No - public CMS assignment indicator"
        else:
            row["medicare_assignment_indicator"] = "Not available"
        row["insurance_public_summary"] = "; ".join(
            part
            for part in [
                "Medicare Care Compare listed" if row.get("medicare_care_compare_listed") == "Yes" else "",
                row.get("medicare_assignment_indicator", ""),
                f"Medicaid identifier state(s): {row['medicaid_identifier_states']}" if row.get("medicaid_identifier_states") else "",
                "Other public payer identifiers present" if row.get("other_payer_identifiers") else "",
            ]
            if part
        ) or "No public payer indicator found"
        row["source_urls"] = "; ".join(
            url for url in [row.get("source_nppes", ""), row.get("source_cms", "")] if url
        )
        row["extraction_date"] = extracted_at
        row["aco_outreach_priority"] = (
            "1 - High"
            if safe_int(row.get("cms_reported_group_size")) >= 10 and row.get("medicare_care_compare_listed") == "Yes"
            else "2 - Medium"
            if row.get("medicare_care_compare_listed") == "Yes" or safe_int(row.get("known_pcps_at_address")) >= 2
            else "3 - Standard"
        )


def sort_rows(rows: list[dict[str, Any]]) -> None:
    priority_order = {"1 - High": 1, "2 - Medium": 2, "3 - Standard": 3}
    rows.sort(
        key=lambda r: (
            priority_order.get(r.get("aco_outreach_priority", ""), 9),
            -safe_int(r.get("practice_size_sort")),
            normalize_text(r.get("practice_facility_name")),
            normalize_text(r.get("provider_name")),
            address_key(r),
        )
    )


OUTPUT_FIELDS = [
    "aco_outreach_priority",
    "provider_name",
    "credential",
    "npi",
    "scope",
    "primary_specialty",
    "primary_taxonomy_code",
    "other_pcp_taxonomies",
    "practice_facility_name",
    "address_1",
    "address_2",
    "city",
    "state",
    "zip",
    "phone",
    "fax",
    "practice_location_type",
    "county_verification",
    "census_county",
    "census_county_fips",
    "nppes_status",
    "nppes_last_updated",
    "sole_proprietor",
    "medicare_care_compare_listed",
    "medicare_assignment_indicator",
    "medicare_individual_assignment",
    "medicare_group_assignment",
    "medicaid_identifier_states",
    "other_payer_identifiers",
    "insurance_public_summary",
    "cms_reported_group_size",
    "known_pcps_at_address",
    "practice_size_tier",
    "practice_size_sort",
    "organization_pac_id",
    "individual_pac_id",
    "cms_address_id",
    "geocoder_match",
    "source_nppes",
    "source_cms",
    "source_urls",
    "verification_notes",
    "extraction_date",
]


def write_directory(rows: list[dict[str, Any]]) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_DIR / "sarasota_primary_care_raw.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=OUTPUT_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    log(f"Wrote {path} ({path.stat().st_size:,} bytes)")


def write_practices(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        org = clean(row.get("organization_pac_id"))
        if org:
            key = f"ORG|{org}|{address_key(row)}"
        else:
            key = f"ADDR|{address_key(row)}"
        groups[key].append(row)

    practice_rows: list[dict[str, Any]] = []
    for group in groups.values():
        exemplar = max(group, key=lambda r: (safe_int(r.get("cms_reported_group_size")), bool(r.get("practice_facility_name"))))
        npi_count = len({r["npi"] for r in group})
        specialties = sorted({r["primary_specialty"] for r in group if r.get("primary_specialty")})
        scopes = sorted({r["scope"] for r in group if r.get("scope")})
        medicare_count = len({r["npi"] for r in group if r.get("medicare_care_compare_listed") == "Yes"})
        practice_rows.append(
            {
                "aco_outreach_priority": min((r["aco_outreach_priority"] for r in group), default="3 - Standard"),
                "practice_facility_name": exemplar["practice_facility_name"],
                "organization_pac_id": exemplar.get("organization_pac_id", ""),
                "address_1": exemplar["address_1"],
                "address_2": exemplar["address_2"],
                "city": exemplar["city"],
                "state": exemplar["state"],
                "zip": exemplar["zip"],
                "phone": exemplar["phone"],
                "known_primary_care_physicians": npi_count,
                "cms_reported_group_size": max(safe_int(r.get("cms_reported_group_size")) for r in group),
                "practice_size_tier": exemplar["practice_size_tier"],
                "medicare_listed_pcp_count": medicare_count,
                "specialties": "; ".join(specialties),
                "patient_scope": "; ".join(scopes),
                "physicians": "; ".join(sorted({r["provider_name"] for r in group})),
                "source_urls": "; ".join(sorted({url for r in group for url in r["source_urls"].split("; ") if url})),
                "extraction_date": exemplar["extraction_date"],
            }
        )
    practice_rows.sort(
        key=lambda r: (
            {"1 - High": 1, "2 - Medium": 2, "3 - Standard": 3}.get(r["aco_outreach_priority"], 9),
            -safe_int(r["cms_reported_group_size"]),
            -safe_int(r["known_primary_care_physicians"]),
            normalize_text(r["practice_facility_name"]),
        )
    )
    fields = [
        "aco_outreach_priority",
        "practice_facility_name",
        "organization_pac_id",
        "address_1",
        "address_2",
        "city",
        "state",
        "zip",
        "phone",
        "known_primary_care_physicians",
        "cms_reported_group_size",
        "practice_size_tier",
        "medicare_listed_pcp_count",
        "specialties",
        "patient_scope",
        "physicians",
        "source_urls",
        "extraction_date",
    ]
    path = OUTPUT_DIR / "sarasota_practices_ranked.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(practice_rows)
    log(f"Wrote {path} ({path.stat().st_size:,} bytes)")
    return practice_rows


def write_summary(rows: list[dict[str, Any]], practices: list[dict[str, Any]], extracted_at: str) -> None:
    summary = {
        "extraction_date": extracted_at,
        "physician_location_rows": len(rows),
        "unique_physicians": len({r["npi"] for r in rows}),
        "unique_practice_locations": len({address_key(r) for r in rows}),
        "practice_groups_or_address_clusters": len(practices),
        "medicare_listed_unique_physicians": len({r["npi"] for r in rows if r["medicare_care_compare_listed"] == "Yes"}),
        "specialty_counts_unique_npi": dict(
            sorted(
                Counter(
                    (r["primary_specialty"], r["npi"])
                    for r in rows
                ).keys()
            )
        ),
        "specialty_counts": dict(Counter(r["primary_specialty"] for r in rows)),
        "scope_counts": dict(Counter(r["scope"] for r in rows)),
        "practice_size_tier_counts": dict(Counter(r["practice_size_tier"] for r in rows)),
        "city_counts": dict(Counter(r["city"] for r in rows)),
        "county_verification_counts": dict(Counter(r["county_verification"] for r in rows)),
        "methodology": {
            "nppes_filter": "Entity Type 1, active physician records, primary taxonomy in requested primary-care code set, practice location ZIP 342*, then Sarasota County address verification",
            "county_fips": "12115",
            "insurance_caution": "Public payer identifiers and CMS assignment fields are indicators only; they are not confirmation of current plan-network participation or acceptance of new patients.",
            "all_caveat": "All qualifying records found through the defined public-data method as of the extraction date; public registries may contain stale or incomplete practice data.",
        },
    }
    # Correct specialty unique-NPI counts into a normal mapping.
    summary["specialty_counts_unique_npi"] = {
        specialty: len({r["npi"] for r in rows if r["primary_specialty"] == specialty})
        for specialty in sorted({r["primary_specialty"] for r in rows})
    }
    path = OUTPUT_DIR / "sarasota_primary_care_summary.json"
    path.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    (OUTPUT_DIR / "run_complete.txt").write_text(
        f"completed={datetime.now(timezone.utc).isoformat()}\n"
        f"unique_physicians={summary['unique_physicians']}\n"
        f"physician_location_rows={summary['physician_location_rows']}\n"
        f"practice_groups_or_address_clusters={summary['practice_groups_or_address_clusters']}\n",
        encoding="utf-8",
    )
    log(json.dumps(summary, indent=2))


def main() -> int:
    extracted_at = datetime.now(timezone.utc).date().isoformat()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    nppes_rows = fetch_nppes()
    if not nppes_rows:
        raise RuntimeError("NPPES returned no qualifying records; refusing to publish an empty directory")
    cms_rows = fetch_cms()
    merge_cms(nppes_rows, cms_rows)
    geocoded = geocode_addresses(nppes_rows)
    rows = verify_county(nppes_rows, geocoded)
    if not rows:
        raise RuntimeError("County verification returned no records; refusing to publish an empty directory")
    assign_practice_metrics(rows)
    add_final_fields(rows, extracted_at)
    sort_rows(rows)
    write_directory(rows)
    practices = write_practices(rows)
    write_summary(rows, practices, extracted_at)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001
        log(f"FATAL: {exc!r}")
        raise
