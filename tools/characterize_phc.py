#!/usr/bin/env python3
"""Characterize PHC step accuracy against PPS input or an external TICC.

This lab tool applies a sequence of phase steps to a PHC and measures how
closely the resulting PPS phase matches the requested target offset.

Two measurement modes are supported:
  1. self: use PHC EXTS timestamps against PPS IN on the same PHC
  2. ticc: use an external TICC measuring PHC PPS OUT vs reference PPS

The TICC mode is the authoritative lab path. The self-measured PHC mode is
useful when no external TICC is attached, but it reuses the PHC timestamp path
being characterized and therefore should not be treated as ground truth.
"""

from __future__ import annotations

import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'scripts'))

import argparse
import csv
import random
import statistics
import time
from dataclasses import dataclass
from pathlib import Path

from peppar_fix.ptp_device import PTP_PF_EXTTS, PtpDevice


def fractional_ns_from_phc_nsec(nsec: int) -> float:
    """Return signed fractional-second error from a PHC EXTS timestamp."""
    if nsec < 500_000_000:
        return float(nsec)
    return float(nsec - 1_000_000_000)


@dataclass
class MeasurementSample:
    sample_time: float
    offset_ns: float
    source: str


class PtpSelfMeter:
    """Measure DO phase against PPS IN using PHC EXTS timestamps."""

    def __init__(self, ptp: PtpDevice, extts_channel: int):
        self.ptp = ptp
        self.extts_channel = extts_channel

    def drain(self) -> None:
        while True:
            event = self.ptp.read_extts(timeout_ms=0)
            if event is None:
                break

    def next_sample(self, timeout_s: float) -> MeasurementSample:
        deadline = time.monotonic() + timeout_s
        while True:
            remaining = max(0.0, deadline - time.monotonic())
            event = self.ptp.read_extts(timeout_ms=int(remaining * 1000))
            if event is None:
                raise TimeoutError("Timed out waiting for PHC EXTS event")
            sec, nsec, _idx, recv_mono, _queue_remains, _parse_age = event
            return MeasurementSample(
                sample_time=recv_mono,
                offset_ns=fractional_ns_from_phc_nsec(nsec),
                source="self",
            )


class TiccPairMeter:
    """Measure PHC PPS OUT against a reference PPS using paired TICC edges."""

    def __init__(self, ticc: Ticc, phc_channel: str, ref_channel: str):
        self.ticc = ticc
        self.phc_channel = phc_channel
        self.ref_channel = ref_channel
        self._events = ticc.iter_events()
        self._pending = {phc_channel: {}, ref_channel: {}}

    def _pair_events(self, timeout_s: float) -> MeasurementSample:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            event = next(self._events)
            self._pending[event.channel][event.ref_sec] = event
            other_channel = (
                self.ref_channel if event.channel == self.phc_channel else self.phc_channel
            )
            other = self._pending[other_channel].pop(event.ref_sec, None)
            if other is None:
                cutoff = event.ref_sec - 4
                for bucket in self._pending.values():
                    stale = [k for k in bucket if k < cutoff]
                    for key in stale:
                        bucket.pop(key, None)
                continue
            if event.channel == self.phc_channel:
                phc_event = event
                ref_event = other
            else:
                phc_event = other
                ref_event = event
            diff_ps = (
                (phc_event.ref_sec - ref_event.ref_sec) * 1_000_000_000_000
                + phc_event.ref_ps
                - ref_event.ref_ps
            )
            return MeasurementSample(
                sample_time=max(phc_event.recv_mono, ref_event.recv_mono),
                offset_ns=diff_ps * 1e-3,
                source="ticc",
            )
        raise TimeoutError("Timed out waiting for paired TICC measurement")

    def drain(self, timeout_s: float = 0.2) -> None:
        end = time.monotonic() + timeout_s
        while time.monotonic() < end:
            try:
                self._pair_events(timeout_s=0.05)
            except TimeoutError:
                break

    def next_sample(self, timeout_s: float) -> MeasurementSample:
        return self._pair_events(timeout_s)


def average_measurement(meter, n_samples: int, timeout_s: float) -> tuple[float, list[MeasurementSample]]:
    samples = [meter.next_sample(timeout_s) for _ in range(n_samples)]
    mean_ns = statistics.fmean(s.offset_ns for s in samples)
    return mean_ns, samples


def run_trial(ptp: PtpDevice, meter, target_ns: float, settle_s: float,
              measure_count: int, timeout_s: float) -> dict:
    meter.drain()
    initial_ns, initial_samples = average_measurement(meter, measure_count, timeout_s)
    step_ns = initial_ns - target_ns
    ptp.step_time(int(round(step_ns)))
    step_monotonic = time.monotonic()
    time.sleep(settle_s)
    meter.drain()
    final_ns, final_samples = average_measurement(meter, measure_count, timeout_s)
    residual_ns = final_ns - target_ns
    return {
        "target_ns": target_ns,
        "initial_ns": initial_ns,
        "commanded_step_ns": step_ns,
        "step_monotonic": step_monotonic,
        "final_ns": final_ns,
        "residual_ns": residual_ns,
        "initial_first_ns": initial_samples[0].offset_ns,
        "final_first_ns": final_samples[0].offset_ns,
        "final_last_ns": final_samples[-1].offset_ns,
        "measurement_source": final_samples[-1].source,
    }


