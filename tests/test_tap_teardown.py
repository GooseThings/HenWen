"""Tests for the AudioSocket tap teardown helpers (_hangup_tap_channel and
friends). The AMI is replaced by a fake: there's no live Asterisk here, so
the sample `core show channels concise` rows below were captured from a real
Asterisk 22 with a live tap (Originate ChannelId=henwen-tap-lab-H1) rather
than invented, since the uniqueid/';2' suffix convention is what the leak
check keys on.
"""
import app

CONCISE_LIVE = [
    "Local/tap@henwen-audiosocket-tap-000000ce;2!henwen-audiosocket-tap!tap!2!Up!AudioSocket!"
    "3556791e-78d1-42cd-ad5e-d88b3a66460f,127.0.0.1:47419!!!!3!2!!henwen-tap-lab-H1;2",
    "Local/tap@henwen-audiosocket-tap-000000ce;1!henwen-audiosocket-tap!tap!1!Up!ChanSpy!"
    "SimpleUSB/643930,q!!!!3!2!!henwen-tap-lab-H1",
    "SimpleUSB/643930!default!!1!Up!Rpt!643930|P!!!!3!2!!1791292431.0",
]


class FakeAMI:
    def __init__(self, hangup_raw, listings, timeout=1):
        self.timeout = timeout
        self._raw = hangup_raw
        self._listings = list(listings)   # one entry per `core show channels concise` call
        self.sent = []

    def _send_action(self, params):
        self.sent.append(params)

    def _recv_until(self, term, timeout=None):
        return self._raw

    def _parse_packet(self, raw):
        return app.AMIClient._parse_packet(None, raw)

    def command(self, cmd, log_level="INFO"):
        assert cmd == 'core show channels concise'
        item = self._listings.pop(0) if len(self._listings) > 1 else self._listings[0]
        if isinstance(item, Exception):
            raise item
        return item


def _patch(monkeypatch, ami):
    logs = []
    monkeypatch.setattr(app, 'ami_send_command', lambda fn: fn(ami))
    monkeypatch.setattr(app, 'log', lambda lvl, msg, *a, **k: logs.append((lvl, msg)))
    monkeypatch.setattr(app.time, 'sleep', lambda s: None)
    return logs


def _warns(logs):
    return [m for lvl, m in logs if lvl in ('WARN', 'ERROR')]


class TestParseConcise:
    def test_extracts_name_app_uniqueid(self):
        rows = app._parse_concise_channels(CONCISE_LIVE)
        assert rows[0] == {'name': 'Local/tap@henwen-audiosocket-tap-000000ce;2',
                           'app': 'AudioSocket', 'uniqueid': 'henwen-tap-lab-H1;2',
                           'duration': 2}
        assert rows[1]['uniqueid'] == 'henwen-tap-lab-H1' and rows[1]['app'] == 'ChanSpy'

    def test_skips_short_or_blank_rows(self):
        assert app._parse_concise_channels(['', 'garbage!only!three', 'a!b!c']) == []


class TestClassifyHangupReply:
    def test_success(self):
        assert app._classify_hangup_reply({'Response': 'Success', 'Message': 'Channel Hungup'}) == 'ok'

    def test_no_such_channel_is_gone(self):
        assert app._classify_hangup_reply({'Response': 'Error', 'Message': 'No such channel'}) == 'gone'

    def test_other_error(self):
        assert app._classify_hangup_reply({'Response': 'Error', 'Message': 'Permission denied'}) == 'error'

    def test_empty_packet_is_error(self):
        assert app._classify_hangup_reply({}) == 'error'


class TestTapLegsAlive:
    def test_matches_both_legs_but_not_a_prefix_collision(self, monkeypatch):
        _patch(monkeypatch, FakeAMI('', [CONCISE_LIVE]))
        assert app._tap_legs_alive('henwen-tap-lab-H1') is True
        # 'H1' must not match 'H10' or a different node's tap
        assert app._tap_legs_alive('henwen-tap-lab-H') is False

    def test_listing_failure_is_unknown_not_a_leak(self, monkeypatch):
        _patch(monkeypatch, FakeAMI('', [RuntimeError('boom')]))
        assert app._tap_legs_alive('henwen-tap-lab-H1') is None


class TestHangupTapChannel:
    OK  = 'Response: Success\r\nMessage: Channel Hungup\r\n\r\n'
    GONE = 'Response: Error\r\nMessage: No such channel\r\n\r\n'
    BAD = 'Response: Error\r\nMessage: Permission denied\r\n\r\n'

    def test_clean_hangup_confirms_gone_without_warnings(self, monkeypatch):
        ami = FakeAMI(self.OK, [CONCISE_LIVE[2:]])   # nothing tap-shaped left
        logs = _patch(monkeypatch, ami)
        assert app._hangup_tap_channel('henwen-tap-lab-H1', '643930', 't') is True
        assert ami.sent == [{'Action': 'Hangup', 'Channel': 'henwen-tap-lab-H1'}]
        assert _warns(logs) == []

    def test_already_gone_is_not_a_warning(self, monkeypatch):
        logs = _patch(monkeypatch, FakeAMI(self.GONE, [CONCISE_LIVE[2:]]))
        assert app._hangup_tap_channel('henwen-tap-lab-H1', '643930', 't') is True
        assert _warns(logs) == []

    def test_rejected_hangup_warns(self, monkeypatch):
        logs = _patch(monkeypatch, FakeAMI(self.BAD, [CONCISE_LIVE[2:]]))
        app._hangup_tap_channel('henwen-tap-lab-H1', '643930', 't')
        assert any('rejected' in m and 'Permission denied' in m for m in _warns(logs))

    def test_leg_that_ignores_hangup_is_reported_as_leaked(self, monkeypatch):
        logs = _patch(monkeypatch, FakeAMI(self.OK, [CONCISE_LIVE]))   # still listed every poll
        assert app._hangup_tap_channel('henwen-tap-lab-H1', '643930', 't') is False
        assert any('likely leaked' in m for m in _warns(logs))

    def test_leg_that_disappears_during_the_wait_is_fine(self, monkeypatch):
        logs = _patch(monkeypatch, FakeAMI(self.OK, [CONCISE_LIVE, CONCISE_LIVE, CONCISE_LIVE[2:]]))
        assert app._hangup_tap_channel('henwen-tap-lab-H1', '643930', 't') is True
        assert _warns(logs) == []

    def test_ami_exception_warns_instead_of_vanishing(self, monkeypatch):
        logs = []
        monkeypatch.setattr(app, 'log', lambda lvl, msg, *a, **k: logs.append((lvl, msg)))
        def boom(fn): raise ConnectionError('AMI down')
        monkeypatch.setattr(app, 'ami_send_command', boom)
        assert app._hangup_tap_channel('henwen-tap-lab-H1', '643930', 't') is False
        assert any('AMI down' in m and 'may be leaked' in m for m in _warns(logs))

    def test_unverifiable_listing_does_not_cry_leak(self, monkeypatch):
        logs = _patch(monkeypatch, FakeAMI(self.OK, [RuntimeError('listing failed')]))
        assert app._hangup_tap_channel('henwen-tap-lab-H1', '643930', 't') is False
        assert not any('leaked' in m for m in _warns(logs))
