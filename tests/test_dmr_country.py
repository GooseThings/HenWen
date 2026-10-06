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


def test_dmr_lookup_returns_callsign_and_name(tmp_path, monkeypatch):
    f = tmp_path / "DMRIds.dat"
    f.write_text("3125678 N8GMZ Levi G\n3125679 W1AW\n")
    monkeypatch.setattr(henwen, "DVSWITCH_DMRIDS_PATH", str(f))
    monkeypatch.setattr(henwen, "_dvswitch_caller_id_cache", {})
    monkeypatch.setattr(henwen, "_radioid_fetch_name", lambda i: (False, None))
    assert henwen._dvswitch_lookup_dmr("3125678") == ("N8GMZ", "Levi G")
    assert henwen._dvswitch_lookup_dmr("3125679") == ("W1AW", None)
    assert henwen._dvswitch_lookup_dmr("1") == (None, None)


def test_radioid_full_name_joins_first_and_surname():
    assert henwen._radioid_full_name({"results": [{"fname": "Gary", "surname": "Smith"}]}) == "Gary Smith"
    assert henwen._radioid_full_name({"results": [{"fname": "Gary", "surname": ""}]}) == "Gary"
    assert henwen._radioid_full_name({"results": []}) is None
    assert henwen._radioid_full_name(None) is None


def test_full_name_from_radioid_wins_and_is_cached(tmp_path, monkeypatch):
    f = tmp_path / "DMRIds.dat"
    f.write_text("3125678 N8GMZ Levi\n")
    monkeypatch.setattr(henwen, "DVSWITCH_DMRIDS_PATH", str(f))
    monkeypatch.setattr(henwen, "_dvswitch_caller_id_cache", {})
    calls = []
    monkeypatch.setattr(henwen, "_radioid_fetch_name", lambda i: (calls.append(i) or (True, "Levi Gates")))
    assert henwen._dvswitch_lookup_dmr("3125678") == ("N8GMZ", "Levi Gates")
    henwen._dvswitch_lookup_dmr("3125678")
    assert calls == ["3125678"]


def test_failed_radioid_call_falls_back_then_retries(tmp_path, monkeypatch):
    f = tmp_path / "DMRIds.dat"
    f.write_text("3125678 N8GMZ Levi\n")
    monkeypatch.setattr(henwen, "DVSWITCH_DMRIDS_PATH", str(f))
    monkeypatch.setattr(henwen, "_dvswitch_caller_id_cache", {})
    results = iter([(False, None), (True, "Levi Gates")])
    monkeypatch.setattr(henwen, "_radioid_fetch_name", lambda i: next(results))
    assert henwen._dvswitch_lookup_dmr("3125678") == ("N8GMZ", "Levi")
    henwen._dvswitch_caller_id_cache["3125678"] = ("N8GMZ", "Levi", 0)   # retry window elapsed
    assert henwen._dvswitch_lookup_dmr("3125678") == ("N8GMZ", "Levi Gates")


def test_radioid_fetch_name_parses_response(monkeypatch):
    import io

    class _Resp(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False

    seen = {}
    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        return _Resp(b'{"results":[{"fname":"Gary","surname":"Smith"}]}')
    monkeypatch.setattr(henwen.urlreq, "urlopen", fake_urlopen)
    assert henwen._radioid_fetch_name("3226835") == (True, "Gary Smith")
    assert seen["url"].endswith("?id=3226835")
