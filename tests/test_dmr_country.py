import app as henwen


def test_us_dmr_id_maps_to_us():
    assert henwen._dmr_id_country_iso("3125678") == "US"


def test_hotspot_suffixed_id_uses_leading_mcc():
    assert henwen._dmr_id_country_iso("312567801") == "US"


def test_legacy_six_digit_id_has_no_country():
    assert henwen._dmr_id_country_iso("312567") == ""


def test_unknown_prefix_and_junk_give_empty():
    assert henwen._dmr_id_country_iso("9999999") == ""
    assert henwen._dmr_id_country_iso(None) == ""
    assert henwen._dmr_id_country_iso("abcdefg") == ""


def test_every_mapped_country_has_a_vendored_flag():
    import os
    d = os.path.join(os.path.dirname(henwen.__file__), "static", "vendor", "flags")
    missing = sorted({i for i in henwen._DMR_MCC_TO_ISO.values()
                      if not os.path.isfile(os.path.join(d, i.lower() + ".svg"))})
    assert not missing
