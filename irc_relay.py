"""
irc_relay.py -- two-way IRC bridge for the kiosk Chat panel.

Deliberately independent of app.py, matching recording.py/stream_relay.py's
pattern: no import-time dependency on Flask or the SQLite DB, no shared
globals. app.py's background loop (_irc_relay_loop) drives an IRCClient
instance directly -- connect()/identify()/join()/read_loop() -- and hands
in a plain on_message(nick, text) callback rather than this module ever
reaching back into app.py's chat_messages table itself. If this file were
deleted, the rest of the app (including the one-way Discord chat relay it
sits next to) keeps working unmodified.

No pip dependency for the IRC protocol itself: it's CRLF-terminated text
lines under 512 bytes with a handful of numeric replies and PRIVMSG/PING/
JOIN commands -- simpler than the AMI protocol app.py's own AMIClient
already hand-rolls over a raw socket, and far simpler than the binary/
framed protocols aprslib/paho-mqtt were pulled in for elsewhere in this
app. A hand-rolled client matches that existing precedent instead of
adding a third socket-handling style or a new requirements.txt entry.

Reconnect/backoff is deliberately NOT this module's job -- IRCClient stays
"dumb": one connect(), one read_loop(), raises IRCConnectionError or
returns on any failure. app.py's _irc_relay_loop() owns the state machine
(re-reading owner config every reconnect cycle, exponential backoff),
mirroring _meshtastic_poll_loop()'s shape.
"""
import queue
import socket
import ssl
import threading
import time

# Paced outbound send rate -- a flat, conservative rate comfortably under
# every major IRC network's flood-control threshold. Not tuned against any
# specific network's actual policy (this is a chat relay, not a high-volume
# bot, so precision here matters less than just staying safely under any
# reasonable limit).
SEND_INTERVAL_SEC = 1.0

# If no line at all (not even a keepalive PING, which well-behaved servers
# send every 1-5 minutes) arrives within this long, treat the connection as
# dead and let the caller reconnect -- a plain TCP socket gives no other
# signal that a network has silently vanished out from under it.
IDLE_TIMEOUT_SEC = 300

# Kiosk chat already caps messages at CHAT_MESSAGE_MAX_LEN=500 chars
# (app.py), so this is a safety net for IRC's 512-byte line limit
# (accounting for "PRIVMSG #channel :" + CRLF overhead), not the primary
# length control.
MAX_LINE_TEXT_LEN = 400


class IRCConnectionError(Exception):
    """Raised on any I/O/registration/join failure. The app.py driver loop
    catches this to trigger reconnect/backoff -- the same role a raised
    exception plays for AMIClient's caller."""


def parse_irc_line(line: str) -> dict:
    """Parses one raw IRC protocol line (no trailing CRLF) into
    {'prefix': str|None, 'command': str, 'params': list[str], 'trailing': str|None}.
    Pure string logic, no I/O. Handles the leading ':<prefix>' form, a
    trailing ':<text with spaces>' param, and prefix-less lines (e.g. a
    bare PING from the server)."""
    line = line.rstrip("\r\n")
    prefix = None
    if line.startswith(":"):
        prefix, _, line = line[1:].partition(" ")
    if " :" in line:
        line, _, trailing = line.partition(" :")
    else:
        trailing = None
    params = [p for p in line.split(" ") if p]
    command = params[0].upper() if params else ""
    params = params[1:]
    if trailing is not None:
        params = params + [trailing]
    return {"prefix": prefix, "command": command, "params": params, "trailing": trailing}


def extract_privmsg(parsed: dict):
    """Given parse_irc_line()'s output, returns (nick, target, text) if
    this is a channel/user PRIVMSG, else None. Nick is extracted from the
    'nick!user@host' prefix form."""
    if parsed["command"] != "PRIVMSG" or not parsed["prefix"] or len(parsed["params"]) < 2:
        return None
    nick = parsed["prefix"].split("!", 1)[0]
    target = parsed["params"][0]
    text = parsed["params"][-1]
    return (nick, target, text)


