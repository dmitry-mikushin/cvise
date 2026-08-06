# Reducing a real project: what one run taught

This is a field guide for repeating what this fork was built for -- taking a
C++ project of some hundreds of translation units and reducing it against
"the project builds and one named test still passes" -- on a different test, a
different component, or a different project.

Every claim below is followed by the measurement that produced it. Where a
number appears, it came from a run; where it does not, the statement is
structural and can be checked in the code named beside it.

The reference application is `ns-projection`, a component of `ns-nvrtc`, graded
by `ctest -R ^IngestTest.ParsesRepresentativeRequestJson$`. In nine hours it
removed 446 of 1412 source files, 166 208 of 350 737 lines of code and 6 100 of
13 565 function definitions, and every published state was verified to build
from nothing and pass.


## 1. The criterion decides whether anything else matters

A reduction does exactly what its criterion says. Design that first, and design
it adversarially, because the reducer is an adversary: it will find every way to
satisfy the criterion while destroying what you cared about.

**The question to ask about any criterion: what does it report when the thing it
measures has been DELETED?** If the answer is "success", nothing else you do
matters.

For GoogleTest that answer is "success", and it is not obvious. A binary whose
`--gtest_filter` matches nothing prints

    [  PASSED  ] 0 tests.

and exits ZERO. By return code, a test that no longer exists reports success.
So judge the output:

```cmake
gtest_discover_tests(<target>
    PROPERTIES
        PASS_REGULAR_EXPRESSION "\\[  PASSED  \\] 1 test\\.|\\[  SKIPPED \\] 1 test"
        FAIL_REGULAR_EXPRESSION "\\[  FAILED  \\]|<your watchdog marker>"
)
```

and invoke it with `--no-tests=error`, so a test that vanished from discovery is
an error rather than an absence. `cvise/utils/projectcheck.py` does exactly
this for every candidate, not only at checkpoints.

The singular `1 test.` is what makes this work: gtest writes `1 test.` against
`N tests.`, so "exactly one" is distinguishable from "none" and from "many" in
one line. It also means a ctest test that runs several gtest tests will fail
this regex -- the safe direction, but know it before you register one.

And the reducer only DELETES. It cannot fabricate `[  PASSED  ] 1 test.`; that
line is printed by gtest, having actually run the test.

### The second guard, which is not the same guard

The regex proves the test RAN and passed. It cannot prove the test still
ASSERTS anything -- an empty test body satisfies it perfectly. Mark the body:

```c++
CVISE_NOREDUCE
void Suite_Case_Test::TestBody() { ... }
```

The macro form `TEST(suite, case) { ... }` has nowhere to attach that attribute:
MEASURED, g++ says "attributes are not allowed on a function-definition" after
it and "expected unqualified-id" before it. `gtest-explicit-form.py` rewrites a
`TEST()` into the equivalent class + `RegisterTest` + out-of-line `TestBody()`,
copying the body as the exact bytes it already was.

Neither guard is sufficient alone. Without the regex, a deleted test reports
success by exit code. Without the marker, an emptied test satisfies the regex.

### Verify that the criterion is out of reach

The pass condition lives in a `CMakeLists.txt`. If the reduction can edit that
file it can delete the condition. Check by measurement rather than by belief --
in this project the reducible set comes from the compilation database, which
names translation units, so build files are unreachable:

| kind       | in tree | modified in 9 h |
|------------|--------:|----------------:|
| headers    |     972 |             937 |
| sources    |     440 |             377 |
| **cmake**  |   **2** |           **0** |
| json       |     204 |               0 |
| python     |    1843 |               0 |

Re-run that census on your project before trusting the guard. "Never modified"
is weaker than "cannot be modified", and if someone adds a pass that edits CMake
the guard becomes reachable and nothing will warn you.


## 2. It must run in the project's own build environment

Not for tidiness. C-Vise's C++ passes parse each file with `clang_delta`, and
they are only as good as the flags they parse with, which live in the build's
`compile_commands.json` and name container paths and a container compiler.

MEASURED on two files of this project: `clang_delta` on the host, against a
path-rewritten copy of that database, produced 5 crashing transformations and
ZERO usable ones. Inside the build's own container, against the database as
written, 1 crash and 32 transformations finding 2900+ instances.

A host run is not the same thing more slowly. It is a reduction with its
semantic passes switched off, which still finishes and still prints a
percentage.

