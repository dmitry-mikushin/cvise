#!/usr/bin/env python3

import argparse
import datetime
import importlib.util
import logging
import multiprocessing
import multiprocessing.forkserver
import os
import os.path
import shutil
import sys
import tempfile
import time
from contextlib import nullcontext
from itertools import chain
from pathlib import Path

# If the cvise modules cannot be found
# add the known install location to the path
destdir = os.getenv('DESTDIR', '')
if importlib.util.find_spec('cvise') is None:
    sys.path.append('@CMAKE_INSTALL_FULL_DATADIR@')
    sys.path.append(destdir + '@CMAKE_INSTALL_FULL_DATADIR@')

import chardet  # noqa: E402
import psutil  # noqa: E402

from cvise.cvise import CVise  # noqa: E402
from cvise.passes.abstract import AbstractPass  # noqa: E402
from cvise.utils import memory  # noqa: E402
from cvise.utils import project as project_utils  # noqa: E402
from cvise.utils import statistics, testing  # noqa: E402
from cvise.utils.error import CViseError, MissingPassGroupsError  # noqa: E402
from cvise.utils.externalprograms import find_external_programs  # noqa: E402


class DeltaTimeFormatter(logging.Formatter):
    def format(self, record):  # noqa: A003
        delta = str(datetime.timedelta(seconds=int(record.relativeCreated / 1000)))
        # pad with one more zero
        if delta[1] == ':':
            delta = '0' + delta
        record.delta = delta
        return super().format(record)


script_path = os.path.dirname(os.path.realpath(__file__))


def get_share_dir():
    """Where this C-Vise keeps its pass schedules and other data.

    Beside this script first, and only then the installed copy. The other order
    means a build tree runs its own Python against the pass groups of whatever
    was installed last -- so a pass added to the schedule is simply not there,
    with nothing to say why, and the same trap as the overlay library had.
    """
    share_dirs = [
        os.path.join(script_path, '@cvise_SHARE_DIR_SUFFIX@'),
        os.path.join('@CMAKE_INSTALL_FULL_DATADIR@', '@cvise_PACKAGE@'),
        destdir + os.path.join('@CMAKE_INSTALL_FULL_DATADIR@', '@cvise_PACKAGE@'),
    ]

    for d in share_dirs:
        if os.path.isdir(d):
            return d

    raise CViseError('Cannot find cvise module directory!')


def get_pass_group_path(name):
    return os.path.join(get_share_dir(), 'pass_groups', name + '.json')


def get_available_pass_groups():
    pass_group_dir = os.path.join(get_share_dir(), 'pass_groups')

    if not os.path.isdir(pass_group_dir):
        raise MissingPassGroupsError()

    group_names = []

    for entry in os.listdir(pass_group_dir):
        path = os.path.join(pass_group_dir, entry)

        if not os.path.isfile(path):
            continue

        try:
            pass_group_dict = CVise.load_pass_group_file(path)
            CVise.parse_pass_group_dict(pass_group_dict, set(), None, None, None, None, None, None, None)
        except MissingPassGroupsError:
            logging.warning(f'Skipping file {path}. Not valid pass group.')
        else:
            (name, _) = os.path.splitext(entry)
            group_names.append(name)

    return group_names


def get_available_cores():
    try:
        # try to detect only physical cores, ignore HyperThreading
        # in order to speed up parallel execution
        core_count = psutil.cpu_count(logical=False)
        if not core_count:
            core_count = psutil.cpu_count(logical=True)
        # respect affinity
        try:
            psutil_ret = psutil.Process().cpu_affinity()
            assert isinstance(psutil_ret, list)
        except AttributeError:
            return core_count
        affinity = len(psutil_ret)
        assert affinity >= 1

        if core_count:
            core_count = min(core_count, affinity)
        else:
            core_count = affinity
        return core_count
    except NotImplementedError:
        return 1


EPILOG_TEXT = f"""
available shortcuts:
  S - skip execution of the current pass
  D - toggle --print-diff option

For bug reporting instructions, please use:
{CVise.Info.PACKAGE_URL}
"""


