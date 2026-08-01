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
import shutil
import subprocess
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

    return Project(
        cmakelists=cmakelists,
        root=root,
        build_dir=build_dir,
        compilation_database=database,
        sources=sources_from(database, root),
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