def format_privmsg(target: str, text: str) -> str:
    """Builds 'PRIVMSG <target> :<text>'. Strips embedded CR/LF from text --
    a message containing a raw newline could otherwise inject a second IRC
    command onto the wire."""
    text = text.replace("\r", " ").replace("\n", " ")
    return f"PRIVMSG {target} :{text}"


def split_for_irc(text: str, max_len: int = MAX_LINE_TEXT_LEN):
    """Splits text longer than max_len into multiple lines, breaking on
    whitespace where possible. Returns [] for empty/whitespace-only text."""
    text = text.strip()
    if not text:
        return []
    if len(text) <= max_len:
        return [text]
    lines = []
    while text:
        if len(text) <= max_len:
            lines.append(text)
            break
        cut = text.rfind(" ", 0, max_len)
        if cut <= 0:
            cut = max_len
        lines.append(text[:cut])
        text = text[cut:].lstrip()
    return lines


def is_self(nick: str, own_nick: str) -> bool:
    """Case-insensitive nick match -- IRC nicks are case-insensitive on the
    wire. Used to drop self-originated PRIVMSGs (e.g. from a server that
    echoes a client's own messages back) before they'd otherwise get
    relayed straight back into kiosk chat."""
    return (nick or "").lower() == (own_nick or "").lower()


