"""One way in: the CMakeLists.txt that drives the project's build.

Everything a reduction needs to know about a project is already written down in
one place, and it is not the CMakeLists.txt itself -- it is the
compile_commands.json that CMake produces from it. That file names every
translation unit in the project and the exact flags each one is compiled with,
which is precisely the two things a reducer has to know: what to reduce, and how
to parse it.

So the user names the CMakeLists.txt, C-Vise runs CMake once to obtain the
database, and from there on nothing else is asked of anybody. Asking the user
for the file list, or for the flags, or for a database path is asking them to
repeat what CMake already knows, and every one of those questions is another way
for the answer to disagree with the build.
"""

import json
import logging
import os
import shlex
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from cvise.utils.error import CViseError


class ProjectError(CViseError):
    def __init__(self, message):
        self.message = message

    def __str__(self):
        return self.message


@dataclass
class Project:
    """What CMake told us about the project."""

    cmakelists: Path
    root: Path
    build_dir: Path
    compilation_database: Path
    sources: list[Path]
    # For every file that can be reduced, one compile command that exercises
    # it: its own for a translation unit, and for a header the command of some
    # unit that includes it. This is what lets a candidate be rejected in
    # milliseconds by a syntax check instead of minutes by a full build.
    check_command: dict[str, list[str]]


def configure(cmakelists: Path, build_dir: Path) -> Project:
    """Run CMake once, for the database and nothing else.

    The build directory here is C-Vise's own: the interestingness test builds
    the project however the project is normally built, and this configure exists
    only to make CMake write down what it knows.
    """
    cmakelists = Path(cmakelists).resolve()
    if cmakelists.is_dir():
        cmakelists = cmakelists / 'CMakeLists.txt'
    if not cmakelists.is_file():
        raise ProjectError(f'{cmakelists} is not a CMakeLists.txt')
    if shutil.which('cmake') is None:
        raise ProjectError('cmake is not installed, so the project cannot describe itself')

    root = cmakelists.parent
    build_dir = Path(build_dir).resolve()
    build_dir.mkdir(parents=True, exist_ok=True)

    logging.info('configuring %s to obtain its compilation database', cmakelists)
    proc = subprocess.run(
        ['cmake', '-S', str(root), '-B', str(build_dir), '-DCMAKE_EXPORT_COMPILE_COMMANDS=ON'],
        capture_output=True,
        text=True,
    )
    database = build_dir / 'compile_commands.json'
    if not database.is_file():
        raise ProjectError(
            f'CMake produced no compile_commands.json for {cmakelists}:\n'
            + (proc.stderr or proc.stdout)[-2000:]
        )

    translation_units = sources_from(database, root)
    headers, check_command = scan_headers(database, root, translation_units)
    return Project(
        cmakelists=cmakelists,
        root=root,
        build_dir=build_dir,
        compilation_database=database,
        sources=translation_units + headers,
        check_command=check_command,
    )


