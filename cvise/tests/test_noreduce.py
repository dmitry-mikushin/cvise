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


class TestAMarkerThatDoesNothingSaysSo:
    """The guard's own silent-no-op, which is the failure it exists to prevent.

    A marker only counts when it begins its line. Written in prose, or indented
    by an editor, it protects nothing -- and a file with an inert marker is
    indistinguishable from one that never asked for a guard.
    """

    def test_an_indented_marker_still_protects(self, tmp_path, caplog):
        """Indentation is not prose: a marker is recognised by what its line
        begins with once stripped, so an editor reindenting it changes nothing."""
        indented = GUARDED.replace('CVISE_NOREDUCE\n', '        CVISE_NOREDUCE\n')
        path = write(tmp_path, 'g.cpp', indented)
        noreduce.marked_files.cache_clear()
        with caplog.at_level('WARNING'):
            assert noreduce.marked_files(str(path.parent)) == (str(path),)
        assert not caplog.records, 'warned about a marker that does work'

    def test_prose_naming_the_marker_warns_too(self, tmp_path, caplog):
        prose = '// see CVISE_NOREDUCE for why\nint f() { return 1; }\n'
        path = write(tmp_path, 'p.cpp', prose)
        noreduce.marked_files.cache_clear()
        with caplog.at_level('WARNING'):
            assert noreduce.marked_files(str(path.parent)) == ()
        assert caplog.records, 'an inert marker went unmentioned'

    def test_the_header_that_defines_it_is_silent(self, tmp_path, caplog):
        """The one file whose purpose is to name the marker must not be warned
        about. MEASURED on a real run: 73 of 103 log lines were this warning
        about that header, drowning everything the log was for."""
        header = ('#pragma once\n'
                  '// Marks a definition a reduction must not change.\n'
                  '#if defined(__clang__)\n'
                  '#define CVISE_NOREDUCE [[clang::annotate("cvise::noreduce")]]\n'
                  '#else\n'
                  '#define CVISE_NOREDUCE\n'
                  '#endif\n')
        path = write(tmp_path, 'noreduce.h', header)
        noreduce.marked_files.cache_clear()
        with caplog.at_level('WARNING'):
            assert noreduce.marked_files(str(path.parent)) == ()
        assert not caplog.records, 'warned about the header that provides the marker'

    def test_a_file_with_no_marker_at_all_is_silent(self, tmp_path, caplog):
        path = write(tmp_path, 'q.cpp', 'int f() { return 1; }\n')
        noreduce.marked_files.cache_clear()
        with caplog.at_level('WARNING'):
            assert noreduce.marked_files(str(path.parent)) == ()
        assert not caplog.records, 'warned about a file that never mentioned the marker'


class TestTheGuardCannotBeSwitchedOffQuietly:
    """Ways the comparison could become vacuous without anyone noticing."""

    def test_comparing_a_tree_with_itself_is_refused(self, tmp_path):
        """Under python -O the assertion that keeps test cases relative is gone,
        and `folder / absolute` is the absolute path, so a job would compare the
        original against itself and agree with everything."""
        root = tmp_path / 'src'
        root.mkdir()
        (root / 'g.cpp').write_text(GUARDED)
        with pytest.raises(ValueError, match='against itself'):
            noreduce.violation(root, root)

    def test_an_absolute_test_case_is_refused_where_it_is_copied(self, tmp_path):
        from cvise.utils import fileutil

        source = tmp_path / 'abs.cpp'
        source.write_text('int f() { return 1; }\n')
        with pytest.raises(ValueError, match='relative to the working directory'):
            fileutil.copy_test_case(source, tmp_path / 'job')

    def test_an_unreadable_original_is_refused_not_ignored(self, tmp_path):
        """Answering "nothing is protected" for a file nobody could read is the
        one answer that must never be given on a guess."""
        import os

        original = write(tmp_path / 'a', 'g.cpp', GUARDED)
        os.chmod(original, 0)
        try:
            if os.access(original, os.R_OK):
                pytest.skip('running as a user that ignores file modes')
            assert noreduce.protected_regions(original) is None
        finally:
            os.chmod(original, 0o644)


class TestWhatCanCarryAMarker:
    """Only a file that can hold a definition, which is what the marker attaches to.

    MEASURED: a run refused to start with

        rejected: /src/.git/modules/third_party/ns-projection/COMMIT_EDITMSG
                  has a definition marked not to be reduced

    and it was right about the text. That file is the commit message of the
    change that introduced the guard, and it says CVISE_NOREDUCE twice.
    """

    def a_tree(self, tmp_path, name, body):
        root = tmp_path / 'tree'
        (root / Path(name).parent).mkdir(parents=True, exist_ok=True)
        (root / name).write_text(body)
        return root

    def test_a_commit_message_about_the_guard_is_not_a_guard(self, tmp_path):
        root = self.a_tree(
            tmp_path, '.git/COMMIT_EDITMSG',
            'test: every test can carry a reduction guard, one is enforced\n\n'
            'CVISE_NOREDUCE says so, and it needs a definition written out to\n'
            'attach to. CVISE_NOREDUCE_TEST(IngestTest, Parses) is the named form.\n')
        assert noreduce.marked_files(str(root)) == ()

    @pytest.mark.parametrize('name', [
        'README.md', 'notes.txt', 'design.rst', '.git/COMMIT_EDITMSG', 'CMakeLists.txt',
    ])
    def test_prose_that_mentions_the_marker_is_only_prose(self, tmp_path, name):
        """Skipping .git alone would have fixed one file and left the class."""
        root = self.a_tree(tmp_path, name, 'CVISE_NOREDUCE_TEST(Suite, Name)\n')
        assert noreduce.marked_files(str(root)) == ()

    @pytest.mark.parametrize('suffix', ['.cpp', '.cc', '.h', '.hpp', '.inc', '.cu'])
    def test_every_source_suffix_is_still_looked_at(self, tmp_path, suffix):
        """The other direction, which is the one that would be silent.

        A guard that stops looking at a file protects nothing there, and says
        so nowhere.
        """
        root = self.a_tree(tmp_path, f'guarded{suffix}', GUARDED)
        assert [Path(p).name for p in noreduce.marked_files(str(root))] == [f'guarded{suffix}']

    def test_a_file_named_outright_is_read_whatever_it_is_called(self, tmp_path):
        """A single-file test case is whatever the user handed over."""
        path = tmp_path / 'case'
        path.write_text(GUARDED)
        assert noreduce.marked_files(str(path)) == (str(path),)
