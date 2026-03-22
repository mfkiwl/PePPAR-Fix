"""Timing fault injection for source-thread delay robustness tests."""

from __future__ import annotations

import csv
import logging
import os
import random
import threading
import time

log = logging.getLogger(__name__)

_ENV_LOG_PATH = "DELAY_LOG_PATH"
_ENV_THREAD_PCT = "THREAD_DELAY_PROB_PCT"
_ENV_THREAD_MEAN = "THREAD_DELAY_MEAN_MS"
_ENV_THREAD_RANGE = "THREAD_DELAY_RANGE_MS"
_ENV_SYS_PCT = "SYS_DELAY_PROB_PCT"
_ENV_SYS_MEAN = "SYS_DELAY_MEAN_MS"
_ENV_SYS_RANGE = "SYS_DELAY_RANGE_MS"


def _env_float(name):
    value = os.getenv(name)
    if value is None or value == "":
        return None
    try:
        return float(value)
    except ValueError:
        log.warning("Ignoring invalid %s=%r", name, value)
        return None


def _sample_delay_s(mean_ms, range_ms):
    if mean_ms is None:
        return 0.0
    if range_ms is None:
        range_ms = 0.0
    lo_ms = mean_ms - (range_ms / 2.0)
    hi_ms = mean_ms + (range_ms / 2.0)
    delay_ms = random.uniform(lo_ms, hi_ms)
    return max(0.0, delay_ms / 1000.0)


class DelayInjector:
    """Global timing fault injector driven by environment variables."""

    def __init__(self):
        self.thread_prob_pct = _env_float(_ENV_THREAD_PCT)
        self.thread_mean_ms = _env_float(_ENV_THREAD_MEAN)
        self.thread_range_ms = _env_float(_ENV_THREAD_RANGE)
        self.sys_prob_pct = _env_float(_ENV_SYS_PCT)
        self.sys_mean_ms = _env_float(_ENV_SYS_MEAN)
        self.sys_range_ms = _env_float(_ENV_SYS_RANGE)
        self.enabled = bool(
            (self.thread_prob_pct is not None and self.thread_prob_pct > 0.0)
            or (self.sys_prob_pct is not None and self.sys_prob_pct > 0.0)
        )

        self._tls = threading.local()
        self._sys_lock = threading.Lock()
        self._sys_generation = 0
        self._sys_duration_s = 0.0
        self._stop = threading.Event()
        self._controller = None

        self._csv_lock = threading.Lock()
        self._csv_file = None
        self._csv_writer = None
        self._log_path = os.getenv(_ENV_LOG_PATH)
        if self._log_path:
            self._open_log()

        if self.enabled and self.sys_prob_pct is not None and self.sys_prob_pct > 0.0:
            self._controller = threading.Thread(
                target=self._sys_controller,
                name="delay-injector",
                daemon=True,
            )
            self._controller.start()

    def _open_log(self):
        self._csv_file = open(self._log_path, "a", newline="")
        self._csv_writer = csv.writer(self._csv_file)
        if self._csv_file.tell() == 0:
            self._csv_writer.writerow([
                "source",
                "kind",
                "start_mono_s",
                "end_mono_s",
                "planned_delay_s",
                "actual_delay_s",
            ])
            self._csv_file.flush()

    def _log_delay(self, source_name, kind, start_mono, end_mono, planned_s):
        actual_s = max(0.0, end_mono - start_mono)
        log.info(
            "Injected %s delay on %s: start=%.6f end=%.6f planned=%.6fs actual=%.6fs",
            kind,
            source_name,
            start_mono,
            end_mono,
            planned_s,
            actual_s,
        )
        if self._csv_writer is None:
            return
        with self._csv_lock:
            self._csv_writer.writerow([
                source_name,
                kind,
                f"{start_mono:.6f}",
                f"{end_mono:.6f}",
                f"{planned_s:.6f}",
                f"{actual_s:.6f}",
            ])
            self._csv_file.flush()

    def _maybe_sleep(self, source_name, kind, delay_s):
        if delay_s <= 0.0:
            return 0.0
        start_mono = time.monotonic()
        time.sleep(delay_s)
        end_mono = time.monotonic()
        self._log_delay(source_name, kind, start_mono, end_mono, delay_s)
        return end_mono - start_mono

    def _sys_controller(self):
        interval_s = 0.1
        while not self._stop.is_set():
            time.sleep(interval_s)
            pct = self.sys_prob_pct
            if pct is None or pct <= 0.0:
                continue
            trigger_prob = min(1.0, (pct / 100.0) * interval_s)
            if random.random() >= trigger_prob:
                continue
            duration_s = _sample_delay_s(self.sys_mean_ms, self.sys_range_ms)
            if duration_s <= 0.0:
                continue
            with self._sys_lock:
                self._sys_generation += 1
                self._sys_duration_s = duration_s

    def maybe_inject_delay(self, source_name):
        """Optionally inject per-thread and system-wide synthetic delay."""
        if not self.enabled:
            return 0.0

        total_s = 0.0

        pct = self.thread_prob_pct
        if pct is not None and pct > 0.0 and random.random() < (pct / 100.0):
            total_s += self._maybe_sleep(
                source_name,
                "THREAD",
                _sample_delay_s(self.thread_mean_ms, self.thread_range_ms),
            )

        seen_generation = getattr(self._tls, "sys_generation", 0)
        with self._sys_lock:
            generation = self._sys_generation
            duration_s = self._sys_duration_s if generation > seen_generation else 0.0
        if generation > seen_generation:
            self._tls.sys_generation = generation
            total_s += self._maybe_sleep(source_name, "SYS", duration_s)

        return total_s

    def close(self):
        self._stop.set()
        if self._csv_file is not None:
            self._csv_file.close()
            self._csv_file = None
            self._csv_writer = None


_INJECTOR = None
_INJECTOR_LOCK = threading.Lock()


def get_delay_injector():
    """Return a lazily constructed global delay injector."""
    global _INJECTOR
    with _INJECTOR_LOCK:
        if _INJECTOR is None:
            _INJECTOR = DelayInjector()
        return _INJECTOR
