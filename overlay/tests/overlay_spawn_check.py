#!/usr/bin/env python3
"""Does a job's CHILD process stay out of the shared tree?

The overlay redirects what the job itself does. A build system does most of its
work in children, and hands those children paths before they exist: posix_spawn
takes a list of file actions -- open this path onto that descriptor -- performed
by the spawn implementation in the child, between fork and exec, before the new
program image exists.

That open never reaches this library. glibc performs it with an internal call,
so however thoroughly `open` is wrapped, a path recorded in a file action
arrives at the kernel untouched. MEASURED before this was fixed: a child spawned
with its output redirected to a path under the reduction root created that file
in the SHARED TREE, which every other job in the run is reading. Nothing about
it is visible afterwards -- the job succeeds, and what it damaged belongs to
somebody else.

ninja imports posix_spawn_file_actions_addopen, which is how this was noticed:
the coverage check reported it as an entry point asked for and not wrapped.
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path


PROGRAM = r'''
#include <cstdio>
#include <spawn.h>
#include <unistd.h>
#include <sys/wait.h>
#include <fcntl.h>
extern char **environ;
int main(int argc, char **argv) {
    posix_spawn_file_actions_t actions;
    posix_spawn_file_actions_init(&actions);
    posix_spawn_file_actions_addopen(&actions, 1, argv[1],
                                     O_WRONLY | O_CREAT | O_TRUNC, 0644);
    char *args[] = {(char *)"/bin/echo", (char *)"written-by-the-child", nullptr};
    pid_t pid;
    if (posix_spawn(&pid, "/bin/echo", &actions, nullptr, args, environ) != 0) {
        perror("posix_spawn");
        return 1;
    }
    int status;
    waitpid(pid, &status, 0);
    return 0;
}
'''


def compiler():
    for name in ('c++', 'g++', 'clang++', '/usr/lib/llvm-19/bin/clang++'):
        found = subprocess.run(['sh', '-c', f'command -v {name}'],
                               capture_output=True, text=True).stdout.strip()
        if found:
            return found
    return None


def main():
    library = os.environ.get('CVISE_OVERLAY_LIB')
    if not library or not Path(library).exists():
        print('CVISE_OVERLAY_LIB is not set to an existing library', file=sys.stderr)
        return 2
    cxx = compiler()
    if cxx is None:
        print('no C++ compiler to build the probe with', file=sys.stderr)
        return 2

    work = Path(tempfile.mkdtemp(prefix='overlay-spawn-'))
    root, delta = work / 'root', work / 'delta'
    (root / 'build').mkdir(parents=True)
    delta.mkdir()

    source = work / 'spawner.cpp'
    source.write_text(PROGRAM)
    program = work / 'spawner'
    built = subprocess.run([cxx, '-o', str(program), str(source)],
                           capture_output=True, text=True)
    if built.returncode != 0:
        print('the probe would not compile:', built.stderr.strip()[:300], file=sys.stderr)
        return 2

    target = root / 'build' / 'child-output.txt'
    subprocess.run(
        [str(program), str(target)],
        env={
            **os.environ,
            'LD_PRELOAD': library,
            'CVISE_OVERLAY_DELTA': str(delta),
            'CVISE_OVERLAY_ROOT': str(root),
        },
        check=True,
    )

    in_delta = (delta / str(target).lstrip('/')).exists()
    in_shared = target.exists()

    ok = True
    if in_shared:
        print(f'  FAIL the child wrote {target} into the SHARED tree, which every '
              'other job in the run is reading')
        ok = False
    else:
        print('  ok   the shared tree is untouched')
    if in_delta:
        print("  ok   the child's output landed in this job's delta")
    else:
        print('  FAIL the output is in neither place, so this proves nothing about '
              'where a real one would go')
        ok = False

    print('PASS' if ok else 'FAIL')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
