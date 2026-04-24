# Peer State Sharing

Design for PePPAR-Fix instances to exchange filter state in real
time — current position, integer fixes, ZTD, SV state machine,
etc. — so that nearby peers mutually strengthen each other's
solutions and the developer fleet gains live cross-host visibility
instead of post-processed analysis.

## Scope: two layers, separate designs

GNSS data-sharing between co-located nodes splits cleanly into two
layers:

- **Layer 1 — observation sharing.** Raw RXM-RAWX observations,
  decoded navigation subframes, station-reference positions.  The
  vocabulary is RTCM, the transport is NTRIP.  Target scenario: a
  booting node converges from a neighbour's stream instead of
  pulling BCEP / SSR from the internet.
- **Layer 2 — state sharing.** Post-filter artifacts: current
  fix + σ, per-SV integer ambiguities, ZTD residual, SV state
  machine, NAV2 delta.  The vocabulary is engine-native structs;
  the transport is pub/sub (various).  Target scenario:
  neighbours cross-validate each other's filter in real time.

This document is about **Layer 2**.  Layer 1 is already well
specified elsewhere — see the bottom of this document for
cross-references.  The layers are complementary, not competing:
Layer 1 helps a booting node catch up; Layer 2 helps two converged
nodes detect disagreement.

## Short-term value: Layer 2 >> Layer 1 in the lab

**Layer 1 short-term value: low.**  Not because the idea is bad,
but because the lab isn't bandwidth-constrained today — CNES +
BKG deliver reliably — and the design work (RTCM encoders, NTRIP
caster) is non-trivial.  Layer 1's payoff shows up in production
(bunker-deployed nodes without reliable internet) which isn't the
dev pain point.

**Layer 2 short-term value: high.**  The lab runs three L5-fleet
instances on a shared antenna (UFO1 via splitter).  Every run
produces three separate logs that we currently compare overnight
by pulling them back and running post-hoc analysis.  Live Layer 2
would surface:

- cross-host position Δ per epoch (should be mm on shared antenna)
- per-SV integer-fix agreement (must be identical on shared antenna)
- ZTD spread (should be mm — same atmospheric column)
- who anchored first, who's still converging

That's an entire category of overnight discovery turned into
real-time signal.  The effort is small: a state schema, a
multicast UDP emitter, a peppar-mon fleet mode — each a few
hundred lines, done in a session or two.

**Rank order**: Layer 2 MVP first → gets immediate dev leverage.
Layer 1 lands when the first production-edge deployment shows up
with a "this node needs help catching up offline" requirement.

## What to share (Layer 2 taxonomy, ranked by ROI)

1. **Per-SV integer fixes** — `{sv: N_WL_integer, N_NL_integer}`.
   Shared antenna: integers are identical by construction;
   disagreement = one host has a wrong integer.  Near-field
   (<10 km): integers related by single-difference that's itself
   integer and known from the baseline.  Highest diagnostic
   value we have short of ITRF14 truth.
2. **Current fix + σ** — `{lat, lon, alt, sigma_3d}`.  Lab: live
   |A − B| should be mm on shared antenna; any growth to dm is
   immediate null-mode alarm.  Production: cross-validation.
3. **SV state machine** — per-SV `{state: TRACKING | FLOATING |
   CONVERGING | ANCHORING | ANCHORED | WAITING}`.  Shared-
   antenna: state rosters should match; a constellation Anchored
   on A but Floating on B means B is lagging — why?
4. **ZTD residual** — `{ztd_m, sigma_mm}`.  Shared-antenna: same
   to mm.  Near-field: correlated within 10 km; peer's ZTD is a
   bootstrapping prior.
5. **Solid Earth tide** — `{total_mm, u_mm}`.  Identical at a
   given lat/lon/epoch; useful as a sanity check that both nodes
   computed it the same way.
6. **NAV2 delta** — already per-host; mostly just a cross-check
   that the F9T hardware is behaving consistently.
7. **Correction stream in use** — `{ssr_mount, eph_mount}`.
   Session metadata.  Lets the aggregator say "all three hosts
   on SSRA00CNE0" or flag heterogeneous streams.
8. **Filter covariance (reduced)** — just the diagonal position
   terms.  Feeds Q2-style σ-trust across hosts.
9. **Float ambiguity values** — high-bandwidth, narrow audience
   (developer drilling into one specific SV).  Send on demand,
   not continuously.

