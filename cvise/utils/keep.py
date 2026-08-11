"""Keeping what a reduction has produced, in git, as it goes.

A reduction publishes its result by overwriting the tree in place. That is the
right thing -- the answer is on disk continuously and an interrupted run loses
nothing -- but it keeps exactly one version, and that version lives in tmpfs.
MEASURED, and it is why this module exists: a run of llama.cpp published 19
files at its fifth minute, went on for eleven hours, died, and left nothing.
The tree on disk today is byte-identical to the one it started from. Nothing
was recoverable: no branch, no bundle, no stash, and the only trace of a 7.2%
reduction was one line in a statistics file.

Two operations, and the split between them is the point.

COMMITTING is cheap and involves no judgement, so C-Vise does it itself on
every publication. MEASURED on a 1400-file tree: `git add -A` 0.01 s, `git
commit` 0.01 s, against a seven-minute interval between publications. It moves
no file in the working tree, so it is safe while jobs are reading it.

VERIFYING is neither, and it stays outside. verify-reduction.py builds the tree
from nothing, outside the overlay and outside the jobserver, and runs the
criterion -- which is what makes it able to catch a defect in the reducer.
A reducer that certified its own output would preserve its own systematic
errors along with it. So what C-Vise writes here says plainly that it is
unverified, and only the independent check may say otherwise.
"""

from __future__ import annotations

import logging
import subprocess
import time
from pathlib import Path

#: Where results are put so that they survive the machine. The state directory
#: is tmpfs and the run that made it can die at any moment.
RESULTS = Path.home() / 'cvise-results'

#: The commits are made by a program, so they say so rather than borrowing
#: whoever happened to be logged in.
AUTHOR = ('-c', 'user.name=C-Vise', '-c', 'user.email=cvise@localhost')

SOURCE_SUFFIXES = ('.cpp', '.cc', '.cxx', '.c', '.hpp', '.hh', '.hxx', '.h', '.inc')

#: How often the branch is packed into a bundle outside the tmpfs. Every
#: publication would be right if it were free; MEASURED it is 0.77 s and about
#: 19 MB each time, so it is done on a clock instead.
BUNDLE_INTERVAL = 900


def git(tree: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(['git', '-C', str(tree), *args], capture_output=True, text=True)


def is_a_checkout(tree: Path) -> bool:
    return git(tree, 'rev-parse', '--git-dir').returncode == 0


def how_much_code(tree: Path) -> str:
    """Lines of source and how many files hold them, for the commit message.

    Counted here rather than taken from the reducer's own figures, which count
    only what it was given to reduce. A message that says "169085 lines" should
    mean the tree, or it will be compared with something it does not describe.
    """
    lines = files = 0
    for path in tree.rglob('*'):
        if not path.is_file() or path.suffix not in SOURCE_SUFFIXES:
            continue
        try:
            counted = sum(1 for line in path.read_text(errors='replace').splitlines()
                          if line.strip())
        except OSError:
            continue
        if counted:
            files += 1
            lines += counted
    return f'{lines} lines of code in {files} files'


def on_the_branch(tree: Path, branch: str) -> bool:
    """Put this history on a branch of its own, creating it once.

    A worktree is checked out detached at the pin, so the first commit has
    nowhere to go. Creating the branch at HEAD moves no file, which is why this
    is safe to do while the reduction is running.
    """
    if git(tree, 'rev-parse', '--verify', branch).returncode == 0:
        return git(tree, 'checkout', branch).returncode == 0
    return git(tree, 'checkout', '-b', branch).returncode == 0


class Keeper:
    """Commits every publication, and bundles the branch out of tmpfs on a clock.

    Silent about a tree that is not a checkout, once. A reduction of a plain
    directory is a legitimate thing to do and this is a convenience, not a
    dependency -- but it says so, because a preservation that is quietly not
    happening is exactly the failure it exists to prevent.
    """

    def __init__(self, tree: Path | None, branch: str | None = None):
        self.tree = Path(tree) if tree else None
        self.branch = branch or (f'{self.tree.name}-reduced' if self.tree else None)
        self.commits = 0
        self.bundled_at = 0.0
        self.ready = False
        if self.tree is None:
            return
        if not is_a_checkout(self.tree):
            logging.info('%s is not a git checkout, so publications will not be kept in '
                         'git; the tree on disk is the only copy', self.tree)
            return
        self.ready = on_the_branch(self.tree, self.branch)
        if not self.ready:
            logging.warning('could not put the reduction on branch %s, so publications '
                            'will not be kept', self.branch)

    def keep(self, note: str = '') -> None:
        """Record what has just been published. Never raises, never blocks."""
        if not self.ready:
            return
        try:
            git(self.tree, 'add', '-A')
            if git(self.tree, 'diff', '--cached', '--quiet').returncode == 0:
                return  # nothing changed; an empty commit would claim a reduction
            message = (f'reduced by C-Vise: {how_much_code(self.tree)} (NOT verified)'
                       + (f'\n\n{note}' if note else ''))
            done = git(self.tree, *AUTHOR, 'commit', '-m', message)
            if done.returncode != 0:
                logging.warning('a publication could not be kept: %s',
                                done.stderr.strip()[:200])
                self.ready = False
                return
            self.commits += 1
            self._maybe_bundle()
        except OSError as e:
            logging.warning('keeping publications has stopped: %s', e)
            self.ready = False

    def _maybe_bundle(self) -> None:
        now = time.monotonic()
        if now - self.bundled_at < BUNDLE_INTERVAL:
            return
        self.bundled_at = now
        self.bundle()

    def bundle(self) -> Path | None:
        """Pack the branch into a file outside the tmpfs it lives in.

        HEAD as well as the branch, and it is not redundant: a bundle holding
        only a branch has no HEAD to check out, so `git clone` of it produces an
        empty working tree and the warning "remote HEAD refers to nonexistent
        ref". MEASURED -- the content is all there, but reaching it takes a
        second, non-obvious step.
        """
        if not self.ready:
            return None
        try:
            RESULTS.mkdir(parents=True, exist_ok=True)
            path = RESULTS / f'{self.tree.name}-{self.branch}.bundle'
            made = git(self.tree, 'bundle', 'create', str(path), 'HEAD', self.branch)
            if made.returncode != 0:
                logging.warning('could not bundle %s: %s', self.branch,
                                made.stderr.strip()[:200])
                return None
            logging.info('%d publications kept on %s, packed into %s',
                         self.commits, self.branch, path)
            return path
        except OSError as e:
            logging.warning('could not bundle: %s', e)
            return None
