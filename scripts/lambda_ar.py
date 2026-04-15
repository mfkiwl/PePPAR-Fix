"""
lambda_ar.py — LAMBDA integer least-squares ambiguity resolution.

Implements the Least-squares AMBiguity Decorrelation Adjustment
(Teunissen 1995) with partial AR fallback (Li et al. 2015).

Algorithm:
  1. LDL^T decomposition of float ambiguity covariance
  2. Integer Gauss transforms to decorrelate (Z-transform)
  3. Sequential conditional enumeration for integer search
  4. Ratio test validation: accept if Omega(2nd) / Omega(1st) > threshold
  5. Partial AR: drop worst ambiguity and retry if full set fails

Reference: RTKLIB src/lambda.c (Takasu), de Jonge & Tiberius 1996.
"""

import numpy as np
from scipy.stats import norm


def _ldl(Q):
    """LDL^T decomposition of symmetric positive-definite Q.

    Returns (L, D) where Q = L @ diag(D) @ L.T, L is unit lower
    triangular, D is the diagonal vector.
    """
    n = Q.shape[0]
    L = np.zeros((n, n))
    D = np.zeros(n)

    for i in range(n - 1, -1, -1):
        D[i] = Q[i, i] - sum(L[i, j] ** 2 * D[j] for j in range(i + 1, n))
        if D[i] <= 0:
            D[i] = 1e-20  # regularize
        for j in range(i - 1, -1, -1):
            L[j, i] = (Q[j, i] - sum(L[j, k] * L[i, k] * D[k]
                                      for k in range(i + 1, n))) / D[i]

    for i in range(n):
        L[i, i] = 1.0

    return L, D


def lambda_decorrelate(Qa):
    """LDL^T decomposition + integer Gauss transforms (Z-transform).

    Decorrelates the ambiguity covariance so the search space is small.

    Args:
        Qa: (n, n) float ambiguity covariance matrix

    Returns:
        z_float_transform: (n, n) integer Z matrix (unimodular)
        L: (n, n) unit lower triangular of decorrelated LDL^T
        D: (n,) diagonal of decorrelated LDL^T
    """
    n = Qa.shape[0]
    Q = Qa.copy()
    Z = np.eye(n, dtype=float)

    L, D = _ldl(Q)

    # Integer Gauss transforms to reduce off-diagonal elements
    for i in range(n - 2, -1, -1):
        for j in range(i + 1, n):
            mu = round(L[i, j])
            if mu != 0:
                L[i, j:] -= mu * L[j, j:]
                Z[:, i] -= mu * Z[:, j]

        # Permutation: swap i and i+1 if it improves conditioning
        if i + 1 < n:
            delta = D[i] + L[i, i + 1] ** 2 * D[i + 1]
            if delta < D[i + 1]:
                eta = D[i] / delta
                lam = D[i + 1] * L[i, i + 1] / delta
                D[i] = D[i + 1] * eta
                D[i + 1] = delta

                # Swap rows in L
                L[i, i + 1] = lam
                tmp = L[i, i + 2:].copy()
                L[i, i + 2:] = L[i + 1, i + 2:]
                L[i + 1, i + 2:] = tmp

                # Update columns below
                for k in range(i - 1, -1, -1):
                    tmp_k = L[k, i]
                    L[k, i] = L[k, i + 1]
                    L[k, i + 1] = tmp_k

                # Swap columns in Z
                tmp_z = Z[:, i].copy()
                Z[:, i] = Z[:, i + 1]
                Z[:, i + 1] = tmp_z

                # Re-do Gauss transforms after permutation
                for j in range(i + 1, n):
                    mu = round(L[i, j])
                    if mu != 0:
                        L[i, j:] -= mu * L[j, j:]
                        Z[:, i] -= mu * Z[:, j]

    return Z, L, D


def lambda_search(z_float, L, D, n_candidates=2):
    """Integer search in decorrelated space (Teunissen 1995).

    Sequential conditional enumeration: search dimension by dimension
    from last to first, pruning branches whose partial cost exceeds
    the current best.

    Args:
        z_float: (n,) decorrelated float ambiguities
        L: (n, n) unit lower triangular from decorrelated LDL^T
        D: (n,) diagonal from decorrelated LDL^T
        n_candidates: number of best candidates to keep

    Returns:
        list of (candidate_vector, omega) sorted by omega (ascending)
    """
    n = len(z_float)
    candidates = []
    max_omega = float('inf')

    dist = np.zeros(n + 1)   # accumulated partial distances (dist[n]=0)
    z_cond = np.zeros(n)     # conditional means
    z_int = np.zeros(n, dtype=int)  # current integer candidate
    step = np.zeros(n, dtype=int)   # zigzag step counter

    # Initialize top level
    k = n - 1
    z_cond[k] = z_float[k]
    z_int[k] = round(z_cond[k])
    step[k] = 0  # 0 = nearest, will increment after use

    itr = 0
    max_itr = max(10000, n * 500)

    while itr < max_itr:
        itr += 1
        residual = z_cond[k] - z_int[k]
        new_dist = dist[k + 1] + residual ** 2 / D[k]

        if new_dist < max_omega:
            if k == 0:
                # Complete candidate found
                cand = z_int.copy()
                candidates.append((cand, new_dist))
                candidates.sort(key=lambda c: c[1])
                if len(candidates) > n_candidates:
                    candidates = candidates[:n_candidates]
                if len(candidates) >= n_candidates:
                    max_omega = candidates[-1][1]
                # Zigzag to next integer at level 0
                step[k] += 1
                z_int[k] = round(z_cond[k]) + _zigzag(step[k])
            else:
                # Descend to next dimension
                k -= 1
                dist[k + 1] = new_dist
                z_cond[k] = z_float[k]
                for j in range(k + 1, n):
                    z_cond[k] -= L[k, j] * (z_int[j] - z_float[j])
                z_int[k] = round(z_cond[k])
                step[k] = 0
        else:
            # Prune: ascend
            k += 1
            if k >= n:
                break  # search complete
            # Zigzag to next integer at this level
            step[k] += 1
            z_int[k] = round(z_cond[k]) + _zigzag(step[k])

    return candidates