Numbered 1-6 are the MVP payload.  7-9 are future expansions.

## Baseline regimes: what sharing semantics apply

The value of a peer's state depends on the geometric relationship:

| Regime          | Baseline | Integer-fix portable? | ZTD correlated? | Best sharing mode |
|---              |---       |---                    |---              |---                |
| Shared antenna  | 0        | Yes (identical mod antenna PCO) | Yes (same column) | All of 1–7        |
| Near-field      | <10 km   | Yes via single-diff   | Yes (cm-scale)  | 2, 3, 4, 5, 7     |
| Short-baseline  | 10-50 km | Single-diff degrades  | Yes (dm-scale)  | 2, 3, 4 (as priors) |
| Long-baseline   | > 50 km  | No                    | Weak            | 2, 3, 7 (metadata) |

Lab's UFO1 is the first row (baseline 0 via splitter).  Production
pairs around ChicagoLand would be rows 1–2.  Cross-continental
would be row 4.

The integer-fix row is load-bearing.  Shared-antenna: peer's
integer IS your integer (mod millimeter PCO from the ANTEX file).
Near-field: peer's integer plus the known baseline's single-
difference gives you yours.  Further out: adopt at your own
risk.

## Design: transport-agnostic peer bus

The central design decision is **do not couple the payload schema
to one transport**.  Multicast UDP is the right MVP for lab, but
production may need TCP p2p (firewall-restricted deployments) or
broker-mediated (distributed fleets, QoS/durability).  A single
`PeerBus` interface keeps the engine and peppar-mon oblivious to
the wire.

### Interface

```python
class PeerBus:
    """Abstract peer-state bus.  Transport-agnostic.  Implementations:
    UDPMulticastBus, TCPP2PBus, MQTTBus.
    """

    def publish(self, topic: str, payload: bytes) -> None: ...

    def subscribe(
        self, topic_pattern: str,
        callback: Callable[[PeerMessage], None],
    ) -> None: ...

    def peers(self) -> list[PeerIdentity]: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class PeerMessage:
    from_host: str          # peer identifier (hostname or UID)
    topic: str              # matched topic string
    timestamp_mono_ns: int  # monotonic clock at send
    payload: bytes          # raw serialized body (JSON / msgpack)


@dataclass(frozen=True)
class PeerIdentity:
    host: str
    version: str            # engine version string
    systems: str            # "G+E+C" or similar
    antenna_ref: str        # shared antenna ID / surveyed point name
    first_seen_mono_ns: int
```

### Topic structure

Dot-hierarchical for fan-out filtering:

```
peppar-fix.<hostname>.position
peppar-fix.<hostname>.sv-state
peppar-fix.<hostname>.integer-fix.<sv>
peppar-fix.<hostname>.ztd
peppar-fix.<hostname>.tide
peppar-fix.<hostname>.streams
peppar-fix.<hostname>.heartbeat
```

Fleet monitor subscribes to `peppar-fix.*.position` to get every
host's position stream regardless of how many.

### Payload serialization

JSON Lines (one JSON object per message) for the MVP:

- Human-readable; debuggable with `tcpdump` + `jq`
- Schema-evolution friendly (new fields ignored by old parsers)
- ~300-500 bytes per message
- Verbose compared to msgpack / protobuf, but at ~1 msg/sec per
  host the bandwidth cost is negligible

Payload schemas documented in `peppar_fix/peer_bus/schemas.py`
(proposed).  Example for the `.position` topic:

```json
{
  "ts": "2026-04-24T02:15:00.103Z",
  "ant_pos_est_state": "anchored",
  "position": {
    "lat_deg": 40.12345678,
    "lon_deg": -90.12345678,
    "alt_m": 198.247
  },
  "position_sigma_m": 0.023,
  "worst_sigma_m": 1.45,
  "reached_anchored": true
}
```

## Transport implementations

### UDPMulticastBus (lab MVP)

- Multicast group: configurable (default `239.18.8.13:12468` —
  arbitrary link-local range)
- TTL: 1 (single-subnet only, matches lab topology)
- Discovery: implicit — a peer whose heartbeat arrives becomes
  known; no central registry
- Heartbeat cadence: 1 s, carries `PeerIdentity`
- State cadence: driven by the engine's own log-line cadence
  (every ~10 s for position, on-change for integer fixes)
