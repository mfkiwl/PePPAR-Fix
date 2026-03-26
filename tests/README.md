# tests/ — Automated and Smoke Tests

| Test | Purpose |
|------|---------|
| `test_phc_bootstrap.py` | PHC bootstrap integration tests (pytest) |
| `test_ssr_decode.py` | SSR decoder test against recorded CLK93 RTCM3 data (pytest) |
| `servo_fault_smoke.py` | Servo smoke test with timing fault injection |

## Running

```bash
source venv/bin/activate

# Unit/integration tests:
python3 -m pytest tests/test_ssr_decode.py -v

# Smoke test (requires hardware + sudo):
python3 tests/servo_fault_smoke.py --serial /dev/gnss-top --servo /dev/ptp0 \
    --ntrip-conf ntrip.conf --eph-mount BCEP00BKG0
```