def summarize(rows: list[dict]) -> str:
    residuals = [abs(float(r["residual_ns"])) for r in rows]
    commanded = [abs(float(r["commanded_step_ns"])) for r in rows]
    lines = []
    lines.append("PHC characterization summary")
    lines.append(f"trials: {len(rows)}")
    if residuals:
        lines.append(
            "abs residual ns min/median/max: "
            f"{min(residuals):.1f} / {statistics.median(residuals):.1f} / {max(residuals):.1f}"
        )
    if commanded:
        lines.append(
            "abs commanded step ns min/median/max: "
            f"{min(commanded):.1f} / {statistics.median(commanded):.1f} / {max(commanded):.1f}"
        )
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="Characterize PHC step accuracy")
    ap.add_argument("--ptp-dev", required=True, help="PHC device, e.g. /dev/ptp0")
    ap.add_argument("--mode", choices=["self", "ticc"], default="self",
                    help="Measurement mode")
    ap.add_argument("--extts-channel", type=int, default=0,
                    help="PHC EXTS channel for self mode")
    ap.add_argument("--pps-pin", type=int, default=1,
                    help="PHC PPS input pin for self mode if pin programming is enabled")
    ap.add_argument("--program-pin", action="store_true",
                    help="Program the PHC pin for EXTS in self mode")
    ap.add_argument("--ticc-port", help="TICC port for ticc mode")
    ap.add_argument("--ticc-baud", type=int, default=115200)
    ap.add_argument("--ticc-phc-channel", choices=["chA", "chB"], default="chA")
    ap.add_argument("--ticc-ref-channel", choices=["chA", "chB"], default="chB")
    ap.add_argument("--trials", type=int, default=20)
    ap.add_argument("--min-target-ns", type=float, default=-200_000.0)
    ap.add_argument("--max-target-ns", type=float, default=200_000.0)
    ap.add_argument("--settle-s", type=float, default=2.0,
                    help="Seconds to wait after each phase step")
    ap.add_argument("--measure-count", type=int, default=3,
                    help="Number of consecutive measurements to average")
    ap.add_argument("--timeout-s", type=float, default=3.0,
                    help="Per-sample measurement timeout")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--out", help="CSV output path")
    args = ap.parse_args()

    rng = random.Random(args.seed)
    ptp = PtpDevice(args.ptp_dev)
    rows: list[dict] = []
    try:
        if args.mode == "self":
            if args.program_pin:
                try:
                    ptp.set_pin_function(args.pps_pin, PTP_PF_EXTTS, args.extts_channel)
                except OSError:
                    pass
            ptp.enable_extts(args.extts_channel, rising_edge=True)
            meter = PtpSelfMeter(ptp, args.extts_channel)
            context = None
        else:
            from ticc import Ticc

            if not args.ticc_port:
                raise SystemExit("--ticc-port required for --mode ticc")
            context = Ticc(args.ticc_port, args.ticc_baud, wait_for_boot=True)
            ticc = context.__enter__()
            meter = TiccPairMeter(ticc, args.ticc_phc_channel, args.ticc_ref_channel)

        for trial_index in range(args.trials):
            target_ns = rng.uniform(args.min_target_ns, args.max_target_ns)
            row = run_trial(
                ptp=ptp,
                meter=meter,
                target_ns=target_ns,
                settle_s=args.settle_s,
                measure_count=args.measure_count,
                timeout_s=args.timeout_s,
            )
            row["trial"] = trial_index
            rows.append(row)
            print(
                f"[{trial_index:02d}] target={row['target_ns']:+.1f}ns "
                f"initial={row['initial_ns']:+.1f}ns "
                f"step={row['commanded_step_ns']:+.1f}ns "
                f"final={row['final_ns']:+.1f}ns "
                f"residual={row['residual_ns']:+.1f}ns"
            )

    finally:
        try:
            ptp.close()
        except Exception:
            pass
        try:
            if args.mode == "self":
                ptp.disable_extts(args.extts_channel)
        except Exception:
            pass
        try:
            if context is not None:
                context.__exit__(None, None, None)
        except Exception:
            pass

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=[
                "trial", "measurement_source", "target_ns", "initial_ns",
                "commanded_step_ns", "step_monotonic", "final_ns", "residual_ns",
                "initial_first_ns", "final_first_ns", "final_last_ns",
            ])
            w.writeheader()
            w.writerows(rows)

    print()
    print(summarize(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
