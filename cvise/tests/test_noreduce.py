"""What a reduction may not touch, and the ways it might try.

The mechanism exists because of one specific way a reduction lies: it empties
the body of the test it is being graded by, the emptied test still passes, and
from then on every candidate is measured against nothing. That failure looks
exactly like an excellent reduction, so these are less about the happy path
than about each way the protection could be got around, and about what happens
when it cannot tell.
"""

from pathlib import Path
import shutil

import pytest

from cvise.utils import noreduce
from cvise.utils.testing import protected_rejection


pytestmark = pytest.mark.skipif(
    shutil.which('treesitter_delta', path='.') is None and noreduce._lister() is None,
    reason='requires treesitter_delta',
)


GUARDED = """\
#include <gtest/gtest.h>
#include "noreduce.h"

int helper() { return 41; }

namespace {
class IngestTest_Parses_Test : public ::testing::Test {
public:
    void TestBody() override;
};
}

CVISE_NOREDUCE
void IngestTest_Parses_Test::TestBody() {
    EXPECT_EQ(helper() + 1, 42);
    EXPECT_TRUE(true);
}

void unprotected() {
    int x = 1;
}
"""


def write(tmp_path, name, text):
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


class TestFindingTheMarkedDefinition:
    def test_an_unmarked_file_has_nothing_to_protect(self, tmp_path):
        path = write(tmp_path, 'plain.cpp', 'int main() { return 0; }\n')
        assert not noreduce.has_protection(path.read_text())
        assert noreduce.protected_regions(path) == []

    def test_the_marker_is_part_of_what_is_protected(self):
        """Otherwise removing it would be a legal way to unlock the definition."""
        assert noreduce.MARKER in GUARDED

    def test_the_region_is_the_whole_definition(self, tmp_path):
        regions = noreduce.protected_regions(write(tmp_path, 'g.cpp', GUARDED))
        assert len(regions) == 1
        assert regions[0].startswith(noreduce.MARKER)
        assert 'EXPECT_EQ(helper() + 1, 42);' in regions[0]
        assert regions[0].rstrip().endswith('}')

    def test_only_the_marked_definition(self, tmp_path):
        regions = noreduce.protected_regions(write(tmp_path, 'g.cpp', GUARDED))
        assert 'unprotected' not in regions[0]
        assert 'int helper()' not in regions[0]

    def test_defining_the_macro_is_not_using_it(self):
        """The header that defines CVISE_NOREDUCE must stay reducible."""
        header = '#pragma once\n#define CVISE_NOREDUCE [[clang::annotate("x")]]\n'
        assert not noreduce.has_protection(header)


class TestWhatCountsAsDisturbing:
    def check(self, tmp_path, produced):
        before = write(tmp_path / 'a', 'g.cpp', GUARDED)
        after = write(tmp_path / 'b', 'g.cpp', produced)
        return noreduce.disturbed(before, after)

    def test_leaving_it_alone_is_allowed(self, tmp_path):
        assert not self.check(tmp_path, GUARDED)

    def test_reducing_around_it_is_allowed(self, tmp_path):
        """The point is to protect the criterion, not to freeze the file."""
        elsewhere = GUARDED.replace('void unprotected() {\n    int x = 1;\n}\n', '')
        assert elsewhere != GUARDED
        assert not self.check(tmp_path, elsewhere)

    def test_moving_it_is_allowed(self, tmp_path):
        """Deleting a definition above it shifts it; that is not a change to it."""
        moved = GUARDED.replace('int helper() { return 41; }\n', '')
        assert not self.check(tmp_path, moved)

    def test_hollowing_the_test_out_is_refused(self, tmp_path):
        """The failure this exists for: an emptied test still passes."""
        hollow = GUARDED.replace('    EXPECT_EQ(helper() + 1, 42);\n', '')
        assert self.check(tmp_path, hollow)

    def test_weakening_an_assertion_is_refused(self, tmp_path):
        weaker = GUARDED.replace('EXPECT_EQ(helper() + 1, 42)', 'EXPECT_EQ(1, 1)')
        assert self.check(tmp_path, weaker)

    def test_deleting_the_definition_is_refused(self, tmp_path):
        gone = GUARDED[: GUARDED.index('CVISE_NOREDUCE')] + GUARDED[GUARDED.index('void unprotected'):]
        assert self.check(tmp_path, gone)

    def test_removing_only_the_marker_is_refused(self, tmp_path):
        unlocked = GUARDED.replace('CVISE_NOREDUCE\n', '')
        assert self.check(tmp_path, unlocked)

    def test_emptying_the_whole_file_is_refused(self, tmp_path):
        assert self.check(tmp_path, '')


