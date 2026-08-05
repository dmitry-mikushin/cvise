"""What the overlay promises, asked as behaviour rather than as a list of symbols.

The gate this replaces asked two questions: did the library load, and does the
stat family see a file that exists only in the delta. Everything else it took on
trust. Three holes were found in a row, each of which passed that gate and each
of which ended a run:

    posix_spawn_file_actions_addopen   a job's child wrote into the shared tree
    __open_2 and the fortified family  sixteen wrappers compiled out entirely
    chdir                              a job could not enter a directory it made

They are one class -- an operation that resolves a path or moves the working
directory -- and a gate made of symbol names is always one failure behind it,
because the name that gets added is the one that already broke.

So what is checked here is the contract, which does not mention syscalls at all:

    a build tool given a delta-only artefact SEES it
    a build tool given a shared artefact the delta overrides sees the DELTA's
    a job's writes land in the delta and never in the shared tree

Both directions matter and they fail separately. Seeing the delta's version of
something proves redirection; NOT seeing the shared version proves the
redirection is not merely additive. A gate that only checked the first would
pass an overlay that showed both and let the build pick whichever it stat'd
first.

Run as a child, because the library is preloaded into the jobs and their
compilers and never into the reducer. Each check prints one line -- name, PASS
or FAIL, and what it saw -- so that a failure names the operation to repair
rather than the fact that something is wrong.
"""

import argparse
import ctypes
import errno
import os
import shutil
import subprocess
import sys
from pathlib import Path


# Written by both sides, so neither can drift from the other's expectations.
SHARED_TEXT = 'from the shared tree, which the candidate replaced'
DELTA_TEXT = "from this job's delta, which is what the build must see"
HEADER_VALUE_SHARED = 11
HEADER_VALUE_DELTA = 42

OVERRIDDEN = 'overridden.txt'   # exists in both; the delta's version must win
DELTA_ONLY = 'candidate.txt'    # exists only in the delta
DELETED = 'removed.txt'         # exists in the shared tree; the job deletes it
DELTA_DIR = 'built'             # the job creates it
LINK = 'link.txt'               # a symlink the job creates
SPAWNED = 'spawned.txt'         # a child writes it through a spawn file action
HEADER = 'value.h'
PROGRAM = 'main.c'


def build_fixture(root: Path, delta: Path) -> None:
    """The shared tree and the delta, as a job would find them.

    The delta is a sibling of the root and never inside it. With the delta
    underneath the root, a write that correctly went to the delta and a write
    that leaked into the shared tree are the same write, and no probe built on
    that arrangement can tell them apart -- a mistake that has already produced
    one confident report of a leak that was not there.
    """
    root.mkdir(parents=True, exist_ok=True)
    delta.mkdir(parents=True, exist_ok=True)
    (root / OVERRIDDEN).write_text(SHARED_TEXT)
    (root / DELETED).write_text(SHARED_TEXT)
    (root / HEADER).write_text(f'#define VALUE {HEADER_VALUE_SHARED}\n')
    (root / PROGRAM).write_text(
        '#include <stdio.h>\n'
        f'#include "{HEADER}"\n'
        'int main(void) { printf("%d\\n", VALUE); return 0; }\n'
    )

    inside = delta / str(root).lstrip('/')
    inside.mkdir(parents=True, exist_ok=True)
    (inside / OVERRIDDEN).write_text(DELTA_TEXT)
    (inside / DELTA_ONLY).write_text(DELTA_TEXT)
    (inside / HEADER).write_text(f'#define VALUE {HEADER_VALUE_DELTA}\n')


def refuse_nested(root: Path, delta: Path) -> str | None:
    """Why this arrangement could not answer the question, if it could not."""
    root, delta = root.resolve(), delta.resolve()
    if delta == root or root in delta.parents:
        return (f'the delta {delta} is inside the root {root}, so a write that went '
                'to the delta and a write that leaked into the shared tree are the '
                'same write and nothing here can tell them apart')
    if str(root) == '/':
        return ('the root is the filesystem root, so every path is inside it and '
                '"did this stay out of the shared tree" has no meaning')
    return None


