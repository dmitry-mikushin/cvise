"""How fast is this going, and when does waiting stop paying?

A reduction has no natural end. It converges, and the difference between
converging and having converged is invisible from inside: the machine is busy
either way, the log keeps moving, and the only thing that changes is that
nothing gets smaller any more. MEASURED on ns-projection, one run of 10.96 h
spent 6.00 h -- 54.8% of itself -- after its last accepted reduction, and
stopped only when five separate passes had each burned 50 000 jobs.

The quantity with an answer in it is not the size curve. Fitted to the 152
accepted reductions of that run and graded by predicting what came after, both
an exponential and a power law miss by a median of 72%, and extrapolating the
recent slope -- what a dashboard would do -- is not only as wrong but biased:
it over-promised in nine cases out of nine.

The GAPS between accepted reductions are a different quantity. They are
observed rather than fitted, and they are stable within a run:

    median gap                                  10.1 to 13.4 min across 3 runs
    error of the median-so-far as a forecast    24% (median, n=62)
    next gap within 3x the median-so-far        98% of the time
    final silence of the two healthy runs       0.2 and 0.6 times the median
    final silence of the run that had ended     35.7 times the median

That last pair is the whole design. A run that is still working and a run that
is over are not near each other on this scale; they are a factor of fifty
apart, and no fit is needed to tell them apart.
"""

from __future__ import annotations

import logging
import os
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path

# How many times the usual gap the silence must exceed before a run is called
# finished. MEASURED on the three long runs, walking each as it happened and
# asking of every setting what it would have saved and what it would have
# thrown away. Of the 6.2 h those runs spent after their last reduction:
#
#            k=3    k=4    k=5    k=6    k=8   k=10   k=12   k=20
#   lost   10737      0      0      0      0      0      0      0   lines
#   saved    8.1    5.3    5.2    5.0    4.7    4.3    4.0    2.6   hours
#
# The loss is a cliff at 3 and there is nothing above it. 8 is chosen rather
# than 4 because the field was mapped with three runs, and a boundary drawn
# from three runs is not a boundary: it costs 0.6 h of the 6.2 h to stand
# almost three times clear of the only setting that ever lost anything.
#
# Beware of measuring this with the median taken over the intervals up to AND
# INCLUDING the one being judged. That look-ahead is not available at the
# moment the decision is made, and it moves the answer -- it is what first made
# the warmup below look load-bearing, which it is not.
PATIENCE = 8

# Intervals needed before their median is trusted at all. This is not a second
# safety margin -- above k=3 the table is flat in it -- it is what stops a run
# being judged on one or two numbers, which is the state every run passes
# through and in which no rule can mean anything.
WARMUP = 5

# The band quoted with the forecast: 98% of the 62 observed gaps fell inside it.
BAND = 3


