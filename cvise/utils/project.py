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
import re
import subprocess
import time
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
    # For a translation unit, the object the build produces from it, as CMake
    # recorded it. A header has none, and that is exactly the difference that
    # matters when something asks whether the build would notice a file going
    # away: a unit is named in the CMakeLists.txt and cannot be removed without
    # editing it, a header is reached through an #include and can.
    output: dict[str, str]


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

    # Resolved, because it is compared against resolved paths everywhere below,
    # and "cvise ../project/CMakeLists.txt" would otherwise put every file of
    # the project outside its own root.
    root = cmakelists.parent.resolve()
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
        output=outputs_from(database, root),
    )


def outputs_from(database: Path, root: Path) -> dict[str, str]:
    """What the build produces from each file it compiles.

    CMake records it, and it is the only thing in the database that says a file
    is a translation unit rather than something a translation unit reads. That
    distinction decides whether deleting the file is a reduction or a candidate
    that cannot be built: the build names its units in the CMakeLists.txt, which
    is not under reduction, so a missing unit is not a smaller project but a
    manifest ninja refuses to load.
    """
    try:
        entries = json.loads(database.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    outputs: dict[str, str] = {}
    for entry in entries:
        try:
            path = Path(entry['file'])
            produced = entry['output']
        except (KeyError, TypeError):
            continue
        if not path.is_absolute():
            path = (Path(entry.get('directory', root)) / path).resolve()
        outputs[str(path)] = produced
    return outputs


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
                # Resolved whatever it looked like. The compiler echoes a
                # dependency the way the include path spelled it, so a header
                # reached through -Icpp/test/../fixtures arrives as
                # ".../cpp/test/../fixtures/x.inc" -- absolute, and a different
                # string from the ".../cpp/fixtures/x.inc" that everything else
                # in this program calls the same file.
                #
                # Two spellings of one file is not untidiness, it is a deleted
                # file: the staged tree holds the normalised name, so the job's
                # copy has nothing under the other one, and the overlay dutifully
                # records the difference as "this file is gone" -- whiting out a
                # fixture that was never touched. C-Vise then refuses to start,
                # saying the project is not interesting, on a project that
                # builds perfectly well.
                path = Path(dep)
                if not path.is_absolute():
                    path = root / path
                path = path.resolve()
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


def baseline_build(project: 'Project') -> float:
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

    Returns how long it took, because that is the only honest basis for a
    deadline. A candidate can never need more work than a build from nothing,
    and how long that is a property of the project, not of C-Vise.
    """
    started = time.monotonic()
    logging.info('building the project once, so that each candidate only rebuilds what it changed')
    proc = subprocess.run(
        ['cmake', '--build', str(project.build_dir)],
        capture_output=True,
        text=True,
    )
    took = time.monotonic() - started
    if proc.returncode != 0:
        logging.warning(
            'the project does not build as it stands, so every candidate will have to build it '
            'from nothing:\n%s',
            (proc.stderr or proc.stdout)[-2000:],
        )
    logging.info('the project builds from nothing in %.0f s on this machine', took)
    return took


def has_test(project: 'Project', name: str) -> bool:
    """Does the project register a ctest test by exactly this name?

    Asked of ctest itself rather than reconstructed from the CMakeLists, since
    a test can be registered by gtest_discover_tests at build time and never
    appear in any file a human wrote.
    """
    proc = subprocess.run(
        ['ctest', '--test-dir', str(project.build_dir), '-N', '-R', f'^{re.escape(name)}$'],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return False
    match = re.search(r'Total Tests:\s*(\d+)', proc.stdout)
    return bool(match) and int(match.group(1)) > 0


def tests_of(project: 'Project') -> list[str]:
    """Every test the project registers, for telling the user what they could have named."""
    proc = subprocess.run(
        ['ctest', '--test-dir', str(project.build_dir), '-N'],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return []
    return re.findall(r'^\s*Test\s+#\d+:\s*(\S+)\s*$', proc.stdout, re.M)


def open_jobserver(path: Path, tokens: int) -> int:
    """One pool of build tokens for every job to draw from.

    ninja is never given a -j. The machine is bounded instead by the thing that
    was actually being multiplied: not how parallel one build is, but how many
    compilers exist across all of them at once. A jobserver is the standard way
    to say that -- ninja 1.13 is a client of it -- and it is adaptive in the way
    a queue is not. A candidate that changed one file compiles one object and
    holds one token; the rest of the pool stays available for other candidates,
    so the machine fills up instead of waiting behind a build that cannot use
    it.

    MEASURED, eight builds at once on this 88-core machine: 192 compilers alive
    with no jobserver, 12 with a pool of 4, 24 with a pool of 16. The rule is
    that each ninja gets one implicit token of its own and the pool is shared on
    top, so the total is (builds + tokens) -- which is why the caller sizes the
    pool as cores minus jobs.

    The fifo lives outside every tree the overlay covers. Inside one it would be
    copied into each job's delta, every job would draw from a private pool of
    its own, and the machine would be oversubscribed exactly as before while
    everything looked correct.

    Returns the descriptor that holds the pool open; it must stay open for the
    whole run, because the tokens live in the pipe and nowhere else.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    os.mkfifo(path, 0o600)
    # O_RDWR so that the pool never sees end-of-file when no build holds it.
    fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
    os.write(fd, b'x' * max(1, tokens))
    logging.info('%d build tokens for every candidate to share', max(1, tokens))
    return fd


def check_script(project: 'Project', name: str, path: Path) -> Path:
    """Build the project, then run the one test -- and insist that it ran.

    A ctest test, not a build target. A target only says the code still
    compiles and links, and a target that runs something says only that the
    something exited zero -- which a test runner does when the case it was
    asked for no longer exists. MEASURED with GoogleTest: a filter matching
    nothing prints "[  PASSED  ] 0 tests." and exits 0, so the cheapest way to
    satisfy such a criterion is to delete the test, and a reduction finds it.

    ctest knows the difference between a test that passed and a test that was
    not there -- but only when told to. MEASURED: `ctest -R nomatch` exits 0
    and prints "No tests were found!!!"; with --no-tests=error it exits 8. So
    the flag is not a nicety here, it is the whole reason for using ctest.

    The build comes first because ctest does not build. Without it a candidate
    would be judged by the binary the previous one left, which is the class of
    wrong answer this program spends most of its care avoiding.

    The build is never given a -j. Not one, not a computed share of the cores,
    not any number: the build gets the machine. Dividing it treats a symptom --
    when N simultaneous builds do not fit, what does not fit is N simultaneous
    builds, and shrinking each of them only hides that while making every
    candidate slower. Both were tried here and both were wrong: with no -j the
    load reached 230 and the cgroup OOM-killed cc1plus, and with -j 1 the
    machine sat half idle while candidates that touched many files timed out
    one after another.

    Nor are the builds serialised. One at a time is wrong in the other
    direction: a candidate that changed a single file compiles a single object
    and holds the whole machine while every other candidate waits for a build
    that cannot use it.

    What bounds them is a shared pool of tokens -- see open_jobserver. Every
    build draws from it and returns what it does not need, so a build with one
    file to compile takes one token and the machine fills up with other
    candidates instead of idling behind it.

    The output is not discarded. C-Vise captures it and shows it when the
    project turns out not to be interesting to begin with -- and a message that
    says the test failed and then prints nothing is the worst thing this
    program can say, because the one run that has to be diagnosed is the one
    that never started.
    """
    build = shlex.quote(str(project.build_dir))
    path.write_text(
        '#!/bin/sh\n'
        '# Generated by C-Vise. The project defines both the build and the check.\n'
        f'cmake --build {build} 2>&1 || exit $?\n'
        f'exec ctest --test-dir {build} -R {shlex.quote("^" + re.escape(name) + "$")} '
        '--no-tests=error --output-on-failure 2>&1\n'
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
        entry = {
            'directory': str(project.build_dir),
            'file': str(staged),
            'command': ' '.join(shlex.quote(a) for a in flags),
        }
        # Carried through, not invented: it says the build compiles this file
        # into that object, and so that the build would miss it if it went
        # away. A header has none, which is how the two are told apart by
        # anything reading this database.
        produced = project.output.get(str(source))
        if produced:
            entry['output'] = produced
        entries.append(entry)

    database = project.build_dir / 'cvise_compile_commands' / 'compile_commands.json'
    database.parent.mkdir(parents=True, exist_ok=True)
    database.write_text(json.dumps(entries, indent=1))
    logging.info('%d staged files described to the semantic passes', len(entries))
    return database