def _zigzag(s):
    """Map step counter to zigzag offset: 0→0, 1→+1, 2→-1, 3→+2, 4→-2, ..."""
    if s == 0:
        return 0
    sign = 1 if s % 2 == 1 else -1
    return sign * ((s + 1) // 2)


def bootstrap_success_rate(D):
    """Bootstrap success rate (Teunissen 1999).

    Estimates P(LAMBDA returns the correct integer vector) from the
    decorrelated LDL^T diagonal.  Each dimension contributes independently:
      P_i = 2*Phi(1/(2*sqrt(D[i]))) - 1
    where Phi is the standard normal CDF.

    When D[i] is large (uncertain ambiguity), P_i ≈ 0 → product → 0.
    When D[i] is small (tight ambiguity), P_i ≈ 1 → product preserved.

    Args:
        D: diagonal vector from decorrelated LDL^T

    Returns:
        float in [0, 1]: probability that the integer solution is correct
    """
    p = 1.0
    for d in D:
        if d <= 0:
            return 0.0
        p *= 2.0 * norm.cdf(0.5 / np.sqrt(d)) - 1.0
        if p < 1e-10:
            return 0.0  # early exit — already negligible
    return p


def lambda_resolve(a_float, Qa, ratio_threshold=2.0, min_fixed=4,
                   min_success_rate=0.999):
    """Full LAMBDA AR with partial AR fallback.

    Args:
        a_float: (n,) float ambiguity vector
        Qa: (n, n) ambiguity covariance matrix
        ratio_threshold: accept if Omega(2nd)/Omega(1st) > this
        min_fixed: minimum ambiguities for partial AR
        min_success_rate: minimum bootstrap success rate to accept
            (Teunissen 1999).  Prevents premature fixing when the
            float covariance is too large for reliable integers.

    Returns:
        (fixed_vector, n_fixed, ratio, fixed_mask)
        fixed_vector: (n,) integer ambiguities (or None if failed)
        n_fixed: number of resolved ambiguities
        ratio: validation ratio (0 if no candidates)
        fixed_mask: boolean array, True for resolved ambiguities
    """
    n = len(a_float)
    if n < min_fixed:
        return None, 0, 0.0, np.zeros(n, dtype=bool)

    # Try full set first, then partial AR
    indices = np.arange(n)

    while len(indices) >= min_fixed:
        a_sub = a_float[indices]
        Qa_sub = Qa[np.ix_(indices, indices)]

        # Regularize if needed
        eigvals = np.linalg.eigvalsh(Qa_sub)
        if eigvals.min() <= 0:
            Qa_sub += np.eye(len(indices)) * max(1e-10, -eigvals.min() * 2)

        Z, L, D = lambda_decorrelate(Qa_sub)

        # Bootstrap success rate gate: don't even search if the
        # covariance says P(correct) is too low
        p_success = bootstrap_success_rate(D)
        if p_success < min_success_rate:
            indices = _drop_worst(a_sub, Qa_sub, indices)
            continue

        z_float = Z.T @ a_sub
        candidates = lambda_search(z_float, L, D, n_candidates=2)

        if len(candidates) < 2:
            # Not enough candidates — remove worst and retry
            indices = _drop_worst(a_sub, Qa_sub, indices)
            continue

        ratio = candidates[1][1] / max(candidates[0][1], 1e-30)

        if ratio > ratio_threshold:
            # Accept — back-transform to original space
            z_fixed = candidates[0][0]
            # Z^{-T} * z_fixed = original-space integers
            # Z is integer unimodular, so Z^{-1} exists and is integer
            n_fixed_vec = np.linalg.solve(Z.T, z_fixed.astype(float))
            n_fixed_vec = np.round(n_fixed_vec).astype(int)

            result = np.full(n, np.nan)
            mask = np.zeros(n, dtype=bool)
            for i, idx in enumerate(indices):
                result[idx] = n_fixed_vec[i]
                mask[idx] = True

            return result, len(indices), ratio, mask

        # Partial AR: remove the ambiguity with largest contribution
        indices = _drop_worst(a_sub, Qa_sub, indices)

    return None, 0, 0.0, np.zeros(n, dtype=bool)


def _drop_worst(a_sub, Qa_sub, indices):
    """Remove the ambiguity with the largest marginal variance.

    This is the standard PAR heuristic: the ambiguity with the
    largest diagonal covariance contributes most uncertainty.
    """
    diag = np.diag(Qa_sub)
    worst = np.argmax(diag)
    return np.delete(indices, worst)