- Packet loss tolerance: stateless re-publish; peppar-mon's
  fleet view is best-effort display; engine that ingests peer
  state uses it as soft prior, not ground truth

Pros: zero infrastructure, works the moment the multicast group
is joined.  Cons: LAN-only, no QoS, no durability.

Failure modes that need thought:
- Peer restart — heartbeat gap on receiver; stale state persists
  for N seconds then "peer DOWN" label appears.  Matches
  peppar-mon's existing 30 s staleness rule.
- Packet loss during bulk state publish — tolerable; next
  send overwrites.
- Multi-subnet deployments — force a TTL bump or switch
  transports.

### TCPP2PBus (production v1)

- Explicit peer list (configured or mDNS-discovered)
- Each peer exposes `tcp://<host>:<port>` speaking the same
  protocol as UDPMulticastBus (JSON Lines over a framed TCP
  stream, one line per message)
- Framing: `\n` terminator (JSON Lines); no length-prefix
  complication
- Reconnect on failure with exponential backoff
- Subscribe on connect with a topic filter; peer sends every
  matching message

Pros: reliable delivery; works across routed networks / VPN;
firewall-friendly (single TCP port per instance).  Cons:
O(n²) connection count at fleet scale; explicit peer config.

### MQTTBus (production v2, when fleet grows)

- Central broker (Mosquitto or NATS)
- Publish to `peppar-fix/<host>/<topic>`; subscribe with wildcards
- Broker handles discovery, durability (retained last-known
  position per host), offline queueing
- QoS=1 (at-least-once) for state updates; QoS=0 for heartbeats

Pros: scales to dozens of instances cleanly; retained messages
mean a newly-launched peppar-mon sees everyone's last-known
state immediately.  Cons: broker dependency.

### Transport selection

Choose at startup via one flag:

```
--peer-bus udp-multicast        # default on lab network
--peer-bus tcp:host1,host2,...  # explicit TCP p2p
--peer-bus mqtt:broker:1883     # broker-mediated
--peer-bus none                 # disabled (current behavior)
```

The engine and peppar-mon both take the same flag.  `PeerBus` is
constructed once in each process and injected into the components
that need it.

## peppar-mon fleet view

Current peppar-mon is single-host: consumes one log file, renders
one instance's state.  Extend with a fleet mode that subscribes
to the peer bus and aggregates.

### Layout additions (top of screen)

```
21:34:17 CDT                         Antenna Position Anchored LAT° / LON° / ALT m  positionσ 2.3 cm
PePPAR-Fix UpTime 2d 4h 18m                                2nd Opinion 2.8 m Δ 3D
                                                   ZTD -2.434 m ±371 mm  Earth tide 135 mm (U+130)  SSR SSRA00CNE0  EPH BCEP00BKG0

Fleet (3 hosts, all on UFO1):  Δ  3 mm / 5 mm   |  Anchored 13/13 13/13 12/13  |  ZTD σ  2 mm
```

The fleet row gives the operator a one-glance health readout:
- **Δ 3 mm / 5 mm** — max pair-wise position difference across
  fleet; first number is horizontal, second is 3D.  Should be
  mm on shared antenna.  Colorize red past a threshold.
- **Anchored 13/13 13/13 12/13** — Anchored count per host.
  Mismatch = one host lagging.
- **ZTD σ 2 mm** — standard deviation of ZTD residual across
  hosts.  Shared antenna → should be < 1 cm.

### Per-host drill-down

A new binding (e.g. `F2`) opens a per-host grid comparing all SV
state tables side by side.  Shows which SVs are at different
states on different hosts — the wrong-integer diagnostic.

### Integer-fix comparison view

Third binding (`F3`): grid of `{sv × host → integer}` with
disagreements highlighted.  On shared antenna any non-blank
disagreement is a wrong-integer event.

### Source of fleet state

peppar-mon can consume fleet state two ways:
1. Own `PeerBus` subscription (preferred; gets every message as
   published)
2. Aggregation of multiple log-files (`--log host1.log
   --log host2.log` — useful for post-run analysis)

## Engine-side integration

`peppar_fix_engine.py` gains a `PeerBus` member (optional; None
when `--peer-bus none`).  Key call sites:

- `AntPosEstThread.on_epoch()` — publish position + σ when log
  emits the `[AntPosEst]` status line
