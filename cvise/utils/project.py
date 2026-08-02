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

    logging.info('configuring %s', cmakelists)
    proc = subprocess.run(
        [
            'cmake',
            '-S', str(root),
            '-B', str(build_dir),
            # Ninja, because this build directory is not configured once and
            # forgotten: it is rebuilt for every candidate, and what makes that
            # affordable is a build system that rebuilds exactly what changed.
            '-G', 'Ninja',
            '-DCMAKE_EXPORT_COMPILE_COMMANDS=ON',
        ],
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
            # The reduction deleted this file. Leaving it in place would undo
            # that silently: the answer the user gets would contain a file the
            # reduction proved unnecessary, and it would differ from the thing
            # that was actually verified.
            if source.is_file():
                source.unlink()
                published += 1
            continue
        if staged.read_bytes() == source.read_bytes():
            continue
        shutil.copyfile(staged, source)
        published += 1
    return published


def baseline_build(project: 'Project') -> None:
    """Build the project once, from the sources as they are.

    Every job gets this directory copy-on-write, so what it finds here is what
    it does not have to build. Without a baseline each job starts from an empty
    build tree and compiles the whole project, which for anything larger than a
    toy is the entire cost of the reduction paid once per candidate. With one, a
    job compiles what its candidate changed and links.

    It also has to be correct, not merely fast: a job reads an object from here
    whenever its own sources are older, so these objects must be the ones the
    pristine sources produce. That is exactly what building here, outside any
    delta, guarantees.

    What is built is the default target, not the one the user named to check
    with. The two are usually the same work, but not always: a check target
    typically runs the program, and running it is neither cheaper here than in a
    job nor of any use to one. Building the check target meant C-Vise began by
    executing the user's check -- which, for a check that waits for something,
    is a reducer that appears to hang before it has printed a line.

    A failure is not fatal. Whether the project as it stands is interesting is
    the sanity check's question, and the user can waive that; this is only the
    work that would otherwise be repeated by every job, so not having it costs
    time and nothing else.
    """
    logging.info('building the project once, so that each candidate only rebuilds what it changed')
    proc = subprocess.run(
        ['cmake', '--build', str(project.build_dir)],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        logging.warning(
            'the project does not build as it stands, so every candidate will have to build it '
            'from nothing:\n%s',
            (proc.stderr or proc.stdout)[-2000:],
        )


def has_target(project: 'Project', target: str) -> bool:
    """Does the project define this target?

    Asked of ninja, not of `cmake --build --target help`: that lists only the
    phony "primary targets", so every executable and library -- which is to say
    the targets a user is most likely to name -- is absent from it. Checking
    against that list rejected `prog` for a project that plainly builds prog.
    """
    proc = subprocess.run(
        ['ninja', '-C', str(project.build_dir), '-t', 'targets', 'all'],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return True  # cannot tell; let the sanity check speak instead
    names = set()
    for line in proc.stdout.splitlines():
        path, _, _ = line.partition(':')
        path = path.strip()
        if not path:
            continue
        names.add(path)
        names.add(Path(path).name)
    return target in names


def links_something(project: 'Project', target: str, depth: int = 3) -> bool:
    """Does building this target resolve symbols?

    It matters a great deal. A target that is only compiled and archived -- a
    static or object library -- never looks for a definition, so deleting a
    function while its callers remain builds perfectly well, looks interesting,
    and the reduction produces a project that does not build. Only linking an
    executable, a shared library or a module asks the question the reduction
    depends on.

    The answer has to be followed through ninja's graph rather than read off a
    name: a CMake target is a phony node pointing at whatever actually produces
    it, so `justcompile: phony` says nothing at all by itself.
    """
    seen: set[str] = set()

    def rule_of(name: str, left: int) -> bool:
        if left <= 0 or name in seen:
            return False
        seen.add(name)
        proc = subprocess.run(
            ['ninja', '-C', str(project.build_dir), '-t', 'query', name],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            return True  # cannot tell; do not cry wolf
        inputs: list[str] = []
        rule = ''
        for line in proc.stdout.splitlines():
            stripped = line.strip()
            if stripped.startswith('input:'):
                rule = stripped.split(':', 1)[1].strip()
            elif line.startswith('    ') and rule and not stripped.startswith(('outputs:', '|')):
                inputs.append(stripped.lstrip('| '))
            elif stripped.startswith('outputs:'):
                break
        if 'STATIC_LIBRARY' in rule or 'OBJECT_LIBRARY' in rule:
            return False
        if 'LINKER' in rule or 'CUSTOM_COMMAND' in rule:
            return True
        if rule == 'phony':
            return any(rule_of(i, left - 1) for i in inputs)
        return False

    return rule_of(target, depth)


def check_script(project: 'Project', target: str, path: Path) -> Path:
    """The interestingness test, written by C-Vise rather than by the user.

    The project already says how it is built and how it is checked -- that is
    what a target is. Asking the user for a shell script on top of that asks
    them to restate it, in another language, with another set of assumptions
    about where the files are and which compiler to call; and the two
    descriptions then disagree the moment the project changes.

    So the test is this: build the target the way the project builds it. Ninja
    rebuilds what the candidate touched and nothing else, and the target's own
    definition decides what "interesting" means -- a library target says the
    code still compiles and links, a custom target that runs something says it
    still behaves.
    """
    path.write_text(
        '#!/bin/sh\n'
        '# Generated by C-Vise. The project defines both the build and the check.\n'
        f'exec cmake --build {shlex.quote(str(project.build_dir))} '
        f'--target {shlex.quote(target)} > /dev/null 2>&1\n'
    )
    path.chmod(0o755)
    return path


def database_for(project: 'Project', staging: Path) -> Path:
    """The compilation database clang_delta is given, naming the files it is given.

    clang_delta refuses to work on a file its database does not name, and it is
    right to: without the project's flags it parses a truncated AST and deletes
    things on no basis at all. But the file it is handed is never the project's
    -- a reduction works on a staged copy -- so a database written in the
    project's paths answers no question anybody asks. It looked correct, it was
    passed on every command line, and every semantic pass exited 255 on every
    file of every project, leaving headers and sources alike to the passes that
    can only delete lines.

    So the database describes the staged tree: one entry per reducible file, at
    the path the reduction actually works on, carrying the flags the build uses
    for it. Headers get an entry too -- a header has no compile command of its
    own, but it has one that exercises it, the unit that includes it, whose
    flags are exactly the context the header is meant to be read in.

    Written into C-Vise's own build directory rather than over CMake's, because
    CMake owns that file and rewrites it whenever the project is reconfigured.
    """
    entries = []
    for source in project.sources:
        command = project.check_command.get(str(source))
        if not command:
            continue
        staged = staging / source.relative_to(project.root)
        # The command may end with a different file -- for a header it is the
        # unit that includes it -- and the entry has to be about this file.
        flags = list(command[:-1]) + [str(staged)]
        entries.append(
            {
                'directory': str(project.build_dir),
                'file': str(staged),
                'command': ' '.join(shlex.quote(a) for a in flags),
            }
        )

    database = project.build_dir / 'cvise_compile_commands' / 'compile_commands.json'
    database.parent.mkdir(parents=True, exist_ok=True)
    database.write_text(json.dumps(entries, indent=1))
    logging.info('%d staged files described to the semantic passes', len(entries))
    return database
