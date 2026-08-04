"""Baseline internal watchlist used by sanctions and PEP screening.

These are illustrative internal-list entries so screening is functional out of the box.
Production deployments load the authoritative lists with `finops load-sanctions`, which
downloads the OFAC SDN, consolidated UN and EU files and replaces these rows.
"""

from __future__ import annotations

from typing import Any

INTERNAL_WATCHLIST: list[dict[str, Any]] = [
    {
        "list_name": "INTERNAL", "full_name": "Viktor Petrovich Sokolov",
        "aliases": ["V. P. Sokolov", "Viktor Sokolow"], "date_of_birth": "1968-04-11",
        "nationality": "RU", "program": "Internal watchlist - trade finance",
        "remarks": "Associated with structuring activity in prior investigations.",
    },
    {
        "list_name": "INTERNAL", "full_name": "Zenith Trading FZE", "entry_type": "entity",
        "aliases": ["Zenith Trading Free Zone Establishment", "Zenith FZE"],
        "nationality": "AE", "program": "Internal watchlist - shell entity",
        "remarks": "Counterparty in multiple pass-through typology alerts.",
    },
    {
        "list_name": "INTERNAL", "full_name": "Cygnus Holdings LLC", "entry_type": "entity",
        "aliases": ["Cygnus LLC"], "nationality": "US",
        "program": "Internal watchlist - layering",
        "remarks": "Repeated round-value transfers with no economic rationale.",
    },
    {
        "list_name": "INTERNAL", "full_name": "Farhan Abdul Rahman",
        "aliases": ["F. A. Rahman"], "date_of_birth": "1975-09-02", "nationality": "IR",
        "program": "Internal watchlist - jurisdiction risk",
    },
    {
        "list_name": "INTERNAL", "full_name": "Helios Metals Trading Ltd", "entry_type": "entity",
        "aliases": ["Helios Metals"], "nationality": "GB",
        "program": "Internal watchlist - trade-based money laundering",
    },
    # --- PEP entries ---------------------------------------------------------
    {
        "list_name": "PEP", "full_name": "Rajesh Kumar Venkatesan", "is_pep": True,
        "aliases": ["R. K. Venkatesan"], "date_of_birth": "1961-02-17", "nationality": "IN",
        "position": "Former State Minister of Finance",
        "program": "Domestic PEP - Tier 1",
    },
    {
        "list_name": "PEP", "full_name": "Amara Chidi Okafor", "is_pep": True,
        "aliases": ["A. C. Okafor"], "date_of_birth": "1972-11-30", "nationality": "NG",
        "position": "Central Bank Deputy Governor",
        "program": "Foreign PEP - Tier 1",
    },
    {
        "list_name": "PEP", "full_name": "Elena Marisol Vargas", "is_pep": True,
        "aliases": ["E. M. Vargas"], "date_of_birth": "1980-06-08", "nationality": "VE",
        "position": "Ambassador",
        "program": "Foreign PEP - Tier 2",
    },
    {
        "list_name": "PEP", "full_name": "Suresh Nathan Iyer", "is_pep": True,
        "date_of_birth": "1966-01-23", "nationality": "IN",
        "position": "Chairman, State Infrastructure Corporation",
        "program": "Domestic PEP - Tier 2 (state owned enterprise)",
    },
    {
        "list_name": "PEP", "full_name": "Marcus Aurelius Brandt", "is_pep": True,
        "date_of_birth": "1958-08-14", "nationality": "DE",
        "position": "Member of Parliament (retired)",
        "program": "Foreign PEP - Tier 3",
    },
]
