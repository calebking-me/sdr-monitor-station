"""Choose an SDR centre frequency that covers the enabled channels.

The RTL-SDR has a DC spike (LO leakage) at the tuned centre frequency, and its
anti-alias filter rolls off near the band edges, so only the middle ~85% of the
sample rate is usable. The planner picks a centre that keeps every channel
inside the usable span and as far as possible from the DC spike.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional, Sequence

from .config import ChannelConfig, format_mhz

USABLE_FRACTION = 0.85     # fraction of the sample rate considered usable
DC_GUARD_HZ = 15_000.0     # wanted clearance between the DC spike and a channel edge
SEARCH_STEP_HZ = 1_000.0


@dataclass
class Plan:
    center: float
    sample_rate: float
    in_band: set[str] = field(default_factory=set)
    out_of_band: set[str] = field(default_factory=set)
    dc_conflict: set[str] = field(default_factory=set)
    warnings: list[str] = field(default_factory=list)

    @property
    def usable_lo(self) -> float:
        return self.center - usable_half_span(self.sample_rate)

    @property
    def usable_hi(self) -> float:
        return self.center + usable_half_span(self.sample_rate)


def usable_half_span(sample_rate: float) -> float:
    return sample_rate * USABLE_FRACTION / 2.0


def _edges(ch: ChannelConfig) -> tuple[float, float]:
    return ch.frequency - ch.bandwidth / 2.0, ch.frequency + ch.bandwidth / 2.0


def _dc_clearance(center: float, channels: Sequence[ChannelConfig]) -> float:
    """Smallest distance from the centre to any channel passband (negative = inside)."""
    best = float("inf")
    for ch in channels:
        lo, hi = _edges(ch)
        if lo <= center <= hi:
            d = -min(center - lo, hi - center)
        else:
            d = min(abs(center - lo), abs(center - hi))
        best = min(best, d)
    return best


def _best_window(channels: Sequence[ChannelConfig], span: float) -> list[ChannelConfig]:
    """Largest subset of channels that fits in `span` (sliding window over sorted freqs)."""
    chs = sorted(channels, key=lambda c: c.frequency)
    best: list[ChannelConfig] = []
    j = 0
    for i in range(len(chs)):
        while _edges(chs[i])[1] - _edges(chs[j])[0] > span:
            j += 1
        if i - j + 1 > len(best):
            best = chs[j:i + 1]
    return best


def classify(plan: Plan, channels: Iterable[ChannelConfig]) -> Plan:
    half = usable_half_span(plan.sample_rate)
    for ch in channels:
        if not ch.enabled:
            continue
        lo, hi = _edges(ch)
        if lo < plan.center - half or hi > plan.center + half:
            plan.out_of_band.add(ch.id)
            plan.warnings.append(
                f"'{ch.name}' ({format_mhz(ch.frequency)} MHz) is outside the captured band "
                f"{format_mhz(plan.center - half)}-{format_mhz(plan.center + half)} MHz")
            continue
        plan.in_band.add(ch.id)
        if lo - DC_GUARD_HZ / 3 < plan.center < hi + DC_GUARD_HZ / 3:
            plan.dc_conflict.add(ch.id)
            plan.warnings.append(
                f"'{ch.name}' is close to the DC spike at the centre frequency "
                f"({format_mhz(plan.center)} MHz)")
    return plan


def plan_center(
    channels: Sequence[ChannelConfig],
    sample_rate: float,
    auto_center: bool = True,
    manual_center: Optional[float] = None,
    fixed_center: Optional[float] = None,
) -> Plan:
    """Compute the centre frequency and which channels can be received.

    `fixed_center` is used for sources that cannot be retuned (IQ files).
    """
    enabled = [c for c in channels if c.enabled]
    if fixed_center is not None:
        return classify(Plan(fixed_center, sample_rate), enabled)
    if not auto_center or not enabled:
        center = manual_center if manual_center else (
            enabled[0].frequency if enabled else 146_000_000.0)
        if not auto_center:
            return classify(Plan(center, sample_rate), enabled)
        return Plan(center, sample_rate)

    span = 2 * usable_half_span(sample_rate)
    target = list(enabled)
    lo = min(_edges(c)[0] for c in target)
    hi = max(_edges(c)[1] for c in target)
    extra_warn = []
    if hi - lo > span:
        target = _best_window(enabled, span)
        lo = min(_edges(c)[0] for c in target)
        hi = max(_edges(c)[1] for c in target)
        extra_warn.append(
            f"Enabled channels span {(max(_edges(c)[1] for c in enabled) - min(_edges(c)[0] for c in enabled)) / 1e6:.3f} MHz, "
            f"more than the usable {span / 1e6:.3f} MHz at {sample_rate / 1e6:.3f} MS/s; "
            f"tuning to cover {len(target)} of {len(enabled)} channels")

    mid = (lo + hi) / 2.0
    # Allowed centre range keeps every target channel inside the usable span.
    c_min = hi - span / 2.0
    c_max = lo + span / 2.0
    best_c, best_score = mid, None
    n_steps = int((c_max - c_min) / SEARCH_STEP_HZ)
    for i in range(n_steps + 1):
        c = c_min + i * SEARCH_STEP_HZ
        clearance = min(_dc_clearance(c, target), 3 * DC_GUARD_HZ)
        # Prefer enough DC clearance, then staying close to the middle (away from band edges).
        score = (clearance, -abs(c - mid))
        if best_score is None or score > best_score:
            best_c, best_score = c, score
    center = round(best_c / 100.0) * 100.0
    plan = classify(Plan(center, sample_rate), enabled)
    plan.warnings[:0] = extra_warn
    return plan
