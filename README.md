# C-Vise

[![Build](https://github.com/marxin/cvise/actions/workflows/build.yml/badge.svg)](https://github.com/marxin/cvise/actions/workflows/build.yml)
[![Build LLVM nightly](https://github.com/marxin/cvise/actions/workflows/build-llvm-nightly.yml/badge.svg)](https://github.com/marxin/cvise/actions/workflows/build-llvm-nightly.yml)

## About

C-Vise is a super-parallel Python port of the [C-Reduce](https://github.com/csmith-project/creduce/).
The port is fully compatible to the C-Reduce and uses the same efficient
LLVM-based C/C++ reduction tool named `clang_delta`.

**This project is looking for maintainers — reach out to [@marxin] if you're interested.**

C-Vise takes a CMake project that has a property of interest -- it triggers a
compiler bug, or produces a particular output, or fails a particular test -- and
produces a much smaller project that still has it. What survives the reduction
is, by construction, the code responsible for that property; what it deletes is
code that provably had nothing to do with it.

This fork reduces projects, not files. Upstream C-Vise accepts a single file, a
list of files, a directory, or a hand-written compilation database, and can also
apply hints instead of reducing; all of that is gone. The project's own build
system already knows which files exist and how each is compiled, so it is asked,
once, and everything follows from its answer.

## Speed Comparison

I made a comparison for couple of GCC bug reports on my AMD Ryzen 7 2700X Eight-Core Processor
machine with the following results:

| Test-case | Size | C-Vise Reduction | C-Reduce Reduction | Speed Up |
| --- | --- | --- | --- | --- |
| [PR92516](http://gcc.gnu.org/PR92516) | 6.5 MB | 35m | 77m | 220% |
| [PR94523](http://gcc.gnu.org/PR94523) | 2.1 MB | 15m | 33m | 220% |
| [PR94632](http://gcc.gnu.org/PR94632) | 3.3 MB | 20m | 28m | 40% |
| [PR94937](http://gcc.gnu.org/PR94937) | 8.5 MB | 242m | 303m | 125% |

## Installation

See [INSTALL.md](INSTALL.md).

## Usage

C-Vise reduces a CMake project. It takes two things, and nothing else:

```console
$ cvise path/to/CMakeLists.txt some_test
```

The CMakeLists.txt is the one that drives the project's build. The name is that
of a ctest test, and it is what makes a variant interesting. Everything else follows from those two,
because everything else is already written down in the build: C-Vise runs CMake
once to obtain `compile_commands.json`, and from it takes which translation
units exist, which headers they include, and what flags each is compiled with.
There is nothing else to tell it, and every extra question would be another way
for the answer to disagree with the build.

A variant is interesting when the project builds and that one test passes. A
test, not a build target, and the difference is not bookkeeping: a target says
only that something exited zero, and a test runner exits zero when the case it
was asked for no longer exists. MEASURED with GoogleTest: a filter matching
nothing prints "[  PASSED  ] 0 tests." and exits 0 -- so the cheapest way to
satisfy such a criterion is to delete the test, and a reduction will find that
before it finds anything else.

ctest can tell a test that passed from a test that was not there, which is the
whole reason it is what C-Vise asks for. Say what must have happened rather
than what must not:

```cmake
enable_testing()
add_test(NAME still_crashes COMMAND prog --input case.txt)
set_tests_properties(still_crashes PROPERTIES
                     PASS_REGULAR_EXPRESSION "assertion failed")
```

The test is registered in the CMakeLists.txt, which is not under reduction, so
the reduction cannot make the criterion easier by rewriting it.

That is the whole interface. Reducing a single file, a list of files, a bare
directory, a Makefile project, or anything described by a hand-written
compilation database used to be separate ways in, and they are gone: a tool with
five entrances has five sets of assumptions to keep straight, and the one that
matters here is the one the build system can state for itself.

### An interrupted reduction is not a lost one

The reduced tree IS the input. C-Vise rewrites it in place as soon as a smaller
interesting variant is found, so what is on disk at any moment is the best
result so far, and a run that is killed -- by a reboot, by the OOM killer, by
somebody's Ctrl-C -- loses only the candidates that were in flight. Start it
again on the same tree and it goes on from there.

There is no state file to keep and nothing to configure: the property follows
from where the result is kept. What a driver has to do is refrain from resetting
the tree first. `cvise-ns-projection.py` takes `--resume` for exactly that --
without it the worktree is put back to its pin before starting, which is what a
fresh reduction wants and never what a crashed one does.

The pass schedule is NOT carried over, and on a project that is the better half
of the bargain rather than a shortcoming. MEASURED on ns-projection: a run that
had been going for 10 h 55 min had removed 49.1% of the lines and was finding a
few hundred more per hour. Restarted on the tree it had itself produced, it
reached 54.6% within the first hour and 60.5% within three. Beginning the
schedule again lets the passes that work in bulk -- reachability above all --
look afresh at a tree that has changed shape underneath them, and propose in one
step what the line-by-line passes had been finding one at a time.

Keep the tree, restart the schedule. A checkpoint that restored the position in
the schedule would optimise away the part that was doing the work.

### What a candidate costs

A reduction asks one question millions of times, so what the question costs is
what the reduction costs. It is asked in two stages:

1. `-fsyntax-only` on the file that changed, with the flags its own build uses.
   Most rejected candidates die here, in the time it takes to parse one unit --
   on a small project, about six candidates in ten never reach a compiler
   again.
2. the project built the way the project is built, and then the target.

Only the second knows what "interesting" means, and only it belongs to you. The
first is the compiler's own opinion of the file, and C-Vise already has
everything needed to ask for it.

The first stage runs only when the candidate changed exactly one file, because
that is the only case in which it is cheaper than the build it avoids. Several
passes are folded into one candidate wherever they can be, so a candidate
touching dozens of files is ordinary -- and asking about each of them in turn
is serial work that ninja would have done in parallel and is about to do again.
MEASURED on a C++23 project of 376 units: 2.3 s to parse one unit, 16 s to
build and link a one-file candidate, and the two costs cross between one file
and two.

### Each job answers about its own candidate

The files under reduction are never copied for a candidate and never edited in
place. Each parallel job gets its own private view of the project: what its
candidate changed is served at the project's real path, for that job alone,
everything else is read from the one shared tree, and everything the job writes
lands in a directory of its own. Nothing is copied, no timestamp is disturbed,
and no job can disturb another.

That covers the build directory as well as the sources, and it has to. The
objects, the link, and the build system's own record of what is up to date are
what a verdict is actually made of. Shared, they hand each job whatever the
previous one left there: a file this candidate did not change is read from the
pristine tree with the pristine timestamp, which is older than the object the
previous candidate built from its own copy of it, so it counts as up to date and
is linked as it stands. The verdict then describes a program that no candidate
ever was -- a good one discarded because it was graded on someone else's code,
and a broken one kept for the same reason.

The project is built once before the reduction starts, so that a job compiles
what its candidate changed and links, rather than compiling the project.

This is not a mode and there is no flag for it. C-Vise refuses to start unless
the overlay proves itself -- in a child process built exactly like a job's,
because that is where redirection has to work -- since an overlay that is loaded
but inert lets every build read the pristine sources, which looks exactly like a
reduction that is going well.

### Reduce with the compiler the project builds with

`clang_delta` links one specific Clang, and the flags in the database were
written for whatever compiler the project uses. If they disagree -- a different
standard library, a different sysroot, headers that exist only inside a build
container -- then `clang_delta` parses something the build never sees, and its
transformations are guesses. Run the reduction in the same environment as the
build.

### Memory

The scratch space of a parallel reduction is not reclaimable memory. When
`$TMPDIR` is a tmpfs and it fills, the OOM killer can free none of it, because
the pages belong to no process it can kill, and the machine dies rather than the
reduction failing. C-Vise therefore places itself under a cgroup memory limit at
startup and says what it chose. If it cannot -- no systemd user session with the
memory controller delegated -- it says so, and only warns when the scratch is
actually held in memory, since a reduction whose scratch is on a disk cannot
take a machine down however large it grows. Either point `TMPDIR` at a disk, or
run under something that bounds memory:

```console
$ systemd-run --user --scope -p MemoryMax=64G -p MemorySwapMax=0 \
      cvise path/to/CMakeLists.txt some-target
```

### When the test cannot answer

An interestingness test that is killed -- by the OOM killer, or because its
filesystem filled -- has not said "not interesting"; it has said nothing. Read
as a verdict, it silently discards a candidate that was probably fine, and the
closer the machine is to its limits the more it discards. A test may exit 125 to
say so explicitly, and a test that dies by signal is treated the same way. Such
a candidate keeps its previous state and is not counted against the pass.

## Notes

1. C-Vise creates its temporary directories in `$TMPDIR`, and a `tmpfs` is
about a hundred times faster than a disk for what it does there, so it is worth
having -- under a memory limit, which C-Vise gives itself. See
[Memory](#memory).

1. The build a candidate is judged by runs where the project is, not in a
scratch directory. The only things that differ from an ordinary build are the
contents of the files the candidate changed, and the files it deleted, which
are reported as absent.

1. If the project is built with `-Werror`, consider removing it for the
reduction. Some C-Vise passes introduce warnings, and with `-Werror` those
passes can never succeed. The reduction will typically be faster without it, at
the cost of a result full of warnings.

1. Adding `-Wfatal-errors` can speed up large reductions by causing the compiler
   to bail out quickly on errors, rather than trying to soldier on producing a
   result that is eventually discarded.