@dataclass
class Pace:
    """The rhythm of a reduction, and what it implies about the next hour.

    Fed one call per accepted reduction. Everything it reports is derived from
    what has already happened in THIS run -- there is no model of reductions in
    general, and there is nothing to configure per project, because the numbers
    that would go in such a configuration are exactly the ones this measures.
    """

    patience: int = PATIENCE
    started: float = field(default_factory=time.monotonic)
    gaps: list[float] = field(default_factory=list)
    #: When the first point was recorded, on whatever clock recorded it.
    first: float | None = None
    last: float | None = None
    lines: int | None = None
    removed: int = 0

    def record(self, lines: int, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        if self.first is None:
            self.first = now
        if self.last is not None:
            self.gaps.append(now - self.last)
        if self.lines is not None:
            self.removed += max(0, self.lines - lines)
        self.last = now
        self.lines = lines

    @property
    def usual(self) -> float | None:
        """The gap to expect, or None while there is not enough to say."""
        if len(self.gaps) < WARMUP:
            return None
        return statistics.median(self.gaps)

    def silence(self, now: float | None = None) -> float:
        now = time.monotonic() if now is None else now
        return now - (self.last if self.last is not None else self.started)

    def deadline(self) -> float | None:
        """The moment this run is called finished unless something arrives.

        A bound rather than a guess, and it moves forward every time a candidate
        succeeds. That is what makes a reduction schedulable: not "it will take
        about a day" but "it is over by 14:20 unless it isn't, and you will know
        by 14:20 either way".
        """
        usual = self.usual
        if usual is None or self.last is None:
            return None
        return self.last + self.patience * usual

    def spent(self, now: float | None = None) -> bool:
        deadline = self.deadline()
        return deadline is not None and (time.monotonic() if now is None else now) > deadline

    def per_hour(self, now: float | None = None) -> float | None:
        """Lines removed per hour, measured from the first recorded point.

        Over the whole run and not a recent window on purpose: a window short
        enough to be current is shorter than the gap between successes for most
        of a reduction, so it reads zero at every instant that is not a success
        and a spike at every instant that is.

        From the first POINT and not from construction, and that is not a
        detail: `started` is a monotonic reading while `now` may be anything a
        caller passes, and mixing the two produced "0 lines/h" on a run that had
        removed 150 000 of them. Both ends of this subtraction now come from the
        same clock by construction.
        """
        now = time.monotonic() if now is None else now
        if self.first is None:
            return None
        elapsed = now - self.first
        if elapsed < 60 or not self.removed:
            return None
        return self.removed / (elapsed / 3600)


def clock(seconds: float) -> str:
    seconds = int(max(0, seconds))
    if seconds >= 3600:
        return f'{seconds // 3600}h{seconds % 3600 // 60:02d}m'
    return f'{seconds // 60}m{seconds % 60:02d}s'


def report(pace: Pace, now: float | None = None) -> str:
    """One line, saying what is known and refusing to say what is not."""
    now = time.monotonic() if now is None else now
    usual = pace.usual
    if usual is None:
        return (f'{len(pace.gaps)} of {WARMUP} intervals needed before this run can '
                f'say how fast it is going')
    parts = [f'a reduction every {clock(usual)}']
    rate = pace.per_hour(now)
    if rate is not None:
        parts.append(f'{rate:.0f} lines/h')
    left = pace.deadline() - now
    if left > 0:
        # Only while there is still a run to forecast for. Saying "next one
        # within 32m" beside "silent for 3h00m" is two statements that cannot
        # both be true, and the reader believes the reassuring one.
        parts.append(f'next one due within {clock(BAND * usual)}')
        parts.append(f'giving up in {clock(left)}')
    else:
        parts.append(f'silent for {clock(pace.silence(now))}')
        parts.append('giving up now')
    return '; '.join(parts)


class Series:
    """The progress of a run, written where another program can read it.

    Until now this existed only as a log line with a relative clock, so nothing
    downstream could compute anything from it -- and the monitor scraped
    `docker logs`. Worse, three consecutive runs in one log are indistinguishable
    from one, because each starts its clock at zero: reconstructing the series
    for the measurements above meant ordering the segments by file count and
    hoping. An absolute timestamp is the fix and it costs one field.

    The file outlives the run that wrote it, and several runs share a state
    directory, so each says where it begins. A reader that had to guess would
    guess by the size of the interval -- and the longest gap ever observed
    WITHIN a run is 66 min, which is not comfortably below how quickly a run can
    be restarted. Saying it is one line and removes the question.
    """

    HEADER = '#when\tbytes\tlines\tfiles\tvia\n'

    def __init__(self, path: Path | None):
        self.path = path
        if path is None:
            return
        try:
            if not path.exists():
                path.write_text(self.HEADER)
            with path.open('a') as f:
                f.write(f'#run\t{time.time():.0f}\t{os.getpid()}\n')
        except OSError as e:
            logging.warning('progress will not be recorded to %s: %s', path, e)
            self.path = None

    def add(self, byts: int, lines: int, files: int, via: str) -> None:
        if self.path is None:
            return
        try:
            with self.path.open('a') as f:
                f.write(f'{time.time():.0f}\t{byts}\t{lines}\t{files}\t{via}\n')
        except OSError as e:
            # Said once and then never again: a full disk would otherwise turn
            # every accepted reduction into a warning, and the reduction itself
            # is unharmed -- this file is a record, not a dependency.
            logging.warning('progress is no longer being recorded: %s', e)
            self.path = None
