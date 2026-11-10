from watchdogs.p25_catalog import (
    BUILTIN_SITES,
    all_sites,
    find_site,
)


def test_builtin_catalog_has_the_rr_control_channels_and_metadata():
    assert len(BUILTIN_SITES) == 7
    assert sum(len(site["control_hz"]) for site in BUILTIN_SITES) == 19
    assert len({site["id"] for site in BUILTIN_SITES}) == len(BUILTIN_SITES)
    for site in BUILTIN_SITES:
        assert site["region"] == "Texas"
        assert site["sysid"] == "0x693"
        assert site["wacn"] == "0xBEE00"
        assert type(site["rfss"]) is int
        assert type(site["site_number"]) is int
        assert site["control_hz"]


def test_catalog_merges_custom_sites_and_returns_independent_values():
    settings = {
        "p25_custom_sites": [
            {
                "id": "custom:local",
                "region": "Test",
                "county": "Local",
                "site": "Site",
                "system": "Test System",
                "control_hz": [851_000_000],
            }
        ]
    }
    sites = all_sites(settings)
    assert len(sites) == 8
    assert find_site(settings, "custom:local")["control_hz"] == [851_000_000]
    sites[0]["site"] = "changed"
    assert BUILTIN_SITES[0]["site"] == "Denton County Simulcast"
    assert find_site(settings, "missing") is None
