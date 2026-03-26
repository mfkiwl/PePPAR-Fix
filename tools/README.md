# tools/ — Diagnostic and Probing Tools

Standalone scripts for measuring, probing, and characterizing hardware.
Not imported by the engine.  Each is self-contained with `--help`.

## Hardware probes

| Tool | Purpose |
|------|---------|
| `read_stall_probe.py` | Measure raw `read()` latency on any GNSS device |
| `i2c_flush_probe.py` | Test whether extra F9T output flushes kernel GNSS I2C buffer |
| `rcvtow_dt_rx_probe.py` | Probe `rcvTow` vs PPP `dt_rx` relationship (125 MHz tick model) |
| `gnss_lag_probe.py` | Probe GNSS delivery cadence and RXM-RAWX lag |
| `characterize_phc.py` | PHC step accuracy against PPS input or external TICC |
| `characterize_phc_step.py` | PHC step accuracy via `PTP_SYS_OFFSET_PRECISE` |
| `qerr_test.py` | Capture TIM-TP qErr, show PPS error budget |
| `log_observations.py` | Log raw GNSS observations (RAWX, SFRBX, PVT, TIM-TP) to CSV |
| `phc_fault_inject.py` | Set PHC to a specific time/frequency for testing |
| `topology_patrol.py` | SSH to lab hosts, check connected devices vs inventory |

## Subdirectories

| Directory | Contents |
|-----------|----------|
| `analysis/` | Post-hoc analysis, plotting, diagnostics (no hardware needed) |
| `timebeat/` | Renesas 8A34002 / Timebeat OTC hardware tools |
