"""Preserving a verified result, and the ways that can look like it worked.

MEASURED twice during one reduction: a run died at 56% with an exception while
writing a crash report, and a later one was stopped by hand after converging.
In both cases the result survived only because a copy had already been committed
and bundled. Nothing else about either run did -- the state directory is tmpfs.

The tests here are about the failures that leave a reassuring message behind: an
empty commit that says a reduction happened when none did, and a bundle file
that exists and cannot be read back.
"""

import importlib.util
import subprocess
from pathlib import Path

import pytest

VERIFY = Path(__file__).resolve().parents[2] / 'verify-reduction.py'


@pytest.fixture
def tool(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location('verify_reduction', VERIFY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, 'RESULTS', tmp_path / 'results')
    return module


def a_checkout(path: Path) -> Path:
    """A git checkout with one source file, detached, as the worktree is."""
    path.mkdir(parents=True)
    subprocess.run(['git', 'init', '-q', str(path)], check=True)
    (path / 'a.cpp').write_text('int a(){return 1;}\nint b(){return 2;}\n')
    subprocess.run(['git', '-C', str(path), 'add', '-A'], check=True)
    subprocess.run(
        ['git', '-C', str(path), '-c', 'user.name=T', '-c', 'user.email=t@t',
         'commit', '-q', '-m', 'pristine'],
        check=True,
    )
    return path


def head(tree: Path) -> str:
    return subprocess.run(['git', '-C', str(tree), 'rev-parse', 'HEAD'],
                          capture_output=True, text=True).stdout.strip()


def test_a_verified_tree_is_committed_and_bundled(tool, tmp_path, capsys):
    tree = a_checkout(tmp_path / 'state' / 'ns-projection')
    state = tree.parent
    (tree / 'a.cpp').write_text('int a(){return 1;}\n')  # the reduction happened

    before = head(tree)
    tool.preserve(tree, state, 'SomeTest.SomeCase')
    assert head(tree) != before, 'the reduced tree was not committed'

    bundle = tool.RESULTS / f'{tree.name}-{state.name}-verified.bundle'
    assert bundle.is_file(), capsys.readouterr().out


def test_the_bundle_can_be_read_back(tool, tmp_path):
    """The one property a backup has to have.

    Asserted by cloning it, not by the file existing: a bundle that cannot be
    read is a backup that only looks like one, and the day it is needed is the
    wrong day to discover that.
    """
    tree = a_checkout(tmp_path / 'state' / 'ns-projection')
    state = tree.parent
    (tree / 'a.cpp').write_text('int only_this_survived(){return 7;}\n')
    tool.preserve(tree, state, 'SomeTest.SomeCase')

    bundle = tool.RESULTS / f'{tree.name}-{state.name}-verified.bundle'
    restored = tmp_path / 'restored'
    cloned = subprocess.run(['git', 'clone', '-q', str(bundle), str(restored)],
                            capture_output=True, text=True)
    assert cloned.returncode == 0, cloned.stderr
    assert (restored / 'a.cpp').read_text() == 'int only_this_survived(){return 7;}\n'


def test_an_unchanged_tree_does_not_get_an_empty_commit(tool, tmp_path, capsys):
    """A commit per verification would say a reduction happened when none did."""
    tree = a_checkout(tmp_path / 'state' / 'ns-projection')
    state = tree.parent
    (tree / 'a.cpp').write_text('int a(){return 1;}\n')
    tool.preserve(tree, state, 'SomeTest.SomeCase')
    after_first = head(tree)

    tool.preserve(tree, state, 'SomeTest.SomeCase')

    assert head(tree) == after_first, 'a second verification made an empty commit'
    assert 'already preserved' in capsys.readouterr().out


def test_the_history_accumulates_rather_than_replacing(tool, tmp_path):
    """Each verified point is a commit, so the result has a history to read."""
    tree = a_checkout(tmp_path / 'state' / 'ns-projection')
    state = tree.parent
    for text in ('int a(){return 1;}\n', 'int a();\n'):
        (tree / 'a.cpp').write_text(text)
        tool.preserve(tree, state, 'SomeTest.SomeCase')

    log = subprocess.run(['git', '-C', str(tree), 'log', '--oneline'],
                         capture_output=True, text=True).stdout.splitlines()
    assert len(log) == 3, log  # pristine plus two verified points


def test_the_message_counts_code_and_not_bytes(tool, tmp_path):
    """Bytes move the same for a stripped space and a deleted unit."""
    tree = a_checkout(tmp_path / 'state' / 'ns-projection')
    state = tree.parent
    (tree / 'a.cpp').write_text('int a(){return 1;}\n\n\n   \n')
    tool.preserve(tree, state, 'SomeTest.SomeCase')

    message = subprocess.run(['git', '-C', str(tree), 'log', '-1', '--format=%s'],
                             capture_output=True, text=True).stdout
    assert '1 lines of code in 1 files' in message, message
    assert 'bytes' not in message


def test_a_tree_that_is_not_a_checkout_is_reported_not_ignored(tool, tmp_path, capsys):
    plain = tmp_path / 'plain'
    plain.mkdir()
    (plain / 'a.cpp').write_text('int a(){return 1;}\n')

    tool.preserve(plain, tmp_path, 'SomeTest.SomeCase')

    assert 'not a git checkout' in capsys.readouterr().err
