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
from pathlib import Path
from unittest.mock import patch

import pytest

from cvise.passes.hint_based import HintState
from cvise.utils import memory, overlay
from cvise.utils.error import CViseError, UndecidedTestError
from cvise.utils.folding import FoldingManager, FoldingStateIn, FoldingStateOut
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


class TestUndecidedFoldIsRetryable:
    """The fold analogue of the pass-result undecided handling.

    maybe_prepare_folding_job records a fold in attempted_folds at schedule
    time -- before any verdict exists. handle_finished_transform_job then does
    nothing for an undecided fold (it never judged it). Unless that
    schedule-time record is undone, one transient out-of-memory permanently
    bans a good reduction from the fold search for the rest of the run.
    """

    @staticmethod
    def _manager_with_two_candidates():
        manager = FoldingManager()
        # Two distinct, hashable HintState instances is the minimum
        # maybe_prepare_folding_job will fold. Only the identity/hash matters here;
        # the underlying enumeration state is never exercised.
        candidate_a = HintState(
            tmp_dir=Path('/a'),
            per_type_states=(),
            ptr=0,
            special_hints=(),
        )
        candidate_b = HintState(
            tmp_dir=Path('/b'),
            per_type_states=(),
            ptr=0,
            special_hints=(),
        )
        manager.on_transform_job_success(candidate_a)
        manager.on_transform_job_success(candidate_b)
        return manager

    def test_an_unjudged_fold_is_not_permanently_banned(self):
        manager = self._manager_with_two_candidates()
        # Schedule a fold exactly as the reducer does: this records it in
        # attempted_folds before any test runs.
        first = manager.maybe_prepare_folding_job(job_order=0, best_success_state=None)
        assert first is not None
        # The fold comes back undecided. Replicate the real UNDECIDED branch in
        # handle_finished_transform_job: it must hand the returned state back to
        # the manager so the schedule-time ban is undone. The transform returns a
        # FoldingStateOut carrying the same sub_states as the scheduled
        # FoldingStateIn, so reconstruct that shape.
        returned = FoldingStateOut(
            sub_states=first.sub_states,
            size_delta_per_pass={},
            passes_ordered_by_delta=[],
        )
        manager.on_transform_job_undecided(returned)
        # The fold must be re-schedulable: it was never judged against it.
        again = manager.maybe_prepare_folding_job(job_order=0, best_success_state=None)
        assert again is not None


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
    def test_naming_the_library_is_asking_for_the_overlay(self, monkeypatch):
        """There is no second switch to forget.

        It cannot key off the delta: there is one per job now, created by
        C-Vise itself, so the guard would be reading a variable the user never
        sets and would never fire in the only mode that uses the overlay.
        """
        monkeypatch.setenv(overlay.LIB_ENV, '/somewhere/libfakechroot.so')
        assert overlay.overlay_configured()

    def test_the_library_of_this_build_comes_first(self, monkeypatch, tmp_path):
        """Not the installed one, which is whatever was installed last.

        A stale overlay does not fail; it answers differently. Preferring the
        installed copy meant a change was tested against the previous version of
        the very component whose being wrong looks like a plausible result.
        """
        monkeypatch.delenv(overlay.LIB_ENV, raising=False)
        built = tmp_path / 'built.so'
        installed = tmp_path / 'installed.so'
        built.write_bytes(b'built')
        installed.write_bytes(b'installed')
        monkeypatch.setattr(overlay, 'BUILT_LIB', str(built))
        monkeypatch.setattr(overlay, 'INSTALLED_LIB', str(installed))
        assert overlay.library_path() == str(built)

    def test_the_installed_library_is_used_when_there_is_no_build_tree(self, monkeypatch, tmp_path):
        """Which is the case for every user who did not build C-Vise themselves."""
        monkeypatch.delenv(overlay.LIB_ENV, raising=False)
        installed = tmp_path / 'installed.so'
        installed.write_bytes(b'installed')
        monkeypatch.setattr(overlay, 'BUILT_LIB', str(tmp_path / 'never-built.so'))
        monkeypatch.setattr(overlay, 'INSTALLED_LIB', str(installed))
        assert overlay.library_path() == str(installed)

    def test_without_a_library_the_overlay_is_not_expected(self, monkeypatch, tmp_path):
        no_library_anywhere(monkeypatch, tmp_path)
        assert not overlay.overlay_configured()

    def test_a_library_that_is_not_there_is_refused(self, monkeypatch, tmp_path):
        """The whole point: an absent overlay must not look like a working one.

        Without redirection the compiler reads the original sources, every
        candidate is "interesting", and the run confidently reduces nothing.
        """
        monkeypatch.setenv(overlay.LIB_ENV, str(tmp_path / 'not-built.so'))
        with pytest.raises(OverlayNotProvenError):
            overlay.prove_overlay()

    def test_a_library_that_does_not_redirect_is_refused(self, monkeypatch, tmp_path):
        """Loaded is not the same as working."""
        impostor = tmp_path / 'impostor.so'
        impostor.write_bytes(b'not an object file')
        monkeypatch.setenv(overlay.LIB_ENV, str(impostor))
        with pytest.raises(OverlayNotProvenError):
            overlay.prove_overlay()

    def test_no_library_named_means_nothing_to_prove(self, monkeypatch, tmp_path):
        no_library_anywhere(monkeypatch, tmp_path)
        with pytest.raises(OverlayNotProvenError):
            overlay.prove_overlay()


