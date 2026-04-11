"""PI servo controller for DO frequency steering."""


class PIServo:
    """Proportional-integral controller for DO frequency steering.

    Modeled after SatPulse's PI servo with anti-windup clamping.
    """

    def __init__(self, kp, ki, max_ppb=62_500_000.0, initial_freq=0.0):
        self.kp = kp
        self.ki = ki
        self.max_ppb = max_ppb
        if ki != 0:
            self.integral = -initial_freq / ki
        else:
            self.integral = 0.0
        self.freq = initial_freq

    def update(self, offset_ns, dt=1.0):
        """Process one sample. Returns frequency adjustment in ppb.

        Args:
            offset_ns: measured offset in nanoseconds (averaged over dt)
            dt: seconds since last correction. Scales the integral
                contribution so that a 2ns mean offset sustained for
                10s accumulates 10x more integral than 2ns for 1s.
                The proportional term is NOT scaled -- it responds
                to the current average error magnitude only.

        With M7 accumulate-then-correct:
            offset_ns = mean of N error samples
            dt = N (the discipline interval)
            Proportional: kp * avg_error (instantaneous response)
            Integral: ki * avg_error * dt ~ ki * sum_of_errors
        """
        output = self.kp * offset_ns + self.ki * (self.integral + offset_ns * dt)

        if abs(output) < self.max_ppb:
            self.integral += offset_ns * dt

        self.freq = max(-self.max_ppb, min(self.max_ppb, output))
        return self.freq

    def reset(self, current_freq):
        """Reset for bumpless transfer at mode change."""
        if self.ki != 0:
            self.integral = -current_freq / self.ki
        self.freq = current_freq