def main():
    parser = argparse.ArgumentParser(
        description='C-Vise',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=EPILOG_TEXT,
    )
    parser.add_argument(
        '--n',
        '-n',
        type=int,
        default=get_available_cores(),
        help='Number of cores to use; C-Vise tries to automatically pick a good setting but its choice may be too low or high for your situation',
    )
    parser.add_argument(
        '--tidy',
        action='store_true',
        help='Do not make a backup copy of each file to reduce as file.orig',
    )
    parser.add_argument(
        '--shaddap',
        action='store_true',
        help='Suppress output about non-fatal internal errors',
    )
    parser.add_argument(
        '--die-on-pass-bug',
        action='store_true',
        help='Terminate C-Vise if a pass encounters an otherwise non-fatal problem',
    )
    parser.add_argument(
        '--sllooww',
        action='store_true',
        help='Try harder to reduce, but perhaps take a long time to do so',
    )
    parser.add_argument(
        '--also-interesting',
        metavar='EXIT_CODE',
        type=int,
        help='A process exit code (somewhere in the range 64-113 would be usual) that, when returned by the interestingness test, will cause C-Vise to save a copy of the variant',
    )
    parser.add_argument(
        '--debug',
        action='store_true',
        help='Print debug information (alias for --log-level=DEBUG)',
    )
    parser.add_argument(
        '--log-level',
        type=str,
        choices=['INFO', 'DEBUG', 'WARNING', 'ERROR'],
        default='INFO',
        help='Define the verbosity of the logged events',
    )
    parser.add_argument(
        '--log-file',
        type=str,
        help='Log events into LOG_FILE instead of stderr. New events are appended to the end of the file',
    )
    parser.add_argument(
        '--no-give-up',
        action='store_true',
        help=f"Don't give up on a pass that hasn't made progress for {testing.TestManager.GIVEUP_CONSTANT} iterations",
    )
    parser.add_argument(
        '--print-diff',
        action='store_true',
        help='Show changes made by transformations, for debugging',
    )
    parser.add_argument(
        '--save-temps',
        action='store_true',
        help="Don't delete /tmp/cvise-xxxxxx directories on termination",
    )
    parser.add_argument(
        '--skip-initial-passes',
        action='store_true',
        help='Skip initial passes (useful if input is already partially reduced)',
    )
    parser.add_argument(
        '--skip-interestingness-test-check',
        '-s',
        action='store_true',
        help='Skip initial interestingness test check',
    )
    parser.add_argument(
        '--remove-pass',
        help='Remove all instances of the specified passes from the schedule (comma-separated)',
    )
    parser.add_argument('--start-with-pass', help='Start with the specified pass')
    parser.add_argument(
        '--no-timing',
        action='store_true',
        help='Do not print timestamps about reduction progress',
    )
    parser.add_argument(
        '--timestamp',
        action='store_true',
        help='Print timestamps instead of relative time from a reduction start',
    )
    parser.add_argument(
        '--timeout',
        type=int,
        nargs='?',
        default=None,
        help='Interestingness test timeout in seconds. By default it is measured rather than '
        'guessed: a candidate can never need more work than building the project from nothing, '
        'so the deadline is that, scaled for the share of the machine one job gets',
    )
    parser.add_argument('--no-cache', action='store_true', help="Don't cache behavior of passes")
    parser.add_argument(
        '--skip-key-off',
        action='store_true',
        help="Disable skipping the rest of the current pass when 's' is pressed",
    )
    parser.add_argument(
        '--max-improvement',
        metavar='BYTES',
        type=int,
        help='Largest improvement in file size from a single transformation that C-Vise should accept (useful only to slow C-Vise down)',
    )
    passes_group = parser.add_mutually_exclusive_group()
    passes_group.add_argument(
        '--pass-group',
        type=str,
        choices=get_available_pass_groups(),
        help='Set of passes used during the reduction',
    )
    passes_group.add_argument('--pass-group-file', type=str, help='JSON file defining a custom pass group')
    parser.add_argument(
        '--clang-delta-std',
        type=str,
        choices=['c++98', 'c++11', 'c++14', 'c++17', 'c++20', 'c++2b'],
        help='Specify clang_delta C++ standard, it can rapidly speed up all clang_delta passes',
    )
    parser.add_argument(
        '--clang-delta-preserve-routine',
        type=str,
        help='Preserve the given function in replace-function-def-with-decl clang delta pass',
    )
    parser.add_argument(
        '--not-c',
        action='store_true',
        help="Don't run passes that are specific to C and C++, use this mode for reducing other languages",
    )
    parser.add_argument(
        '--renaming',
        action='store_true',
        help='Enable all renaming passes (that are disabled by default)',
    )
    parser.add_argument('--list-passes', action='store_true', help='Print all available passes and exit')
    parser.add_argument(
        '--version',
        action='version',
        version=CVise.Info.PACKAGE_STRING
        + (f' ({CVise.Info.GIT_VERSION})' if CVise.Info.GIT_VERSION != 'unknown' else ''),
    )
    parser.add_argument(
        '--to-utf8',
        action='store_true',
        help='Convert any non-UTF-8 encoded input file to UTF-8',
    )
    parser.add_argument(
        '--skip-after-n-transforms',
        type=int,
        help='Skip each pass after N successful transformations',
    )
    parser.add_argument(
        '--under',
        metavar='SUBTREE',
        help='reduce only what is under this directory of the project, relative to the '
        'CMakeLists.txt. Configuring and reducing are not the same scope: a component whose '
        'tests only exist when the whole tree is configured still wants only itself reduced, '
        'and CMake has no opinion on which part is under study. MEASURED on one such tree: '
        '3651 translation units configured, 378 of them the component in question, and the '
        'other 3273 cost every job 6375 files and 1485 directories to copy before it could '
        'start',
    )
    parser.add_argument(
        'project',
        metavar='CMAKELISTS',
        nargs='?',
        help='CMakeLists.txt of the project to reduce. C-Vise runs CMake once on it to obtain '
        'compile_commands.json, and takes everything else from there: which files exist and '
        'what flags each one is compiled with',
    )
    parser.add_argument(
        'test',
        metavar='TEST',
        nargs='?',
        help='name of the ctest test that decides whether a variant of the project is still '
        'interesting: it is interesting if the project builds and that one test passes. A test, '
        'not a build target, because a target says only that something exited zero -- and a test '
        'runner exits zero when the case it was asked for no longer exists, so the cheapest way '
        'to satisfy such a criterion is to delete the test',
    )
    parser.add_argument(
        '--stopping-threshold',
        default=1.0,
        type=float,
        help='CVise will stop reducing a test case once it has reduced by this fraction of its original size.  Between 0.0 and 1.0.',
    )

    args = parser.parse_args()


    if not args.list_passes and (not args.project or not args.test):
        parser.error('the following arguments are required: CMAKELISTS, TEST')

    log_config = {}

    log_format = '%(levelname)s %(message)s'
    if not args.no_timing:
        if args.timestamp:
            log_format = '%(asctime)s ' + log_format
        else:
            log_format = '%(delta)s ' + log_format

    if args.debug:
        log_config['level'] = logging.DEBUG
    else:
        log_config['level'] = getattr(logging, args.log_level.upper())

    logging.getLogger().setLevel(log_config['level'])
    formatter = DeltaTimeFormatter(log_format)
    root_logger = logging.getLogger()

    if args.log_file is not None:
        file_handler = logging.FileHandler(args.log_file)
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)
    else:
        syslog = logging.StreamHandler()
        syslog.setFormatter(formatter)
        root_logger.addHandler(syslog)

    # After logging is configured, not before: the first logging call installs a
    # handler of its own, and every message would then be printed twice.
    if not args.list_passes:
        memory.bound_or_warn()

    # One action, because there is one thing to do: reduce the project the
    # CMakeLists.txt describes.
    do_reduce(args)

    logging.shutdown()


