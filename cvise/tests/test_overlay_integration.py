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

Skipped unless CVISE_CLI points at a built cvise-cli.py and FAKECHROOT_LIB at
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
LIB = os.environ.get('FAKECHROOT_LIB', '')
MARKER = 'KEEP-THIS-STRING-42'
# Whichever C++ compiler this environment actually has: the reduction image
# carries clang, a desktop usually has g++, and hardcoding either makes the
# test report a broken overlay when the only thing missing is a compiler.
CXX = os.environ.get('CXX') or shutil.which('g++') or shutil.which('clang++-19') or shutil.which('clang++')

CMAKELISTS = (
    'cmake_minimum_required(VERSION 3.20)\n'
    'project(demo CXX)\n'
    'add_executable(prog main.cpp core.cpp extra.cpp)\n'
)

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


def write_project(root: Path):
    root.mkdir(parents=True)
    (root / 'CMakeLists.txt').write_text(CMAKELISTS)
    for name, text in PROJECT.items():
        (root / name).write_text(text)


def write_test_script(path: Path, project: Path, witness: Path):
    path.write_text(
        '#!/bin/sh\n'
        '# Build the project AT ITS REAL PATH. Nothing here knows about deltas:\n'
        '# if the overlay is doing its job, these are the candidate sources.\n'
        f'cd {project} || exit 125\n'
        f'md5sum *.cpp *.hpp >> {witness} 2>/dev/null\n'
        f'{CXX} -O0 -o prog main.cpp core.cpp extra.cpp 2>/dev/null || exit 1\n'
        f'./prog 2>/dev/null | grep -q {MARKER}\n'
    )
    path.chmod(0o755)


@pytest.mark.skipif(not CVISE or not Path(LIB).exists() or not CXX,
                    reason='needs CVISE_CLI, FAKECHROOT_LIB and a C++ compiler')
def test_reduction_through_the_overlay(tmp_path):
    work = tmp_path
    if True:
        project = work / 'project'
        write_project(project)
        pristine = {name: (project / name).read_text() for name in PROJECT}
        witness = work / 'compiled.txt'
        script = work / 'interesting.sh'
        write_test_script(script, project, witness)

        env = {
            **os.environ,
            'CVISE_OVERLAY_LIB': LIB,
            'TMPDIR': str(work / 'tmp'),
        }
        (work / 'tmp').mkdir()

        # The whole interface: the CMakeLists.txt that drives the build, and
        # the question to ask about each variant. Everything else -- which files
        # exist, what flags they need -- comes from the database CMake writes.
        cmd = [sys.executable, CVISE, '--n', '4', '--timeout', '60',
               str(project / 'CMakeLists.txt'), str(script)]
        proc = subprocess.run(cmd, cwd=project, env=env, capture_output=True,
                              text=True, timeout=900)
        print(f'cvise exit {proc.returncode}')
        tail = (proc.stderr or proc.stdout).strip().splitlines()[-4:]
        for line in tail:
            print(f'  {line}')

        reduced = {name: (project / name).read_text() for name in PROJECT}
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

        assert total_builds > 0, 'the interestingness test never built anything'
        assert pristine_builds <= 1, (
            f'{pristine_builds} builds compiled the PRISTINE core.cpp: the overlay was inert '
            'and the reduction graded candidates against code it never changed'
        )
        assert kept, 'the reduction destroyed the property it was told to preserve'
        assert shrank, 'nothing was reduced'

