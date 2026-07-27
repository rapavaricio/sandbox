#!/usr/bin/env python3
"""Exact-ZIP NPPES acquisition wrapper for the Sarasota PCP directory.

The original extractor's transformation, CMS enrichment, county verification, ranking,
and outputs are reused. This wrapper replaces the broad 342* NPPES search with exact
ZIP-by-ZIP searches so smaller specialties, especially general pediatrics, are not
underrepresented by search-result ordering or pagination limits.
"""

from __future__ import annotations

import time
from typing import Any

import build_sarasota_pcp as base

SARASOTA_AREA_ZIPS = [
    "34223", "34224", "34228", "34229", "34230", "34231", "34232", "34233",
    "34234", "34235", "34236", "34237", "34238", "34239", "34240", "34241",
    "34242", "34243", "34249", "34251", "34260", "34266", "34272", "34274",
    "34275", "34276", "34277", "34278", "34284", "34285", "34286", "34287",
    "34288", "34289", "34290", "34291", "34292", "34293", "34295",
]

SEARCH_TERMS = [
    "Family Medicine",
    "Internal Medicine",
    "Pediatrics",
    "General Practice",
    "Geriatric Medicine",
    "Adult Medicine",
    "Adolescent Medicine",
]


def nppes_exact_zip_page(term: str, postal_code: str, skip: int) -> dict[str, Any]:
    params = {
        "version": "2.1",
        "enumeration_type": "NPI-1",
        "state": "FL",
        "postal_code": postal_code,
        "taxonomy_description": term,
        "address_purpose": "LOCATION",
        "limit": 200,
        "skip": skip,
        "pretty": "off",
    }
    return base.get_json(base.NPPES_API, params=params)


def fetch_nppes_exact_zips() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    raw_seen: set[tuple[str, str]] = set()
    npi_seen: set[str] = set()
    diagnostic_counts: dict[str, int] = {term: 0 for term in SEARCH_TERMS}

    for term in SEARCH_TERMS:
        base.log(f"NPPES exact-ZIP search: {term}")
        term_npis: set[str] = set()
        for postal_code in SARASOTA_AREA_ZIPS:
            skip = 0
            while skip <= 1000:
                payload = nppes_exact_zip_page(term, postal_code, skip)
                results = payload.get("results") or []
                if not isinstance(results, list):
                    results = []
                if results:
                    base.log(f"  ZIP {postal_code}, skip={skip}: {len(results)} results")
                for result in results:
                    if not isinstance(result, dict):
                        continue
                    npi = base.clean(result.get("number"))
                    basic = result.get("basic") or {}
                    status = base.upper_clean(basic.get("status"))
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
                    primary_code = base.upper_clean((primary or {}).get("code"))
                    if primary_code not in base.PRIMARY_TAXONOMIES:
                        continue

                    all_codes = {
                        base.upper_clean(item.get("code"))
                        for item in taxonomies
                        if isinstance(item, dict) and base.upper_clean(item.get("code"))
                    }
                    specialty, scope = base.PRIMARY_TAXONOMIES[primary_code]
                    if "207R00000X" in all_codes and "208000000X" in all_codes:
                        scope = "Mixed Adult & Pediatric (Med-Peds indicator)"
                    other_pcp = [
                        f"{code} - {base.PRIMARY_TAXONOMIES[code][0]}"
                        for code in sorted(all_codes & set(base.PRIMARY_TAXONOMIES))
                        if code != primary_code
                    ]
                    medicaid_states, other_payer_ids = base.extract_identifiers(result)
                    addresses = base.extract_practice_addresses(result)
                    for addr, location_type in addresses:
                        state = base.upper_clean(addr.get("state"))
                        z5 = base.zip5(addr.get("postal_code"))
                        if state != "FL" or z5 != postal_code:
                            continue
                        row = {
                            "provider_name": base.make_full_name(basic),
                            "credential": base.standardize_credential(basic.get("credential")),
                            "npi": npi,
                            "scope": scope,
                            "primary_specialty": specialty,
                            "primary_taxonomy_code": primary_code,
                            "other_pcp_taxonomies": "; ".join(other_pcp),
                            "practice_facility_name": "",
                            "address_1": base.clean(addr.get("address_1")),
                            "address_2": base.clean(addr.get("address_2")),
                            "city": base.clean(addr.get("city")),
                            "state": state,
                            "zip": z5,
                            "phone": base.normalize_phone(addr.get("telephone_number")),
                            "fax": base.normalize_phone(addr.get("fax_number")),
                            "practice_location_type": location_type,
                            "nppes_status": status or "A",
                            "nppes_last_updated": base.epoch_to_date(result.get("last_updated_epoch")),
                            "sole_proprietor": base.clean(basic.get("sole_proprietor")),
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
                        dedupe = (npi, base.address_key(row))
                        if dedupe in raw_seen:
                            continue
                        raw_seen.add(dedupe)
                        rows.append(row)
                        npi_seen.add(npi)
                        term_npis.add(npi)

                if len(results) < 200:
                    break
                skip += 200
                if skip > 1000:
                    base.log(f"  Reached NPPES skip cap for {term} / {postal_code}")
                    break
                time.sleep(0.05)
        diagnostic_counts[term] = len(term_npis)
        base.log(f"NPPES qualifying unique NPIs found via term {term}: {len(term_npis)}")

    base.log(
        f"Exact-ZIP NPPES produced {len(rows)} unique physician-location records "
        f"across {len(npi_seen)} NPIs before county verification; term counts={diagnostic_counts}"
    )
    return rows


if __name__ == "__main__":
    base.fetch_nppes = fetch_nppes_exact_zips
    raise SystemExit(base.main())
