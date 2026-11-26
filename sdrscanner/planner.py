"""Choose SDR centre frequencies that cover the enabled channels.

The RTL-SDR has a DC spike (LO leakage) at the tuned centre frequency, and its
anti-alias filter rolls off near the band edges, so only the middle ~85% of the
sample rate is usable. `plan_center` picks one centre that keeps every channel
inside the usable span and as far as possible from the DC spike.

When the channels are too far apart for one tuning, `plan_scan` splits them
into scan sets - groups that each fit in the usable span - so the receiver can
step between them (see scan.py).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable, Optional, Sequence

import numpy as np

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


def _choose_center(target: Sequence[ChannelConfig], span: float) -> float:
    """Centre that keeps `target` inside `span`, clear of the DC spike, near the middle.

    Candidates are 1 kHz apart; the best has the most DC clearance (capped at
    3x the guard) and, among equals, is closest to the middle of the channels.
    """
    los = np.array([_edges(c)[0] for c in target], dtype=np.float64)
    his = np.array([_edges(c)[1] for c in target], dtype=np.float64)
    lo, hi = float(los.min()), float(his.max())
    mid = (lo + hi) / 2.0
    # Allowed centre range keeps every target channel inside the usable span.
    c_min = hi - span / 2.0
    c_max = lo + span / 2.0
    n_steps = max(0, int((c_max - c_min) / SEARCH_STEP_HZ))
    cand = c_min + np.arange(n_steps + 1) * SEARCH_STEP_HZ
    d_lo = cand[:, None] - los[None, :]
    d_hi = his[None, :] - cand[:, None]
    inside = (d_lo >= 0) & (d_hi >= 0)
    dist = np.where(inside, -np.minimum(d_lo, d_hi), np.minimum(np.abs(d_lo), np.abs(d_hi)))
    clearance = np.minimum(dist.min(axis=1), 3 * DC_GUARD_HZ)
    # Most clearance first, then closest to the middle; the first (lowest) candidate wins ties.
    best = int(np.lexsort((np.abs(cand - mid), -clearance))[0])
    c = float(cand[best])
    # Round to 100 Hz, but never so far that a channel edge leaves the usable span.
    r = round(c / 100.0) * 100.0
    if c_min - 1e-6 <= r <= c_max + 1e-6:
        return r
    lo_r, hi_r = math.ceil(c_min / 100.0) * 100.0, math.floor(c_max / 100.0) * 100.0
    return min(max(r, lo_r), hi_r) if lo_r <= hi_r else c


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

    center = _choose_center(target, span)
    plan = classify(Plan(center, sample_rate), enabled)
    plan.warnings[:0] = extra_warn
    return plan


# --------------------------------------------------------------------------- scanning

@dataclass
class ScanSet:
    """Channels received together at one tuning while scanning."""

    number: int                   # 1-based, in frequency order
    center: float
    channel_ids: list[str]
    lo: float                     # lowest channel edge, Hz
    hi: float                     # highest channel edge, Hz

    def label(self) -> str:
        if self.hi - self.lo < 50_000:
            return f"{format_mhz((self.lo + self.hi) / 2)} MHz"
        return f"{self.lo / 1e6:.3f}–{self.hi / 1e6:.3f} MHz"


@dataclass
class ScanPlan:
    sample_rate: float
    sets: list[ScanSet] = field(default_factory=list)

    def set_index_of(self, channel_id: str) -> Optional[int]:
        for i, s in enumerate(self.sets):
            if channel_id in s.channel_ids:
                return i
        return None

    def index_of_center(self, center: float, tolerance: float = 1.0) -> Optional[int]:
        """The set tuned to `center` (the closest one within `tolerance` Hz)."""
        best, best_d = None, tolerance
        for i, s in enumerate(self.sets):
            d = abs(s.center - center)
            if d <= best_d:
                best, best_d = i, d
        return best

    def cycle_seconds(self, listen_time: float) -> float:
        """Time to check every set once when all of them are quiet."""
        return len(self.sets) * listen_time if len(self.sets) > 1 else 0.0


def plan_scan(channels: Sequence[ChannelConfig], sample_rate: float) -> ScanPlan:
    """Split the enabled channels into as few sets as practical, each fitting one tuning.

    Greedy from the lowest frequency: a set takes every remaining channel that
    fits in the usable span above the lowest one. A set whose channels would
    leave no room for the DC spike gives up its highest channels to the next set.
    """
    span = 2 * usable_half_span(sample_rate)
    remaining = sorted((c for c in channels if c.enabled), key=lambda c: (_edges(c)[0], c.frequency, c.id))
    plan = ScanPlan(sample_rate)
    while remaining:
        start = _edges(remaining[0])[0]
        group = [c for c in remaining if _edges(c)[1] - start <= span] or remaining[:1]
        center = _choose_center(group, span)
        while len(group) > 1 and _dc_clearance(center, group) < DC_GUARD_HZ / 3:
            group = group[:-1]
            center = _choose_center(group, span)
        ids = {c.id for c in group}
        plan.sets.append(ScanSet(len(plan.sets) + 1, center, [c.id for c in group],
                                 min(_edges(c)[0] for c in group), max(_edges(c)[1] for c in group)))
        remaining = [c for c in remaining if c.id not in ids]
    return plan
