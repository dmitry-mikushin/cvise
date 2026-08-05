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
import re
from pathlib import Path

from cvise.utils import invalidate
from cvise.utils import project
from cvise.utils import projectcheck


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
    def setup(self, tmp_path, monkeypatch, removed):
        fake_ninja(tmp_path / 'bin', DEPS)
        monkeypatch.setenv('PATH', str(tmp_path / 'bin') + os.pathsep + os.environ['PATH'])
        build = tmp_path / 'build'
        (build / 'obj').mkdir(parents=True)
        (build / 'obj' / 'foo.o').write_text('object')
        (build / 'obj' / 'bar.o').write_text('object')
        delta = tmp_path / 'delta'
        for path in removed:
            whiteout(delta, path)
        return build, delta

    def test_the_condemned_object_is_deleted_and_counted(self, tmp_path, monkeypatch):
        build, delta = self.setup(tmp_path, monkeypatch, ['/src/include/ids.hpp'])
        assert invalidate.remove_stale(build, delta) == 1
        assert not (build / 'obj' / 'foo.o').exists()
        assert (build / 'obj' / 'bar.o').exists(), 'condemned an object that was fine'

    def test_removing_nothing_deletes_nothing(self, tmp_path, monkeypatch):
        build, delta = self.setup(tmp_path, monkeypatch, [])
        assert invalidate.remove_stale(build, delta) == 0
        assert (build / 'obj' / 'foo.o').exists()

    def test_a_delta_that_was_never_created_is_not_an_error(self, tmp_path, monkeypatch):
        build, _ = self.setup(tmp_path, monkeypatch, [])
        assert invalidate.remove_stale(build, tmp_path / 'nope') == 0


