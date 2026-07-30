# HenWen + Tailscale Funnel — Setup Notes & Open Issue

Context dump from a live debugging session (2026-07-30) on `sandbox2`, getting
HenWen reachable on the public internet via Tailscale Funnel. Written up so
Claude Code can pick up where this left off.

## Environment

- Host: `sandbox2` — an LXC container (Proxmox), container-local IP `10.10.10.21`
- HenWen installed at `/opt/HenWen`, running as `HenWen.service` (gunicorn,
  `--bind 0.0.0.0:5000`)
- Tailscale node: `sandbox2.bombay-chroma.ts.net`
- Box's actual public IP (per setup-https.sh's own check): `24.247.3.110`

## What was broken, in order, and how each was fixed

### 1. `tailscale up` failed — tailscaled wouldn't start
Symptom: `failed to connect to local tailscaled; it doesn't appear to be
running`, and `systemctl start tailscaled` didn't actually keep it up.

Root cause: `/dev/net/tun` didn't exist inside the LXC container. Proxmox
doesn't pass the TUN device into unprivileged containers by default.

Fix (on the **Proxmox host**, not inside the container):
```bash
pct list                          # find the CTID for sandbox2
nano /etc/pve/lxc/<CTID>.conf
```
Add:
```
lxc.cgroup2.devices.allow: c 10:200 rwm
lxc.mount.entry: /dev/net/tun dev/net/tun none bind,create=file
```
Then `pct stop <CTID>` / `pct start <CTID>`. After that, `ls -la /dev/net/tun`
showed the device and `tailscale up` worked normally.

### 2. Tailscale Funnel CLI syntax has changed (v1.52+)
Old syntax (`tailscale funnel 443 on`) errors out. Current syntax:
```bash
tailscale funnel 443            # foreground, bare port = plain HTTP backend
tailscale funnel --bg 443        # persist in background
tailscale funnel status          # check current config
tailscale funnel --https=443 off # tear down (note: NOT `--bg ... off`)
```
For an HTTPS backend instead of HTTP, use `https://127.0.0.1:PORT` or
`https+insecure://127.0.0.1:PORT` (self-signed) as the target instead of a
bare port number.

### 3. Funnel was pointed at the wrong port/protocol
HenWen's gunicorn is bound to **port 5000**, plain HTTP — confirmed via:
```bash
ss -tlnp | grep gunicorn
# LISTEN 0 2048 0.0.0.0:5000 ... gunicorn
grep -i "bind\|port" /etc/systemd/system/HenWen.service
# ExecStart=... gunicorn --bind 0.0.0.0:5000 ...
```
There is no reverse proxy/HTTPS listener on port 443 from HenWen itself —
Funnel *is* the HTTPS layer, terminating TLS publicly and forwarding plain
HTTP to the app. Correct config:
```bash
tailscale funnel --bg 5000
```
Sanity check after each config change:
```bash
tailscale funnel status
# should show: |-- / proxy http://127.0.0.1:5000
```

