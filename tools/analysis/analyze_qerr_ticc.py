#!/usr/bin/env python3
"""Analyze F9T qErr against TICC-measured raw PPS edges.

Pairs each TIM-TP sample to the following TICC chB edge using host UTC receive
timestamps, then compares raw chB stability against qErr-corrected variants.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import allantools
import numpy as np
import pandas as pd


def load_timtp(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    if df.empty:
        return df
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True, format="ISO8601")
    df["q_err_ps"] = df["q_err_ps"].astype("int64")
    df["tow_ms"] = df["tow_ms"].astype("int64")
    df = df.sort_values("timestamp").reset_index(drop=True)
    return df


def load_ticc_chb(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    if df.empty:
        return df
    df = df[df["channel"] == "chB"].copy()
    df["host_timestamp"] = pd.to_datetime(df["host_timestamp"], utc=True, format="ISO8601")
    df["ref_sec"] = df["ref_sec"].astype("int64")
    df["ref_ps"] = df["ref_ps"].astype("int64")
    df = df.sort_values("host_timestamp").reset_index(drop=True)
    return df


def pair_qerr_to_chb(timtp: pd.DataFrame, chb: pd.DataFrame, max_follow_s: float) -> pd.DataFrame:
    rows = []
    chb_ts = chb["host_timestamp"].to_numpy()
    chb_ns = chb["ref_sec"].to_numpy(dtype=np.int64) * 1_000_000_000_000 + chb["ref_ps"].to_numpy(dtype=np.int64)
    idx = 0
    for _, row in timtp.iterrows():
        ts = row["timestamp"]
        while idx < len(chb_ts) and chb_ts[idx] <= ts:
            idx += 1
        if idx >= len(chb_ts):
            break
        dt_s = (chb_ts[idx] - ts).total_seconds()
        if dt_s < 0 or dt_s > max_follow_s:
            continue
        rows.append({
            "timtp_timestamp": ts,
            "chb_timestamp": chb_ts[idx],
            "follow_s": dt_s,
            "q_err_ps": int(row["q_err_ps"]),
            "tow_ms": int(row["tow_ms"]),
            "chb_time_ps": int(chb_ns[idx]),
        })
    return pd.DataFrame(rows)


def phase_from_time_ps(time_ps: np.ndarray) -> np.ndarray:
    secs = time_ps // 1_000_000_000_000
    frac_ps = time_ps - secs * 1_000_000_000_000
    return frac_ps * 1e-12


def stability_metrics(phase_s: np.ndarray, taus=(1, 2, 5, 10)):
    out = {}
    for tau in taus:
        try:
            taus_a, adev, _, _ = allantools.adev(phase_s, rate=1.0, data_type="phase", taus=[tau])
            taus_t, tdev, _, _ = allantools.tdev(phase_s, rate=1.0, data_type="phase", taus=[tau])
            out[tau] = {
                "adev_ns": float(adev[0] * 1e9),
                "tdev_ns": float(tdev[0] * 1e9),
            }
        except Exception:
            out[tau] = None
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--timtp", type=Path, required=True)
    ap.add_argument("--ticc", type=Path, required=True)
    ap.add_argument("--max-follow-s", type=float, default=1.2)
    args = ap.parse_args()

    timtp = load_timtp(args.timtp)
    chb = load_ticc_chb(args.ticc)
    pairs = pair_qerr_to_chb(timtp, chb, args.max_follow_s)
    if pairs.empty:
        raise SystemExit("No TIM-TP -> chB pairs found")

    raw_phase = phase_from_time_ps(pairs["chb_time_ps"].to_numpy(dtype=np.int64))
    plus_phase = phase_from_time_ps((pairs["chb_time_ps"] + pairs["q_err_ps"]).to_numpy(dtype=np.int64))
    minus_phase = phase_from_time_ps((pairs["chb_time_ps"] - pairs["q_err_ps"]).to_numpy(dtype=np.int64))

    raw_var = float(np.var(raw_phase))
    plus_var = float(np.var(plus_phase))
    minus_var = float(np.var(minus_phase))

    print(f"pairs={len(pairs)}")
    print(f"follow_s min/med/max = {pairs['follow_s'].min():.6f} / {pairs['follow_s'].median():.6f} / {pairs['follow_s'].max():.6f}")
    print(f"raw_var_over_plus = {raw_var / plus_var:.3f}" if plus_var > 0 else "raw_var_over_plus = inf")
    print(f"raw_var_over_minus = {raw_var / minus_var:.3f}" if minus_var > 0 else "raw_var_over_minus = inf")

    for label, phase in [("raw", raw_phase), ("plus", plus_phase), ("minus", minus_phase)]:
        metrics = stability_metrics(phase)
        print(label)
        for tau in (1, 2, 5, 10):
            item = metrics.get(tau)
            if item is None:
                print(f"  tau={tau}s adev=NA tdev=NA")
            else:
                print(f"  tau={tau}s adev={item['adev_ns']:.3f}ns tdev={item['tdev_ns']:.3f}ns")


if __name__ == "__main__":
    main()
