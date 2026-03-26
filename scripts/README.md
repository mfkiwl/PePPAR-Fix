# scripts/ — Engine Runtime

This directory contains the PePPAR Fix engine and its supporting
libraries.  Everything here is part of the live observation-to-servo
pipeline.

## Entry points

| Script | Purpose |
|--------|---------|
| `peppar_fix_engine.py` | Main engine: bootstrap + steady-state clock estimation + PHC servo |
| `phc_bootstrap.py` | PHC warm-start: step phase, set frequency, characterize |
| `configure_f9t.py` | Configure u-blox ZED-F9T signals, messages, and timing mode |
| `peppar_rx_config.py` | Verify/configure receiver (called by engine at startup) |
| `peppar_host_config.py` | Resolve per-host config (serial port, PHC, antenna) |

## Libraries (imported by the engine)

| Module | Role |
|--------|------|
| `realtime_ppp.py` | Live serial reader, NTRIP reader, QErrStore |
| `solve_ppp.py` | PPPFilter, FixedPosFilter (EKF) |
| `solve_pseudorange.py` | Least-squares position solver, coordinate transforms |
| `solve_dualfreq.py` | Dual-frequency ionosphere-free combination |
| `broadcast_eph.py` | Broadcast ephemeris computation (GPS, Galileo, BeiDou) |
| `ssr_corrections.py` | Real-time SSR correction state manager |
| `ppp_corrections.py` | PPP correction file parsers (SP3, CLK, OSB) |
| `ntrip_client.py` | NTRIP v2 client for RTCM3 correction streams |
| `ntrip_caster.py` | NTRIP caster for peer-to-peer bootstrap |
| `rtcm_encoder.py` | RTCM 3.3 message encoder |
| `ticc.py` | TAPR TICC time interval counter serial reader |
| `peppar_fix/` | Core library package (servo, error sources, correlation, PTP device, etc.) |

## Usage

```bash
source venv/bin/activate

# Full engine with PHC servo:
python3 scripts/peppar_fix_engine.py \
    --serial /dev/gnss-top --ntrip-conf ntrip.conf \
    --eph-mount BCEP00BKG0 --servo /dev/ptp0

# Bootstrap PHC only:
python3 scripts/phc_bootstrap.py \
    --serial /dev/gnss-top --ntrip-conf ntrip.conf \
    --eph-mount BCEP00BKG0 --ptp-dev /dev/ptp0
```
