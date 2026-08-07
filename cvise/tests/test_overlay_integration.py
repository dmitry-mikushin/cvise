"""The reducer and the overlay, driven together against a real project.

Every other test here covers one half. This one is the only check that the two
work as a system: C-Vise proposes variants, the interestingness test builds a
multi-file C++ project and greps its output, and the overlay is what makes that
build see the candidate instead of the pristine sources.

It is also the only check that can catch the failure that matters most, and the
one three rounds of review missed: a run whose overlay is inert compiles the
original sources every time, finds every candidate interesting, and reports a
triumphant reduction of nothing. So the test script records the checksum of what
it compiled, and the test fails unless the builds really saw the variants.

Skipped unless CVISE_CLI points at a built cvise-cli.py and CVISE_OVERLAY_LIB at
the overlay library, because it needs both halves actually built.
"""

import pytest

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

CVISE = os.environ.get('CVISE_CLI', '')
# The library is built with C-Vise now, so the test finds it the way the
# reducer does rather than being told where it is.
LIB = os.environ.get('CVISE_OVERLAY_LIB', '')
if not LIB:
    from cvise.utils import overlay as _overlay
    LIB = _overlay.library_path()
MARKER = 'KEEP-THIS-STRING-42'
TEST_NAME = 'says_the_marker'
# Whichever C++ compiler this environment actually has: the reduction image
# carries clang, a desktop usually has g++, and hardcoding either makes the
# test report a broken overlay when the only thing missing is a compiler.
CXX = os.environ.get('CXX') or shutil.which('g++') or shutil.which('clang++-19') or shutil.which('clang++')

def cmakelists(recorder: Path) -> str:
    """The whole criterion, written where the reduction cannot reach it.

    The property is asserted on the program's OUTPUT rather than its exit
    status, because `prog` exits 0 whatever it prints -- and a criterion an
    empty program satisfies is one the reduction will satisfy by emptying the
    program.

    The recorder is a BUILD step, not part of the test, and that is the point of
    the whole file: it notes the checksum of the source the compiler is about to
    be given. Taken from the test run it would say only what the binary did, and
    an inert overlay produces a perfectly good binary -- of the pristine sources.
    """
    return (
        'cmake_minimum_required(VERSION 3.20)\n'
        'project(demo CXX)\n'
        'add_executable(prog main.cpp core.cpp extra.cpp)\n'
        'add_custom_target(witness\n'
        f'    COMMAND {recorder}\n'
        '    WORKING_DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR})\n'
        'add_dependencies(prog witness)\n'
        'enable_testing()\n'
        f'add_test(NAME {TEST_NAME} COMMAND prog)\n'
        f'set_tests_properties({TEST_NAME} PROPERTIES\n'
        f'                     PASS_REGULAR_EXPRESSION "{MARKER}")\n'
    )


def write_recorder(path: Path, witness: Path):
    """A script rather than a shell one-liner in COMMAND.

    MEASURED: `COMMAND sh -c "md5sum core.cpp >> witness"` reaches sh with the
    redirection mangled -- ninja escapes the argument, sh tries to EXECUTE the
    witness path, and the target fails with "No such file or directory" about a
    file it was supposed to create. A script takes its arguments from a file
    nobody re-quotes.
    """
    path.write_text(
        '#!/bin/sh\n'
        f'md5sum core.cpp >> {witness} 2>/dev/null\n'
        '# Never the reason a build fails: whether a candidate that deleted this\n'
        '# file is interesting is for the compiler and the test to say.\n'
        'exit 0\n'
    )
    path.chmod(0o755)


PROJECT = {
    'core.hpp': '#pragma once\nconst char* core_message();\nint core_padding();\n',
    'core.cpp': (
        '#include "core.hpp"\n'
        f'const char* core_message() {{ return "{MARKER}"; }}\n'
        'int core_padding() { return 1; }\n'
        'static int unused_one() { return 11; }\n'
        'static int unused_two() { return 22; }\n'
    ),
    'extra.hpp': '#pragma once\nint extra_value();\n',
    'extra.cpp': (
        '#include "extra.hpp"\n'
        'int extra_value() { return 7; }\n'
        'static int also_unused() { return 33; }\n'
    ),
    'main.cpp': (
        '#include <cstdio>\n'
        '#include "core.hpp"\n'
        '#include "extra.hpp"\n'
        'int main() { std::printf("%s %d\\n", core_message(), extra_value()); }\n'
    ),
}


