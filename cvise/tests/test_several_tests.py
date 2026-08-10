"""A criterion of several tests, and the hole that opens when there is more than one.

`--no-tests=error` makes ctest say 8 instead of 0 when the test it was asked
for is gone. That is what makes a one-test criterion safe, and it stops working
the moment there are two. MEASURED, two tests asked for and one of them deleted:

    ctest -R '^(alpha|deleted)$' --no-tests=error
    100% tests passed, 0 tests failed out of 1        rc=0

ctest ran what it found, all of it passed, and said so with a zero exit -- the
flag only fires when NOTHING matched. So the surviving test would speak for the
deleted one for the rest of the run, and the cheapest way to satisfy a two-test
criterion would be to delete one of the tests. Which a reduction finds.

The full table, measured on the same three-test project:

    both pass       rc=0   100% tests passed, 0 tests failed out of 2
    one fails       rc=8    50% tests passed, 1 tests failed out of 2
    one deleted     rc=0   100% tests passed, 0 tests failed out of 1
    both deleted    rc=8   No tests were found!!!

Only the third row is dangerous, and only the count distinguishes it.
"""

import shutil
import subprocess

import pytest

from cvise.utils import noreduce
from cvise.utils.projectcheck import they_did_not_all_run

pytestmark = pytest.mark.skipif(shutil.which('ctest') is None, reason='requires ctest')


PASSED = '100% tests passed, 0 tests failed out of {}\n'


class TestCountingWhatRan:
    def test_all_of_them_ran(self):
        assert they_did_not_all_run(PASSED.format(3), 3) == ''

    def test_one_of_them_is_gone(self):
        why = they_did_not_all_run(PASSED.format(2), 3)
        assert why and '2 of the 3' in why

    def test_a_single_test_is_left_to_no_tests_error(self):
        """One name is already covered, and a summary line ctest someday words
        differently must not start failing every single-test run."""
        assert they_did_not_all_run('anything at all', 1) == ''
        assert they_did_not_all_run('', 1) == ''

    def test_a_summary_that_cannot_be_read_is_a_refusal(self):
        """Not silence. If the count cannot be found, the one thing that makes
        a multi-test criterion safe is missing, and continuing would mean
        trusting exactly what has not been established."""
        why = they_did_not_all_run('ctest said something else entirely', 2)
        assert why and 'did not say how many' in why

    def test_more_than_asked_for_is_not_a_refusal(self):
        """A project may register more tests matching the pattern than were
        named; that is the project's business and not a deletion."""
        assert they_did_not_all_run(PASSED.format(5), 3) == ''


class TestAgainstCtestItself:
    """The table above, re-measured rather than remembered.

    Every number in this module comes from ctest's own wording, and ctest is
    free to change it. If it does, these fail here -- where it is a puzzle --
    rather than in a reduction, where it would be a criterion that silently
    stopped guarding anything.
    """

    def a_project(self, tmp_path):
        (tmp_path / 'CMakeLists.txt').write_text(
            'cmake_minimum_required(VERSION 3.20)\n'
            'project(t NONE)\n'
            'enable_testing()\n'
            'add_test(NAME alpha COMMAND ${CMAKE_COMMAND} -E echo ok)\n'
            'add_test(NAME beta COMMAND ${CMAKE_COMMAND} -E echo ok)\n'
            'add_test(NAME broken COMMAND ${CMAKE_COMMAND} -E false)\n'
        )
        build = tmp_path / 'b'
        subprocess.run(['cmake', '-S', str(tmp_path), '-B', str(build)],
                       capture_output=True, check=True)
        return build

    def run(self, build, names):
        pattern = '^(' + '|'.join(names) + ')$'
        proc = subprocess.run(
            ['ctest', '--test-dir', str(build), '-R', pattern, '--no-tests=error'],
            capture_output=True, text=True)
        return proc.returncode, proc.stdout

    def test_two_that_pass_are_accepted(self, tmp_path):
        rc, out = self.run(self.a_project(tmp_path), ['alpha', 'beta'])
        assert rc == 0
        assert they_did_not_all_run(out, 2) == ''

    def test_one_that_fails_is_refused_by_ctest(self, tmp_path):
        rc, out = self.run(self.a_project(tmp_path), ['alpha', 'broken'])
        assert rc != 0

    def test_a_deleted_one_is_refused_ONLY_by_the_count(self, tmp_path):
        """The whole reason this module exists.

        ctest is content: it exits zero and reports 100% passed. Nothing but
        the count says that the criterion no longer means what it said.
        """
        rc, out = self.run(self.a_project(tmp_path), ['alpha', 'never_existed'])
        assert rc == 0, 'ctest itself has started refusing this; check the count is still needed'
        assert '100% tests passed' in out
        why = they_did_not_all_run(out, 2)
        assert why, 'a deleted test slipped through the criterion'
        assert '1 of the 2' in why

    def test_all_deleted_is_caught_by_no_tests_error(self, tmp_path):
        rc, _ = self.run(self.a_project(tmp_path), ['gone', 'also_gone'])
        assert rc != 0


class TestTheGuardHoldsEveryNamedTest:
    """A run graded by several tests must protect every one of their markers."""

    def test_a_string_is_one_name_and_not_a_sequence_of_letters(self):
        assert noreduce._named('IngestTest.Parses') == frozenset({'IngestTest.Parses'})

    def test_several_names_are_all_active(self):
        criterion = ['A.one', 'B.two']
        assert noreduce._marks('CVISE_NOREDUCE_TEST(A, one)', criterion)
        assert noreduce._marks('CVISE_NOREDUCE_TEST(B, two)', criterion)

    def test_a_test_nobody_named_stays_reducible(self):
        assert not noreduce._marks('CVISE_NOREDUCE_TEST(C, three)', ['A.one', 'B.two'])

    def test_no_criterion_still_means_every_marker(self):
        """The safe direction: a guard that goes quiet when it does not know
        what it is guarding is worse than one that refuses too much."""
        assert noreduce._marks('CVISE_NOREDUCE_TEST(C, three)', None)

    def test_the_bare_marker_is_unconditional(self):
        assert noreduce._marks('CVISE_NOREDUCE', ['A.one'])


class TestEveryShapeOfCriterionSurvivesTheCache:
    """marked_files is lru_cached, so its arguments must be hashable.

    MEASURED: passing the list a multi-test run naturally produces cost the
    end-to-end run with `TypeError: unhashable type: 'list'`, and the unit
    tests missed it because they call _marks directly and never reach the
    cache. These go through the front door.
    """

    def a_tree(self, tmp_path):
        (tmp_path / 'a.cpp').write_text('int f();\n')
        return str(tmp_path)

    @pytest.mark.parametrize('criterion', [
        None,
        'A.one',
        ['A.one', 'B.two'],
        ('A.one', 'B.two'),
        frozenset({'A.one'}),
        [],
    ])
    def test_it_does_not_raise(self, tmp_path, criterion):
        assert noreduce.marked_files(self.a_tree(tmp_path), criterion) == ()

    def test_a_list_and_a_tuple_are_the_same_question(self, tmp_path):
        root = self.a_tree(tmp_path)
        assert (noreduce.marked_files(root, ['A.one', 'B.two'])
                == noreduce.marked_files(root, ('B.two', 'A.one')))