class TestTheVerdictRefusesToRestOnAnotherCandidatesBinary:
    """Stubbed at the two places the check reaches outside itself -- the build
    and the project's test -- because what is under test is which verdict it
    reaches from which facts, not whether cmake works."""

    def stub(self, directory, name, body):
        directory.mkdir(parents=True, exist_ok=True)
        tool = directory / name
        tool.write_text('#!/bin/sh\n' + body)
        tool.chmod(0o755)

    def run(self, tmp_path, monkeypatch, *, killed, rebuild, target='unit_tests', binary=True):
        build = tmp_path / 'build'
        build.mkdir(parents=True, exist_ok=True)
        binary_path = build / 'unit_tests'
        if binary:
            binary_path.write_text('old')
            os.utime(binary_path, (1000, 1000))

        bin_dir = tmp_path / 'bin'
        self.stub(bin_dir, 'cmake', (f'touch {binary_path}\n' if rebuild else '') + 'exit 0\n')
        self.stub(bin_dir, 'ctest', 'echo "ctest ran"\nexit 0\n')
        monkeypatch.setenv('PATH', str(bin_dir) + os.pathsep + os.environ['PATH'])
        monkeypatch.setattr(projectcheck.invalidate, 'remove_stale', lambda *a: killed)

        job = tmp_path / 'job'
        job.mkdir(exist_ok=True)
        monkeypatch.chdir(job)
        argv = ['--build', str(build), '--test', 'Some.Test']
        if target:
            argv += ['--target', target]
        return projectcheck.main(argv)

    def test_an_untouched_tree_that_rebuilds_nothing_is_allowed_through(self, tmp_path, monkeypatch, capsys):
        """Removing nothing rightly rebuilds nothing; refusing that would refuse
        every candidate that only edited a file the test does not reach."""
        assert self.run(tmp_path, monkeypatch, killed=0, rebuild=False) == 0
        assert 'ctest ran' in capsys.readouterr().out

    def test_removals_that_did_rebuild_are_allowed_through(self, tmp_path, monkeypatch, capsys):
        assert self.run(tmp_path, monkeypatch, killed=7, rebuild=True) == 0
        assert 'ctest ran' in capsys.readouterr().out

    def test_removals_that_rebuilt_nothing_are_refused(self, tmp_path, monkeypatch, capsys):
        """The failure this exists for: the binary is the previous candidate's."""
        assert self.run(tmp_path, monkeypatch, killed=7, rebuild=False) == projectcheck.UNDECIDABLE
        out = capsys.readouterr().out
        assert 'judged by another one' in out
        assert 'ctest ran' not in out

    def test_a_binary_that_was_never_there_is_refused_not_assumed(self, tmp_path, monkeypatch, capsys):
        """Two absences compare equal, and calling that "unchanged" or calling it
        "fine" are both answers about a comparison that did not happen."""
        code = self.run(tmp_path, monkeypatch, killed=7, rebuild=False, binary=False)
        assert code == projectcheck.UNDECIDABLE
        assert 'cannot be shown' in capsys.readouterr().out

    def test_without_a_target_the_check_refuses_rather_than_guesses(self, tmp_path, monkeypatch):
        """No target means no binary to look for, so nothing can be verified."""
        code = self.run(tmp_path, monkeypatch, killed=7, rebuild=True, target=None, binary=False)
        assert code == projectcheck.UNDECIDABLE

    def test_a_failing_build_is_reported_by_the_usual_reporter(self, tmp_path, monkeypatch, capsys):
        """Not `cat`: the generated shell could only print the raw log, which is
        the failure build_failure_report was written to remove."""
        build = tmp_path / 'build'
        build.mkdir()
        bin_dir = tmp_path / 'bin'
        self.stub(bin_dir, 'cmake',
                  'echo "FAILED: obj/foo.o "\necho "  /usr/bin/clang++ -c -o obj/foo.o foo.cpp"\n'
                  'echo "foo.cpp:1:10: fatal error: ids.hpp file not found"\nexit 1\n')
        self.stub(bin_dir, 'ctest', 'echo "ctest ran"\nexit 0\n')
        monkeypatch.setenv('PATH', str(bin_dir) + os.pathsep + os.environ['PATH'])
        job = tmp_path / 'job'
        job.mkdir()
        monkeypatch.chdir(job)
        code = projectcheck.main(['--build', str(build), '--test', 'Some.Test'])
        out = capsys.readouterr().out
        assert code == 1
        assert 'ids.hpp file not found' in out
        assert 'ctest ran' not in out
        assert (build / 'cvise-last-build-failure.log').exists()

    def test_the_record_names_what_the_verdict_rested_on(self, tmp_path, monkeypatch):
        witness = tmp_path / 'verdicts.log'
        build = tmp_path / 'build'
        build.mkdir()
        (build / 'unit_tests').write_text('old')
        bin_dir = tmp_path / 'bin'
        self.stub(bin_dir, 'cmake', 'exit 0\n')
        self.stub(bin_dir, 'ctest', 'exit 0\n')
        monkeypatch.setenv('PATH', str(bin_dir) + os.pathsep + os.environ['PATH'])
        monkeypatch.setattr(projectcheck.invalidate, 'remove_stale', lambda *a: 0)
        job = tmp_path / 'job'
        job.mkdir()
        monkeypatch.chdir(job)
        projectcheck.main(['--build', str(build), '--test', 'S.T',
                           '--target', 'unit_tests', '--witness', str(witness)])
        line = witness.read_text()
        assert 'build=0' in line and 'killed=0' in line and 'rebuilt=no' in line

    def test_a_witness_that_cannot_be_written_does_not_change_a_verdict(self, tmp_path, monkeypatch, capsys):
        build = tmp_path / 'build'
        build.mkdir()
        (build / 'unit_tests').write_text('old')
        bin_dir = tmp_path / 'bin'
        self.stub(bin_dir, 'cmake', 'exit 0\n')
        self.stub(bin_dir, 'ctest', 'echo "ctest ran"\nexit 0\n')
        monkeypatch.setenv('PATH', str(bin_dir) + os.pathsep + os.environ['PATH'])
        monkeypatch.setattr(projectcheck.invalidate, 'remove_stale', lambda *a: 0)
        job = tmp_path / 'job'
        job.mkdir()
        monkeypatch.chdir(job)
        code = projectcheck.main(['--build', str(build), '--test', 'S.T',
                                  '--witness', str(tmp_path / 'no' / 'such' / 'dir' / 'log')])
        assert code == 0
        assert 'ctest ran' in capsys.readouterr().out


