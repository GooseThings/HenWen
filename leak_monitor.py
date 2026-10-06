"""Resource-leak detection helpers for HenWen's leak monitor.

Pure functions over /proc and plain data -- no Flask, no DB, no AMI, same
independence story as recording.py / stream_relay.py / irc_relay.py. app.py
owns the schedule, the alert plumbing and the per-condition on/off state;
this module only answers "what do the numbers look like right now" and "do
they cross a threshold".

Born from a real incident: Asterisk's soft fd limit was 1024, 456 of those
descriptors were AMI sockets stuck in CLOSE_WAIT, and its log grew 171 GB in
two days with nothing alerting until the disk was 92% full. Each check below
corresponds to one stage of that failure.
"""
import os
import re
from collections import Counter

TCP_STATES = {
    '01': 'ESTABLISHED', '02': 'SYN_SENT', '03': 'SYN_RECV', '04': 'FIN_WAIT1',
    '05': 'FIN_WAIT2', '06': 'TIME_WAIT', '07': 'CLOSE', '08': 'CLOSE_WAIT',
    '09': 'LAST_ACK', '0A': 'LISTEN', '0B': 'CLOSING',
}

_SOCKET_RE = re.compile(r'^socket:\[(\d+)\]$')


def find_pid(name, pidfile=None, proc='/proc'):
    """PID of the process called `name`: from `pidfile` if it names a live
    process, else a scan of /proc/*/comm. None if not found."""
    if pidfile:
        try:
            with open(pidfile) as f:
                pid = int(f.read().strip())
            if os.path.isdir(f'{proc}/{pid}'):
                return pid
        except (OSError, ValueError):
            pass
    try:
        entries = os.listdir(proc)
    except OSError:
        return None
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open(f'{proc}/{entry}/comm') as f:
                if f.read().strip() == name:
                    return int(entry)
        except OSError:
            continue
    return None


def read_fd_limit(pid, proc='/proc'):
    """The process's soft 'Max open files' limit, or None if unlimited or
    unreadable."""
    try:
        with open(f'{proc}/{pid}/limits') as f:
            for line in f:
                if line.startswith('Max open files'):
                    soft = line.split()[3]
                    return int(soft) if soft.isdigit() else None
    except (OSError, IndexError):
        pass
    return None


def snapshot_fds(pid, proc='/proc', max_classify=3000):
    """Count a process's open descriptors and classify (up to `max_classify`
    of) them. Returns {'count', 'limit', 'kinds': Counter, 'socket_inodes':
    [int, ...]} or None if the process can't be read (gone, or not ours).
    The cap bounds the cost if a runaway leak has tens of thousands of fds;
    `count` is always the true total."""
    fd_dir = f'{proc}/{pid}/fd'
    try:
        names = os.listdir(fd_dir)
    except OSError:
        return None
    kinds = Counter()
    inodes = []
    for name in names[:max_classify]:
        try:
            target = os.readlink(f'{fd_dir}/{name}')
        except OSError:
            continue    # closed between listdir and readlink
        m = _SOCKET_RE.match(target)
        if m:
            kinds['socket'] += 1
            inodes.append(int(m.group(1)))
        elif target.startswith('anon_inode:'):
            kinds['anon_inode'] += 1
        elif target.startswith('pipe:'):
            kinds['pipe'] += 1
        elif target.startswith('/'):
            kinds['file'] += 1
        else:
            kinds['other'] += 1
    return {'count': len(names), 'limit': read_fd_limit(pid, proc),
            'kinds': kinds, 'socket_inodes': inodes}


def parse_proc_net_tcp(text):
    """Parse /proc/net/tcp (or tcp6) into {socket_inode: (state, local_port)}."""
    table = {}
    for line in text.splitlines()[1:]:
        f = line.split()
        if len(f) < 10:
            continue
        try:
            port = int(f[1].rsplit(':', 1)[1], 16)
            inode = int(f[9])
        except (IndexError, ValueError):
            continue
        table[inode] = (TCP_STATES.get(f[3].upper(), f[3]), port)
    return table


def read_tcp_table(proc='/proc'):
    table = {}
    for name in ('tcp', 'tcp6'):
        try:
            with open(f'{proc}/net/{name}') as f:
                table.update(parse_proc_net_tcp(f.read()))
        except OSError:
            pass
    return table


def socket_state_counts(socket_inodes, tcp_table):
    """Tally a process's TCP sockets as {(state, local_port): n}."""
    counts = Counter()
    for inode in socket_inodes:
        hit = tcp_table.get(inode)
        if hit:
            counts[hit] += 1
    return counts


