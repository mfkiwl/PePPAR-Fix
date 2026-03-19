# Peer Bootstrap: peppar-fix Nodes as Mutual NTRIP Casters

## The idea

Multiple peppar-fix nodes within 10 km of each other. Long-running nodes
serve as NTRIP reference stations for booting nodes. Each node
bootstraps from peers, refines its own position, locks it in, monitors
for errors, and offers to help bootstrap others.

## Is it doable?

Yes. Each piece already exists in the GNSS ecosystem — the question is
whether we can assemble them without a heavy lift.

### What a node needs to be an NTRIP caster

An NTRIP caster streams RTCM observations from a known reference
position. A peppar-fix node that has converged needs to:

1. **Know its own position to cm-level** — we already do this (PPP
   convergence, position save/load)
2. **Stream raw observations in RTCM 3.x format** — this is the new
   piece. The F9T outputs RXM-RAWX; we need to encode those as RTCM
   MSM4/MSM7 messages plus RTCM 1005 (reference position)
3. **Serve the stream over TCP** — a minimal NTRIP caster is just an
   HTTP server that sends the RTCM byte stream in response to GET
   requests

### Effort estimate for each piece

**RAWX → RTCM MSM encoding**: Medium. pyrtcm can parse MSM but may not
encode them. We'd need to map F9T RAWX fields (pseudorange, carrier
phase, C/N0, lock time) into RTCM MSM4 or MSM7 bit packing. The MSM
format is well-documented (RTCM 3.3 Table 3.5-78 etc.) but fiddly —
~500-1000 lines of bit-packing code. Alternatively, RTKLIB's `str2str`
can convert UBX to RTCM in real time and is already packaged for Linux.

**RTCM 1005 (reference position)**: Easy. Fixed message: ECEF X/Y/Z of
the antenna. ~20 lines.

**Minimal NTRIP caster**: Easy. Accept TCP connections, send
`ICY 200 OK\r\n\r\n`, then stream RTCM bytes. ~50 lines. Or use SNIP
Lite (free for 3 streams) or str2str's built-in caster mode.

**Total new code if using RTKLIB str2str**: Low — mostly configuration.
str2str already does UBX serial → RTCM 3.x caster. We'd wrap it with
position management (inject our converged position as the reference) and
lifecycle management.

**Total new code if doing it in Python**: Medium — MSM encoding is the
bulk. Everything else is small.

### The bootstrap protocol

```
Node A (long-running, converged):
  - Position locked at cm-level
  - Streaming RTCM MSM + 1005 on port 2102
  - Monitoring own residuals for drift

Node B (booting):
  1. Discover peers (mDNS, or a known peer list)
  2. Connect to Node A's NTRIP stream
  3. Compute RTK fix using A's corrections + own RAWX
     → cm-level position in 10-60 seconds
  4. Seed PPP filter with RTK position
  5. Run PPP independently (own observations + IGS products)
  6. Cross-check: PPP position vs RTK seed
     → If they agree within 5 cm after 10 min: position confirmed
     → If they diverge: flag, discard seed, restart PPP from scratch
  7. Once PPP converged: lock position, start own NTRIP caster
  8. Now B can bootstrap Node C
```

### Trust model: "believe but verify"

The key insight: you never trust a peer's position as final. You use it
to skip the slow PPP convergence, then verify independently. The
verification is cheap — PPP residuals after 10 minutes with a good seed
are unmistakably different from PPP residuals with a bad seed.

Specific checks:

- **Residual magnitude**: Post-fit carrier phase residuals should be
  <5 cm if the seed is good. With a bad seed (wrong by >10 cm), carrier
  phase residuals grow systematically within minutes.

- **Position stability**: With a good seed, the PPP position barely
  moves after the first few minutes. With a bad seed, it drifts toward
  truth — detectable as position rate > 1 cm/min.

- **Cross-peer consistency**: If two peers give you positions that
  disagree by >10 cm, at least one is wrong. Use the one that produces
  smaller PPP residuals.

- **Absolute validation**: After 30+ minutes of PPP, the position is
  self-validating regardless of seed. The seed only affects convergence
  speed, not the final answer.

### What could go wrong

**Common-mode errors**: If all nodes share the same NTRIP ephemeris
source, a bad ephemeris set could bias all of them the same way. This
is mitigated by using IGS multi-center products (GFZ, CODE, JPL produce
independent solutions).

**Correlated multipath**: Nodes within meters of each other see similar
multipath. Peer corrections don't help much if the reference station has
the same reflective environment. This is only a problem for nodes
co-located on the same rooftop — at 100m+ separation, multipath
decorrelates.

**Network partition**: If the "seed" node goes offline during bootstrap,
the booting node falls back to standalone PPP convergence (30-60 min
instead of 2 min). Not catastrophic, just slower.

**Reference position drift**: An OCXO-disciplined node might have a
slowly drifting position if the PPP filter has a subtle bias. The
verification step (independent PPP convergence) catches this as long
as the drift is >2 cm.

### What this enables

- **2-minute cold start** to cm-level positioning (vs 30-60 min standalone)
- **No dependency on external NTRIP casters** (DuPage County, rtk2go, etc.)
- **Self-healing network**: If one node's position drifts, peers detect
  the inconsistency through cross-checks
- **Scalable**: Each new node strengthens the network by adding another
  potential reference station

### Minimum viable version

The simplest version that gives 90% of the benefit:

1. One long-running node runs `str2str` to convert UBX serial →
   RTCM 3.x caster (inject converged position as ARP)
2. Booting nodes connect as NTRIP clients, compute single-epoch RTK
   fix using RTKLIB `rnx2rtkp` or a simple RTK solver
3. Feed RTK position as seed to peppar_find_position.py `--seed-pos`
4. PPP convergence verifies independently

This is maybe 2-3 days of integration work, not a rewrite. The hard
GNSS math (RTK solve, PPP filter) already exists.

### The heavy version (later)

- Python-native MSM encoding (no RTKLIB dependency)
- Automatic peer discovery via mDNS (`_peppar-rtk._tcp`)
- Continuous cross-validation between peers
- Distributed position consensus (like a timing ensemble but for position)
- Web dashboard showing peer mesh status

This is a project, not a task. But the MVP is small.
