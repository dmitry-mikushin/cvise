"""Keeping publications in git, so that a run cannot leave nothing behind.

MEASURED, and it is why this exists: a reduction of llama.cpp published 19
files at its fifth minute, ran for eleven more hours, died, and left nothing
recoverable — no branch, no bundle, no stash, and a tree on disk byte-identical
to the one it started from. The only trace of a 7.2% reduction was a single
line in a statistics file.

The division of labour is the design, and these tests hold it: C-Vise keeps
what it publishes and says it is UNVERIFIED, because a reducer that certified
its own output would preserve its own systematic errors along with it.
"""

import subprocess

import pytest

from cvise.utils import keep
from cvise.utils.keep import Keeper


def a_checkout(path, text='int main() { return 0; }\n'):
    path.mkdir(parents=True, exist_ok=True)
    (path / 'a.cpp').write_text(text)
    for command in (['init', '-q'], ['add', '-A'],
                    ['-c', 'user.name=T', '-c', 'user.email=t@t', 'commit', '-qm', 'start']):
        subprocess.run(['git', '-C', str(path), *command], capture_output=True, check=True)
    return path


class TestWhatItKeeps:
    def test_a_publication_becomes_a_commit(self, tmp_path):
        tree = a_checkout(tmp_path / 'tree')
        k = Keeper(tree)
        assert k.ready
        (tree / 'a.cpp').write_text('int main() {}\n')
        k.keep()
        assert k.commits == 1

    def test_the_commit_says_it_is_not_verified(self, tmp_path):
        """The whole reason C-Vise may keep its own output at all."""
        tree = a_checkout(tmp_path / 'tree')
        k = Keeper(tree)
        (tree / 'a.cpp').write_text('int main() {}\n')
        k.keep()
        message = keep.git(tree, 'log', '-1', '--format=%s').stdout
        assert 'NOT verified' in message
        assert 'lines of code' in message

    def test_the_author_is_the_program(self, tmp_path):
        tree = a_checkout(tmp_path / 'tree')
        k = Keeper(tree)
        (tree / 'a.cpp').write_text('int main() {}\n')
        k.keep()
        assert keep.git(tree, 'log', '-1', '--format=%an').stdout.strip() == 'C-Vise'

    def test_it_lives_on_a_branch_of_its_own(self, tmp_path):
        tree = a_checkout(tmp_path / 'tree')
        k = Keeper(tree)
        assert k.branch.endswith('-reduced')
        assert keep.git(tree, 'rev-parse', '--abbrev-ref', 'HEAD').stdout.strip() == k.branch

    def test_a_publication_that_changed_nothing_is_not_a_commit(self, tmp_path):
        """An empty commit would claim a reduction that did not happen."""
        tree = a_checkout(tmp_path / 'tree')
        k = Keeper(tree)
        k.keep()
        k.keep()
        assert k.commits == 0

    def test_every_publication_is_kept_not_just_the_last(self, tmp_path):
        tree = a_checkout(tmp_path / 'tree')
        k = Keeper(tree)
        for i in range(4):
            (tree / 'a.cpp').write_text('int main() {}\n' + '\n' * i + f'// {i}\n')
            k.keep()
        assert k.commits == 4
        history = keep.git(tree, 'rev-list', '--count', 'HEAD').stdout.strip()
        assert int(history) == 5  # the start plus four


class TestTheBundle:
    def test_it_carries_a_head_that_can_be_cloned(self, tmp_path, monkeypatch):
        """A bundle of a branch alone clones into an EMPTY working tree, with
        `remote HEAD refers to nonexistent ref`. The content is all there and
        reaching it takes a second, non-obvious step."""
        monkeypatch.setattr(keep, 'RESULTS', tmp_path / 'results')
        tree = a_checkout(tmp_path / 'tree')
        k = Keeper(tree)
        (tree / 'a.cpp').write_text('int main() {}\n')
        k.keep()
        bundle = k.bundle()
        assert bundle and bundle.is_file()

        back = tmp_path / 'back'
        cloned = subprocess.run(['git', 'clone', '-q', str(bundle), str(back)],
                                capture_output=True, text=True)
        assert cloned.returncode == 0, cloned.stderr
        assert (back / 'a.cpp').is_file(), 'the clone has an empty working tree'

    def test_it_is_not_written_on_every_publication(self, tmp_path, monkeypatch):
        """0.77 s and ~19 MB each time, against publications minutes apart."""
        monkeypatch.setattr(keep, 'RESULTS', tmp_path / 'results')
        tree = a_checkout(tmp_path / 'tree')
        k = Keeper(tree)
        made = []
        monkeypatch.setattr(k, 'bundle', lambda: made.append(1))
        for i in range(5):
            (tree / 'a.cpp').write_text(f'int main() {{}} // {i}\n')
            k.keep()
        assert len(made) <= 1, 'a bundle per publication'


class TestWhenItCannot:
    def test_a_plain_directory_is_not_an_error(self, tmp_path, caplog):
        """Reducing a directory that is not a checkout is legitimate; this is a
        convenience, not a dependency. But it says so, because a preservation
        that is quietly not happening is the failure it exists to prevent."""
        import logging
        caplog.set_level(logging.INFO)
        plain = tmp_path / 'plain'
        plain.mkdir()
        k = Keeper(plain)
        assert not k.ready
        k.keep()          # must not raise
        assert k.bundle() is None
        assert 'not a git checkout' in caplog.text

    def test_no_tree_at_all_is_not_an_error(self):
        k = Keeper(None)
        assert not k.ready
        k.keep()
        assert k.bundle() is None

    def test_a_commit_that_fails_stops_trying_rather_than_warning_every_time(
            self, tmp_path, monkeypatch):
        tree = a_checkout(tmp_path / 'tree')
        k = Keeper(tree)
        real = keep.git

        def broken(t, *args):
            if args and args[0] == '-c':
                return subprocess.CompletedProcess([], 1, '', 'no')
            return real(t, *args)

        monkeypatch.setattr(keep, 'git', broken)
        (tree / 'a.cpp').write_text('int main() {}\n')
        k.keep()
        assert not k.ready
        assert k.commits == 0


class TestHowMuchCode:
    def test_it_counts_the_tree_and_not_the_reducible_subset(self, tmp_path):
        """A message saying "169085 lines" should mean the tree, or it will be
        compared with something it does not describe."""
        tree = tmp_path / 'tree'
        (tree / 'sub').mkdir(parents=True)
        (tree / 'a.cpp').write_text('one\ntwo\n\n')
        (tree / 'sub' / 'b.hpp').write_text('three\n')
        (tree / 'notes.txt').write_text('not source\n')
        assert keep.how_much_code(tree) == '3 lines of code in 2 files'

    def test_a_file_of_only_blanks_is_not_a_file_that_is_left(self, tmp_path):
        tree = tmp_path / 'tree'
        tree.mkdir()
        (tree / 'a.cpp').write_text('\n\n  \n')
        assert keep.how_much_code(tree) == '0 lines of code in 0 files'