So the image is the project's build image plus C-Vise, and a candidate is built
by the ordinary `cmake --build` of the project inside it. Jobs are kept apart by
C-Vise's own LD_PRELOAD overlay, not by a container each.


## 3. Configure from the root, reduce under the component

Configure where the tests exist. In this project `cpp/test` is added by the
parent repository, after the targets it inspects are defined; configuring the
component alone produces no tests and therefore no criterion.

Reduce under the component: `--under third_party/ns-projection`.

MEASURED when the two were the same: 3651 translation units instead of 378,
every job copying 6375 files and 1485 directories, 39% of the machine in
`mkdir` and 4% doing useful work, and no verdict at all in 80 minutes.

The consequence is worth stating rather than hiding: the reducible set is
everything the top-level database names under that path.


## 4. The machine

* **State in `/dev/shm`, never `/tmp`.** Both are tmpfs; `/tmp` here is
  deliberately 16 GiB for programs' own scratch, `/dev/shm` is 126 GiB. Dozens
  of jobs each holding a build directory do not fit in the first.
* **`--memory` equal to `--memory-swap`.** A ceiling that permits swap is not a
  ceiling. MEASURED: `--memory 179G --memory-swap 358G` exhausted 28.6 GB of
  swap, put 3539 tasks in uninterruptible sleep, reached a load of 3583 with the
  CPU idle, and had to be killed from another machine. An honest OOM kills one
  candidate; eternal direct reclaim kills the host.
* **`--init`.** C-Vise is not an init. A worker that spawns `clang_delta` and
  exits leaves an orphan reparented to PID 1, and PID 1 is `python3 cvise`,
  which never calls `wait()` on a child it did not create. MEASURED: 44 of 48
  `clang_delta` entries were zombies after one hour. `reap_probe.py` fails
  without the flag and passes with it.
* **ccache outside the overlay root.** The image points `CCACHE_DIR` inside the
  tree being reduced, which is the overlay's root, so every write goes to the
  job's own delta and ccache rebuilds its whole directory chain per object.
  MEASURED: nine `mkdir` calls where a filesystem needs none, 62% of the machine
  in ccache doing `mkdir`, 3.6% doing anything useful -- and the cache was
  worthless there, since a delta is thrown away with its candidate.
* **Sweep ccache's temporary directory.** MEASURED at seven hours: the cache
  held 9.8 GB against its 20 GB ceiling while `ccache/tmp` held 41 GB in 14 175
  files. ccache leaks a temporary when a process is killed between writing and
  renaming -- and a reduction kills candidates routinely -- and sweeps those
  only during a cleanup, which runs only when the cache exceeds its maximum
  size, which a 9.8 GB cache under a 20 GB cap never does. The loop is closed
  and nothing breaks it; the tmpfs fills and the run dies. `cvise-ns-projection.py`
  sweeps every half hour, deleting only files older than an hour, which is far
  beyond any live compile.
* **One reduction at a time.** `flock` catches a live competitor, but the case
  worth guarding is the other one: a runner dies while its container keeps
  running, so a reduction owns the machine and no lock is held for it, the lock
  having died with the process that took it. Once the lock is ours, any
  reduction container still running is unattended -- and it is named, not
  killed, because it may be hours of work.


## 5. Judge progress by code, not by bytes

Bytes move the same for a stripped space and for a deleted translation unit.
`cvise-mon` reports what a person actually judges a reduction by:

```
 files     ████████░░░░░░░░░░░░░░░░░░░░       966 left of 1 412     -446      31.6% gone
 lines     █████████████░░░░░░░░░░░░░░░   184 517 left of 350 737   -166 220  47.4% gone
 functions █████████████░░░░░░░░░░░░░░░     7 465 left of 13 565    -6 100    45.0% gone
```

* A file counts as left only if it still has a non-whitespace line: a pass that
  empties a file has removed it as far as any reader is concerned.
* Functions are counted by `treesitter_delta list-definitions`, the binary the
  reduction itself parses with. A second parser -- even the same grammar from
  crates.io -- could drift from it and report a different number about the same
  file, and the screen would be arguing with the run.
* "Before" is not remembered: the reduced tree is a git worktree, so the
  original is the newest commit C-Vise did not write.


## 6. VERIFIED is a trigger, not a finish line

