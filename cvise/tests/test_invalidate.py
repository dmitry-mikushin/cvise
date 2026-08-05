"""Deleting a file has to reach the build, and the verdict has to prove it did.

A candidate that removes a header changes no file ninja knows about: the header
is not a target and no live edge names it, so ninja reports nothing to do and
the test is run against the previous candidate's binary. That verdict is about
code this candidate never contained, and it arrives as a pass.

So these are about the two halves of making it arrive honestly -- the objects
that depended on what was removed are deleted before the build, and the verdict
is refused unless the binary under test actually came back new.
"""

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from cvise.utils import invalidate
from cvise.utils import project


DEPS = """\
obj/foo.o: #deps 3, deps mtime 1 (VALID)
    /src/foo.cpp
    /src/include/ids.hpp
    /src/include/other.hpp

obj/bar.o: #deps 2, deps mtime 1 (VALID)
    /src/bar.cpp
    /src/include/other.hpp
"""


def fake_ninja(directory: Path, output: str) -> None:
    """A ninja whose only job is to have an answer for `-t deps`."""
    directory.mkdir(parents=True, exist_ok=True)
    tool = directory / 'ninja'
    tool.write_text('#!/bin/sh\ncat <<\'EOF\'\n' + output + 'EOF\n')
    tool.chmod(0o755)


def whiteout(delta: Path, original: str) -> None:
    marker = delta / original.lstrip('/')
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.with_name(marker.name + invalidate.WHITEOUT_SUFFIX).write_text('')


class TestWhichObjectsAreStale:
    def test_a_deleted_header_condemns_what_included_it(self, tmp_path):
        fake_ninja(tmp_path / 'bin', DEPS)
        objects = invalidate.objects_depending_on(
            tmp_path / 'build', {'/src/include/ids.hpp'}
        )
        assert objects == []  # no ninja on PATH yet

    def test_with_ninja_present_the_including_object_is_named(self, tmp_path, monkeypatch):
        fake_ninja(tmp_path / 'bin', DEPS)
        monkeypatch.setenv('PATH', str(tmp_path / 'bin') + os.pathsep + os.environ['PATH'])
        (tmp_path / 'build').mkdir()
        assert invalidate.objects_depending_on(
            tmp_path / 'build', {'/src/include/ids.hpp'}
        ) == ['obj/foo.o']

    def test_a_header_two_objects_include_condemns_both(self, tmp_path, monkeypatch):
        fake_ninja(tmp_path / 'bin', DEPS)
        monkeypatch.setenv('PATH', str(tmp_path / 'bin') + os.pathsep + os.environ['PATH'])
        (tmp_path / 'build').mkdir()
        assert invalidate.objects_depending_on(
            tmp_path / 'build', {'/src/include/other.hpp'}
        ) == ['obj/foo.o', 'obj/bar.o']

    def test_a_header_nobody_included_condemns_nothing(self, tmp_path, monkeypatch):
        fake_ninja(tmp_path / 'bin', DEPS)
        monkeypatch.setenv('PATH', str(tmp_path / 'bin') + os.pathsep + os.environ['PATH'])
        (tmp_path / 'build').mkdir()
        assert invalidate.objects_depending_on(tmp_path / 'build', {'/src/unused.hpp'}) == []

    def test_no_ninja_at_all_is_not_an_exception(self, tmp_path, monkeypatch):
        """This runs inside a worker judging a candidate. Raising here would be
        reported as the candidate being undecided rather than as this being
        unable to do its job."""
        monkeypatch.setenv('PATH', str(tmp_path / 'empty'))
        assert invalidate.objects_depending_on(tmp_path, {'/src/x.hpp'}) == []


class TestWhatTheCandidateRemoved:
    def test_a_whiteout_names_the_path_the_build_knows(self, tmp_path):
        delta = tmp_path / 'delta'
        whiteout(delta, '/src/include/ids.hpp')
        assert invalidate.removed_by(delta) == ['/src/include/ids.hpp']

    def test_a_modified_file_is_not_a_removal(self, tmp_path):
        """It carries a new timestamp through the overlay and ninja rebuilds it
        by itself. Only disappearance is invisible."""
        delta = tmp_path / 'delta' / 'src'
        delta.mkdir(parents=True)
        (delta / 'foo.cpp').write_text('int main() {}\n')
        assert invalidate.removed_by(tmp_path / 'delta') == []