- `NarrowLaneResolver.promote()` — publish integer fix when an
  SV transitions to ANCHORING
- `ZTDMonitor.update()` — publish ZTD when filter state changes
- Startup — publish `PeerIdentity` heartbeat once + periodically

The engine can *subscribe* as well: when a peer publishes an
integer fix on a shared-antenna host, the local engine can adopt
it as a tighter prior during bootstrap.  This is a layered
addition — shipped after the publish-only path proves stable.

## Phased roadmap

### Phase 1 — Layer 2 MVP (lab fleet monitoring)

Scope: publish-only, UDP multicast, MVP payloads (1-6).

- `peppar_fix/peer_bus/` package: `PeerBus` ABC,
  `UDPMulticastBus` implementation, schema dataclasses
- `peppar_fix_engine.py` wires publish calls at the call sites
  listed above behind a `--peer-bus` flag
- `peppar_mon/fleet.py` subscribes, aggregates, exposes via a
  new `FleetStateLine` widget

Effort: 1-2 sessions.  Lab gains live cross-host Δ plus Anchored
comparison.

### Phase 2 — Layer 2 subscribe-side integration

Engine adopts peer integer fixes on shared-antenna hosts.  Needs:
- Peer identity validation (same antenna?)
- Integer-fix verification (LAMBDA bootstrap compatible?)
- Rollback on disagreement

Effort: 1 session, more careful.  Modest payoff (faster
convergence on shared antenna) but unlocks the single-difference
story for near-field in Phase 3.

### Phase 3 — Near-field single-difference

Peer at known baseline; adopt single-difference integers.  Needs:
- Known baseline survey
- Single-difference math (well-established in RTK literature)
- Ionospheric / tropospheric differential gating

Effort: harder; probably 2-3 sessions plus validation.

### Phase 4 — Layer 1 (NTRIP peer-bootstrap)

Per the existing docs.  Turn any node into a mini-caster.  Builds
on `peer-bootstrap-sketch.md` + `caster-ephemeris.md` +
`ntrip-mdns-discovery.md`.

Effort: significant (RTCM encoders).  Deferred until a deployed
edge node needs it.

### Phase 5 — TCP + MQTT transports

Add `TCPP2PBus` then `MQTTBus` as separate implementations of the
`PeerBus` interface.  No engine or peppar-mon changes needed if
Phase 1's interface holds.

## Open questions

1. **Security / authentication** — multicast UDP is open on the
   lab subnet; anyone can publish or listen.  Production
   instances may want TLS (REST + JWT, MQTT-over-TLS, etc.).
   Deferred.  Lab trust model is fine for Phase 1.
2. **Clock sync in the payload** — heartbeats and state messages
   carry `timestamp_mono_ns`, which is local to each host.
   peppar-mon aggregation wants a *common* time axis to align
   peers.  Options: require NTP on all hosts (lab already does
   this); use the engine's GPS-time as the cross-host reference;
   accept jitter and align post-hoc.  Probably option 2 (engine
   already knows GPS time).
3. **Payload versioning** — JSON schemas evolve.  Add a
   `"schema_version"` field to every message, consumer handles
   backwards-compat graciously.
4. **Peer deduplication** — two hosts on the same bus discovering
   each other announce heartbeats.  What if there are three?
   What if someone restarts?  Use `hostname` + engine UID as
   the peer identity, not just hostname.  Matches existing
   `state/receivers/<uid>.json` convention.
5. **Back-pressure** — what if peppar-mon falls behind?  UDP
   multicast doesn't have back-pressure; slow subscribers just
   drop.  Acceptable for display.  Engine-side subscribe (Phase
   2+) needs care.

## Cross-references (Layer 1 — NTRIP)

Layer 2 (this document) doesn't replace Layer 1 — they solve
different problems.  The existing Layer 1 docs:

- `docs/peer-bootstrap-sketch.md` — Node as mini-NTRIP caster.
- `docs/caster-ephemeris.md` — Broadcast-ephemeris encoding for
  the caster.
- `docs/ntrip-mdns-discovery.md` — Zeroconf auto-discovery.

A node running both layers simultaneously would have two
independent roles: an NTRIP server (Layer 1, serving obs) and a
`PeerBus` publisher/subscriber (Layer 2, sharing state).  No
coupling between them; no shared plumbing.  Clean separation.
