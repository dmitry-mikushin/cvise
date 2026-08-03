"""What a reduction may not touch, and the ways it might try.

The mechanism exists because of one specific way a reduction lies: it empties
the body of the test it is being graded by, the emptied test still passes, and
from then on every candidate is measured against nothing. That failure looks
exactly like an excellent reduction, so the tests below are less about the
happy path than about each way the protection could be got around.
"""

from pathlib import Path

from cvise.utils import noreduce
from cvise.utils.testing import protected_rejection


GUARDED = """\
#include <gtest/gtest.h>

int helper() { return 41; }

// cvise noreduce begin
TEST(IngestTest, ParsesRepresentativeRequestJson) {
    EXPECT_EQ(helper() + 1, 42);
}
// cvise noreduce end

TEST(Other, MayBeReduced) {
    EXPECT_TRUE(true);
}
"""


class TestFindingTheRegions:
    def test_an_unmarked_file_has_nothing_to_protect(self):
        assert not noreduce.has_protection('int main() { return 0; }\n')
        assert noreduce.protected_regions('int main() {}\n') == []

    def test_the_markers_are_part_of_what_is_protected(self):
        """Otherwise deleting a marker is a legal way to unlock the region."""
        region = noreduce.protected_regions(GUARDED)[0]
        assert noreduce.OPEN in region
        assert noreduce.CLOSE in region
        assert 'EXPECT_EQ(helper() + 1, 42);' in region

    def test_only_the_marked_part(self):
        region = noreduce.protected_regions(GUARDED)[0]
        assert 'MayBeReduced' not in region
        assert 'int helper()' not in region

    def test_several_regions_are_kept_apart(self):
        text = (
            '// cvise noreduce begin\nA\n// cvise noreduce end\n'
            'middle\n'
            '// cvise noreduce begin\nB\n// cvise noreduce end\n'
        )
        regions = noreduce.protected_regions(text)
        assert len(regions) == 2
        assert 'A' in regions[0] and 'B' in regions[1]
        assert 'middle' not in regions[0] + regions[1]

    def test_an_unterminated_marker_protects_the_rest(self):
        """A typo must not silently leave the criterion unguarded."""
        text = 'before\n// cvise noreduce begin\nafter\nmore\n'
        regions = noreduce.protected_regions(text)
        assert len(regions) == 1
        assert 'after' in regions[0] and 'more' in regions[0]
        assert 'before' not in regions[0]


class TestWhatCountsAsDisturbing:
    def test_leaving_it_alone_is_allowed(self):
        assert not noreduce.disturbed(GUARDED, GUARDED)

    def test_reducing_around_it_is_allowed(self):
        """The point is to protect the criterion, not to freeze the file."""
        elsewhere = GUARDED.replace('TEST(Other, MayBeReduced) {\n    EXPECT_TRUE(true);\n}\n', '')
        assert elsewhere != GUARDED
        assert not noreduce.disturbed(GUARDED, elsewhere)

    def test_moving_it_is_allowed(self):
        """Deleting code above a region shifts it; that is not a change to it."""
        moved = GUARDED.replace('int helper() { return 41; }\n', '')
        assert not noreduce.disturbed(GUARDED, moved)

    def test_hollowing_the_test_out_is_refused(self):
        """The failure this exists for: an emptied test still passes."""
        hollow = GUARDED.replace('    EXPECT_EQ(helper() + 1, 42);\n', '')
        assert noreduce.disturbed(GUARDED, hollow)

    def test_weakening_an_assertion_is_refused(self):
        weaker = GUARDED.replace('EXPECT_EQ(helper() + 1, 42)', 'EXPECT_EQ(1, 1)')
        assert noreduce.disturbed(GUARDED, weaker)

    def test_deleting_the_whole_region_is_refused(self):
        gone = '\n'.join(
            line for line in GUARDED.splitlines() if 'noreduce' not in line and 'EXPECT_EQ' not in line
        )
        assert noreduce.disturbed(GUARDED, gone)

    def test_removing_a_marker_is_refused(self):
        """The way around it, if the markers were not themselves protected."""
        unlocked = GUARDED.replace('// cvise noreduce end\n', '')
        assert noreduce.disturbed(GUARDED, unlocked)

    def test_emptying_the_whole_file_is_refused(self):
        assert noreduce.disturbed(GUARDED, '')


class TestTheCandidateIsRefusedBeforeAnythingIsSpent:
    """The verdict has to arrive before the build, not from it."""

    def candidate(self, tmp_path, produced_text):
        original = tmp_path / 'project' / 'ingest_test.cpp'
        original.parent.mkdir(parents=True)
        original.write_text(GUARDED)
        delta = tmp_path / 'delta'
        placed = delta / str(original).lstrip('/')
        placed.parent.mkdir(parents=True)
        placed.write_text(produced_text)
        return [original], delta

    def test_an_untouched_region_goes_on(self, tmp_path):
        changed, delta = self.candidate(tmp_path, GUARDED.replace('int helper', 'int helper2'))
        assert protected_rejection(changed, delta) is None

    def test_a_disturbed_region_is_rejected_and_says_which_file(self, tmp_path):
        hollow = GUARDED.replace('    EXPECT_EQ(helper() + 1, 42);\n', '')
        changed, delta = self.candidate(tmp_path, hollow)
        verdict = protected_rejection(changed, delta)
        assert verdict is not None
        returncode, _, stderr = verdict
        assert returncode == 1
        assert b'marked not to be reduced' in stderr
        assert b'ingest_test.cpp' in stderr

    def test_a_file_without_markers_is_not_slowed_down(self, tmp_path):
        original = tmp_path / 'project' / 'plain.cpp'
        original.parent.mkdir(parents=True)
        original.write_text('int main() { return 0; }\n')
        delta = tmp_path / 'delta'
        placed = delta / str(original).lstrip('/')
        placed.parent.mkdir(parents=True)
        placed.write_text('int main() {}\n')
        assert protected_rejection([original], delta) is None

    def test_a_binary_file_is_not_an_error(self, tmp_path):
        original = tmp_path / 'project' / 'blob.bin'
        original.parent.mkdir(parents=True)
        original.write_bytes(b'\xff\xfe\x00\x01')
        delta = tmp_path / 'delta'
        placed = delta / str(original).lstrip('/')
        placed.parent.mkdir(parents=True)
        placed.write_bytes(b'\x00')
        assert protected_rejection([original], delta) is None

    def test_a_produced_file_that_is_not_there_is_not_an_error(self, tmp_path):
        """Robustness, not a deletion check -- deletions never arrive this way.

        A candidate that deletes a file records a whiteout instead of placing
        one, so it is not among the files this is given. Deleting the file that
        carries the criterion is refused by the criterion itself: the test is
        then not registered, and an exact ctest filter that matches nothing is
        an error. MEASURED on ns-projection: exit 8.
        """
        original = tmp_path / 'project' / 'x.cpp'
        original.parent.mkdir(parents=True)
        original.write_text(GUARDED)
        assert protected_rejection([original], tmp_path / 'empty-delta') is None