class Checks:
    """Each method is one clause of the contract, named after what it exercises."""

    def __init__(self, root: Path):
        self.root = root
        self.results: list[tuple[str, bool, str]] = []

    def record(self, name, ok, detail):
        self.results.append((name, ok, detail))

    # --- existence -------------------------------------------------------

    def stat(self):
        only = self.root / DELTA_ONLY
        over = self.root / OVERRIDDEN
        seen = only.exists()
        sized = over.stat().st_size == len(DELTA_TEXT)
        self.record('stat', seen and sized,
                    f'delta-only visible={seen}, overridden shows delta size={sized}')

    # --- reading ---------------------------------------------------------

    def open(self):
        only = (self.root / DELTA_ONLY).read_text()
        over = (self.root / OVERRIDDEN).read_text()
        self.record('open', only == DELTA_TEXT and over == DELTA_TEXT,
                    f'delta-only={only[:24]!r}, overridden={over[:24]!r}')

    def fopen(self):
        libc = ctypes.CDLL(None)
        # restype declared: ctypes assumes int, which truncates the FILE* on
        # 64-bit and segfaults on the next call. The probe crashing looks
        # exactly like the overlay crashing.
        libc.fopen.restype = ctypes.c_void_p
        libc.fopen.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
        libc.fgets.restype = ctypes.c_char_p
        libc.fgets.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_void_p]
        libc.fclose.argtypes = [ctypes.c_void_p]
        handle = libc.fopen(str(self.root / OVERRIDDEN).encode(), b'r')
        if not handle:
            self.record('fopen', False, 'could not open the overridden file at all')
            return
        buf = ctypes.create_string_buffer(256)
        libc.fgets(buf, 256, handle)
        libc.fclose(handle)
        got = buf.value.decode(errors='replace')
        self.record('fopen', got == DELTA_TEXT, f'read {got[:24]!r}')

    def fortified_open(self):
        """The fortified entry point, which is what -D_FORTIFY_SOURCE=2 calls.

        Every program on Debian and Ubuntu is compiled that way, so an unwrapped
        one opens the original file instead of the candidate's -- and sixteen
        wrappers including this were being compiled out.
        """
        libc = ctypes.CDLL(None)
        # getattr with a string, never `libc.__open_2`: inside a class body that
        # spelling is mangled to `libc._Checks__open_2`, which raises
        # AttributeError and would be reported as "this C library does not have
        # it" -- a silent pass for the entry point whose absence cost a run.
        try:
            fn = getattr(libc, '__open_2')
        except AttributeError:
            self.record('__open_2', True, 'this C library does not have it')
            return
        fn.restype = ctypes.c_int
        fn.argtypes = [ctypes.c_char_p, ctypes.c_int]
        fd = fn(str(self.root / OVERRIDDEN).encode(), os.O_RDONLY)
        if fd < 0:
            self.record('__open_2', False, 'could not open the overridden file')
            return
        got = os.read(fd, 256).decode(errors='replace')
        os.close(fd)
        self.record('__open_2', got == DELTA_TEXT, f'read {got[:24]!r}')

    # --- working directory ----------------------------------------------

    def chdir(self):
        """Standing in a directory the candidate created, and working there.

        MEASURED before chdir was wrapped: mkdir succeeded, stat agreed the
        directory was there, and chdir into it failed with ENOENT.
        """
        made = self.root / DELTA_DIR
        try:
            made.mkdir(exist_ok=True)
        except OSError as e:
            self.record('mkdir', False, f'could not create it: {e.strerror}')
            self.record('chdir', False, 'nothing to enter')
            return
        self.record('mkdir', made.is_dir(), f'created and visible={made.is_dir()}')

        keep = os.getcwd()
        try:
            os.chdir(made)
        except OSError as e:
            self.record('chdir', False, f'{e.strerror} entering a directory it just made')
            return
        try:
            Path('relative.o').write_text(DELTA_TEXT)
            got = Path('relative.o').read_text()
            self.record('chdir', got == DELTA_TEXT,
                        f'entered, and a relative write read back {got[:20]!r}')
        finally:
            os.chdir(keep)

    def chdir_shared(self):
        """The common case, which must not change.

        Everything else assumes the working directory is a shared path: relative
        names are resolved against the real cwd and substituted afterwards. A cwd
        moved into the delta would put a delta prefix on every relative name the
        build uses and on every path a compiler writes into a depfile.
        """
        keep = os.getcwd()
        try:
            os.chdir(self.root)
            here = Path(os.getcwd()).resolve()
            self.record('chdir_shared', here == self.root.resolve(),
                        f'cwd is {here}')
        except OSError as e:
            self.record('chdir_shared', False, e.strerror)
        finally:
            os.chdir(keep)

    # --- deletion --------------------------------------------------------

    def unlink(self):
        """A file the candidate removed must be gone for the build.

        This is the one that ends runs quietly: a deletion the build cannot see
        makes it rebuild nothing, and the test then runs the previous
        candidate's binary and passes.
        """
        gone = self.root / DELETED
        try:
            gone.unlink()
        except OSError as e:
            self.record('unlink', False, f'could not delete: {e.strerror}')
            return
        try:
            gone.read_text()
        except FileNotFoundError:
            self.record('unlink', True, 'deleted and unreadable afterwards')
            return
        except OSError as e:
            self.record('unlink', False, f'unexpected error {e.strerror}')
            return
        self.record('unlink', False, 'deleted and STILL READABLE')

    # --- dereferencing ---------------------------------------------------

    def readlink(self):
        link = self.root / LINK
        try:
            if link.is_symlink():
                link.unlink()
            link.symlink_to(DELTA_ONLY)
        except OSError as e:
            self.record('readlink', False, f'could not create the link: {e.strerror}')
            return
        try:
            target = os.readlink(link)
            resolved = (self.root / target).read_text()
        except OSError as e:
            self.record('readlink', False, f'{e.strerror}')
            return
        self.record('readlink', resolved == DELTA_TEXT,
                    f'-> {target}, which reads {resolved[:20]!r}')

    def realpath(self):
        try:
            got = os.path.realpath(self.root / LINK)
        except OSError as e:
            self.record('realpath', False, e.strerror)
            return
        # It must name the path in the shared tree's terms, not the delta's:
        # that is the name every other part of the build uses.
        self.record('realpath', got == str(self.root / DELTA_ONLY), f'-> {got}')

    # --- spawning --------------------------------------------------------

    def posix_spawn(self):
        """A child's file action, performed between fork and exec.

        glibc carries it out with an internal call that never reaches the
        overlay, so the path arrives at the kernel untouched however thoroughly
        open() is wrapped. MEASURED before it was fixed: the child created its
        output in the SHARED tree, which every other job is reading.
        """
        target = self.root / SPAWNED
        code = (
            'import os, sys\n'
            'sys.stdout.write(open(sys.argv[1]).read())\n'
        )
        try:
            proc = subprocess.run(
                [sys.executable, '-c', code, str(self.root / OVERRIDDEN)],
                capture_output=True, text=True,
                stdin=subprocess.DEVNULL,
            )
        except OSError as e:
            self.record('posix_spawn', False, f'could not spawn: {e}')
            return
        self.record('posix_spawn', proc.stdout == DELTA_TEXT,
                    f'the child read {proc.stdout[:24]!r}')
        # And the file action itself, which is the part that leaked.
        libc_written = self._spawn_with_action(target)
        self.record('posix_spawn_file_actions_addopen', libc_written,
                    'a child wrote through a file action')

    def _spawn_with_action(self, target: Path) -> bool:
        program = self.root / DELTA_DIR / 'spawner.py'
        try:
            program.parent.mkdir(parents=True, exist_ok=True)
            program.write_text(
                'import os, posix, sys\n'
                'fd = os.open(sys.argv[1], os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)\n'
                'os.dup2(fd, 1)\n'
                'sys.stdout.write("written-by-the-child")\n'
            )
            subprocess.run([sys.executable, str(program), str(target)],
                           capture_output=True, check=True)
        except (OSError, subprocess.SubprocessError):
            return False
        return True

    # --- the whole thing -------------------------------------------------

    def build(self):
        """One compile, one link, one run, which is what all of it is for.

        It does not care which entry point the compiler chose, so it is the only
        check here that survives the next hole. A source that includes a header
        the delta overrides must produce a program that prints the delta's value.
        """
        cc = shutil.which('cc') or shutil.which('gcc') or shutil.which('clang')
        if cc is None:
            self.record('build', False,
                        'no C compiler, so the one check that does not depend on '
                        'which syscall was used could not be made')
            return
        binary = self.root / DELTA_DIR / 'probe'
        try:
            binary.parent.mkdir(parents=True, exist_ok=True)
            compiled = subprocess.run(
                [cc, '-I', str(self.root), '-o', str(binary), str(self.root / PROGRAM)],
                capture_output=True, text=True,
            )
        except OSError as e:
            self.record('build', False, f'could not run {cc}: {e}')
            return
        if compiled.returncode != 0:
            self.record('build', False, f'compile failed: {compiled.stderr.strip()[:120]}')
            return
        try:
            ran = subprocess.run([str(binary)], capture_output=True, text=True)
        except OSError as e:
            self.record('build', False, f'could not run what it built: {e}')
            return
        printed = ran.stdout.strip()
        self.record('build', printed == str(HEADER_VALUE_DELTA),
                    f'the program printed {printed!r}, delta says '
                    f'{HEADER_VALUE_DELTA}, shared tree says {HEADER_VALUE_SHARED}')

    def run(self):
        for name in ('stat', 'open', 'fopen', 'fortified_open', 'chdir_shared',
                     'chdir', 'unlink', 'readlink', 'realpath', 'posix_spawn',
                     'build'):
            try:
                getattr(self, name)()
            except Exception as e:  # noqa: BLE001 - a check that explodes is a failure
                self.record(name, False, f'raised {type(e).__name__}: {e}')
        return self.results


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--root', required=True, type=Path)
    args = parser.parse_args(argv)

    for name, ok, detail in Checks(args.root).run():
        print(f'{name}\t{"PASS" if ok else "FAIL"}\t{detail}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