def sources_from(database: Path, root: Path) -> list[Path]:
    """The translation units the project actually compiles, and nothing else.

    Files outside the project root are dependencies, not the thing under
    reduction: deleting from a system header or a vendored library would change
    what the reduction means and would not survive a rebuild anyway.
    """
    try:
        entries = json.loads(database.read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise ProjectError(f'cannot read {database}: {e}')

    seen = []
    for entry in entries:
        try:
            path = Path(entry['file'])
        except (KeyError, TypeError):
            continue
        if not path.is_absolute():
            path = (Path(entry.get('directory', root)) / path).resolve()
        if not path.is_file():
            continue
        if root not in path.parents:
            continue
        if path not in seen:
            seen.append(path)

    if not seen:
        raise ProjectError(
            f'{database} names no source file under {root}; there is nothing to reduce'
        )
    return seen


def scan_headers(
    database: Path, root: Path, translation_units: list[Path], jobs: int = 0
) -> tuple[list[Path], dict[str, list[str]]]:
    """Ask the compiler what this project is made of.

    compile_commands.json names translation units and nothing else, so a
    reduction driven from it alone can only touch .c and .cpp files. For C++
    that leaves most of the code untouched -- templates, inline functions,
    whole class definitions live in headers -- and what survives such a
    reduction cannot honestly be called "the code responsible", because only
    the part of it that happens to live in a .cpp was ever offered for
    deletion.

    Nothing has to be guessed. Every compiler answers this exactly, with the
    flags the build uses: -M lists what a translation unit includes. The same
    answer also says which unit to compile in order to check a change to a
    given header, which is what makes a cheap check possible for headers at
    all.
    """
    try:
        entries = json.loads(database.read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise ProjectError(f'cannot read {database}: {e}')

    wanted = {str(p) for p in translation_units}
    commands = []
    for entry in entries:
        path = Path(entry.get('file', ''))
        if not path.is_absolute():
            path = (Path(entry.get('directory', root)) / path).resolve()
        if str(path) in wanted:
            commands.append((str(path), entry))

    check_command: dict[str, list[str]] = {}
    for path, entry in commands:
        check_command[path] = _flags_only(shlex.split(entry.get('command', ''))) + [path]

    if not commands:
        return [], check_command

    def dependencies(item):
        path, entry = item
        argv = _flags_only(shlex.split(entry.get('command', '')))
        proc = subprocess.run(
            argv + ['-M', '-MG', path],
            cwd=entry.get('directory', str(root)),
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            # A unit that will not even preprocess tells us nothing about its
            # headers; that is not a reason to abandon the rest of them.
            logging.debug('cannot list the dependencies of %s', path)
            return path, argv, set()
        _, _, listed = proc.stdout.replace('\\\n', ' ').partition(':')
        return path, argv, {x for x in listed.split() if x != '\\'}

    headers: list[Path] = []
    seen = set()
    with ThreadPoolExecutor(max_workers=jobs or (os.cpu_count() or 1)) as pool:
        for unit, argv, deps in pool.map(dependencies, commands):
            for dep in sorted(deps):
                path = Path(dep)
                if not path.is_absolute():
                    path = (root / path).resolve()
                key = str(path)
                if key in wanted or key in seen:
                    continue
                if not path.is_file() or root not in path.parents:
                    continue
                seen.add(key)
                headers.append(path)
                # Any unit that includes this header will do: changing the
                # header changes what that unit sees.
                check_command[key] = argv + [unit]

    logging.info('%d headers of this project are reducible too', len(headers))
    return headers, check_command


def _flags_only(argv: list[str]) -> list[str]:
    """The compile command without its output and input, so it can be re-asked."""
    out, skip = [], 0
    for i, a in enumerate(argv):
        if skip:
            skip -= 1
            continue
        if a == '-o':
            skip = 1
            continue
        if a == '-c' or a.endswith(('.c', '.cc', '.cpp', '.cxx', '.C')):
            continue
        if a == '-Winvalid-pch':
            continue
        if a == '-Xclang' and i + 1 < len(argv) and argv[i + 1] in ('-include-pch', '-include'):
            skip = 3
            continue
        out.append(a)
    return out


def stage(project: 'Project', staging: Path) -> Path:
    """Lay the reducible files out as one tree, so they reduce together.

    A reduction that takes the files one at a time can only ever accept a change
    that is interesting on its own -- and the changes that matter in C++ are not
    like that. Removing a function means removing its declaration in a header
    and its uses in every unit that calls it; neither half compiles alone, so
    both are rejected and the code stays, though nothing needs it. The same goes
    for a field, a template parameter, a virtual method.

    Handing C-Vise one directory instead of a list of files removes that
    limitation without inventing anything: a single candidate can then carry
    edits in several files at once, and the existing folding machinery combines
    discoveries from different files into one.

    Only the reducible files are staged. The project's own directory holds a
    great deal that must not be touched -- .git, build trees, fixtures, the
    CMakeLists.txt itself -- and a reducer pointed at the whole thing would
    happily start deleting from all of it.
    """
    staging = Path(staging)
    if staging.exists():
        shutil.rmtree(staging)
    for source in project.sources:
        target = staging / source.relative_to(project.root)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    logging.info('%d files staged for reduction under %s', len(project.sources), staging)
    return staging


def publish(project: 'Project', staging: Path) -> int:
    """Put what the reduction produced back into the project.

    The staged tree is where the reduction happens; the project is what the user
    came with. Copying back only what differs keeps every untouched file's
    timestamp, which is what the user's build depends on.
    """
    published = 0
    for source in project.sources:
        staged = staging / source.relative_to(project.root)
        if not staged.is_file():
            continue
        if staged.read_bytes() == source.read_bytes():
            continue
        shutil.copyfile(staged, source)
        published += 1
    return published
