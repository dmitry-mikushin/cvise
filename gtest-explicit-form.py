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


MARKER = 'CVISE_NOREDUCE'
MARKER_HEADER = 'noreduce.h'


def tests_in(text: str) -> list[tuple[int, int, str, str]]:
    """Every TEST(suite, name) in the file, as (start, end, suite, name).

    `end` is past the closing brace of the body, so a caller can replace the
    whole macro with what it expands to.
    """
    found = []
    for match in re.finditer(r'^TEST\(\s*(\w+)\s*,\s*(\w+)\s*\)\s*', text, re.M):
        opening = text.index('{', match.end() - 1)
        found.append((match.start(), body_extent(text, opening), match.group(1), match.group(2)))
    return found


def with_marker_header(text: str) -> str:
    """Make CVISE_NOREDUCE mean something in this file.

    Without the include the marker is an undeclared identifier and the file
    stops compiling -- MEASURED across this corpus: one file of 227 had the
    include, so a bulk rewrite that forgot it would break 226.

    Placed after the last include that comes BEFORE the first use of the
    marker, which is not the same as the last include in the file. MEASURED:
    cpython_set_order_test.cpp includes a fixture at line 923, below its tests,
    so "after the last include" put the header 743 lines after the first
    CVISE_NOREDUCE that needed it, and 227 files compiled into
    `unknown type name 'CVISE_NOREDUCE'`. The build found that; reading the
    diff did not.
    """
    if re.search(rf'#\s*include\s*[<"].*{re.escape(MARKER_HEADER)}', text):
        return text
    line = f'#include "{MARKER_HEADER}"'
    first_use = text.find(MARKER)
    if first_use < 0:
        return text
    before = [m for m in re.finditer(r'^#\s*include\s+[<"][^>"]+[>"].*$', text, re.M)
              if m.end() < first_use]
    if not before:
        return line + '\n\n' + text
    at = before[-1].end()
    return text[:at] + '\n' + line + text[at:]


def rewrite(text: str, wanted: set[str] | None) -> tuple[str, list[str]]:
    """Rewrite the selected tests, returning the new text and what was done.

    Backwards through the file, because each replacement changes the offsets of
    everything after it and nothing before it.
    """
    done = []
    for start, end, suite, name in reversed(tests_in(text)):
        if wanted is not None and f'{suite}.{name}' not in wanted:
            continue
        body = text[text.index('{', start):end]
        replacement = explicit_form(suite, name, body)
        # The one thing worth checking twice: the bytes copied are the bytes
        # found. A test rewritten by hand is a test that may quietly assert
        # something slightly different.
        if body not in replacement:
            raise SystemExit(f'refusing: the body of {suite}.{name} was altered in the process')
        if not body.startswith('{') or not body.endswith('}'):
            raise SystemExit(f'refusing: {suite}.{name} does not look like a body')
        text = text[:start] + replacement + text[end:]
        done.append(f'{suite}.{name}')
    if done:
        text = with_marker_header(text)
    return text, list(reversed(done))


def sources(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    return sorted(p for p in path.rglob('*.cpp') if p.is_file())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('path', help='a test file, or a directory of them')
    parser.add_argument('--test', action='append', metavar='Suite.Name',
                        help='only this test; repeatable. Without it, every TEST() found')
    parser.add_argument('--write', action='store_true', help='apply it; otherwise only show it')
    args = parser.parse_args()

    wanted = set(args.test) if args.test else None
    files = sources(Path(args.path))
    if not files:
        raise SystemExit(f'no .cpp under {args.path}')

    total = 0
    touched = 0
    single = None
    for path in files:
        text = path.read_text()
        produced, done = rewrite(text, wanted)
        if not done:
            continue
        total += len(done)
        touched += 1
        single = (path, text, produced) if len(files) == 1 and len(done) == 1 else single
        if args.write:
            path.write_text(produced)
        elif len(files) > 1 or len(done) > 1:
            print(f'{path}: {len(done)} test(s) -- {", ".join(done)}')

    if single is not None:
        path, before, after = single
        sys.stdout.writelines(difflib.unified_diff(
            before.splitlines(keepends=True), after.splitlines(keepends=True),
            fromfile=str(path), tofile=str(path) + ' (rewritten)', n=2,
        ))

    if wanted and total == 0:
        raise SystemExit(f'none of {sorted(wanted)} found under {args.path}')

    print(f'\n{total} test(s) rewritten in {touched} file(s)')
    if not args.write:
        print('nothing written. Re-run with --write to apply.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