def write_project(root: Path, recorder: Path):
    root.mkdir(parents=True)
    (root / 'CMakeLists.txt').write_text(cmakelists(recorder))
    for name, text in PROJECT.items():
        (root / name).write_text(text)


@pytest.mark.skipif(not CVISE or not Path(LIB).exists() or not CXX,
                    reason='needs CVISE_CLI, CVISE_OVERLAY_LIB and a C++ compiler')
def test_reduction_through_the_overlay(tmp_path):
    work = tmp_path
    if True:
        witness = work / 'compiled.txt'
        project = work / 'project'
        write_recorder(work / 'record.sh', witness)
        write_project(project, work / 'record.sh')
        pristine = {name: (project / name).read_text() for name in PROJECT}

        env = {
            **os.environ,
            'CVISE_OVERLAY_LIB': LIB,
            'TMPDIR': str(work / 'tmp'),
        }
        (work / 'tmp').mkdir()

        # The whole interface: the CMakeLists.txt that drives the build, and
        # the name of the ctest test that says whether a variant is still
        # interesting. Everything else -- which files exist, what flags they
        # need -- comes from the database CMake writes.
        cmd = [sys.executable, CVISE, '--n', '4', '--timeout', '60',
               str(project / 'CMakeLists.txt'), TEST_NAME]
        proc = subprocess.run(cmd, cwd=project, env=env, capture_output=True,
                              text=True, timeout=900)
        print(f'cvise exit {proc.returncode}')
        tail = (proc.stderr or proc.stdout).strip().splitlines()[-4:]
        for line in tail:
            print(f'  {line}')

        # The exit code, which this used to ignore. A reduction that produced a
        # good tree and then died writing the last log line looks like a
        # success in every other assertion here, and did: `len()` of an int
        # crashed the final write-back of EVERY project reduction, and the tree
        # was already correct so nothing else noticed.
        assert proc.returncode == 0, 'cvise did not exit cleanly'

        # A file the reduction removed reads as empty rather than raising: a
        # header nothing needs any more is a result, not a broken run. MEASURED:
        # extra.hpp goes entirely, and the test used to die of FileNotFoundError
        # about the best thing that had happened.
        reduced = {name: (project / name).read_text() if (project / name).is_file() else ''
                   for name in PROJECT}
        shrank = sum(len(pristine[n]) for n in PROJECT) > sum(len(reduced[n]) for n in PROJECT)
        kept = MARKER in reduced['core.cpp']

        # Did any build ever see the pristine core.cpp? If the overlay were
        # inert, the witness would record the pristine checksum for every
        # candidate and the "reduction" would be meaningless.
        seen = witness.read_text() if witness.exists() else ''
        import hashlib
        pristine_md5 = hashlib.md5(pristine['core.cpp'].encode()).hexdigest()
        pristine_builds = sum(1 for line in seen.splitlines()
                              if line.startswith(pristine_md5) and 'core.cpp' in line)
        total_builds = sum(1 for line in seen.splitlines() if 'core.cpp' in line)

        print()
        print(f'the tree got smaller:              {shrank}')
        print(f'the property was preserved:        {kept}')
        print(f'builds that saw core.cpp:          {total_builds}')
        print(f'  of those, the pristine version:  {pristine_builds}')
        print(f'  of those, a variant:             {total_builds - pristine_builds}')

        assert total_builds > 0, 'the interestingness test never built anything'
        # Not "no build ever saw the original": a candidate that edits another
        # file does not change this one, and the delta holds only what changed,
        # so compiling the original here is right. What must never happen is
        # that EVERY build saw the original -- that is the inert overlay, which
        # finds every candidate interesting and reduces nothing while looking
        # like a triumph.
        variant_builds = total_builds - pristine_builds
        assert variant_builds > 0, (
            f'all {total_builds} builds compiled the PRISTINE core.cpp: the overlay was inert '
            'and the reduction graded candidates against code it never changed'
        )
        assert kept, 'the reduction destroyed the property it was told to preserve'
        assert shrank, 'nothing was reduced'

