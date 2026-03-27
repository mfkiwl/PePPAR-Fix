# NTRIP Peer Discovery via mDNS

## Problem

peppar-fix nodes need GNSS corrections to bootstrap.  Today they use
a hardcoded NTRIP caster (hostname + port in ntrip.conf).  For peer
bootstrap — where one node that has already converged serves
corrections to others — the caster address shouldn't need manual
configuration.

## Design

Use mDNS (Avahi/Zeroconf) service advertisement so casters announce
themselves on the local network and clients discover them automatically.

### Service type

```
_ntrip._tcp
```

Not IANA-registered for NTRIP, but used by convention in GNSS software.
Port defaults to 2102.

### TXT records

| Key | Example | Purpose |
|-----|---------|---------|
| `mount` | `PEPPAR` | Mountpoint name |
| `systems` | `gps,gal,bds` | GNSS constellations available |
| `format` | `rtcm3-msm4` | Correction format |
| `lat` | `41.843` | Approximate station latitude (for proximity selection) |
| `lon` | `-88.104` | Approximate station longitude |
| `accuracy` | `0.03` | Position sigma in meters (lower = more converged) |
| `station_id` | `0` | RTCM station ID |
| `version` | `peppar-fix/0.1` | Software identifier |

### Caster side (ntrip_caster.py)

On startup, after the TCP listener is bound:

1. Register the service via `zeroconf` (Python library) or Avahi D-Bus
2. Include position and accuracy from the position file in TXT records
3. Update TXT records periodically if accuracy improves (re-registration)
4. Unregister on shutdown

### Client side (ntrip_client.py)

New discovery mode when no explicit caster is configured:

1. Browse for `_ntrip._tcp.local` services
2. Collect responses for a bounded time (e.g., 3 seconds)
3. Select the best caster (see selection below)
4. Connect as normal using the discovered hostname + port + mountpoint

### Selection with multiple casters

When multiple peppar-fix nodes advertise:

1. **Prefer highest accuracy** — lowest `accuracy` TXT value means
   the caster's position is most converged
2. **Prefer proximity** — if client has an approximate position (from
   a previous position file or known-pos), prefer the caster closest
   to it (shorter baseline = better corrections)
3. **Prefer matching systems** — caster should cover at least the
   systems the client needs
4. **Tie-break: first responder** — mDNS responses arrive in
   network-proximity order naturally

### Fallback

If mDNS discovery finds no casters within the browse timeout, fall
back to the configured `ntrip.conf` caster (the external NTRIP
service).  This makes peer discovery opportunistic — it helps when
a peer is available but doesn't break anything when one isn't.

## Dependencies

- Python `zeroconf` library (pip-installable, pure Python, no C deps)
- Avahi daemon running on the host (standard on Raspberry Pi OS)
- mDNS-capable network (no firewalling of UDP 5353)

All lab hosts already resolve `.local` hostnames via mDNS, so the
infrastructure is in place.

## Scope

This is a specification, not an implementation plan.  The caster and
client both work today with explicit configuration.  mDNS discovery
is a convenience layer that eliminates manual caster configuration
for peer bootstrap on local networks.
