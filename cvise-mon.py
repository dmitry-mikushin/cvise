#!/usr/bin/env python3
"""What the running reduction is doing, on one screen.

WHY THIS IS A PROGRAM AND NOT A HANDFUL OF SHELL

    The same numbers were being read by hand, with pipelines, and the pipelines
    lied. MEASURED on this machine, same command, same instant:

        docker logs <cid>   through a shell   ->  16 lines, a "Log Summary"
                                                  digest, 0 progress lines
        docker logs <cid>   from subprocess   -> 657 lines, 23 progress lines

    The shell layer here replaces long output with a plausible-looking summary,
    truncated lines and all. It is not silent about what it dropped -- it prints
    something that reads like data. Four separate "anomalies" chased during one
    reduction were that wrapper: a count of 484 that came back 28, a `tail -2`
    that reported "+566 more", a log that looked stale, a log that looked empty.

    A program reads the bytes itself. That is the whole reason this file exists.

WHAT IT REFUSES TO DO

    Count processes with pgrep. `pgrep -c clang_delta` counts zombies, and in
    one run 44 of 48 clang_delta entries were corpses while the count was read
    as the hint phase ramping up. Everything here counts by process state.

    Report an instantaneous CPU figure as a verdict. `docker stats` sampled at
    one moment during a folding batch reads as an idle machine; the run was at
    8300% thirty seconds later. Throughput is measured as a rate between
    refreshes, which is what actually says whether the reduction is moving.
"""

import argparse
import os
import re
import subprocess
import sys
import time
from pathlib import Path

IMAGE = 'ns-rtc-cvise'
STATE_GLOB = 'cvise-*'
VERDICTS = 'tmp/cvise-verdicts.log'

PROGRESS = re.compile(
    r'\((?P<percent>[\d.]+)%, (?P<bytes>\d+) bytes, (?P<lines>\d+) lines, '
    r'(?P<files>\d+) files in (?P<dirs>\d+) dirs(?:, via (?P<passes>.*))?\)'
)


def run(command: list[str]) -> str:
    """Output as the command actually produced it.

    Not through a shell, and not through a pipe: both are where this
    environment's summarising wrapper gets in.
    """
    proc = subprocess.run(command, capture_output=True, text=True)
    return proc.stdout + proc.stderr


def reduction() -> tuple[str, Path] | None:
    """The running reduction and the state directory it works in.

    Found by what it runs and what it has mounted, never by name -- a container
    name is assigned at random, and the image tag is shared by the verification
    builds and by any one-off probe.
    """
    listing = run(['docker', 'ps', '--format', '{{.ID}}\t{{.Image}}\t{{.Command}}'])
    for row in listing.splitlines():
        parts = row.split('\t')
        if len(parts) != 3 or parts[1] != IMAGE or not parts[2].strip('"').startswith('cvise'):
            continue
        cid = parts[0]
        mounts = run(['docker', 'inspect', cid,
                      '--format', '{{range .Mounts}}{{.Source}}\n{{end}}'])
        for source in mounts.splitlines():
            path = Path(source)
            if path.parent == Path('/dev/shm') and path.name.startswith('cvise-'):
                return cid, path
    return None


def dead_reduction() -> str | None:
    """A container that ran a reduction and is no longer running."""
    listing = run(['docker', 'ps', '-a', '--format', '{{.ID}}\t{{.Image}}\t{{.Command}}\t{{.Status}}'])
    for row in listing.splitlines():
        parts = row.split('\t')
        if len(parts) == 4 and parts[1] == IMAGE and parts[2].strip('"').startswith('cvise'):
            return parts[0]
    return None


class Log:
    """The container's own output, counted over the whole of it."""

    def __init__(self, cid: str):
        self.text = run(['docker', 'logs', cid])
        self.lines = self.text.splitlines()

    def count(self, needle: str) -> int:
        return sum(1 for line in self.lines if needle in line)

    def progress(self, first: bool = False) -> dict | None:
        found = [m for m in (PROGRESS.search(line) for line in self.lines) if m]
        if not found:
            return None
        return (found[0] if first else found[-1]).groupdict()

    def jobs(self) -> str:
        match = re.search(r'a candidate has \d+ s.*?up to (\d+) candidates', self.text, re.S)
        return match.group(1) if match else '?'

    def last_traceback(self) -> str | None:
        for i in range(len(self.lines) - 1, -1, -1):
            if 'Traceback (most recent call last)' in self.lines[i]:
                return '\n'.join(self.lines[i:i + 24])
        return None


def verdicts(state: Path) -> tuple[int, dict[str, int]]:
    """Verdicts of the LIVE job root only.

    The journal is appended to across runs of the same state directory, so a
    plain count mixes a dead run's hundreds of thousands with a live run's
    hundreds. The live root is the one the most recent verdict names.
    """
    path = state / VERDICTS
    try:
        lines = path.read_text(errors='replace').splitlines()
    except FileNotFoundError:
        return 0, {}
    if not lines:
        return 0, {}
    root = None
    for line in reversed(lines):
        match = re.search(r'dir=\S*?/(cvise-[A-Za-z0-9_]+)/job', line)
        if match:
            root = match.group(1)
            break
    if root is None:
        return 0, {}

    total = 0
    tally: dict[str, int] = {}
    for line in lines:
        if root not in line:
            continue
        total += 1
        verdict = re.search(r'\btest=(\w+)', line)
        if verdict:
            tally[verdict.group(1)] = tally.get(verdict.group(1), 0) + 1
    return total, tally