class IRCClient:
    """One persistent connection to one IRC server/channel. Not
    reconnect-aware -- see module docstring."""

    def __init__(self, host, port, use_tls, nick, channel, channel_key="",
                 nickserv_password="", log_fn=None, timeout=15):
        self.host = host
        self.port = port
        self.use_tls = use_tls
        self.nick = nick
        self.channel = channel
        self.channel_key = channel_key
        self.nickserv_password = nickserv_password
        self.log_fn = log_fn or (lambda msg: None)
        self.timeout = timeout

        self._sock = None
        self._buf = ""
        self._dead = False
        self._send_queue = queue.Queue()
        self._send_thread = None

    def _log(self, msg):
        self.log_fn(msg)

    def _readline(self) -> str:
        """Blocks until one complete line is available. Raises
        IRCConnectionError on EOF, socket error, or read timeout (the
        latter governed by whatever settimeout() the caller last set --
        a short one during connect()/join(), IDLE_TIMEOUT_SEC during
        read_loop())."""
        while "\r\n" not in self._buf:
            try:
                chunk = self._sock.recv(4096)
            except socket.timeout:
                raise IRCConnectionError("Read timed out")
            except OSError as e:
                raise IRCConnectionError(f"Socket error: {e}")
            if not chunk:
                raise IRCConnectionError("Connection closed by server")
            self._buf += chunk.decode("utf-8", errors="replace")
        line, self._buf = self._buf.split("\r\n", 1)
        return line

    def _send_line(self, line: str):
        try:
            self._sock.sendall((line + "\r\n").encode("utf-8"))
        except OSError as e:
            self._dead = True
            raise IRCConnectionError(f"Send failed: {e}")

    def connect(self):
        """Opens the socket (TLS-wrapped if use_tls), performs the NICK/USER
        registration handshake, and blocks until RPL_WELCOME (001) or a
        registration failure. No CAP negotiation at all -- deliberately
        never requests echo-message, so there's nothing to filter for the
        common case (is_self() in read_loop() is the backstop for networks
        that echo by default)."""
        raw = socket.create_connection((self.host, self.port), timeout=self.timeout)
        if self.use_tls:
            ctx = ssl.create_default_context()
            self._sock = ctx.wrap_socket(raw, server_hostname=self.host)
        else:
            self._sock = raw
        self._sock.settimeout(self.timeout)

        self._send_line(f"NICK {self.nick}")
        self._send_line(f"USER {self.nick} 0 * :HenWen IRC Relay")

        while True:
            parsed = parse_irc_line(self._readline())
            if parsed["command"] == "PING":
                self._send_line(f"PONG :{parsed['trailing'] or ''}")
                continue
            if parsed["command"] == "001":
                self._log(f"Registered with {self.host} as {self.nick}")
                break
            if parsed["command"] == "ERROR":
                raise IRCConnectionError(f"Server error during registration: {parsed['trailing']}")
            if parsed["command"] in ("432", "433", "436"):
                raise IRCConnectionError(f"Nick {self.nick!r} rejected ({parsed['command']})")

        self._dead = False
        self._send_thread = threading.Thread(
            target=self._sender_loop, name="irc-relay-send", daemon=True)
        self._send_thread.start()

    def identify(self):
        """PRIVMSG NickServ :IDENTIFY <password> -- not SASL, just a plain
        in-band identify, matching how a human would do it manually."""
        self._send_line(f"PRIVMSG NickServ :IDENTIFY {self.nickserv_password}")

    def join(self):
        """JOIN <channel>[ <channel_key>], blocks until our own JOIN is
        echoed back, RPL_ENDOFNAMES (366), or a join-failure numeric."""
        if self.channel_key:
            self._send_line(f"JOIN {self.channel} {self.channel_key}")
        else:
            self._send_line(f"JOIN {self.channel}")

        while True:
            parsed = parse_irc_line(self._readline())
            command = parsed["command"]
            if command == "PING":
                self._send_line(f"PONG :{parsed['trailing'] or ''}")
                continue
            if command == "JOIN" and parsed["prefix"] and parsed["params"] and \
                    parsed["prefix"].split("!", 1)[0].lower() == self.nick.lower() and \
                    parsed["params"][0].lower() == self.channel.lower():
                self._log(f"Joined {self.channel}")
                return
            if command == "366":  # RPL_ENDOFNAMES
                self._log(f"Joined {self.channel}")
                return
            if command in ("471", "473", "474", "475"):
                raise IRCConnectionError(f"Failed to join {self.channel} ({command})")
            if command == "ERROR":
                raise IRCConnectionError(f"Server error while joining: {parsed['trailing']}")

    def send_message(self, text: str):
        """Non-blocking: splits text and enqueues onto the internal paced
        send queue, drained by the sender thread started in connect()."""
        for line_text in split_for_irc(text):
            self._send_queue.put(format_privmsg(self.channel, line_text))

    def _sender_loop(self):
        while not self._dead:
            line = self._send_queue.get()
            if line is None:  # shutdown sentinel, see close()
                return
            try:
                self._send_line(line)
            except IRCConnectionError:
                return
            time.sleep(SEND_INTERVAL_SEC)

    def read_loop(self, on_message):
        """Blocking; runs in the caller's thread until the connection ends.
        Answers PING with PONG inline, calls on_message(nick, text) for
        PRIVMSGs targeting our channel (self-originated ones filtered via
        is_self()). Returns (does not raise) on a clean disconnect/ERROR/
        idle timeout -- the caller (app.py's _irc_relay_loop) treats
        "read_loop returned" the same as a raised connection error: time
        to reconnect."""
        self._sock.settimeout(IDLE_TIMEOUT_SEC)
        while not self._dead:
            try:
                parsed = parse_irc_line(self._readline())
            except IRCConnectionError as e:
                self._log(f"Read loop ending: {e}")
                return
            command = parsed["command"]
            if command == "PING":
                try:
                    self._send_line(f"PONG :{parsed['trailing'] or ''}")
                except IRCConnectionError:
                    return
                continue
            if command == "ERROR":
                self._log(f"Server sent ERROR: {parsed['trailing']}")
                return
            if command == "PRIVMSG":
                result = extract_privmsg(parsed)
                if not result:
                    continue
                nick, target, text = result
                if target.lower() != self.channel.lower() or is_self(nick, self.nick):
                    continue
                try:
                    on_message(nick, text)
                except Exception as e:
                    self._log(f"on_message callback failed (not fatal): {e}")

    def close(self):
        self._dead = True
        try:
            if self._sock:
                self._send_line("QUIT :HenWen shutting down")
        except Exception:
            pass
        try:
            self._send_queue.put_nowait(None)
        except Exception:
            pass
        try:
            if self._sock:
                self._sock.close()
        except Exception:
            pass

    @property
    def alive(self) -> bool:
        return not self._dead