def port_state_counts(tcp_table, port):
    """Tally every non-listening TCP socket whose LOCAL port is `port` as
    {(state, port): n}. Needs no per-process access, which matters: Asterisk
    runs with a Linux capability, so its /proc/<pid>/fd links are unreadable
    to HenWen even though both run as the same user (the fd *count* and
    limit are still readable). For a server's well-known port -- Asterisk's
    AMI listener -- the local-port-matching sockets are the server side of
    each client connection, i.e. exactly the ones that go stale."""
    counts = Counter()
    for state, local in tcp_table.values():
        if local == port and state != 'LISTEN':
            counts[(state, local)] += 1
    return counts


def count_state(counts, state):
    return sum(n for (st, _), n in counts.items() if st == state)


def describe_sockets(counts, state, top=3):
    """'CLOSE_WAIT on :5038 x456, :8088 x2' for the busiest ports in `state`."""
    rows = sorted(((n, port) for (st, port), n in counts.items() if st == state), reverse=True)
    return ', '.join(f':{port} x{n}' for n, port in rows[:top])


def growth_over_window(samples, window_sec, now):
    """How far a counter has climbed within the trailing window: latest
    value minus the window's minimum. `samples` is [(ts, value), ...]
    oldest-first. Returns 0 until the history spans at least half the
    window -- a freshly started monitor can't call a trend yet."""
    recent = [(t, v) for t, v in samples if now - t <= window_sec]
    if len(recent) < 2 or recent[-1][0] - recent[0][0] < window_sec / 2:
        return 0
    return recent[-1][1] - min(v for _, v in recent)


def _fmt_bytes(n):
    for unit in ('B', 'KB', 'MB', 'GB'):
        if n < 1024 or unit == 'GB':
            return f'{n:.1f} {unit}' if unit != 'B' else f'{n} B'
        n /= 1024.0


def _fd_findings(prefix, label, snap, growth, th):
    out = {}
    if not snap:
        return out
    count, limit = snap['count'], snap['limit']
    top = ', '.join(f'{n} {k}' for k, n in Counter(snap['kinds']).most_common(3))
    kinds = f'; mostly {top}' if top else ''
    pct = th['fd_pct']
    over = bool(limit) and count * 100 >= limit * pct
    out[f'{prefix}_fds_high'] = (
        over, f'{label} is using {count} of {limit} file descriptors '
              f'({count * 100 // limit}%, threshold {pct}%{kinds}).' if over else '')
    grow = th['fd_growth']
    grew = growth >= grow
    out[f'{prefix}_fds_growing'] = (
        grew, f'{label} has gained {growth} file descriptors in the last '
              f'{th["fd_growth_window"] // 60} min without releasing them '
              f'(now {count}{kinds}).' if grew else '')
    return out


def evaluate(readings, th):
    """Turn raw readings into findings: {key: (active, message)}.

    Every key this can ever produce is always present for a given set of
    readings, so the caller can tell "cleared" (active False) from "never
    looked" (key absent) -- a reading that's unavailable (process not
    found, AMI down) simply contributes no keys, leaving that condition's
    existing state alone rather than falsely clearing it.

    readings keys (all optional): 'asterisk' and 'henwen' (snapshot_fds
    dicts), 'asterisk_growth'/'henwen_growth' (int), 'asterisk_sockets'
    (socket_state_counts), 'orphan_taps' (list of channel ids), 'log_bytes'.
    """
    out = {}
    out.update(_fd_findings('asterisk', 'Asterisk', readings.get('asterisk'),
                            readings.get('asterisk_growth', 0), th))
    out.update(_fd_findings('henwen', 'HenWen', readings.get('henwen'),
                            readings.get('henwen_growth', 0), th))

    counts = readings.get('asterisk_sockets')
    if counts is not None:
        n = count_state(counts, 'CLOSE_WAIT')
        bad = n >= th['closewait']
        out['asterisk_closewait'] = (
            bad, f'Asterisk is holding {n} sockets in CLOSE_WAIT '
                 f'({describe_sockets(counts, "CLOSE_WAIT")}) -- peers hung up and '
                 f'Asterisk never closed its end. Each one costs a file descriptor.'
                 if bad else '')

    taps = readings.get('orphan_taps')
    if taps is not None:
        out['tap_orphans'] = (
            bool(taps), f'{len(taps)} AudioSocket tap channel(s) are still up with no '
                        f'owning broadcast: {", ".join(sorted(taps)[:3])}'
                        f'{"..." if len(taps) > 3 else ""}. A leaked tap holds channels '
                        f'and descriptors until Asterisk restarts.' if taps else '')

    size = readings.get('log_bytes')
    if size is not None:
        big = size >= th['log_bytes']
        out['asterisk_log_big'] = (
            big, f'Asterisk\'s messages.log is {_fmt_bytes(size)} (threshold '
                 f'{_fmt_bytes(th["log_bytes"])}) -- usually a runaway warning loop; '
                 f'check the tail of /var/log/asterisk/messages.log.' if big else '')
    return out
