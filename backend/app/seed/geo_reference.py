"""Geographic reference data.

Static ISO-3166 country and city coordinates used to place real transaction and customer
activity on the map. This is reference data, not business data -- the figures shown on the
geography screen all come from the ledger.
"""

from __future__ import annotations

from typing import NamedTuple


class Place(NamedTuple):
    name: str
    latitude: float
    longitude: float
    region: str


# ISO 3166-1 alpha-3 -> representative centroid.
COUNTRIES: dict[str, Place] = {
    "IND": Place("India", 22.35, 78.67, "South Asia"),
    "ARE": Place("United Arab Emirates", 24.30, 54.30, "Middle East"),
    "SGP": Place("Singapore", 1.35, 103.82, "South East Asia"),
    "GBR": Place("United Kingdom", 54.00, -2.00, "Europe"),
    "USA": Place("United States", 39.50, -98.35, "North America"),
    "IRN": Place("Iran", 32.43, 53.69, "Middle East"),
    "RUS": Place("Russia", 61.52, 60.00, "Eurasia"),
    "CHN": Place("China", 35.86, 104.20, "East Asia"),
    "HKG": Place("Hong Kong", 22.32, 114.17, "East Asia"),
    "DEU": Place("Germany", 51.17, 10.45, "Europe"),
    "FRA": Place("France", 46.23, 2.21, "Europe"),
    "CHE": Place("Switzerland", 46.82, 8.23, "Europe"),
    "NLD": Place("Netherlands", 52.13, 5.29, "Europe"),
    "AUS": Place("Australia", -25.27, 133.78, "Oceania"),
    "JPN": Place("Japan", 36.20, 138.25, "East Asia"),
    "CAN": Place("Canada", 56.13, -106.35, "North America"),
    "BRA": Place("Brazil", -14.24, -51.93, "South America"),
    "ZAF": Place("South Africa", -30.56, 22.94, "Africa"),
    "NGA": Place("Nigeria", 9.08, 8.68, "Africa"),
    "KEN": Place("Kenya", -0.02, 37.91, "Africa"),
    "SAU": Place("Saudi Arabia", 23.89, 45.08, "Middle East"),
    "QAT": Place("Qatar", 25.35, 51.18, "Middle East"),
    "MYS": Place("Malaysia", 4.21, 101.98, "South East Asia"),
    "IDN": Place("Indonesia", -0.79, 113.92, "South East Asia"),
    "LKA": Place("Sri Lanka", 7.87, 80.77, "South Asia"),
    "BGD": Place("Bangladesh", 23.68, 90.36, "South Asia"),
    "PAK": Place("Pakistan", 30.38, 69.35, "South Asia"),
    "PRK": Place("North Korea", 40.34, 127.51, "East Asia"),
    "SYR": Place("Syria", 34.80, 38.997, "Middle East"),
    "AFG": Place("Afghanistan", 33.94, 67.71, "South Asia"),
    "MMR": Place("Myanmar", 21.91, 95.96, "South East Asia"),
    "YEM": Place("Yemen", 15.55, 48.52, "Middle East"),
    "SSD": Place("South Sudan", 6.88, 31.31, "Africa"),
    "CUB": Place("Cuba", 21.52, -77.78, "Caribbean"),
    "VEN": Place("Venezuela", 6.42, -66.59, "South America"),
}

# Two-letter codes appear on watchlist records; map them onto the three-letter set.
ALPHA2_TO_ALPHA3: dict[str, str] = {
    "IN": "IND",
    "AE": "ARE",
    "SG": "SGP",
    "GB": "GBR",
    "US": "USA",
    "IR": "IRN",
    "RU": "RUS",
    "CN": "CHN",
    "HK": "HKG",
    "DE": "DEU",
    "FR": "FRA",
    "CH": "CHE",
    "NL": "NLD",
    "AU": "AUS",
    "JP": "JPN",
    "CA": "CAN",
    "BR": "BRA",
    "ZA": "ZAF",
    "NG": "NGA",
    "KE": "KEN",
    "SA": "SAU",
    "QA": "QAT",
    "MY": "MYS",
    "ID": "IDN",
    "LK": "LKA",
    "BD": "BGD",
    "PK": "PAK",
    "KP": "PRK",
    "SY": "SYR",
    "AF": "AFG",
    "MM": "MMR",
    "YE": "YEM",
    "SS": "SSD",
    "CU": "CUB",
    "VE": "VEN",
}

CITIES: dict[str, Place] = {
    "Mumbai": Place("Mumbai", 19.076, 72.877, "IND"),
    "Bengaluru": Place("Bengaluru", 12.972, 77.594, "IND"),
    "Delhi": Place("Delhi", 28.614, 77.209, "IND"),
    "Chennai": Place("Chennai", 13.083, 80.270, "IND"),
    "Pune": Place("Pune", 18.520, 73.857, "IND"),
    "Hyderabad": Place("Hyderabad", 17.385, 78.487, "IND"),
    "Kolkata": Place("Kolkata", 22.573, 88.364, "IND"),
    "Ahmedabad": Place("Ahmedabad", 23.023, 72.571, "IND"),
}

DOMESTIC_COUNTRY = "IND"


def resolve(code: str | None) -> str | None:
    """Normalise an alpha-2 or alpha-3 code to alpha-3, or None if it is not known here."""
    if not code:
        return None
    normalised = code.strip().upper()
    if len(normalised) == 2:
        normalised = ALPHA2_TO_ALPHA3.get(normalised, normalised)
    return normalised if normalised in COUNTRIES else None


def country(code: str | None) -> Place | None:
    resolved = resolve(code)
    return COUNTRIES.get(resolved) if resolved else None


def city(name: str | None) -> Place | None:
    return CITIES.get(name.strip()) if name else None