def live_processes(cid: str) -> dict[str, int]:
    """Processes by name, EXCLUDING zombies.

    A zombie keeps its name, so a count that does not filter on state answers
    "entries in the process table" while being read as "work being done". In one
    run that difference was 44 against 4.
    """
    listing = run(['docker', 'exec', cid, 'ps', '-eo', 'stat=,comm='])
    counts: dict[str, int] = {}
    for line in listing.splitlines():
        parts = line.split(None, 1)
        if len(parts) != 2 or parts[0].startswith('Z'):
            continue
        counts[parts[1].strip()] = counts.get(parts[1].strip(), 0) + 1
    return counts


def stats(cid: str) -> tuple[str, str]:
    row = run(['docker', 'stats', '--no-stream', '--format', '{{.CPUPerc}}\t{{.MemUsage}}', cid])
    parts = row.strip().split('\t')
    return (parts[0], parts[1]) if len(parts) == 2 else ('?', '?')


def thousands(value) -> str:
    return f'{int(value):,}'.replace(',', ' ')


def gib(blocks: int, size: int) -> float:
    return blocks * size / 1024 ** 3


def screen(cid: str, state: Path, previous: dict) -> tuple[str, dict]:
    log = Log(cid)
    now = log.progress()
    start = log.progress(first=True)
    total, tally = verdicts(state)
    procs = live_processes(cid)
    cpu, memory = stats(cid)
    fs = os.statvfs('/dev/shm')
    uptime = run(['docker', 'ps', '--filter', f'id={cid}', '--format', '{{.Status}}']).strip()

    sample = {'at': time.monotonic(), 'verdicts': total,
              'bytes': int(now['bytes']) if now else None}
    rate = ''
    shrink = ''
    if previous:
        seconds = sample['at'] - previous['at']
        if seconds > 0:
            rate = f"{(total - previous['verdicts']) / seconds:.2f}/s"
        if now and previous['bytes'] is not None:
            delta = int(now['bytes']) - previous['bytes']
            # Silent when nothing moved: a reduction publishes in batches, so
            # "+0" is the ordinary state between them and saying it every
            # refresh trains the eye to skip the line that matters.
            shrink = f'{delta:+d} bytes since the last refresh' if delta else ''

    out = []
    out.append(f'cvise-mon   {IMAGE}  {cid}  {uptime}   N={log.jobs()}')
    out.append('')

    if now:
        began = int(start['bytes']) if start else None
        out.append(f"tree        {thousands(now['bytes'])} bytes   "
                   f"{thousands(now['lines'])} lines   "
                   f"{thousands(now['files'])} files in {now['dirs']} dirs")
        if began is not None:
            gone = began - int(now['bytes'])
            out.append(f"            {now['percent']}% of this run's starting "
                       f'{thousands(began)} bytes  ({thousands(gone)} gone)')
        if shrink:
            out.append(f'            {shrink}')
        if now.get('passes'):
            passes = now['passes']
            out.append(f"            via {passes if len(passes) < 96 else passes[:93] + '...'}")
    else:
        out.append('tree        no progress line yet -- the run is still configuring')
    out.append('')

    ordered = sorted(tally.items(), key=lambda kv: -kv[1])
    spread = '   '.join(f'{name} {thousands(count)}' for name, count in ordered)
    out.append(f'work        {thousands(total)} verdicts'
               + (f'   {rate}' if rate else '   (rate after the next refresh)'))
    out.append(f'            {spread if spread else "none yet"}')
    out.append(f"            published {log.count('files written back')} times   "
               f"baseline failed {log.count('does not build as it stands')} times")
    out.append('')

    interesting = ('clang++-19', 'clang_delta', 'ninja', 'cmake', 'ccache', 'python3')
    alive = '   '.join(f'{name} {procs[name]}' for name in interesting if procs.get(name))
    out.append(f'machine     CPU {cpu} of {os.cpu_count() * 100}%   mem {memory}')
    out.append(f'            /dev/shm {gib(fs.f_blocks - fs.f_bfree, fs.f_frsize):.0f} '
               f'of {gib(fs.f_blocks, fs.f_frsize):.0f} GiB used')
    out.append(f'            live: {alive if alive else "nothing"}')
    out.append('')

    bugs = log.count('has encountered a non fatal bug')
    trace = log.count('Traceback (most recent call last)')
    out.append(f'health      pass bugs {bugs}   tracebacks {trace}')
    if trace:
        out.append('')
        out.append(log.last_traceback() or '')
    return '\n'.join(out), sample


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--interval', type=float, default=10.0,
                        help='seconds between refreshes (default 10)')
    parser.add_argument('--once', action='store_true', help='print one screen and stop')
    args = parser.parse_args()

    previous: dict = {}
    while True:
        found = reduction()
        if found is None:
            corpse = dead_reduction()
            if corpse:
                state = run(['docker', 'inspect', corpse, '--format',
                             'exit={{.State.ExitCode}} oom={{.State.OOMKilled}} '
                             'finished={{.State.FinishedAt}}']).strip()
                print(f'no reduction is running; the last one ({corpse}) {state}',
                      file=sys.stderr)
            else:
                print('no reduction is running and none has run', file=sys.stderr)
            return 1

        cid, state = found
        text, previous = screen(cid, state, previous)
        if args.once:
            print(text)
            return 0
        # Home and erase-down rather than a full clear, so the screen does not
        # blink and a terminal's scrollback keeps what came before.
        sys.stdout.write('\033[H\033[J' + text + '\n')
        sys.stdout.flush()
        time.sleep(args.interval)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
