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
$ cvise path/to/CMakeLists.txt ./interesting.sh
```

The CMakeLists.txt is the one that drives the project's build. C-Vise runs CMake
on it once, purely to obtain `compile_commands.json`, and takes everything else
from that file: which translation units exist, and what flags each one is
compiled with. There is nothing else to tell it, because there is nothing else
it needs to know that the build system does not already know -- and every extra
question would be another way for the answer to disagree with the build.

The interestingness test is an executable that answers one question about a
variant of the project: is it still interesting? It takes no arguments and is
hard-coded to refer to the project it is testing. It should exit 0 for
interesting, nonzero for not, and 125 for "I could not decide" -- see below.

That is the whole interface. Reducing a single file, a list of files, a
directory, a Makefile project, or anything described by a hand-written
compilation database used to be separate ways in, and they are gone: a tool with
five entrances has five sets of assumptions to keep straight, and the one that
matters here is the one the build system can state for itself.

### Why the flags matter

A file compiled without its project's flags cannot be parsed: it will not find
its own headers, its language standard is a guess, and the C++ passes -- the
ones that delete functions, classes and whole templates -- have nothing to work
on. MEASURED on one project: with the flags from `compile_commands.json`, 32
transformations found 2900+ instances between them; without, none did.

### Reduce with the compiler the project builds with

`clang_delta` links one specific Clang, and the flags in the database were
written for whatever compiler the project uses. If they disagree -- a different
standard library, a different sysroot, headers that exist only inside a build
container -- then `clang_delta` parses something the build never sees, and its
transformations are guesses. Run the reduction in the same environment as the
build.

### Reducing in place, without copying the tree

By default each candidate is prepared in a scratch directory. For a project of
any size that is the wrong shape: the build system decides what to rebuild from
timestamps, and a copied tree has none of the original ones, so every candidate
costs a full rebuild.

Set `CVISE_OVERLAY_LIB` to an overlay library and C-Vise gives each parallel job
its own private view of the project instead. The job's candidate is served at
the project's real path, everything it did not change is read from the one
shared tree, and everything it writes lands in its own directory -- so nothing
is copied, no timestamp is disturbed, and no job can disturb another.

```console
$ CVISE_OVERLAY_LIB=/path/to/libfakechroot.so \
      cvise path/to/CMakeLists.txt ./interesting.sh
```

C-Vise refuses to start this way unless two things hold. The overlay must prove
itself -- in a child process built exactly like a job's, because that is where
redirection has to work -- since an overlay that is loaded but inert lets every
build read the pristine sources, which looks exactly like a reduction that is
going well. And the run must be under a cgroup memory limit below the machine's
RAM, because the scratch space of a parallel reduction is not reclaimable
memory: when it fills, the OOM killer can free none of it, and the machine dies
rather than the reduction failing.

```console
$ systemd-run --user --scope -p MemoryMax=64G -p MemorySwapMax=0 \
      env CVISE_OVERLAY_LIB=/path/to/libfakechroot.so \
      cvise path/to/CMakeLists.txt ./interesting.sh
```

### When the test cannot answer

An interestingness test that is killed -- by the OOM killer, or because its
filesystem filled -- has not said "not interesting"; it has said nothing. Read
as a verdict, it silently discards a candidate that was probably fine, and the
closer the machine is to its limits the more it discards. A test may exit 125 to
say so explicitly, and a test that dies by signal is treated the same way. Such
a candidate keeps its previous state and is not counted against the pass.

## Notes

1. C-Vise creates temporary directories in `$TMPDIR` and so usage
of a `tmpfs` directory is recommended.

1. By default each invocation of the interestingness test runs in a fresh
temporary directory holding a copy of the files being reduced, so a test that
needs anything else must refer to it by absolute path. With `CVISE_OVERLAY_LIB`
this is no longer so: the test sees the project at its real path, and the only
thing that differs from an ordinary build is the content of the files this
candidate changed.

1. If you copy the compiler invocation line from your build tool, remove
-Werror if present. Some C-Vise passes introduce warnings, so -Werror
will make those passes ineffective.

   Doing that, a reduction will typically end up faster, however,
   one may end up with a code snippet full of warnings that needs
   to be addresses after the reduction.

1. Adding `-Wfatal-errors` to the interestingness test can speed up
   large reductions by causing the compiler to bail out quickly on errors,
   rather than trying to soldier on producing a result that is eventually
   discarded.
