"""M6 competitive error source selection."""


class ErrorSource:
    """One candidate error estimate with its confidence."""
    __slots__ = ('name', 'error_ns', 'confidence_ns')

    def __init__(self, name, error_ns, confidence_ns):
        self.name = name
        self.error_ns = error_ns
        self.confidence_ns = confidence_ns

    def __repr__(self):
        return f"{self.name}({self.error_ns:+.1f}ns ±{self.confidence_ns:.1f})"


def compute_error_sources(pps_error_ns, qerr_ns, dt_rx_ns, dt_rx_sigma_ns,
                          pps_confidence=20.0, qerr_confidence=3.0,
                          carrier_max_sigma=50.0):
    """Compute all available error sources and return sorted by confidence.

    Args:
        pps_error_ns: fractional-second PHC error from PPS timestamp
        qerr_ns: quantization error from TIM-TP (None if unavailable)
        dt_rx_ns: receiver clock offset from carrier-phase filter
        dt_rx_sigma_ns: filter's confidence in dt_rx (None if unavailable)
        pps_confidence: assumed PPS-only confidence (ns)
        qerr_confidence: assumed PPS+qErr confidence (ns)
        carrier_max_sigma: max sigma to accept carrier-phase (ns)

    Returns:
        List of ErrorSource, sorted by confidence (best first).
    """
    sources = []

    sources.append(ErrorSource('PPS', pps_error_ns, pps_confidence))

    if qerr_ns is not None:
        sources.append(ErrorSource('PPS+qErr',
                                   pps_error_ns + qerr_ns,
                                   qerr_confidence))

    if dt_rx_sigma_ns is not None and dt_rx_sigma_ns < carrier_max_sigma:
        sources.append(ErrorSource('PPS+PPP',
                                   pps_error_ns + dt_rx_ns,
                                   dt_rx_sigma_ns))

    sources.sort(key=lambda s: s.confidence_ns)
    return sources
