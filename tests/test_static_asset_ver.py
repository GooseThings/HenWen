"""Unit tests for _static_asset_ver(), the cache-bust token appended to the
low-latency RX worklet's addModule() URL.

The worklet is fetched by URL at runtime rather than as a <script> tag, and
Flask serves /static with `Cache-Control: no-cache` -- which mandates
revalidation but does not stop Chrome's in-memory cache from satisfying a
same-session reuse with the old body. So a deployed worklet fix could keep
being ignored by an already-open board; this token is what makes the URL
itself change on deploy. Pure filesystem logic -- no network, DB, or Flask
context involved.
"""
import os

import app


class TestStaticAssetVer:
    def test_returns_mtime_of_the_asset(self, tmp_path, monkeypatch):
        static = tmp_path / 'static'
        static.mkdir()
        asset = static / 'thing.js'
        asset.write_text('// x\n')
        os.utime(asset, (1_700_000_000, 1_700_000_000))
        monkeypatch.setattr(app.os.path, 'abspath', lambda _p: str(tmp_path / 'app.py'))

        assert app._static_asset_ver('thing.js') == '1700000000'

    def test_token_changes_when_the_asset_changes(self, tmp_path, monkeypatch):
        """The whole point: an in-place deploy that rewrites the worklet has
        to produce a different URL, or an open board keeps the old one."""
        static = tmp_path / 'static'
        static.mkdir()
        asset = static / 'thing.js'
        asset.write_text('// before\n')
        os.utime(asset, (1_700_000_000, 1_700_000_000))
        monkeypatch.setattr(app.os.path, 'abspath', lambda _p: str(tmp_path / 'app.py'))
        before = app._static_asset_ver('thing.js')

        asset.write_text('// after\n')
        os.utime(asset, (1_700_000_900, 1_700_000_900))
        after = app._static_asset_ver('thing.js')

        assert before != after

    def test_falls_back_to_release_string_when_asset_missing(self, tmp_path, monkeypatch):
        """A missing or unreadable asset must not fail the page render."""
        monkeypatch.setattr(app.os.path, 'abspath', lambda _p: str(tmp_path / 'app.py'))
        assert app._static_asset_ver('nope.js') == app.HENWEN_VERSION

    def test_real_worklet_resolves_to_a_digit_token(self):
        """Guards the actual wiring: the filename the status route passes in
        must exist in this checkout, or every board render silently falls
        back to the release string and stops busting on deploy."""
        token = app._static_asset_ver('audio-worklet-lowlatency.js')
        assert token.isdigit(), token
