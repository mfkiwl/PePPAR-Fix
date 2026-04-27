# The streaming-EKF ceiling

This doc tracks where PePPAR-Fix's streaming-EKF PPP solution
sits relative to the published-literature ceiling for static-
receiver PPP, and what closes the remaining gap.

## Why this doc exists

Bravo's PRIDE-vs-streaming work
(`memory/project_to_main_pride_*_20260426`) established the
empirical gap.  Charlie's literature synthesis
(`memory/project_to_main_bravo_charlie_position_stability_lit_20260426`)
established the geodetic vocabulary and reachable-floor
benchmarks.  This doc consolidates them into one reference an
analyst can look at to ask "where on the ladder are we today?"

This is a living doc.  It starts with the IGS-class baseline
reference table (the ceiling) and grows to capture the engine's
position on the ladder, the levers proposed to close the gap,
and the experimental results as we move.

## IGS-class converged-PPP-AR baseline (the ceiling)

Static-receiver position scatter at canonical averaging windows
τ, for permanent IGS reference stations running converged
PPP-AR.  Numbers from peer-reviewed literature; see the
position-stability memo for the source mapping between geodetic
PSD-slope κ vocabulary and time-frequency Allan vocabulary.

| τ          | Horizontal σ | Vertical σ | Reference |
|------------|--------------|------------|-----------|
| 1 s        | 5–20 mm      | 10–40 mm   | Geng et al. 2010, 2019 |
| 30 s       | 3–10 mm      | 5–20 mm    | Geng et al. 2019 |
| 5 min      | 2–5 mm       | 4–10 mm    | Bisnath & Gao 2009 |
| 1 hr       | 1–3 mm       | 3–6 mm     | Kouba & Héroux 2001 |
| 1 day      | 1–2 mm       | 3–6 mm     | Williams et al. 2004 |

**Reference receivers and conditions assumed:** geodetic-grade
choke ring antennas with surveyed ARP and full ANTEX
characterization, multi-system PPP-AR with mature ambiguity
fixing, hours of forward convergence, post-processed (not
real-time) precise products.  These are the *ceiling* numbers
the literature reports as routinely achievable, not the floor
of what PPP-AR can in principle deliver.

## Where PePPAR-Fix sits today

Snapshot from the day0426 evening 10-min engine smoke runs +
the day0426 overnight ptpmon-BNC run, computed by
`scripts/overlay/pos_adev.py`:

| Source | τ   | σ_H (m) | σ_U (m) | Notes |
|--------|-----|---------|---------|-------|
| Engine (TimeHat, MadHat, clkPoC3) — converged | 2 s | 0.05    | 0.08    | 10× the IGS-class 1s floor |
| Engine — converged | 60 s | 0.55    | 1.10    | 100× the IGS 30s floor |
| Engine — converged | 120 s | 0.97    | 1.10    | 200× the IGS 5min floor |
| BNC float-PPP (ptpmon, naive) | 2 s | 2.26    | 1.19    | 30–40× engine; pre-AR floor |
| BNC float-PPP | 480 s | 5.12    | 4.65    | wandering plateau |
| BNC float-PPP | 3600 s | ~19     | ~22     | bounded wander (overnight 13h) |

So PePPAR-Fix engines at present sit **2–3 orders of magnitude
above the IGS ceiling**, with the gap dominated by drift in the
flicker-walk band (τ ≈ 30 s to 30 min).  The engine's filter-
feature stack (LAMBDA WL/NL AR + GMF + ZTD-state + slip-rate-
limit + windup) is doing real work — it's 30–40× tighter than
naive float-PPP at short τ — but doesn't yet close the gap to
the IGS ceiling.

## The structural lever: fixed-lag smoother

Per `memory/project_to_main_bravo_charlie_ar_readiness_diagnostic_20260426`
and Geng et al. 2019, the streaming-EKF-vs-batch-LSE gap closes
with a **fixed-lag (RTS) smoother over the last N minutes**.
Forward EKF produces float + AR attempts; backward smoother
re-floats with fixed integers; AR re-attempts on smoothed float.
This is the literature recipe to approach PRIDE-class quality
from a streaming architecture.  Substantial engineering task;
needs separate design discussion before implementation.

## What this doc still needs

- Bravo: extension covering the streaming-EKF ceiling story
  in the team's own data (PRIDE-on-ABMF gap, σ+elev sweep, ZTD
  PWC + WNO clock outcomes).  D2 from the gap memo.
- Bravo: experimental results once the fixed-lag smoother (E1)
  is in flight.

## References

- Williams, S. D. P. (2003). The effect of coloured noise on the
  uncertainties of rates estimated from geodetic time series.
  *J. Geodesy* 76, 483–494. doi:10.1007/s00190-002-0283-4
- Williams, S. D. P., Bock, Y., Fang, P., Jamason, P., Nikolaidis,
  R. M., Prawirodirdjo, L., Miller, M., Johnson, D. J. (2004).
  Error analysis of continuous GPS position time series.
  *JGR* 109, B03412. doi:10.1029/2003JB002741
- Mao, A., Harrison, C. G. A., Dixon, T. H. (1999). Noise in GPS
  coordinate time series. *JGR* 104(B2), 2797–2816.
  doi:10.1029/1998JB900033
- Bos, M. S., Fernandes, R. M. S., Williams, S. D. P., Bastos, L.
  (2013). Fast error analysis of continuous GNSS observations
  including missing data. *J. Geodesy* 87, 351–360.
  doi:10.1007/s00190-012-0605-0
- Le Bail, K. (2006). Estimating the noise in space-geodetic
  positioning: the case of DORIS. *J. Geodesy* 80, 541–565.
  doi:10.1007/s00190-006-0088-y
- Geng, J., Chen, X., Pan, Y., Mao, S., Li, C., Zhou, J., Zhang,
  K. (2019). PRIDE PPP-AR: an open-source software for GNSS PPP
  ambiguity resolution. *GPS Solutions* 23, 91.
  doi:10.1007/s10291-019-0888-1
- Geng, J., Bock, Y. (2013). Triple-frequency GNSS PPP with
  rapid ambiguity resolution. *J. Geodesy* 87, 449–460.
  doi:10.1007/s00190-013-0619-2
- Bisnath, S., Gao, Y. (2009). Current state of precise point
  positioning and future prospects and limitations. *IAG Symp.*
  133.
- Kouba, J., Héroux, P. (2001). Precise Point Positioning Using
  IGS Orbit and Clock Products. *GPS Solutions* 5(2), 12–28.
- Riley, W. J. (2008). *Handbook of Frequency Stability Analysis*.
  NIST SP 1065. https://tf.nist.gov/general/pdf/2220.pdf
  (Time-frequency vocabulary cross-walk reference.)
