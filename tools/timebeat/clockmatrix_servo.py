#!/usr/bin/env python3
"""clockmatrix_servo.py — Self-contained ClockMatrix servo loop.

Reads PFD phase error from DPLL_2 (locked to CLK2 = F9T PPS) and
steers DPLL_3 output frequency via FCW. Everything runs over I2C —
no PHC, no EXTTS, no TICC needed.

Architecture:
  DPLL_0: PLL locked to CLK2 (PPS), drives 25/10 MHz to i226 (untouched)
  DPLL_2: PLL locked to CLK2 (PPS), phase sensor (PFD measures PPS vs output)
  DPLL_3: write_freq mode, steered via FCW (drives PPS output, PTP)

The PI servo reads DPLL_2's phase error and adjusts DPLL_3's FCW to
minimize the error. The OCXO's natural frequency offset is corrected
by the FCW, and the PFD provides 50 ps resolution feedback.

Usage:
    # Stop Timebeat first:
    sudo systemctl stop timebeat

    # Run servo for 5 minutes:
    python3 clockmatrix_servo.py --bus 15 --duration 300

    # With custom gains:
    python3 clockmatrix_servo.py --bus 15 --kp 0.5 --ki 0.01
"""

import argparse
import logging
import signal
import sys
import time

sys.path.insert(0, __file__.rsplit("/tools/", 1)[0] + "/scripts")

from peppar_fix.clockmatrix import ClockMatrixI2C
from peppar_fix.clockmatrix_actuator import ClockMatrixActuator, fcw_to_ppb
from peppar_fix.clockmatrix_phase import ClockMatrixPhaseSource

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("cm_servo")

stop = False
def _sigint(sig, frame):
    global stop
    stop = True
signal.signal(signal.SIGINT, _sigint)


class PIServo:
    """Simple PI controller. Input: phase error (ns). Output: freq (ppb)."""

    def __init__(self, kp, ki, max_ppb=200_000.0, initial_ppb=0.0):
        self.kp = kp
        self.ki = ki
        self.max_ppb = max_ppb
        self.integral = -initial_ppb / ki if ki != 0 else 0.0
        self.freq = initial_ppb

    def update(self, error_ns, dt=1.0):
        output = self.kp * error_ns + self.ki * (self.integral + error_ns * dt)
        if abs(output) < self.max_ppb:
            self.integral += error_ns * dt
        self.freq = max(-self.max_ppb, min(self.max_ppb, output))
        return self.freq


def main():
    ap = argparse.ArgumentParser(description="ClockMatrix I2C servo loop")
    ap.add_argument("--bus", type=int, default=15)
    ap.add_argument("--addr", type=lambda x: int(x, 0), default=0x58)
    ap.add_argument("--phase-dpll", type=int, default=2,
                    help="DPLL for phase measurement (default: 2)")
    ap.add_argument("--actuator-dpll", type=int, default=3,
                    help="DPLL for FCW steering (default: 3)")
    ap.add_argument("--pps-clk", type=int, default=2,
                    help="CLK input for PPS (default: 2)")
    ap.add_argument("--kp", type=float, default=0.3,
                    help="Proportional gain (default: 0.3)")
    ap.add_argument("--ki", type=float, default=0.01,
                    help="Integral gain (default: 0.01)")
    ap.add_argument("--duration", type=int, default=300,
                    help="Duration in seconds (default: 300)")
    ap.add_argument("--csv", type=str, help="Output CSV file")
    args = ap.parse_args()

    i2c = ClockMatrixI2C(args.bus, args.addr)
    phase_src = ClockMatrixPhaseSource(i2c, dpll_id=args.phase_dpll,
                                       pps_clk=args.pps_clk)
    actuator = ClockMatrixActuator(i2c, dpll_id=args.actuator_dpll)

    csv_f = None
    if args.csv:
        csv_f = open(args.csv, "w")
        csv_f.write("epoch,phase_ps,freq_ppb,kp,ki\n")

    try:
        log.info("Setting up phase source (DPLL_%d, CLK%d)...",
                 args.phase_dpll, args.pps_clk)
        phase_src.setup()

        log.info("Setting up actuator (DPLL_%d, FCW)...", args.actuator_dpll)
        actuator.setup()

        # Read initial phase to estimate OCXO offset
        time.sleep(2)
        p0 = phase_src.read_phase_ns()
        time.sleep(1)
        p1 = phase_src.read_phase_ns()
        if p0 is not None and p1 is not None:
            initial_drift = (p1 - p0)  # ns/s ≈ ppb
            log.info("Initial phase: %.1f ns, drift: %.1f ns/s (≈%.0f ppb)",
                     p1, initial_drift, initial_drift)
        else:
            initial_drift = 0.0
            log.warning("Could not read initial phase")

        # Seed the servo with the estimated drift
        # Positive drift = phase growing = output slow = need positive FCW
        servo = PIServo(args.kp, args.ki, initial_ppb=initial_drift)
        actuator.adjust_frequency_ppb(initial_drift)
        log.info("Servo started: kp=%.3f ki=%.4f initial_freq=%.1f ppb",
                 args.kp, args.ki, -initial_drift)

        log.info("")
        log.info("%5s  %12s  %12s  %12s", "Epoch", "Phase(ns)", "Freq(ppb)", "Integral")
        log.info("%5s  %12s  %12s  %12s", "-----", "---------", "---------", "--------")

        for epoch in range(args.duration):
            if stop:
                log.info("Interrupted.")
                break

            time.sleep(1)
            phase_ns = phase_src.read_phase_ns()
            if phase_ns is None:
                log.warning("[%d] No phase reading", epoch)
                continue

            # Positive phase = output late → need positive FCW (speed up)
            freq_ppb = servo.update(phase_ns)
            actuator.adjust_frequency_ppb(freq_ppb)

            if csv_f:
                phase_ps = phase_src.read_phase_ps()
                csv_f.write("%d,%d,%.3f,%.3f,%.4f\n" % (
                    epoch, phase_ps or 0, freq_ppb, args.kp, args.ki))
                csv_f.flush()

            if epoch % 10 == 0:
                log.info("%5d  %+12.1f  %+12.1f  %+12.1f",
                         epoch, phase_ns, freq_ppb, servo.integral)

    except Exception as e:
        log.error("Servo error: %s", e)
        raise
    finally:
        log.info("Shutting down...")
        try:
            actuator.teardown()
        except Exception:
            pass
        try:
            phase_src.teardown()
        except Exception:
            pass
        i2c.close()
        if csv_f:
            csv_f.close()
        log.info("Done.")


if __name__ == "__main__":
    main()