class TestTheObjectsAreActuallyGone:
    def run(self, tmp_path, monkeypatch, removed):
        fake_ninja(tmp_path / 'bin', DEPS)
        monkeypatch.setenv('PATH', str(tmp_path / 'bin') + os.pathsep + os.environ['PATH'])
        build = tmp_path / 'build'
        (build / 'obj').mkdir(parents=True)
        (build / 'obj' / 'foo.o').write_text('object')
        (build / 'obj' / 'bar.o').write_text('object')
        delta = tmp_path / 'delta'
        for path in removed:
            whiteout(delta, path)
        proc = subprocess.run(
            [sys.executable, str(Path(invalidate.__file__)), str(build), str(delta)],
            capture_output=True,
            text=True,
        )
        return proc, build

    def test_the_condemned_object_is_deleted_and_counted(self, tmp_path, monkeypatch):
        proc, build = self.run(tmp_path, monkeypatch, ['/src/include/ids.hpp'])
        assert proc.stdout.strip() == '1'
        assert not (build / 'obj' / 'foo.o').exists()
        assert (build / 'obj' / 'bar.o').exists(), 'condemned an object that was fine'

    def test_removing_nothing_deletes_nothing(self, tmp_path, monkeypatch):
        proc, build = self.run(tmp_path, monkeypatch, [])
        assert proc.stdout.strip() == '0'
        assert (build / 'obj' / 'foo.o').exists()

    def test_a_delta_that_was_never_created_is_not_an_error(self, tmp_path, monkeypatch):
        fake_ninja(tmp_path / 'bin', DEPS)
        monkeypatch.setenv('PATH', str(tmp_path / 'bin') + os.pathsep + os.environ['PATH'])
        proc = subprocess.run(
            [sys.executable, str(Path(invalidate.__file__)), str(tmp_path), str(tmp_path / 'nope')],
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0
        assert proc.stdout.strip() == '0'


class TestTheVerdictRefusesToRestOnAnotherCandidatesBinary:
    """The generated check script's own decision, exercised through sh.

    Stubbed at the two places it reaches outside itself -- the thing that counts
    removals and the thing that builds -- because what is under test is which
    verdict it reaches from which facts, not whether cmake works.
    """

    def script(self, tmp_path, target='unit_tests'):
        class Project:
            build_dir = tmp_path / 'build'

        Project.build_dir.mkdir(parents=True, exist_ok=True)
        return project.check_script(Project, 'Some.Test', tmp_path / 'check.sh', target=target)

    def stub(self, directory, name, body):
        directory.mkdir(parents=True, exist_ok=True)
        tool = directory / name
        tool.write_text('#!/bin/sh\n' + body)
        tool.chmod(0o755)

    def run(self, tmp_path, *, killed, rebuild, target='unit_tests', binary=True):
        """Run the generated script with `killed` removals and a build that does
        or does not touch the binary."""
        path = self.script(tmp_path, target)
        text = path.read_text()
        # The count comes from an absolute path baked into the script; replace
        # that one command rather than the logic that consumes it.
        for line in text.splitlines():
            if line.startswith('killed=$('):
                text = text.replace(line, f'killed={killed}')
        path.write_text(text)

        binary_path = tmp_path / 'build' / 'unit_tests'
        if binary:
            binary_path.write_text('old')
            os.utime(binary_path, (1000, 1000))
        bin_dir = tmp_path / 'bin'
        touch = f'touch {binary_path}\n' if rebuild else ''
        self.stub(bin_dir, 'cmake', touch + 'exit 0\n')
        self.stub(bin_dir, 'ctest', 'echo "ctest ran"\nexit 0\n')
        env = dict(os.environ, PATH=str(bin_dir) + os.pathsep + os.environ['PATH'])
        job = tmp_path / 'job'
        job.mkdir(exist_ok=True)
        return subprocess.run(
            ['sh', str(path)], cwd=job, env=env, capture_output=True, text=True
        )

    def test_an_untouched_tree_that_rebuilds_nothing_is_allowed_through(self, tmp_path):
        """Removing nothing rightly rebuilds nothing; refusing that would refuse
        every candidate that only edited a file the test does not reach."""
        proc = self.run(tmp_path, killed=0, rebuild=False)
        assert proc.returncode == 0
        assert 'ctest ran' in proc.stdout

    def test_removals_that_did_rebuild_are_allowed_through(self, tmp_path):
        proc = self.run(tmp_path, killed=7, rebuild=True)
        assert proc.returncode == 0
        assert 'ctest ran' in proc.stdout

    def test_removals_that_rebuilt_nothing_are_refused(self, tmp_path):
        """The failure this exists for: the binary is the previous candidate's."""
        proc = self.run(tmp_path, killed=7, rebuild=False)
        assert proc.returncode == 125
        assert 'judged by another one' in proc.stdout
        assert 'ctest ran' not in proc.stdout

    def test_a_binary_that_was_never_there_is_refused_not_assumed(self, tmp_path):
        """Two absences compare equal, and calling that "unchanged" or calling it
        "fine" are both answers about a comparison that did not happen."""
        proc = self.run(tmp_path, killed=7, rebuild=False, binary=False)
        assert proc.returncode == 125
        assert 'cannot be shown to' in proc.stdout

    def test_without_a_target_the_check_still_refuses_rather_than_guesses(self, tmp_path):
        """No target means no binary to look for, so nothing can be verified."""
        proc = self.run(tmp_path, killed=7, rebuild=True, target=None, binary=False)
        assert proc.returncode == 125

    def test_a_failing_build_reports_the_build_and_not_the_guard(self, tmp_path):
        path = self.script(tmp_path)
        bin_dir = tmp_path / 'bin'
        self.stub(bin_dir, 'cmake', 'echo "error: ids.hpp not found"\nexit 1\n')
        self.stub(bin_dir, 'ctest', 'echo "ctest ran"\nexit 0\n')
        (tmp_path / 'build' / 'unit_tests').write_text('old')
        job = tmp_path / 'job'
        job.mkdir(exist_ok=True)
        proc = subprocess.run(
            ['sh', str(path)],
            cwd=job,
            env=dict(os.environ, PATH=str(bin_dir) + os.pathsep + os.environ['PATH']),
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 1
        assert 'ids.hpp not found' in proc.stdout
        assert 'ctest ran' not in proc.stdout
