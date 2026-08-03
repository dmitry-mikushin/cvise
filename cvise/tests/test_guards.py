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
    """One policy, and the same one for every run.

    There used to be two: bound_or_warn, which every run went through, and
    guard_memory_ceiling(required=...), which nothing called. The second was a
    switch with no user, kept alive by these tests -- so the checks worth having
    are folded into the first and the second is gone.
    """

    def test_a_real_ceiling_is_accepted(self):
        ram = memory.total_ram()
        with patch.object(memory, 'memory_ceiling', return_value=ram // 4):
            assert memory.bound_or_warn() == ram // 4

    def test_a_ceiling_above_physical_memory_is_not_a_ceiling(self):
        """The machine dies of its own scratch long before the cgroup notices.

        So it is not accepted as one: the run goes on to look for a real limit,
        and says why it did.
        """
        ram = memory.total_ram()
        with patch.object(memory, 'memory_ceiling', return_value=ram * 2), \
             patch.object(memory, 'available_ram', return_value=0), \
             patch.object(memory, 'scratch_is_ram', return_value=False):
            assert memory.bound_or_warn() != ram * 2

    def test_an_uninspectable_hierarchy_is_not_the_same_as_no_ceiling(self):
        """A bounded container reports exactly this, and must not be second-guessed."""
        with patch.object(memory, 'memory_ceiling', return_value=memory.UNKNOWN_CEILING):
            assert memory.bound_or_warn() is None

    def test_a_run_without_a_ceiling_still_runs(self):
        """An ordinary reduction on a desktop must not be refused.

        Demanding a cgroup limit from everyone trades one failure mode for a
        worse one: a tool that refuses to start is not protecting anything.
        """
        with patch.object(memory, 'memory_ceiling', return_value=None), \
             patch.object(memory, 'available_ram', return_value=0):
            assert memory.bound_or_warn() is None

    def test_the_root_cgroup_is_a_cgroup(self):
        """An empty path means the root, not "no cgroup at all"."""
        with patch('builtins.open', side_effect=lambda *a, **k: _fake_cgroup_file()):
            assert memory.current_cgroup() == '/'


def _fake_cgroup_file():
    import io

    return io.StringIO('0::/\n')


class TestWhereTheScratchLives:
    """The ceiling is about a combination, and this is the other half of it.

    A reduction whose scratch is on a disk cannot take the machine down by
    filling it, however large it grows. One whose scratch is a tmpfs can, and
    will, because those pages are charged to nobody the OOM killer can kill --
    and every job now materialises the part of the build its candidate changed
    there, the linked binary included. Warning about a missing ceiling
    regardless of which case it is teaches the user to ignore the warning by
    the time the dangerous one arrives.
    """

    def test_a_disk_is_not_memory(self, tmp_path):
        """tmp_path is under the machine's real temporary directory."""
        assert memory.filesystem_of('/proc') == 'proc'

    @pytest.mark.skipif(
        memory.filesystem_of('/dev/shm') != 'tmpfs', reason='/dev/shm is not a tmpfs here'
    )
    def test_shared_memory_is_memory(self):
        assert memory.scratch_is_ram('/dev/shm')

    def test_a_path_that_does_not_exist_answers_about_its_nearest_parent(self, tmp_path):
        deep = tmp_path / 'not' / 'created'
        assert memory.filesystem_of(deep) == memory.filesystem_of(tmp_path)

    def test_the_longest_mount_point_wins(self, tmp_path):
        """A nested mount is the one a path is actually on, not the one above it."""
        mountinfo = (
            '1 0 0:1 / / rw - ext4 /dev/sda1 rw\n'
            '2 1 0:2 / /nested rw - tmpfs tmpfs rw\n'
        )
        with patch('builtins.open', side_effect=lambda *a, **k: __import__('io').StringIO(mountinfo)):
            assert memory.filesystem_of('/nested/deep/file') == 'tmpfs'
            assert memory.filesystem_of('/elsewhere/file') == 'ext4'

    def test_scratch_on_a_disk_without_a_ceiling_is_not_alarming(self, tmp_path, caplog):
        with patch.object(memory, 'memory_ceiling', return_value=None), \
             patch.object(memory, 'available_ram', return_value=0), \
             patch.object(memory, 'scratch_is_ram', return_value=False):
            memory.bound_or_warn(scratch=str(tmp_path))
        assert not [r for r in caplog.records if r.levelname == 'WARNING']

    def test_scratch_in_memory_without_a_ceiling_is_alarming(self, tmp_path, caplog):
        with patch.object(memory, 'memory_ceiling', return_value=None), \
             patch.object(memory, 'available_ram', return_value=0), \
             patch.object(memory, 'scratch_is_ram', return_value=True):
            memory.bound_or_warn(scratch=str(tmp_path))
        warnings = [r.getMessage() for r in caplog.records if r.levelname == 'WARNING']
        assert warnings, 'the dangerous combination was not reported'
        assert 'TMPDIR' in warnings[0], 'the warning does not say what to do about it'


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


class TestTheCheapStageStaysCheap:
    """It runs for one changed file and for no more than one.

    The premise is that asking the compiler about one file is cheaper than the
    build it avoids, and the premise inverts the moment there is a second: the
    check is serial while the build is ninja, which is parallel, and every file
    asked about is a file the build is about to compile again.

    Passes are folded into a single candidate wherever they can be, so a
    candidate touching dozens of files is ordinary rather than exotic. MEASURED
    on ns-projection: such candidates spent some 430 s in this check, more than
    the whole 300 s timeout, before the build it was meant to save had started
    -- seventeen lost in eight minutes, and no progress at all.
    """

    def _asked_about(self, monkeypatch, changed):
        from cvise.utils import precheck
        from cvise.utils.testing import cheap_rejection

        asked = []
        monkeypatch.setattr(precheck, 'syntax_check', lambda c, e, t: asked.append(c) or True)
        command = {str(p): ['cc', str(p)] for p in changed}
        assert cheap_rejection(changed, command, {}, 60) is None
        return asked

    def test_one_changed_file_is_asked_about(self, tmp_path, monkeypatch):
        assert len(self._asked_about(monkeypatch, [tmp_path / 'a.c'])) == 1

    def test_two_changed_files_are_not_asked_about_at_all(self, tmp_path, monkeypatch):
        """Not "the first of them": none. The build is cheaper than asking twice."""
        assert self._asked_about(monkeypatch, [tmp_path / 'a.c', tmp_path / 'b.c']) == []

    def test_nothing_changed_is_not_asked_about(self, monkeypatch):
        assert self._asked_about(monkeypatch, []) == []

    def test_a_file_with_no_compile_command_is_skipped(self, tmp_path):
        from cvise.utils.testing import cheap_rejection

        assert cheap_rejection([tmp_path / 'a.c'], {}, {}, 60) is None

    def test_a_file_that_does_not_parse_is_rejected(self, tmp_path):
        from cvise.utils.testing import cheap_rejection

        bad = tmp_path / 'bad.c'
        bad.write_text('this is not C\n')
        verdict = cheap_rejection([bad], {str(bad): ['cc', str(bad)]}, dict(os.environ), 60)
        assert verdict is not None and verdict[0] == 1

    def test_a_stage_that_could_not_run_decides_nothing(self, tmp_path, monkeypatch):
        """A timeout is not a verdict; it is the absence of one."""
        from cvise.utils import precheck
        from cvise.utils.testing import UNDECIDED_EXIT_CODE, cheap_rejection

        def refuse(command, env, timeout):
            raise precheck.Undecided('the machine was too busy to answer')

        monkeypatch.setattr(precheck, 'syntax_check', refuse)
        a = tmp_path / 'a.c'
        verdict = cheap_rejection([a], {str(a): ['cc', str(a)]}, {}, 60)
        assert verdict is not None and verdict[0] == UNDECIDED_EXIT_CODE

    def test_the_object_stage_is_gone(self):
        """It duplicated the build's own compile; the build is the authority."""
        from cvise.utils import precheck

        assert not hasattr(precheck, 'object_check')


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


class TestStaleCgroups:
    """Every run leaves one behind, because it cannot remove the one it sits in.

    MEASURED after a day of work on this machine: 424 empty cvise-* cgroups.
    Each costs almost nothing, which is exactly why nobody notices them piling
    up.
    """

    def test_an_empty_one_is_removed(self, tmp_path):
        (tmp_path / 'cvise-1234').mkdir()
        assert memory.sweep_stale_cgroups(tmp_path) == 1
        assert not (tmp_path / 'cvise-1234').exists()

    def test_something_else_is_left_alone(self, tmp_path):
        """Only what this program names, so a sibling's cgroup is not ours."""
        (tmp_path / 'cvise-1').mkdir()
        (tmp_path / 'someone-elses.scope').mkdir()
        memory.sweep_stale_cgroups(tmp_path)
        assert (tmp_path / 'someone-elses.scope').is_dir()

    def test_one_still_in_use_survives(self, tmp_path):
        """rmdir on a non-empty directory fails, which is the whole safety here."""
        busy = tmp_path / 'cvise-999'
        busy.mkdir()
        (busy / 'cgroup.procs').write_text('999\n')
        assert memory.sweep_stale_cgroups(tmp_path) == 0
        assert busy.is_dir()

    def test_a_missing_parent_is_not_an_error(self, tmp_path):
        assert memory.sweep_stale_cgroups(tmp_path / 'nowhere') == 0
