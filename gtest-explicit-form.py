#!/usr/bin/env python3
"""Rewrite a GoogleTest TEST() into the explicit form, without retyping it.

TEST(suite, name) { ... } is sugar for three things: a fixture class, a
registration, and an out-of-line TestBody() definition. The macro form has no
place to attach an attribute to -- MEASURED: g++ says "attributes are not
allowed on a function-definition" after it and "expected unqualified-id before
static_assert" before it, and clang refuses both too -- while the explicit form
ends in an ordinary function definition, which takes attributes like any other.

The body is not reformatted, re-indented or retyped. It is copied as the exact
bytes it already was, and the script refuses to write anything if the bytes it
copied do not match the bytes it found. A test rewritten by hand is a test that
may quietly assert something slightly different, which for the one test a whole
reduction is graded by is the worst possible place for a typo.

Finding where the body ends is done with a scanner rather than by counting
braces naively, because braces inside string literals, character literals, raw
strings and comments are not braces.

Usage:
  gtest_explicit_form.py <file> <Suite.Name>            # show the diff only
  gtest_explicit_form.py --write <file> <Suite.Name>    # apply it
"""

import argparse
import difflib
import re
import sys
from pathlib import Path


def skip_literals_and_comments(text: str, i: int) -> int | None:
    """If something non-code starts at i, return where it ends. Else None."""
    if text.startswith('//', i):
        end = text.find('\n', i)
        return len(text) if end < 0 else end
    if text.startswith('/*', i):
        end = text.find('*/', i + 2)
        return len(text) if end < 0 else end + 2
    # Raw string: R"delim( ... )delim", where delim has no parens or spaces.
    raw = re.compile(r'(?:u8|u|U|L)?R"([^ ()\\\t\n]{0,16})\(').match(text, i)
    if raw is not None:
        closing = ')' + raw.group(1) + '"'
        end = text.find(closing, raw.end())
        return len(text) if end < 0 else end + len(closing)
    if text[i] in '"\'':
        quote = text[i]
        j = i + 1
        while j < len(text):
            if text[j] == '\\':
                j += 2
                continue
            if text[j] == quote:
                return j + 1
            j += 1
        return len(text)
    return None


def body_extent(text: str, opening: int) -> int:
    """Where the block that opens at `opening` closes, exclusive."""
    depth = 0
    i = opening
    while i < len(text):
        skipped = skip_literals_and_comments(text, i)
        if skipped is not None:
            i = skipped
            continue
        if text[i] == '{':
            depth += 1
        elif text[i] == '}':
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise SystemExit('the test body never closes')


def explicit_form(suite: str, name: str, body: str) -> str:
    """The three things the macro would have produced, spelled out."""
    cls = f'{suite}_{name}_Test'
    return f"""\
namespace {{

// TEST({suite}, {name}) written out, because the macro
// form has nowhere to attach the marker below.
class {cls} : public ::testing::Test {{
public:
    void TestBody() override;
}};

// The factory is declared returning ::testing::Test*, NOT {cls}*, and that is
// load-bearing. RegisterTest takes the fixture identity from this return type,
// TEST() uses the sentinel GetTestTypeId() == GetTypeId<Test>(), and gtest
// refuses a suite that mixes two fixtures: "test ... is defined using TEST_F
// but test ... is defined using TEST". Returning the base keeps this test a
// TEST(), so its siblings in the suite need not change.
const ::testing::TestInfo* const k{cls}Registered =
    ::testing::RegisterTest("{suite}", "{name}",
                            nullptr, nullptr, __FILE__, __LINE__,
                            []() -> ::testing::Test* {{
                                return new {cls};
                            }});

}} // namespace

CVISE_NOREDUCE
void {cls}::TestBody() {body}"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('file')
    parser.add_argument('test', help='Suite.Name')
    parser.add_argument('--write', action='store_true', help='apply it; otherwise only show it')
    args = parser.parse_args()

    path = Path(args.file)
    text = path.read_text()
    suite, name = args.test.split('.', 1)

    match = re.search(rf'^TEST\(\s*{re.escape(suite)}\s*,\s*{re.escape(name)}\s*\)\s*', text, re.M)
    if match is None:
        raise SystemExit(f'TEST({suite}, {name}) not found in {path}')
    opening = text.index('{', match.end() - 1)
    end = body_extent(text, opening)
    body = text[opening:end]

    # The one thing worth checking twice: the bytes copied are the bytes found.
    replacement = explicit_form(suite, name, body)
    if body not in replacement:
        raise SystemExit('refusing: the body was altered in the process')
    if not body.startswith('{') or not body.endswith('}'):
        raise SystemExit('refusing: that does not look like a body')

    produced = text[: match.start()] + replacement + text[end:]

    diff = difflib.unified_diff(
        text.splitlines(keepends=True),
        produced.splitlines(keepends=True),
        fromfile=str(path), tofile=str(path) + ' (rewritten)', n=2,
    )
    sys.stdout.writelines(diff)

    kept = len(body.splitlines())
    print(f'\n{kept} lines of body copied unchanged; {len(body)} bytes')
    if not args.write:
        print('nothing written. Re-run with --write to apply.')
        return 0
    path.write_text(produced)
    print(f'written to {path}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
