"""Static P25 control-channel catalog imported from RadioReference.

Snapshot source: https://www.radioreference.com/db/sid/8438
RadioReference page last updated 2026-08-09. Only frequencies marked Ctrl
are included; this offline snapshot can become stale and is not refreshed at
runtime.
"""

from copy import deepcopy

SOURCE_URL = "https://www.radioreference.com/db/sid/8438"
SOURCE_UPDATED = "2026-08-09"
SYSTEM_NAME = "Denton County Regional Radio System"
SYSTEM_SYSID = "0x693"
SYSTEM_WACN = "0xBEE00"

BUILTIN_SITES = (
    {
        "id": "rr8438:1-001",
        "region": "Texas",
        "county": "Denton",
        "site": "Denton County Simulcast",
        "system": SYSTEM_NAME,
        "sysid": SYSTEM_SYSID,
        "wacn": SYSTEM_WACN,
        "rfss": 1,
        "site_number": 1,
        "nac": "690",
        "control_hz": (772_606_250, 773_656_250, 858_287_500, 859_287_500),
    },
    {
        "id": "rr8438:1-002",
        "region": "Texas",
        "county": "Denton",
        "site": "Lewisville Simulcast",
        "system": SYSTEM_NAME,
        "sysid": SYSTEM_SYSID,
        "wacn": SYSTEM_WACN,
        "rfss": 1,
        "site_number": 2,
        "nac": "692",
        "control_hz": (856_212_500, 857_587_500, 858_212_500, 859_212_500),
    },
    {
        "id": "rr8438:1-003",
        "region": "Texas",
        "county": "Denton",
        "site": "Denton City",
        "system": SYSTEM_NAME,
        "sysid": SYSTEM_SYSID,
        "wacn": SYSTEM_WACN,
        "rfss": 1,
        "site_number": 3,
        "nac": "693",
        "control_hz": (856_312_500, 857_312_500, 858_312_500, 858_587_500),
    },
    {
        "id": "rr8438:1-004",
        "region": "Texas",
        "county": "Cooke",
        "site": "Muenster",
        "system": SYSTEM_NAME,
        "sysid": SYSTEM_SYSID,
        "wacn": SYSTEM_WACN,
        "rfss": 1,
        "site_number": 4,
        "nac": "690",
        "control_hz": (772_681_250, 774_493_750),
    },
    {
        "id": "rr8438:1-005",
        "region": "Texas",
        "county": "Cooke",
        "site": "Gainesville",
        "system": SYSTEM_NAME,
        "sysid": SYSTEM_SYSID,
        "wacn": SYSTEM_WACN,
        "rfss": 1,
        "site_number": 5,
        "nac": "690",
        "control_hz": (772_356_250, 774_181_250),
    },
    {
        "id": "rr8438:1-006",
        "region": "Texas",
        "county": "Cooke",
        "site": "Cooke Northwest",
        "system": SYSTEM_NAME,
        "sysid": SYSTEM_SYSID,
        "wacn": SYSTEM_WACN,
        "rfss": 1,
        "site_number": 6,
        "nac": "690",
        "control_hz": (773_931_250,),
    },
    {
        "id": "rr8438:1-007",
        "region": "Texas",
        "county": "Cooke",
        "site": "Cooke Northeast",
        "system": SYSTEM_NAME,
        "sysid": SYSTEM_SYSID,
        "wacn": SYSTEM_WACN,
        "rfss": 1,
        "site_number": 7,
        "nac": "690",
        "control_hz": (771_556_250, 772_931_250),
    },
)


def all_sites(settings=None):
    """Return copies of imported sites followed by validated user sites."""
    sites = [deepcopy(site) for site in BUILTIN_SITES]
    if isinstance(settings, dict):
        custom = settings.get("p25_custom_sites", ())
        if isinstance(custom, list):
            sites.extend(deepcopy(site) for site in custom if isinstance(site, dict))
    return sites


def find_site(settings, site_id):
    """Return a site by stable ID, or ``None`` when it no longer exists."""
    if not isinstance(site_id, str) or not site_id:
        return None
    return next((site for site in all_sites(settings)
                 if site.get("id") == site_id), None)


def find_builtin_site_for_frequency(frequency_hz):
    """Return the imported site containing ``frequency_hz``, if any."""
    return next((deepcopy(site) for site in BUILTIN_SITES
                 if frequency_hz in site["control_hz"]), None)