def no_library_anywhere(monkeypatch, tmp_path) -> None:
    """A machine with no overlay at all, whichever machine is running the test.

    Unsetting the environment variable is not enough now that a build tree and
    an installed copy are searched as well: on a developer's machine both exist,
    so a test written that way asks about this machine rather than about the
    policy it means to check.
    """
    monkeypatch.delenv(overlay.LIB_ENV, raising=False)
    monkeypatch.setattr(overlay, 'BUILT_LIB', str(tmp_path / 'no-build-tree.so'))
    monkeypatch.setattr(overlay, 'INSTALLED_LIB', str(tmp_path / 'not-installed.so'))


class TestWhichTreesAreIsolated:
    """More than one, and the second one decides most of the answers.

    The sources are what a candidate changes; the build directory is where the
    answer about it is computed. Sharing the second gave each job whatever the
    previous one built, so the list has to survive being written down and read
    back exactly.
    """

    def test_several_trees_are_passed_on(self):
        assert overlay.roots_value(['/a/sources', '/b/build']) == '/a/sources:/b/build'

    def test_one_tree_still_works(self):
        assert overlay.roots_value(['/a/sources']) == '/a/sources'

    def test_a_name_that_cannot_be_written_down_is_refused(self):
        """Silence here means a tree left shared, which is a wrong answer."""
        with pytest.raises(CViseError, match='colon'):
            overlay.roots_value(['/a/odd:name', '/b/build'])

    def test_the_filesystem_root_is_refused(self):
        """It means "redirect everything", and everything is not the reduction."""
        with pytest.raises(CViseError, match='filesystem root'):
            overlay.roots_value(['/', '/b/build'])

    def test_the_environment_carries_every_tree(self, monkeypatch, tmp_path):
        library = tmp_path / 'lib.so'
        library.write_bytes(b'not really, but named')
        monkeypatch.setenv(overlay.LIB_ENV, str(library))
        env = overlay.job_environment({}, tmp_path / 'delta', ['/a/sources', '/b/build'])
        assert env[overlay.ROOT_ENV] == '/a/sources:/b/build'
        assert env[overlay.DELTA_ENV] == str(tmp_path / 'delta')
        assert env['LD_PRELOAD'] == str(library)


class TestPrecheckVerdicts:
    """A cheap stage that could not run has not decided anything.

    Reading a timeout as "it passed" is how a machine under load quietly turns
    into a reducer that accepts things nobody checked; reading it as "it failed"
    is how it turns into one that discards good candidates. Both are wrong in
    the same way: they answer a question that was never asked.
    """

    def test_a_failing_compile_is_a_verdict(self, tmp_path):
        from cvise.utils import precheck

        bad = tmp_path / 'bad.c'
        bad.write_text('this is not C\n')
        assert not precheck.syntax_check(['cc', str(bad)], dict(os.environ), 60)

    def test_a_compiling_file_is_a_verdict(self, tmp_path):
        from cvise.utils import precheck

        good = tmp_path / 'good.c'
        good.write_text('int main(void) { return 0; }\n')
        assert precheck.syntax_check(['cc', str(good)], dict(os.environ), 60)

    def test_a_timeout_is_not_a_verdict(self, tmp_path):
        from cvise.utils import precheck

        # A script, not `sh -c`: syntax_check strips -c, which is right for a
        # compile command and fatal for a shell invocation.
        slow = tmp_path / 'slow.sh'
        slow.write_text('#!/bin/sh\nsleep 30\n')
        slow.chmod(0o755)
        with pytest.raises(precheck.Undecided):
            precheck.syntax_check([str(slow)], dict(os.environ), 0.2)

    def test_a_missing_compiler_is_not_a_verdict(self):
        from cvise.utils import precheck

        with pytest.raises(precheck.Undecided):
            precheck.syntax_check(['/nonexistent/cc'], dict(os.environ), 60)