class TestWhenItCannotTell:
    """Not knowing is refused, because a silent failure here looks like success."""

    def test_a_marker_outside_any_definition_is_refused(self, tmp_path):
        stray = 'int x = 1;\nCVISE_NOREDUCE\nint y = 2;\n'
        assert noreduce.protected_regions(write(tmp_path, 's.cpp', stray)) is None

    def test_a_file_that_no_longer_parses_that_far_is_refused(self, tmp_path):
        before = write(tmp_path / 'a', 'g.cpp', GUARDED)
        after = write(tmp_path / 'b', 'g.cpp', 'CVISE_NOREDUCE\n{{{ not c++ at all\n')
        assert noreduce.disturbed(before, after)


class TestFindingTheMarkedFilesOfATree:
    """Which files carry a marker is asked of the original, never of the candidate.

    A candidate that deleted the marker has nothing left to find, so scanning it
    would answer "nothing is protected here" for precisely the edit that has to
    be refused.
    """

    def tree(self, tmp_path):
        root = tmp_path / 'src'
        (root / 'sub').mkdir(parents=True)
        (root / 'guarded.cpp').write_text(GUARDED)
        (root / 'plain.cpp').write_text('int f() { return 1; }\n')
        (root / 'sub' / 'other.cpp').write_text('int g() { return 2; }\n')
        return root

    def candidate_of(self, tmp_path, root):
        candidate = tmp_path / 'job' / 'src'
        candidate.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(root, candidate)
        return candidate

    def test_only_the_marked_file_is_named(self, tmp_path):
        root = self.tree(tmp_path)
        assert [Path(p).name for p in noreduce.marked_files(str(root))] == ['guarded.cpp']

    def test_a_tree_with_nothing_marked_is_empty(self, tmp_path):
        root = tmp_path / 'clean'
        root.mkdir()
        (root / 'a.cpp').write_text('int a() { return 0; }\n')
        assert noreduce.marked_files(str(root)) == ()

    def test_a_single_file_test_case_works_too(self, tmp_path):
        path = write(tmp_path, 'one.cpp', GUARDED)
        assert noreduce.marked_files(str(path)) == (str(path),)

    def test_leaving_the_tree_alone_is_no_violation(self, tmp_path):
        root = self.tree(tmp_path)
        assert noreduce.violation(root, self.candidate_of(tmp_path, root)) is None

    def test_reducing_an_unmarked_file_is_no_violation(self, tmp_path):
        root = self.tree(tmp_path)
        candidate = self.candidate_of(tmp_path, root)
        (candidate / 'plain.cpp').write_text('')
        (candidate / 'sub' / 'other.cpp').unlink()
        assert noreduce.violation(root, candidate) is None

    def test_hollowing_the_marked_file_is_a_violation(self, tmp_path):
        root = self.tree(tmp_path)
        candidate = self.candidate_of(tmp_path, root)
        (candidate / 'guarded.cpp').write_text(
            GUARDED.replace('    EXPECT_EQ(helper() + 1, 42);\n', ''))
        offender = noreduce.violation(root, candidate)
        assert offender is not None and offender.name == 'guarded.cpp'

    def test_deleting_the_marked_file_is_a_violation(self, tmp_path):
        """What a per-changed-file check cannot see: nothing was written."""
        root = self.tree(tmp_path)
        candidate = self.candidate_of(tmp_path, root)
        (candidate / 'guarded.cpp').unlink()
        offender = noreduce.violation(root, candidate)
        assert offender is not None and offender.name == 'guarded.cpp'


class TestTheCandidateIsRefusedBeforeAnythingIsSpent:
    """The verdict has to arrive before the build, not from it."""

    def pair(self, tmp_path, produced_text):
        original = write(tmp_path / 'project', 'ingest_test.cpp', GUARDED)
        candidate = write(tmp_path / 'job', 'ingest_test.cpp', produced_text)
        return [(original, candidate)]

    def test_an_untouched_definition_goes_on(self, tmp_path):
        pairs = self.pair(tmp_path, GUARDED.replace('int helper', 'int helper2'))
        assert protected_rejection(pairs) is None

    def test_a_disturbed_definition_is_rejected_and_says_which_file(self, tmp_path):
        hollow = GUARDED.replace('    EXPECT_EQ(helper() + 1, 42);\n', '')
        verdict = protected_rejection(self.pair(tmp_path, hollow))
        assert verdict is not None
        returncode, _, stderr = verdict
        assert returncode == 1
        assert b'marked not to be reduced' in stderr
        assert b'ingest_test.cpp' in stderr

    def test_a_file_without_markers_is_not_slowed_down(self, tmp_path):
        original = write(tmp_path / 'a', 'plain.cpp', 'int main() { return 0; }\n')
        candidate = write(tmp_path / 'b', 'plain.cpp', 'int main() {}\n')
        assert protected_rejection([(original, candidate)]) is None

    def test_a_binary_file_is_not_an_error(self, tmp_path):
        original = tmp_path / 'a' / 'blob.bin'
        original.parent.mkdir(parents=True)
        original.write_bytes(b'\xff\xfe\x00\x01')
        candidate = tmp_path / 'b' / 'blob.bin'
        candidate.parent.mkdir(parents=True)
        candidate.write_bytes(b'\x00')
        assert protected_rejection([(original, candidate)]) is None
