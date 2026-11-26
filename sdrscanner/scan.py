"""Scanning between sets of channels: when to move the receiver to the next set.

The receiver covers one scan set at a time (see planner.plan_scan). The
controller below decides, after every block of samples, whether to stay or to
move on. Times are counted in received samples ("stream seconds"), so a slow
computer or a retune does not change what was actually heard.

States
    listen  nothing heard yet on this set; move on after `listen_time` (the scan
            speed). A carrier on a CTCSS/DCS channel extends this until its tone
            has been decoded, so tone-squelched channels are not skipped.
    active  a channel of this set is open; stay.
    dwell   the last transmission ended; stay `dwell_time` for a reply, then move on.

`max_dwell` (if > 0) moves on from a set that has been busy that long, so one
busy channel cannot stop the scan for ever. `hold` stops scanning on the
current set without changing the state shown.
"""

from __future__ import annotations

from typing import Optional

from .config import ScanConfig

LISTEN, ACTIVE, DWELL = "listen", "active", "dwell"

# Time a tone decoder needs from the start of a carrier (see dsp.ChannelBank: CTCSS uses a
# 0.5 s window and 3 agreeing estimates, DCS a 0.75 s window and 2 agreeing decodes).
TONE_WAIT_CTCSS = 1.0
TONE_WAIT_DCS = 1.3


class ScanController:
    def __init__(self, cfg: ScanConfig, n_sets: int, index: int = 0):
        self.cfg = cfg                 # shared with the GUI: speed / dwell changes apply at once
        self.n = max(1, n_sets)
        self.hold = False
        self.skip: set[int] = set()    # sets the receiver could not tune to
        self.visits = 0
        self.enter(index)

    def enter(self, index: int) -> None:
        """Start listening on set `index` (called after every retune)."""
        self.index = index % self.n
        self.state = LISTEN
        self.t = 0.0                   # seconds received on this set
        self.busy = 0.0                # seconds since this visit's first transmission
        self.quiet = 0.0               # seconds since a channel was last open
        self._pending_since: Optional[float] = None
        self.visits += 1

    def next_index(self, step: int = 1) -> int:
        """The set `step` places on (negative = backwards), not counting skipped sets."""
        direction = 1 if step >= 0 else -1
        i, left = self.index, abs(step) or 1
        for _ in range(self.n * (abs(step) or 1)):
            i = (i + direction) % self.n
            if i not in self.skip:
                left -= 1
                if left == 0:
                    return i
        return (self.index + step) % self.n       # everything skipped: plain step

    def update(self, dt: float, any_open: bool, tone_wait: float = 0.0) -> bool:
        """Account for `dt` more seconds on this set. Returns True when it is time to move on.

        `any_open`: a channel of the set was open during these samples.
        `tone_wait`: > 0 while a tone-squelched channel has a carrier whose tone is not
        decoded yet; how long such a decode can take from the start of the carrier.
        """
        self.t += dt
        if any_open:
            if self.state == LISTEN:
                self.busy = 0.0
            self.state = ACTIVE
            self.quiet = 0.0
        else:
            if self.state == ACTIVE:
                self.state = DWELL
            self.quiet += dt
        if self.state != LISTEN:
            self.busy += dt
        if tone_wait > 0:
            if self._pending_since is None:
                self._pending_since = self.t - dt
        else:
            self._pending_since = None
        if self.hold or self.n <= 1:
            return False
        cfg = self.cfg
        # A carrier whose tone may still be decoded keeps the scanner here (listen and dwell).
        deciding = self._pending_since is not None and self.t < self._pending_since + tone_wait
        if self.state == LISTEN:
            return self.t >= cfg.listen_time and not deciding
        if cfg.max_dwell > 0 and self.busy >= cfg.max_dwell:
            return True
        return self.state == DWELL and self.quiet >= cfg.dwell_time and not deciding

    def describe(self) -> str:
        if self.hold:
            return "hold"
        return {LISTEN: "scanning", ACTIVE: "active", DWELL: "dwell"}[self.state]
