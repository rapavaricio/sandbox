#!/usr/bin/env python3
"""Comprehensive exact-ZIP Sarasota County PCP acquisition.

NPPES taxonomy-description search is ambiguous for terms such as "Pediatrics"
(it can return pediatric nurse practitioners while omitting physician pediatricians).
This acquisition pass therefore searches all individual providers in each candidate
ZIP, paginates the results, and filters locally by exact primary physician taxonomy
code. If a ZIP reaches the NPPES 1,200-result pagination ceiling, it automatically
subdivides that ZIP by last-name prefix and deduplicates the results.
"""

from __future__ import annotations

import string
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


def nppes_page(postal_code: str, skip: int, last_name: str | None = None) -> dict[str, Any]:
    params: dict[str, Any] = {
        "version": "2.1",
        "enumeration_type": "NPI-1",
        "state": "FL",
        "postal_code": postal_code,
        "address_purpose": "LOCATION",
        "limit": 200,
        "skip": skip,
        "pretty": "off",
    }
    if last_name:
        params["last_name"] = last_name
    return base.get_json(base.NPPES_API, params=params)


def fetch_query(postal_code: str, last_name: str | None = None) -> tuple[list[dict[str, Any]], bool]:
    results_all: list[dict[str, Any]] = []
    hit_cap = False
    for skip in range(0, 1200, 200):
        payload = nppes_page(postal_code, skip, last_name)
        results = payload.get("results") or []
        if not isinstance(results, list):
            results = []
        results_all.extend(item for item in results if isinstance(item, dict))
        if len(results) < 200:
            return results_all, False
        if skip == 1000:
            hit_cap = True
        time.sleep(0.03)
    return results_all, hit_cap


def raw_results_for_zip(postal_code: str) -> list[dict[str, Any]]:
    broad, hit_cap = fetch_query(postal_code)
    if not hit_cap:
        base.log(f"NPPES ZIP {postal_code}: {len(broad)} individual-provider results")
        return broad

    base.log(f"NPPES ZIP {postal_code} reached pagination ceiling; subdividing by last-name prefix")
    segmented: list[dict[str, Any]] = []
    for prefix in [f"{letter}*" for letter in string.ascii_uppercase] + [
        "0*", "1*", "2*", "3*", "4*", "5*", "6*", "7*", "8*", "9*"
    ]:
        segment, segment_hit_cap = fetch_query(postal_code, prefix)
        if segment_hit_cap:
            raise RuntimeError(
                f"NPPES ZIP {postal_code} / last-name prefix {prefix} still reached the 1,200-result ceiling"
            )
        segmented.extend(segment)
    by_npi: dict[str, dict[str, Any]] = {}
    for result in [*broad, *segmented]:
        number = base.clean(result.get("number"))
        if number:
            by_npi[number] = result
    base.log(f"NPPES ZIP {postal_code}: {len(by_npi)} deduplicated segmented results")
    return list(by_npi.values())


def primary_taxonomy(result: dict[str, Any]) -> tuple[dict[str, Any] | None, set[str]]:
    taxonomies = result.get("taxonomies") or []
    if not isinstance(taxonomies, list):
        taxonomies = []
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
    codes = {
        base.upper_clean(item.get("code"))
        for item in taxonomies
        if isinstance(item, dict) and base.upper_clean(item.get("code"))
    }
    return primary, codes


def transform_result(result: dict[str, Any], postal_code: str) -> list[dict[str, Any]]:
    basic = result.get("basic") or {}
    status = base.upper_clean(basic.get("status"))
    if status and status != "A":
        return []
    primary, all_codes = primary_taxonomy(result)
    primary_code = base.upper_clean((primary or {}).get("code"))
    if primary_code not in base.PRIMARY_TAXONOMIES:
        return []

    npi = base.clean(result.get("number"))
    specialty, scope = base.PRIMARY_TAXONOMIES[primary_code]
    if "207R00000X" in all_codes and "208000000X" in all_codes:
        scope = "Mixed Adult & Pediatric (Med-Peds indicator)"
    other_pcp = [
        f"{code} - {base.PRIMARY_TAXONOMIES[code][0]}"
        for code in sorted(all_codes & set(base.PRIMARY_TAXONOMIES))
        if code != primary_code
    ]
    medicaid_states, other_payer_ids = base.extract_identifiers(result)
    output: list[dict[str, Any]] = []
    for addr, location_type in base.extract_practice_addresses(result):
        state = base.upper_clean(addr.get("state"))
        z5 = base.zip5(addr.get("postal_code"))
        if state != "FL" or z5 != postal_code:
            continue
        output.append(
            {
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
        )
    return output


def fetch_nppes_comprehensive() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    zip_counts: dict[str, int] = {}

    for postal_code in SARASOTA_AREA_ZIPS:
        qualifying_npis: set[str] = set()
        raw_results = raw_results_for_zip(postal_code)
        for result in raw_results:
            for row in transform_result(result, postal_code):
                key = (row["npi"], base.address_key(row))
                if key in seen:
                    continue
                seen.add(key)
                rows.append(row)
                qualifying_npis.add(row["npi"])
        zip_counts[postal_code] = len(qualifying_npis)
        if qualifying_npis:
            base.log(f"NPPES ZIP {postal_code}: {len(qualifying_npis)} qualifying primary-care physicians")

    peds_npis = {
        r["npi"] for r in rows if r["primary_taxonomy_code"] in {"208000000X", "2080A0000X"}
    }
    known_pediatric_npis = {
        "1659722296", "1871574699", "1427340892", "1497016943",
        "1417948837", "1518985134", "1699091355", "1730529652",
    }
    missing_known = sorted(known_pediatric_npis - peds_npis)
    if missing_known:
        raise RuntimeError(f"Comprehensive NPPES acquisition missed diagnostic pediatric NPIs: {missing_known}")

    base.log(
        f"Comprehensive NPPES produced {len(rows)} physician-location records across "
        f"{len({r['npi'] for r in rows})} NPIs before county verification; "
        f"pediatric NPIs={len(peds_npis)}; ZIP counts={zip_counts}"
    )
    return rows


if __name__ == "__main__":
    base.fetch_nppes = fetch_nppes_comprehensive
    raise SystemExit(base.main())
