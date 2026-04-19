#!/usr/bin/env python3
"""Probe an SSR mount to enumerate phase-bias and code-bias signal codes.

Connects to an NTRIP caster mount, collects a short sample, and prints
per-constellation signal-code coverage (from _parse_code_bias and the
binary phase-bias parser).  Designed to answer questions like:

  - Does CAS SSRA01CAS1 publish GPS L2L (matching F9T L2CL tracking)?
  - What Galileo / BeiDou signal codes does a candidate AC cover?
  - How many satellites, how many biases per SV?

Example:
  python scripts/probe_cas_ssr.py --caster ntrip.data.gnss.ga.gov.au \\
      --port 443 --mount SSRA01CAS1 --user bobvan --password "$NTRIP_PW" \\
      --duration 45
"""
import argparse
import logging
import sys
import time
from collections import defaultdict

from ntrip_client import NtripStream
from ssr_corrections import (
    SSRState, _SSR_SIGNAL_MAP, _RTCM_SSR_SIGNAL_MAP,
)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--caster", required=True)
    ap.add_argument("--port", type=int, default=443)
    ap.add_argument("--mount", required=True)
    ap.add_argument("--user", required=True)
    ap.add_argument("--password", required=True)
    ap.add_argument("--duration", type=int, default=45,
                    help="seconds to sample (default 45)")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    stream = NtripStream(args.caster, args.port, args.mount,
                         user=args.user, password=args.password)
    stream.connect()

    ssr = SSRState()
    msg_counts = defaultdict(int)
    start = time.monotonic()

    for parsed in stream.messages():
        ident = str(getattr(parsed, "identity", ""))
        msg_counts[ident] += 1
        try:
            ssr.update_from_rtcm(parsed)
        except Exception as e:
            logging.debug("update_from_rtcm: %s", e)
        if time.monotonic() - start > args.duration:
            break

    stream.close()

    print("\n=== Message counts ===")
    for k in sorted(msg_counts):
        print(f"  {k}: {msg_counts[k]}")

    # Per-constellation phase-bias signal code coverage
    print("\n=== Phase-bias signals per constellation ===")
    by_sys_phase = defaultdict(lambda: defaultdict(int))
    for sv, codes in ssr._phase_bias.items():
        sys_prefix = sv[0]
        for c in codes:
            by_sys_phase[sys_prefix][c] += 1
    for sys_prefix in sorted(by_sys_phase):
        codes = by_sys_phase[sys_prefix]
        total_sv = sum(1 for sv in ssr._phase_bias if sv[0] == sys_prefix)
        print(f"  [{sys_prefix}] SVs with phase bias: {total_sv}")
        for c in sorted(codes):
            print(f"      {c:6s}: {codes[c]} SVs")

    print("\n=== Code-bias signals per constellation ===")
    by_sys_code = defaultdict(lambda: defaultdict(int))
    for sv, codes in ssr._code_bias.items():
        sys_prefix = sv[0]
        for c in codes:
            by_sys_code[sys_prefix][c] += 1
    for sys_prefix in sorted(by_sys_code):
        codes = by_sys_code[sys_prefix]
        total_sv = sum(1 for sv in ssr._code_bias if sv[0] == sys_prefix)
        print(f"  [{sys_prefix}] SVs with code bias: {total_sv}")
        for c in sorted(codes):
            print(f"      {c:6s}: {codes[c]} SVs")

    # F9T-tracking match assessment
    print("\n=== F9T-tracking compatibility ===")
    f9t_tracks = {
        'GPS L1CA':  ('G', 'L1C', 'C1C'),
        'GPS L2CL':  ('G', 'L2L', 'C2L'),
        'GPS L5Q':   ('G', 'L5Q', 'C5Q'),
        'GAL E1C':   ('E', 'L1C', 'C1C'),
        'GAL E5aQ':  ('E', 'L5Q', 'C5Q'),
        'GAL E5bQ':  ('E', 'L7Q', 'C7Q'),
    }
    for label, (sys_prefix, phase_code, code_code) in f9t_tracks.items():
        n_pb = by_sys_phase.get(sys_prefix, {}).get(phase_code, 0)
        n_cb = by_sys_code.get(sys_prefix, {}).get(code_code, 0)
        state = "HIT" if (n_pb or n_cb) else "miss"
        print(f"  {label:10s} → phase {phase_code}={n_pb:>3d} SVs  "
              f"code {code_code}={n_cb:>3d} SVs  [{state}]")


if __name__ == "__main__":
    main()