### 4. TLS handshake failing publicly (`tlsv1 alert internal error`)
Even with Funnel correctly pointed at 5000, `curl -I
https://sandbox2.bombay-chroma.ts.net/` failed both from inside the tailnet
and — confirmed via SSL Labs (https://www.ssllabs.com/ssltest/) — from
outside it too, with **"No certificates found"** on all 4 anycast relay
IPs. So this wasn't a same-tailnet DNS artifact; Tailscale's edge genuinely
had no cert to present.

Root cause: `tailscale cert sandbox2.bombay-chroma.ts.net` itself was
failing:
```
500 Internal Server Error: acme.GetReg: Get "https://acme-v02.api.letsencrypt.org/directory":
dial tcp: lookup acme-v02.api.letsencrypt.org on [fd7a:115c:a1e0::53]:53: server misbehaving
```
That's MagicDNS (`100.100.100.100` / quad100) — confirmed directly:
```bash
dig @100.100.100.100 acme-v02.api.letsencrypt.org
# SERVFAIL, flags: qr aa rd ad ... "recursion requested but not available"
```
quad100 resolves `*.ts.net` names itself but has **no upstream configured**
to forward everything else to. In the tailnet admin console
(https://login.tailscale.com/admin/dns), **Global nameservers** was empty
and **Override DNS servers** was greyed out/unclickable.

**Key fact that cost time:** the toggle is greyed out *because* the
nameserver list is empty — Tailscale disables "Override DNS servers" until
there's at least one global nameserver to override with. Add the nameserver
first, then the toggle becomes clickable.

Fix:
1. Admin console → DNS → Global nameservers → add `8.8.8.8` (and ideally
   `1.1.1.1`) → Save
2. Now clickable: enable **Override DNS servers** → Save
3. Re-test:
   ```bash
   dig @100.100.100.100 acme-v02.api.letsencrypt.org   # now returns a real A record
   tailscale cert sandbox2.bombay-chroma.ts.net        # now succeeds, writes .crt/.key
   ```

Note: `tailscale set --accept-dns=false` does **not** fix this — the ACME
cert-fetch path inside `tailscaled` uses its own internal resolver
(quad100), independent of whatever the OS's `/etc/resolv.conf` / accept-dns
setting is doing. The only real fix is giving quad100 an upstream via the
admin console.

### Result of steps 1–4
```
root@node643931:~/HenWen# curl -I https://sandbox2.bombay-chroma.ts.net/
HTTP/2 200
server: gunicorn
set-cookie: session=...; Secure; HttpOnly; Path=/; SameSite=Lax
```
HenWen is reachable on the public internet at
**https://sandbox2.bombay-chroma.ts.net/**, TLS terminated by Tailscale
Funnel, forwarded to gunicorn on `127.0.0.1:5000`.

## Open issue: `tx-spike` Browser TX HTTPS setup conflicts with Funnel

`/opt/HenWen/tx-spike/setup-https.sh <hostname> <email>` sets up a
**second, independent HTTPS stack** — Apache + certbot with its own
Let's Encrypt cert — apparently for a "Browser TX" feature. This is
architecturally separate from the Funnel setup above and hit two problems:

### 4a. Port conflict (fixed)
`tailscaled` binds to specific addresses on 443
(`100.117.162.60:443` and its Tailscale IPv6 ULA), and Apache's default
`Listen 443` (wildcard, `0.0.0.0`) can't coexist with that — Linux refuses
a wildcard bind on a port that already has a specific-address bind on it,
even though the addresses don't literally overlap.

Fixed by pinning Apache to the container's own LAN address instead of the
wildcard, in `/etc/apache2/ports.conf` (both the `ssl_module` and
`mod_gnutls` `Listen 443` lines, which are indented inside `<IfModule>`
blocks — a naive anchored `sed` won't match them):
```bash
sed -i 's/^\(\s*\)Listen 443$/\1Listen 10.10.10.21:443/' /etc/apache2/ports.conf
systemctl restart apache2
ss -tlnp | grep ':443'
# apache2 now shown on 10.10.10.21:443, tailscaled still on its own two addresses — no conflict
```

### 4b. Certbot HTTP-01 challenge can never reach this Apache (NOT YET FIXED)
This is the real blocker, and it's structural, not a config typo:

```
Certbot failed to authenticate some domains (authenticator: apache).
Domain: sandbox2.bombay-chroma.ts.net
Type: unauthorized
Detail: 2607:f740:f::b31: Invalid response from
  https://sandbox2.bombay-chroma.ts.net/.well-known/acme-challenge/...: 404
```
`2607:f740:f::b31` is one of **Tailscale's own Funnel anycast relay
addresses** (same one that showed up in the SSL Labs scan earlier). Public
DNS for any `*.ts.net` name with Funnel enabled always resolves to
Tailscale's relay infra, never to this box's real public IP
(`24.247.3.110`, confirmed by setup-https.sh's own preflight warning). So:

- Let's Encrypt's validator → follows public DNS → hits Tailscale's Funnel
  relay → Funnel forwards to gunicorn on 5000 (per the *existing* Funnel
  config) → gunicorn 404s on the ACME challenge path, since it knows
  nothing about `.well-known/acme-challenge/`.
- Apache, sitting locally on `10.10.10.21:443`, is never reached by the
  validator at all — there's no path from the public internet to it under
  this hostname.

**This cannot be fixed by changing Apache/certbot config.** As long as
Funnel is enabled for `sandbox2.bombay-chroma.ts.net`, that hostname will
never resolve publicly to this box directly — that's the whole point of
Funnel (no port-forwarding needed). Two real options, not yet decided:

1. **Browser TX doesn't need its own cert/domain at all.** Funnel already
   provides a valid public HTTPS endpoint for this host
   (`sandbox2.bombay-chroma.ts.net`, cert issued via `tailscale cert` in
   step 4 above). If Browser TX can run as routes inside the existing
   Flask app (`app.py`) or behind a local reverse-proxy hop that Funnel
   already forwards to, no separate Apache/certbot stack is needed at all.
2. **Browser TX genuinely needs a real port-forwarded public domain**
   (e.g., a subdomain of `ironicsandwich.com`, DNS already managed on
   Namecheap) — bypassing Funnel/Tailscale entirely for this one feature,
   with actual router port-forwarding to `24.247.3.110` → this box. That's
   a legitimate use case for a standalone Apache+certbot stack, but it
   needs a **different hostname** than the `.ts.net` one, and real NAT
   port-forwarding on the router, not Funnel.

**Next step:** read `/opt/HenWen/tx-spike/apply.sh` and any README/comments
in `tx-spike/` to determine what Browser TX actually requires HTTPS for
(browser mic/getUserMedia access requiring a secure context, most likely —
worth confirming), then pick option 1 or 2 above and implement it.

## Useful commands for next session

```bash
# Funnel status / reconfigure
tailscale funnel status
tailscale funnel --bg 5000
tailscale funnel --https=443 off

# Confirm what's actually listening where
ss -tlnp | grep ':443'
ss -tlnp | grep gunicorn

# HenWen service
systemctl status HenWen.service
journalctl -u HenWen.service -n 50 --no-pager

# Apache
systemctl status apache2
cat /etc/apache2/ports.conf
grep -rn "Listen\|VirtualHost" /etc/apache2/sites-enabled/*.conf

# DNS sanity check (if cert/ACME issues resurface)
dig @100.100.100.100 acme-v02.api.letsencrypt.org
tailscale cert sandbox2.bombay-chroma.ts.net
```