The percentage is not the acceptance criterion. `verify-reduction.py` is: it
pauses the reduction, configures and builds the published tree from nothing in
a directory of its own, and runs the same ctest. It costs about 150 s and
cannot damage the run.

MEASURED why this is not optional: one run reached 14.87 MB -> 6970 bytes in
four minutes and the result did not compile. Every number it printed was about
a tree nobody had built.

When it says VERIFIED, preserve immediately -- do not wait for the run to
finish, because it may not:

```sh
cd <state>/<worktree> && git add -A \
  && git -c user.name='C-Vise' -c user.email='cvise@localhost' \
        commit -m 'reduced by C-Vise, VERIFIED' \
  && git bundle create ~/cvise-results/<name>.bundle <branch> \
  && git bundle verify ~/cvise-results/<name>.bundle
```

The tree lives in tmpfs and dies with the machine. A bundle carries the history,
so the result is a diff against the real HEAD rather than an opaque blob, and it
is small: 18.8 MB for a 91 MB tree.

MEASURED why "immediately": a run stood at 56% after nine hours when a pass
reported a bug it had itself classified as non-fatal, and writing the report
raised `IsADirectoryError` and ended the run. The published tree survived only
because a publication had landed a minute earlier.


## 7. Instruments that lie

Every one of these produced a confident wrong answer during one run. They are
listed because each will do it again.

* **A shell that summarises long output.** MEASURED, same command, same instant:
  `docker logs <cid>` through a shell returned 16 lines and a "Log Summary"
  digest with zero progress lines in it; from a process, 657 lines and 23
  progress lines. It is not silent about what it dropped -- it prints something
  shaped like data. Read bytes from a program, not through a pipeline.
* **`pgrep -c` counts zombies.** A corpse keeps its name, so the count answers
  "entries in the process table" while being read as "work being done". In one
  run those were 44 and 4, and the rising corpse count read as a phase ramping
  up. Count by state.
* **An instantaneous CPU figure.** Sampled between folding batches it reads as
  an idle machine; the same run was at 8300% thirty seconds later. Four false
  alarms in one session. Measure a rate -- verdicts per second -- not a moment.
* **A comparison of two absences.** A probe that finds nothing in both versions
  and reports "identical" has reported nothing. Assert that what you are
  comparing was found, in both, before comparing it.
* **A tool that reports its own price.** `ps -eo pcpu --sort=-pcpu` pays for the
  sort and then lists `ps` at the top of its own output.
* **Three points in one direction.** Memory rose across five samples and read as
  a leak; it was candidates being born and dying with the folding batch, and it
  fell by 22 GiB on the next publication.


## 8. What to change for a different test or project

1. `TEST` name and target in the runner (`TEST`, `SUBMODULE`, `IMAGE`).
2. The `PASS_REGULAR_EXPRESSION` if the framework is not GoogleTest -- and
   re-derive it by asking what the output looks like when the test is deleted.
3. `CVISE_NOREDUCE` on the new test's body; use `gtest-explicit-form.py` if it
   is still in macro form.
4. The census in section 1, to confirm the criterion's file is unreachable.
5. `JOB_BUDGET_MB` if the project's heaviest translation unit needs more than
   2 GB; it was measured at 689 MiB peak RSS here.

Then run one candidate by hand before starting: build the tree, run the ctest
invocation the check will use, and confirm it prints the line the regex wants.
A criterion that never passes reduces nothing and says so only in the verdict
journal, hours later.


## 9. Where the time actually goes

Useful when someone proposes to make it faster.

* 2 386 031 compiler calls in nine hours, of which **95.6% were ccache hits**
  that never started a compiler. Persisting the compiler driver would remove
  ~60 ms of startup from the 103 285 misses -- 1.7 core-hours out of 379, or
  **0.45%**. Not worth the stability it costs.
* On a median library translation unit, MEASURED: full compile 43.6 s, front end
  35.2 s (81%), of which parsing nine `#include` directives is 24.6 s -- **56%
  of the whole compile**. That is what a precompiled header removes.
* A PCH *is* a serialized AST; there is no third thing to reach for. But it
  caches declarations, not template instantiations and not code generation, so
  the remaining 44% is out of its reach whatever you do.
* Check which targets actually have one. Here the test target had a PCH and the
  library -- 142 of 378 translation units -- did not.