class TestTheGeneratedFileCarriesNoLogic:
    """The point of the rewrite: what is generated is an argv, not a program.

    A branch written into a generated script is first executed in a live run,
    where a wrong verdict is indistinguishable from a good reduction.
    """

    def script(self, tmp_path, target='unit_tests'):
        class Project:
            build_dir = tmp_path / 'build'

        Project.build_dir.mkdir(parents=True, exist_ok=True)
        return project.check_script(
            Project, 'Some.Test', tmp_path / 'check.sh', target=target
        ).read_text()

    def test_it_is_a_shebang_a_comment_and_one_exec(self, tmp_path):
        lines = [line for line in self.script(tmp_path).splitlines() if line.strip()]
        assert len(lines) == 3
        assert lines[0] == '#!/bin/sh'
        assert lines[2].startswith('PYTHONPATH=') and ' exec ' in lines[2]

    def test_it_decides_nothing(self, tmp_path):
        body = self.script(tmp_path).splitlines()[-1]
        words = set(re.findall(r'[a-z]+', body)) | set(re.findall(r'[&|$][&|(]', body))
        for construct in ('if', 'then', 'else', 'fi', 'while', 'case', '&&', '||', '$('):
            assert construct not in words, f'the generated script branches on {construct!r}'

    def test_the_target_reaches_the_check(self, tmp_path):
        assert '--target unit_tests' in self.script(tmp_path)

    def test_no_target_means_no_target_argument(self, tmp_path):
        assert '--target' not in self.script(tmp_path, target=None)


class TestHowMuchTheBuildActuallyDid:
    """"The binary did not change" has two causes repaired in opposite ways.

    A build that compiled nothing and a build that compiled a great deal and
    produced the same bytes look identical in the verdict. A live run gave 69
    such verdicts and the number that tells them apart had never been written
    down, so the run could not be diagnosed from what it left behind.
    """

    def test_the_highest_edge_ninja_reached_is_the_count(self):
        output = '[1/376] Building CXX object a.o\n[2/376] Building CXX object b.o\n[376/376] Linking\n'
        assert projectcheck.edges_run(output) == 376

    def test_a_build_that_did_nothing_counts_zero(self):
        assert projectcheck.edges_run('ninja: no work to do.\n') == 0

    def test_output_that_mentions_brackets_elsewhere_is_not_counted(self):
        """Compiler diagnostics quote code, and code contains brackets."""
        assert projectcheck.edges_run('note: in expansion of [1/2] here\n') == 0

    def test_an_empty_build_output_counts_zero(self):
        assert projectcheck.edges_run('') == 0

    def test_the_record_carries_the_count_and_the_time(self, tmp_path, monkeypatch):
        witness = tmp_path / 'verdicts.log'
        build = tmp_path / 'build'
        build.mkdir()
        bin_dir = tmp_path / 'bin'
        bin_dir.mkdir()
        cmake = bin_dir / 'cmake'
        cmake.write_text('#!/bin/sh\necho "[1/7] Building CXX object x.o"\necho "[7/7] Linking"\nexit 0\n')
        cmake.chmod(0o755)
        ctest = bin_dir / 'ctest'
        ctest.write_text('#!/bin/sh\nexit 0\n')
        ctest.chmod(0o755)
        monkeypatch.setenv('PATH', str(bin_dir) + os.pathsep + os.environ['PATH'])
        monkeypatch.setattr(projectcheck.invalidate, 'remove_stale', lambda *a: 0)
        job = tmp_path / 'job'
        job.mkdir()
        monkeypatch.chdir(job)
        projectcheck.main(['--build', str(build), '--test', 'S.T', '--witness', str(witness)])
        line = witness.read_text()
        assert 'edges=7' in line
        assert 'secs=' in line
