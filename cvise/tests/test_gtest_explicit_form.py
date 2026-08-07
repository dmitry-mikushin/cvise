"""Rewriting a whole test corpus into the form a marker can attach to.

MEASURED on ns-projection: 1017 `TEST(...)` in 227 files, and one of those
files carried the include the marker needs. A rewrite that got any of this
wrong would be found by the compiler across 226 files at once, which is the
expensive way to find it.

The tests here are about what the diff cannot show and the build can: where the
include lands, and whether every body survived byte for byte.
"""

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / 'gtest-explicit-form.py'


@pytest.fixture
def rewriter():
    spec = importlib.util.spec_from_file_location('gtest_explicit_form', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


TWO_TESTS = '''\
#include <gtest/gtest.h>
#include "helpers.hpp"

TEST(Suite, First) {
    EXPECT_EQ(1, 1);
}

TEST(Suite, Second) {
    // a brace inside a string is not a brace: "}"
    EXPECT_EQ(2, 2);
}
'''


def test_every_test_in_the_file_is_rewritten(rewriter):
    produced, done = rewriter.rewrite(TWO_TESTS, None)
    assert done == ['Suite.First', 'Suite.Second']
    # Asked of the same scanner the script uses, not of a substring search: the
    # generated code carries a comment naming the macro it replaced, and a
    # naive check finds that and calls it a leftover.
    assert rewriter.tests_in(produced) == []
    assert produced.count('CVISE_NOREDUCE') == 2
    assert 'void Suite_First_Test::TestBody()' in produced
    assert 'void Suite_Second_Test::TestBody()' in produced


def test_the_bodies_are_the_bytes_they_were(rewriter):
    """The one thing worth checking twice.

    A body rewritten rather than copied is a test that may quietly assert
    something slightly different, which for the test a whole reduction is
    graded by is the worst possible place for a typo.
    """
    produced, _ = rewriter.rewrite(TWO_TESTS, None)
    for body in ('{\n    EXPECT_EQ(1, 1);\n}',
                 '{\n    // a brace inside a string is not a brace: "}"\n    EXPECT_EQ(2, 2);\n}'):
        assert body in produced, body


def test_only_the_named_test_is_rewritten(rewriter):
    produced, done = rewriter.rewrite(TWO_TESTS, {'Suite.Second'})
    assert done == ['Suite.Second']
    assert 'TEST(Suite, First)' in produced
    assert produced.count('CVISE_NOREDUCE') == 1


def test_the_include_precedes_the_first_use(rewriter):
    produced, _ = rewriter.rewrite(TWO_TESTS, None)
    assert produced.index('#include "noreduce.h"') < produced.index('CVISE_NOREDUCE')


def test_an_include_below_the_tests_does_not_drag_the_header_down(rewriter):
    """The shape that broke 227 files at once.

    cpython_set_order_test.cpp includes a fixture at line 923, well below its
    tests. Placing the header "after the last include" put it 743 lines after
    the first use, and every such file compiled into `unknown type name
    'CVISE_NOREDUCE'`. The build found it; the diff looked fine.
    """
    text = TWO_TESTS + '\n#include "fixtures/late.inc"\n'
    produced, _ = rewriter.rewrite(text, None)
    assert produced.index('#include "noreduce.h"') < produced.index('CVISE_NOREDUCE')
    assert '#include "fixtures/late.inc"' in produced


def test_a_file_with_no_includes_still_gets_the_header(rewriter):
    produced, _ = rewriter.rewrite('TEST(A, B) {\n    EXPECT_TRUE(true);\n}\n', None)
    assert produced.startswith('#include "noreduce.h"')
    assert produced.index('#include "noreduce.h"') < produced.index('CVISE_NOREDUCE')


def test_running_it_twice_changes_nothing_the_second_time(rewriter):
    once, _ = rewriter.rewrite(TWO_TESTS, None)
    twice, done = rewriter.rewrite(once, None)
    assert done == []
    assert twice == once
    assert twice.count('#include "noreduce.h"') == 1


def test_a_test_whose_body_holds_a_raw_string_survives(rewriter):
    """Braces inside a raw string are not braces, and the scanner knows it."""
    text = '#include <gtest/gtest.h>\n\nTEST(S, R) {\n    const char* j = R"json({"a": 1})json";\n    EXPECT_NE(j, nullptr);\n}\n'
    produced, done = rewriter.rewrite(text, None)
    assert done == ['S.R']
    assert 'R"json({"a": 1})json"' in produced
    assert produced.rstrip().endswith('}')


def test_the_marker_names_the_test_it_guards(rewriter):
    """All the guards, one of them active.

    Every test can carry a marker; which one is enforced follows from the test
    the reduction is graded by, not from editing a thousand files when that
    changes.
    """
    produced, _ = rewriter.rewrite(TWO_TESTS, None)
    assert 'CVISE_NOREDUCE_TEST(Suite, First)' in produced
    assert 'CVISE_NOREDUCE_TEST(Suite, Second)' in produced


def test_only_the_criterion_is_enforced(rewriter, tmp_path):
    """The point of the whole thing, asked of the guard rather than of the text."""
    from cvise.utils import noreduce

    produced, _ = rewriter.rewrite(TWO_TESTS, None)
    first, second = 'Suite.First', 'Suite.Second'

    assert noreduce.has_protection(produced, first)
    assert noreduce.has_protection(produced, second)
    assert not noreduce.has_protection(produced, 'Other.Test')
    # Not knowing which test is being graded by protects everything, which is
    # the safe direction to be wrong in.
    assert noreduce.has_protection(produced, None)


def test_the_header_gains_the_definition(rewriter, tmp_path):
    """Emitting uses of a macro nobody defined is the trap this already sprang."""
    header = tmp_path / 'noreduce.h'
    header.write_text('#pragma once\n#define CVISE_NOREDUCE\n')
    assert rewriter.ensure_marker_defined(header) is True
    assert '#define CVISE_NOREDUCE_TEST(suite, name)' in header.read_text()
    # Idempotent: a second run must not append it again.
    assert rewriter.ensure_marker_defined(header) is False


ALREADY_EXPLICIT = '''\
#include <gtest/gtest.h>
#include "noreduce.h"

namespace {
class Ingest_Test_Parses_Test : public ::testing::Test {
public:
    void TestBody() override;
};

const ::testing::TestInfo* const kIngest_Test_Parses_TestRegistered =
    ::testing::RegisterTest("Ingest_Test", "Parses",
                            nullptr, nullptr, __FILE__, __LINE__,
                            []() -> ::testing::Test* {
                                return new Ingest_Test_Parses_Test;
                            });
} // namespace

CVISE_NOREDUCE
void Ingest_Test_Parses_Test::TestBody() {
    EXPECT_EQ(1, 1);
}
'''


def test_a_bare_marker_is_given_the_name_of_its_test(rewriter):
    """The hand-written guard is unconditional, so it survives a change of
    criterion that should have made it inert."""
    produced, done = rewriter.rewrite(ALREADY_EXPLICIT, None)
    assert done == ['Ingest_Test.Parses']
    assert 'CVISE_NOREDUCE_TEST(Ingest_Test, Parses)' in produced
    assert '\nCVISE_NOREDUCE\n' not in produced


def test_the_name_comes_from_the_registration_not_the_class(rewriter):
    """Ingest_Test_Parses_Test splits two ways and only one is right.

    By class name it reads as suite Ingest, test Test_Parses; the registration
    says suite Ingest_Test, test Parses, and the registration is what gtest
    itself goes by.
    """
    produced, _ = rewriter.rewrite(ALREADY_EXPLICIT, None)
    assert 'CVISE_NOREDUCE_TEST(Ingest_Test, Parses)' in produced
    assert 'CVISE_NOREDUCE_TEST(Ingest, Test_Parses)' not in produced


def test_a_bare_marker_on_something_else_is_left_alone(rewriter):
    """The unconditional spelling is a legitimate tool; only TestBody guards
    with a registration to name them are converted."""
    text = '#include "noreduce.h"\n\nCVISE_NOREDUCE\nvoid some_helper() {\n    return;\n}\n'
    produced, done = rewriter.rewrite(text, None)
    assert done == []
    assert produced == text
