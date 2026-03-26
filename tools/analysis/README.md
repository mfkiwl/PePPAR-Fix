# tools/analysis/ — Post-hoc Analysis and Diagnostics

Scripts for analyzing captured data, plotting results, and diagnosing
PPP/ephemeris issues.  No hardware needed — these work on log files,
NTRIP streams, and recorded data.

| Tool | Purpose |
|------|---------|
| `analyze_servo.py` | Analyze PHC servo performance from TICC timestamp logs |
| `analyze_qerr_ticc.py` | Compare F9T qErr against TICC-measured raw PPS edges |
| `plot_convergence_curves.py` | Generate theoretical convergence curves |
| `diag_ssr.py` | Dump SSR correction values to verify units and signs |
| `diagnose_eph.py` | Check broadcast ephemeris parameters |
| `diagnose_kepler.py` | Verify Keplerian orbit computation |
| `diagnose_live.py` | Diagnose zero-measurement problem in live mode |
| `solve_gim.py` | Single-frequency GIM PPP solver (reference implementation) |
