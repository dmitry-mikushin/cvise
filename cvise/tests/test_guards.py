"""The environment guards and the "could not decide" verdict.

These exist because of failures that are invisible when they happen: a test
killed by the out-of-memory killer looks exactly like a test that returned "not
interesting", a reduction whose overlay is not loaded looks exactly like one
that is working, and a machine with no memory ceiling looks exactly like a
machine with one, right up until it dies. Every one of them was shipped once in
a state that did nothing, so the point of these tests is to keep them from
quietly reverting to that.
"""

import os
import subprocess
import sys
from unittest.mock import patch

import pytest

from cvise.utils import memory, overlay
from cvise.utils.error import UndecidedTestError
from cvise.utils.memory import NoMemoryCeilingError
from cvise.utils.overlay import OverlayNotProvenError
from cvise.utils.testing import (
    UNDECIDED_BUDGET,
    UNDECIDED_EXIT_CODE,
    PassCheckingOutcome,
    TestManager,
    is_undecided,
)


class TestUndecidedExitCode:
    def test_cooperative_code_is_undecided(self):
        assert is_undecided(UNDECIDED_EXIT_CODE)

    @pytest.mark.parametrize('signal_number', [9, 11, 6, 15])
    def test_death_by_signal_is_undecided(self, signal_number):
        """The case this exists for is not cooperative at all.

        A test killed by the OOM killer never gets to choose an exit code; the
        kernel answers for it and subprocess reports a negative number. Reading
        that as "not interesting" silently discards a good reduction, and does
        so more often the closer the machine is to its limit.
        """
        assert is_undecided(-signal_number)

    @pytest.mark.parametrize('code', [1, 2, 64, 113, 126, 127, 137])
    def test_ordinary_failure_is_a_verdict(self, code):
        """Everything else nonzero still means what it always meant."""
        assert not is_undecided(code)

    def test_success_is_a_verdict(self):
        assert not is_undecided(0)

    def test_a_killed_shell_really_does_report_a_negative_code(self):
        """Not a mock: this is the number the reducer will actually see."""
        proc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
        proc.kill()
        proc.wait()
        assert proc.returncode < 0
        assert is_undecided(proc.returncode)


class TestUndecidedAccounting:
    @staticmethod
    def _manager():
        manager = TestManager.__new__(TestManager)
        manager.undecided_count = 0
        manager.undecided_in_pass = 0
        return manager

    def test_within_budget_the_candidate_is_simply_not_judged(self):
        manager = self._manager()
        outcome = manager._handle_undecided(job=None)
        assert outcome == PassCheckingOutcome.UNDECIDED

    def test_undecided_is_not_a_failure(self):
        """It must not be IGNORE.

        IGNORE feeds the failure statistics and, under interleaving, bans the
        state from the fold search for the rest of the run. A candidate nobody
        managed to test is not evidence against that candidate.
        """
        manager = self._manager()
        assert manager._handle_undecided(job=None) != PassCheckingOutcome.IGNORE

    def test_the_budget_is_finite(self):
        """Past some point it is the environment that cannot be judged."""
        manager = self._manager()
        for _ in range(UNDECIDED_BUDGET):
            manager._handle_undecided(job=None)
        with pytest.raises(UndecidedTestError):
            manager._handle_undecided(job=None)

    def test_the_per_pass_counter_tracks_the_pass(self):
        """It is what suppresses caching, so it has to count the pass."""
        manager = self._manager()
        manager._handle_undecided(job=None)
        manager._handle_undecided(job=None)
        assert manager.undecided_in_pass == 2


class TestMemoryCeiling:
    def test_no_ceiling_is_refused_when_the_run_demands_one(self):
        with patch.object(memory, 'memory_ceiling', return_value=None):
            with pytest.raises(NoMemoryCeilingError):
                memory.guard_memory_ceiling(required=True)

    def test_no_ceiling_is_only_a_warning_otherwise(self):
        """An ordinary reduction on a desktop must still run.

        Demanding a cgroup limit from everyone trades one failure mode for a
        worse one: a tool that refuses to start is not protecting anything.
        """
        with patch.object(memory, 'memory_ceiling', return_value=None):
            assert memory.guard_memory_ceiling(required=False) is None

    def test_a_ceiling_above_physical_memory_is_not_a_ceiling(self):
        """The machine dies of its own scratch long before the cgroup notices."""
        ram = memory.total_ram()
        with patch.object(memory, 'memory_ceiling', return_value=ram * 2):
            with pytest.raises(NoMemoryCeilingError):
                memory.guard_memory_ceiling(required=True)

    def test_a_real_ceiling_is_accepted(self):
        ram = memory.total_ram()
        with patch.object(memory, 'memory_ceiling', return_value=ram // 4):
            assert memory.guard_memory_ceiling(required=True) == ram // 4

    def test_an_uninspectable_hierarchy_is_not_the_same_as_no_ceiling(self):
        """A bounded container reports exactly this, and must not be refused."""
        with patch.object(memory, 'memory_ceiling', return_value=memory.UNKNOWN_CEILING):
            assert memory.guard_memory_ceiling(required=True) is None

    def test_the_root_cgroup_is_a_cgroup(self):
        """An empty path means the root, not "no cgroup at all"."""
        with patch('builtins.open', side_effect=lambda *a, **k: _fake_cgroup_file()):
            assert memory.current_cgroup() == '/'


def _fake_cgroup_file():
    import io

    return io.StringIO('0::/\n')


class TestOverlayProof:
    def test_asking_for_a_delta_is_asking_for_the_overlay(self, monkeypatch):
        """There is no second switch to forget."""
        monkeypatch.setenv(overlay.DELTA_ENV, '/somewhere')
        assert overlay.overlay_configured()

    def test_without_a_delta_the_overlay_is_not_expected(self, monkeypatch):
        monkeypatch.delenv(overlay.DELTA_ENV, raising=False)
        assert not overlay.overlay_configured()

    def test_a_missing_library_is_refused(self, monkeypatch, tmp_path):
        """The whole point: an absent overlay must not look like a working one.

        Without redirection the compiler reads the original sources, every
        candidate is "interesting", and the run confidently reduces nothing.
        """
        monkeypatch.setenv(overlay.DELTA_ENV, str(tmp_path))
        if 'libfakechroot' in os.environ.get('LD_PRELOAD', ''):
            pytest.skip('the overlay is loaded into this process')
        with pytest.raises(OverlayNotProvenError):
            overlay.prove_overlay()

    def test_a_missing_delta_is_refused(self, monkeypatch):
        monkeypatch.delenv(overlay.DELTA_ENV, raising=False)
        with pytest.raises(OverlayNotProvenError):
            overlay.prove_overlay()
