#!/usr/bin/env python3
"""Direct correlation check: is qErr correlated with pps_error?"""
import csv, sys, math
from statistics import pvariance, mean

rows = list(csv.DictReader(open(sys.argv[1])))
# Use last 200 rows (steady state)
tail = rows[-200:]

pps = [float(r["pps_error_ns"]) for r in tail]
qerr = [float(r["qerr_ns"]) for r in tail if r["qerr_ns"]]
# Align lengths
n = min(len(pps), len(qerr))
pps = pps[-n:]
qerr = qerr[-n:]

# First-differences
dpps = [pps[i] - pps[i-1] for i in range(1, n)]
dqerr = [qerr[i] - qerr[i-1] for i in range(1, n)]

# Pearson correlation of first-diffs
mp = mean(dpps)
mq = mean(dqerr)
cov = sum((p-mp)*(q-mq) for p,q in zip(dpps, dqerr)) / len(dpps)
sp = math.sqrt(pvariance(dpps))
sq = math.sqrt(pvariance(dqerr))
r = cov / (sp * sq) if sp > 0 and sq > 0 else 0

print("Steady state (last %d epochs):" % n)
print("  pps_error  range: %.0f to %.0f ns" % (min(pps), max(pps)))
print("  qerr       range: %.1f to %.1f ns" % (min(qerr), max(qerr)))
print()
print("First-differences:")
print("  var(dpps):  %.1f ns^2  (std %.1f ns)" % (pvariance(dpps), sp))
print("  var(dqerr): %.1f ns^2  (std %.1f ns)" % (pvariance(dqerr), sq))
print("  cov:        %.2f ns^2" % cov)
print("  Pearson r:  %.3f" % r)
print()

# Show actual pps values — are they quantized?
print("pps_error sample (last 20):")
for v in pps[-20:]:
    print("  %.0f" % v)
print()
print("Unique pps_error values (last 200):", sorted(set(int(v) for v in pps)))
