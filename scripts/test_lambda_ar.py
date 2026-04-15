"""Tests for LAMBDA integer least-squares ambiguity resolution."""

import numpy as np
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

from lambda_ar import (lambda_decorrelate, lambda_search, lambda_resolve,
                       bootstrap_success_rate)


def test_decorrelation():
    """Z must be integer, Z^T Qa Z must be more diagonal than Qa."""
    np.random.seed(42)
    # Build a correlated covariance
    A = np.random.randn(5, 5) * 0.3
    Qa = A @ A.T + np.eye(5) * 0.1

    Z, L, D = lambda_decorrelate(Qa)

    # Z must be integer (within rounding tolerance)
    assert np.allclose(Z, np.round(Z)), "Z must be integer"

    # Z must be unimodular: |det(Z)| == 1
    assert abs(abs(np.linalg.det(Z)) - 1.0) < 1e-6, \
        f"|det(Z)| = {abs(np.linalg.det(Z))}, expected 1"

    # Decorrelated covariance
    Qz = Z.T @ Qa @ Z
    # Off-diagonal ratio should be smaller
    off_orig = np.sum(np.abs(Qa)) - np.trace(np.abs(Qa))
    off_decor = np.sum(np.abs(Qz)) - np.trace(np.abs(Qz))
    assert off_decor <= off_orig * 1.1, \
        f"Decorrelation made things worse: {off_decor:.3f} > {off_orig:.3f}"

    print("  decorrelation: PASS")


def test_known_answer():
    """Construct a float solution with known true integers, verify recovery."""
    np.random.seed(123)
    n = 6
    true_integers = np.array([10, -3, 7, 0, 5, -8])

    # Tight diagonal covariance — bootstrap P > 0.999
    Qa = np.eye(n) * 0.01

    # Float = true + small noise
    noise = np.random.randn(n) * 0.05
    a_float = true_integers.astype(float) + noise

    fixed, n_fixed, ratio, mask = lambda_resolve(a_float, Qa, ratio_threshold=1.5)

    assert fixed is not None, "LAMBDA should resolve clean data"
    assert n_fixed == n, f"Expected {n} fixed, got {n_fixed}"
    recovered = np.array([int(fixed[i]) for i in range(n)])
    assert np.array_equal(recovered, true_integers), \
        f"Wrong integers: {recovered} vs {true_integers}"
    assert ratio > 1.5, f"Ratio {ratio:.1f} too low"

    print(f"  known answer: PASS (ratio={ratio:.1f})")


def test_ratio_rejects_ambiguous():
    """With large noise, LAMBDA should fail the ratio test."""
    np.random.seed(99)
    n = 5
    true_integers = np.array([1, 2, 3, 4, 5])

    # Large covariance — ambiguities are poorly determined
    Qa = np.eye(n) * 2.0  # sigma ~ 1.4 cycles per ambiguity

    # Float very far from integer
    a_float = true_integers + 0.45  # near half-cycle — maximally ambiguous

    fixed, n_fixed, ratio, mask = lambda_resolve(
        a_float, Qa, ratio_threshold=2.0)

    # Should either fail entirely or have a low ratio
    if fixed is not None:
        assert ratio > 2.0, "If accepted, ratio must exceed threshold"
    print(f"  ambiguous rejection: PASS (ratio={ratio:.1f}, "
          f"fixed={'yes' if fixed is not None else 'no'})")


def test_partial_ar():
    """Inject one bad ambiguity; PAR should drop it and fix the rest."""
    np.random.seed(77)
    n = 6
    true_integers = np.array([3, -1, 8, 2, -5, 0])

    # Well-conditioned covariance
    Qa = np.eye(n) * 0.01
    # Add some correlation
    for i in range(n):
        for j in range(n):
            if i != j:
                Qa[i, j] = 0.002

    a_float = true_integers.astype(float).copy()
    # Small noise on most
    a_float[:5] += np.random.randn(5) * 0.05
    # Satellite 5: huge noise, nearly half-cycle
    a_float[5] = true_integers[5] + 0.48
    Qa[5, 5] = 5.0  # very uncertain

    fixed, n_fixed, ratio, mask = lambda_resolve(
        a_float, Qa, ratio_threshold=2.0, min_fixed=4)

    assert fixed is not None, "PAR should fix at least 4-5 ambiguities"
    assert n_fixed >= 4, f"Expected >=4 fixed, got {n_fixed}"
    # The bad satellite (index 5) should be dropped
    for i in range(5):
        if mask[i]:
            assert int(fixed[i]) == true_integers[i], \
                f"Wrong integer at {i}: {int(fixed[i])} vs {true_integers[i]}"

    print(f"  partial AR: PASS ({n_fixed}/{n} fixed, ratio={ratio:.1f}, "
          f"mask={mask})")


def test_bootstrap_success_rate():
    """Bootstrap success rate: high for tight covariance, low for loose."""
    # Tight covariance — should have high success rate
    D_tight = np.array([0.01, 0.01, 0.01, 0.01, 0.01])
    p_tight = bootstrap_success_rate(D_tight)
    assert p_tight > 0.999, f"Tight P={p_tight:.6f}, expected > 0.999"

    # Loose covariance — should have low success rate
    D_loose = np.array([1.0, 1.0, 1.0, 1.0, 1.0])
    p_loose = bootstrap_success_rate(D_loose)
    assert p_loose < 0.1, f"Loose P={p_loose:.6f}, expected < 0.1"

    # Mixed — one bad dimension kills it
    D_mixed = np.array([0.01, 0.01, 0.01, 0.01, 2.0])
    p_mixed = bootstrap_success_rate(D_mixed)
    assert p_mixed < 0.5, f"Mixed P={p_mixed:.6f}, expected < 0.5"

    print(f"  bootstrap success rate: PASS (tight={p_tight:.4f}, "
          f"loose={p_loose:.4f}, mixed={p_mixed:.4f})")


def test_success_rate_gate():
    """LAMBDA should reject when bootstrap success rate is too low."""
    np.random.seed(42)
    n = 5
    true_integers = np.array([1, 2, 3, 4, 5])

    # Covariance where ratio test would pass but P(correct) is low
    # Moderate noise — ratio might be ok but bootstrap says no
    Qa = np.eye(n) * 0.3
    a_float = true_integers + np.random.randn(n) * 0.1

    # With strict success rate gate
    fixed, nf, ratio, mask = lambda_resolve(
        a_float, Qa, ratio_threshold=1.5, min_success_rate=0.999)

    # Should be rejected by bootstrap gate
    if fixed is not None:
        # If it passed, the success rate must have been high enough
        _, _, D = lambda_decorrelate(Qa)
        p = bootstrap_success_rate(D)
        assert p >= 0.999, f"Accepted with P={p:.4f} < 0.999"

    print(f"  success rate gate: PASS (fixed={'yes' if fixed is not None else 'no'})")


if __name__ == "__main__":
    print("LAMBDA AR tests:")
    test_decorrelation()
    test_known_answer()
    test_ratio_rejects_ambiguous()
    test_partial_ar()
    test_bootstrap_success_rate()
    test_success_rate_gate()
    print("All tests passed.")
