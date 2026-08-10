"""How many jobs run at once, decided every few seconds instead of once.

The count used to be derived from free memory at the instant a run started and
then held for its whole life. MEASURED, the same project on the same machine
within one day: 74 jobs starting on an idle machine, 66 with something else
holding 20 GB, and 16 when it started seconds after a docker build had filled
the page cache. The last run was four times slower than it needed to be, for
hours, because of one moment that had already passed by the time its first
candidate was built.
"""

import time

import pytest

from cvise.utils import memory
from cvise.utils.memory import next_allowance

GiB = 2 ** 30
CEILING = 100 * GiB


class TestTheController:
    def test_it_grows_by_one_when_there_is_room(self):
        assert next_allowance(10, 74, CEILING, 30 * GiB) == 11

    def test_it_retreats_by_a_quarter_when_crowded(self):
        """Fast down, slow up. The cost of growing too eagerly is the cgroup
        killing the run; the cost of growing too slowly is a few seconds."""
        assert next_allowance(40, 74, CEILING, 95 * GiB) == 30

    def test_it_holds_between_the_two_marks(self):
        """A single threshold would raise and lower on alternate readings for
        the whole run; the gap is what stops that."""
        for used in (0.65, 0.70, 0.80):
            assert next_allowance(10, 74, CEILING, int(used * CEILING)) == 10

    def test_it_never_exceeds_the_pool(self):
        """The worker pool is built once, for this many. Permitting more would
        permit nothing -- there is no slot to put the job in."""
        assert next_allowance(74, 74, CEILING, 1 * GiB) == 74

    def test_it_never_reaches_zero(self):
        """One job at a time is slow. Zero is a hang, and a run that cannot
        schedule anything never discovers that its estimate was wrong."""
        allowance = 8
        for _ in range(20):
            allowance = next_allowance(allowance, 74, CEILING, 99 * GiB)
        assert allowance == 1

    @pytest.mark.parametrize('ceiling,used', [
        (None, 5 * GiB),      # cannot read the ceiling
        (CEILING, None),      # cannot read the usage
        (0, 5 * GiB),         # a nonsense ceiling
        (-1, 5 * GiB),        # memory.UNKNOWN_CEILING
    ])
    def test_no_signal_means_the_full_pool_and_not_caution(self, ceiling, used):
        """"I cannot read the cgroup" is not "there is no memory".

        A reduction that throttled itself to one job on a machine it could not
        measure would be worse than one that never tried to measure.
        """
        assert next_allowance(3, 74, ceiling, used) == 74

    def test_it_climbs_back_from_a_bad_start(self):
        """The case the whole thing exists for: a run that began during a
        transient shortage must not be stuck at 16 for the rest of the day."""
        allowance = 16
        for _ in range(200):
            allowance = next_allowance(allowance, 74, CEILING, 20 * GiB)
        assert allowance == 74

    def test_it_settles_rather_than_oscillating(self):
        """Fed a usage that follows the job count, it must come to rest."""
        allowance = 1
        seen = []
        for _ in range(400):
            used = int(CEILING * (0.02 + 0.011 * allowance))
            allowance = next_allowance(allowance, 74, CEILING, used)
            seen.append(allowance)
        assert max(seen[-20:]) - min(seen[-20:]) <= 2, seen[-20:]


class TestReadingTheCgroup:
    def test_usage_is_a_number_or_an_admission(self):
        used = memory.memory_in_use()
        assert used is None or used > 0

    def test_it_does_not_fall_back_to_proc_meminfo(self):
        """Inside a container /proc/meminfo reports the HOST. MEASURED on a
        live run: 157 GB available by /proc/meminfo while the cgroup ceiling
        was 132.8 GiB with 41.7 in use. Answering from the wrong file would be
        worse than answering None."""
        used = memory.memory_in_use()
        if used is not None:
            assert used != memory.available_ram()


class TestTheManagerUsesIt:
    def a_manager(self, tmp_path, parallel):
        from cvise.utils import statistics, testing

        case = tmp_path / 'a.c'
        case.write_text('int a(void) { return 1; }\n')
        script = tmp_path / 'check.sh'
        script.write_text('#!/bin/sh\nexit 0\n')
        script.chmod(0o744)
        return testing.TestManager(
            statistics.PassStatistic(), script, 100, False, [case], parallel, False,
            True, False, False, False, None, False, None, None, None, 1.0,
        )

    def test_it_starts_at_the_full_pool(self, tmp_path):
        assert self.a_manager(tmp_path, 8).allowed_jobs == 8

    def test_a_crowded_ceiling_lowers_it(self, tmp_path, monkeypatch):
        m = self.a_manager(tmp_path, 8)
        monkeypatch.setattr(memory, 'memory_ceiling', lambda: CEILING)
        monkeypatch.setattr(memory, 'memory_in_use', lambda: 99 * GiB)
        m._temperature_taken = 0.0
        m.take_the_memory_temperature()
        assert m.allowed_jobs < 8

    def test_a_roomy_ceiling_raises_it_again(self, tmp_path, monkeypatch):
        m = self.a_manager(tmp_path, 8)
        m.allowed_jobs = 2
        monkeypatch.setattr(memory, 'memory_ceiling', lambda: CEILING)
        monkeypatch.setattr(memory, 'memory_in_use', lambda: 5 * GiB)
        m._temperature_taken = 0.0
        m.take_the_memory_temperature()
        assert m.allowed_jobs == 3

    def test_it_is_not_asked_more_often_than_it_steps(self, tmp_path, monkeypatch):
        """The controller's step is "one more job"; asking a hundred times a
        second would make that step meaningless."""
        m = self.a_manager(tmp_path, 74)
        m.allowed_jobs = 2
        asked = []
        monkeypatch.setattr(memory, 'memory_ceiling', lambda: CEILING)
        monkeypatch.setattr(memory, 'memory_in_use',
                            lambda: asked.append(1) or 5 * GiB)
        m._temperature_taken = time.monotonic()
        for _ in range(1000):
            m.take_the_memory_temperature()
        assert not asked, 'the cgroup was read inside the interval'
        assert m.allowed_jobs == 2