def do_reduce(args):
    pass_options = set()

    if sys.platform == 'win32':
        pass_options.add(AbstractPass.Option.windows)

    if args.sllooww:
        pass_options.add(AbstractPass.Option.slow)


    if args.pass_group is not None:
        pass_group_file = get_pass_group_path(args.pass_group)
    elif args.pass_group_file is not None:
        pass_group_file = args.pass_group_file
    else:
        pass_group_file = get_pass_group_path('all')

    external_programs = find_external_programs()

    pass_group_dict = CVise.load_pass_group_file(pass_group_file)

    if args.list_passes:
        # This question is about C-Vise, not about any project, so it must not
        # be made to configure one.
        pass_group = CVise.parse_pass_group_dict(
            pass_group_dict,
            pass_options,
            external_programs,
            args.remove_pass,
            args.clang_delta_std,
            args.clang_delta_preserve_routine,
            None,
            args.not_c,
            args.renaming,
        )
        print('Available passes:')
        for cat in CVise.PASS_CATEGORIES:
            if cat.name in pass_group:
                print(cat.log_title)
                for p in pass_group[cat.name]:
                    print(str(p))
        sys.exit(0)

    # Everything the reduction needs to know about the project comes from one
    # file, and it is not the CMakeLists.txt -- it is the compile_commands.json
    # CMake writes from it. Asking the user for the file list or the flags on
    # top of that is asking them to repeat what CMake already knows, and every
    # such question is another way for the answer to disagree with the build.
    # CMake needs somewhere to write; that somewhere is C-Vise's, it holds
    # nothing but the database, and it is removed when the run ends. Leaving it
    # behind would litter the user's TMPDIR once per invocation.
    cmake_dir = Path(tempfile.mkdtemp(prefix='cvise-cmake-'))
    staging_dir = Path(tempfile.mkdtemp(prefix='cvise-staging-'))
    # Both directories exist from this line on, and so does the promise to
    # remove them. Setting a project up takes real time -- a CMake configure, a
    # dependency scan, a first build -- and a Control-C during any of it used to
    # leave two directories behind in the user's TMPDIR, one of them a whole
    # build tree, with nothing in them to say what they were.
    project = None
    staged = None
    script = None
    try:
        project = project_utils.configure(Path(args.project), cmake_dir, args.under)
        logging.info(
            # Not "translation units", which is what this said while counting
            # units and headers together -- so a run of 376 units and 513
            # headers announced 889 units, and the number moved between runs
            # for a reason that had nothing to do with the units.
            '%s: %d reducible files under %s',
            project.compilation_database,
            len(project.sources),
            project.root,
        )
        # One directory, not a list of files: a candidate can then carry edits in
        # several files at once, which is the only way the changes that matter in
        # C++ -- a declaration and its uses -- can ever be accepted, since neither
        # half of such a change compiles on its own.
        # Where the user was standing. Anything saved for them goes here, not into
        # the staged copy C-Vise is about to work in and then delete.
        launch_dir = Path.cwd()
        staged = project_utils.stage(project, staging_dir / project.root.name)

        # Built once, here, from the sources as they are. Every job then gets
        # this tree copy-on-write and only compiles what its own candidate
        # changed; without it each of them would build the project from nothing.
        # It runs before the token pool exists, because it is alone on the
        # machine and should have all of it.
        #
        # What is built is what the named test needs, worked out from the test
        # itself, and not the default target: on a large project the default
        # target is the whole tree, and a component the criterion never touches
        # failing to compile would stop a reduction that has nothing to do with
        # it.
        baseline_seconds, build_target = project_utils.build_for_test(project, args.test)

        # One pool of build tokens shared by every candidate from here on. ninja
        # is a client of it and is never given a -j, so a build that can only
        # compile one file takes one token and the machine fills with other
        # candidates rather than idling behind it. Sized cores minus jobs
        # because each ninja gets one implicit token of its own on top of the
        # pool -- MEASURED: eight builds with a pool of 16 ran 24 compilers.
        jobserver_fd = project_utils.open_jobserver(
            staging_dir / 'jobserver', (os.cpu_count() or 1) - max(1, args.n)
        )
        os.environ['MAKEFLAGS'] = f'--jobserver-auth=fifo:{staging_dir / "jobserver"}'
        if args.timeout is None:
            # A fixed deadline is applied to candidates whose cost differs by a
            # factor of hundreds -- one changed file against every file of the
            # project -- so the expensive ones are killed for being expensive
            # rather than judged. MEASURED on ns-projection: the project builds
            # from nothing in 145 s on 88 cores, which is 53 minutes for a job
            # holding two of them, against a 300 s deadline; every candidate
            # from a pass that rewrites whole files timed out, always, and the
            # passes were eventually disabled for it.
            # The worst case is a candidate that has to rebuild everything
            # while every other job is doing the same: the pool is shared, so
            # its share of the machine is what one job in n gets.
            args.timeout = max(300, int(baseline_seconds * max(1, args.n) * 1.5))
            logging.info(
                'a candidate has %d s: the project builds from nothing in %.0f s with the '
                'machine to itself, and up to %d candidates share it',
                args.timeout, baseline_seconds, args.n,
            )
        if not project_utils.has_test(project, args.test):
            known = project_utils.tests_of(project)
            sys.exit(
                f"the project registers no ctest test called '{args.test}'"
                + (f'; it registers: {", ".join(known)}' if known else
                   '; it registers none at all, so add enable_testing() and add_test() -- or '
                   'gtest_discover_tests() -- to the project')
            )
        # The database clang_delta is handed has to name the files clang_delta is
        # handed. It was given the project's paths while every pass works on the
        # staged copy, so the lookup found nothing and every semantic pass -- the
        # ones that delete functions, classes and templates -- exited 255 on every
        # file of every project.
        database = project_utils.database_for(project, staged)
        # The user names a target, not a script: the project already describes how
        # it is built and how it is checked, and asking for both again in shell is
        # asking for two descriptions that will disagree.
        args.interestingness_test = str(
            project_utils.check_script(project, args.test, staging_dir / 'check.sh', build_target)
        )
        os.chdir(staged.parent)
        test_cases = [Path(staged.name)]

        pass_group = CVise.parse_pass_group_dict(
            pass_group_dict,
            pass_options,
            external_programs,
            args.remove_pass,
            args.clang_delta_std,
            args.clang_delta_preserve_routine,
            str(database),
            args.not_c,
            args.renaming,
        )

        pass_statistic = statistics.PassStatistic()

        if args.start_with_pass:
            pass_names = [str(p) for p in chain(*pass_group.values())]
            if args.start_with_pass not in pass_names:
                print(
                    f'Cannot find pass called "{args.start_with_pass}". '
                    'Please use --list-passes to get a list of available passes.'
                )
                sys.exit(1)

        if args.to_utf8:
            for test_case in test_cases:
                encoding = chardet.detect(test_case.read_bytes())['encoding']
                if encoding not in ('ascii', 'utf-8'):
                    logging.info(f'Converting {test_case} file ({encoding} encoding) to UTF-8')
                    with open(test_case, encoding=encoding) as f:
                        data = f.read()
                    test_case.write_text(data)

        assert args.interestingness_test

        def adopt_new_best():
            """Move the tree every job reads through to the reduction's new state.

            A job's delta holds what differs between its candidate and the
            project, and the project only learned the answer at the very end --
            so after the first accepted reduction the difference was the whole
            reduction, not the candidate. MEASURED on ns-projection: 1246 of
            1340 files in every delta, a full rebuild for every candidate,
            gigabytes of scratch, and a candidate that had to be valid in 1246
            places at once to be accepted.

            Publishing here costs one build of what actually changed, once per
            accepted reduction, and those are rare. It also means the answer is
            on disk continuously rather than only when the run ends tidily --
            which, after a run was lost to a rebuild of C-Vise underneath it, is
            not a small thing.
            """
            written = project_utils.publish(project, staged)
            logging.info('%d files written back; rebuilding what the jobs read', written)
            project_utils.baseline_build(project, build_target)

        # Use forkserver to avoid potential problems due to multi-threading, and to reduce the memory usage in workers.
        # Preloading is used as a speedup, so that every worker doesn't need to execute all import statements on startup.
        multiprocessing.set_start_method('forkserver')
        multiprocessing.set_forkserver_preload(['__main__'] + list(sys.modules.keys()))
        multiprocessing.forkserver.ensure_running()

        with testing.TestManager(
            pass_statistic,
            Path(args.interestingness_test),
            args.timeout,
            args.save_temps,
            test_cases,
            args.n,
            args.no_cache,
            args.skip_key_off,
            args.shaddap,
            args.die_on_pass_bug,
            args.print_diff,
            args.max_improvement,
            args.no_give_up,
            args.also_interesting,
            args.start_with_pass,
            args.skip_after_n_transforms,
            args.stopping_threshold,
            # Reject what the compiler alone can reject, before paying for a
            # build: the flags come from the project's own database.
            check_command={str(k): v for k, v in project.check_command.items()},
            precheck_timeout=args.timeout,
            overlay_root=project.root,
            # The build tree is isolated per job as well. It is where the
            # verdict about a candidate is computed, and sharing it means a job
            # is answered with whatever the previous one built there.
            overlay_build_dir=project.build_dir,
            overlay_files=project.sources,
            launch_dir=launch_dir,
            on_new_best=adopt_new_best,
        ) as test_manager:
            reducer = CVise(test_manager, args.skip_interestingness_test_check)

            reducer.tidy = args.tidy

            # Track runtime
            time_start = time.monotonic()

            try:
                reducer.reduce(pass_group, skip_initial=args.skip_initial_passes)
            except CViseError as err:
                print(err)
                sys.exit(1)

        time_stop = time.monotonic()
        with open(args.log_file, 'ab') if args.log_file else nullcontext(sys.stderr.buffer) as fs:
            fs.write(b'===< PASS statistics >===\n')
            fs.write(
                (
                    '  %-60s %14s %8s %8s %8s %8s %15s\n'
                    % (
                        'pass name',
                        'bytes reduced',
                        'time (s)',
                        'time (%)',
                        'worked',
                        'failed',
                        'total executed',
                    )
                ).encode()
            )

            for pass_name, pass_data in pass_statistic.sorted_results:
                fs.write(
                    (
                        '  %-60s %14d %8.2f %8.2f %8d %8d %15d\n'
                        % (
                            pass_name,
                            -pass_data.total_size_delta,
                            pass_data.total_seconds,
                            100.0 * pass_data.total_seconds / (time_stop - time_start),
                            pass_data.worked,
                            pass_data.failed,
                            pass_data.totally_executed,
                        )
                    ).encode()
                )
            fs.write(b'\n')

            if not args.no_timing:
                fs.write(f'Runtime: {round(time_stop - time_start)} seconds\n'.encode())

            fs.write(b'Reduced test-cases:\n\n')
            for test_case in sorted(test_cases):
                is_dir = test_case.is_dir()
                fs.write(f'--- {test_case} ---\n'.encode())
                paths = (
                    sorted(p for p in test_case.rglob('*') if not p.is_dir() and not p.is_symlink())
                    if is_dir
                    else [test_case]
                )
                for path in paths:
                    if is_dir:
                        fs.write(f'\n//--- {path}\n'.encode())
                    fs.write(path.read_bytes())
                    fs.write(b'\n')
    finally:
        # What the reduction produced belongs in the project the user came with;
        # only the files that actually changed are written, so everything else
        # keeps the timestamp the user's build depends on.
        #
        # There may be nothing to write back: an interrupt during the CMake
        # configure or the first build leaves no staged tree, and the two
        # directories still have to go.
        if project is not None and staged is not None:
            published = project_utils.publish(project, staged)
            logging.info('%d reduced files written back to %s', published, project.root)
        if script:
            os.unlink(script.name)
        shutil.rmtree(cmake_dir, ignore_errors=True)
        shutil.rmtree(staging_dir, ignore_errors=True)


if __name__ == '__main__':
    main()
