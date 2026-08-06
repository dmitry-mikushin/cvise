"""Does PID 1 in this container reap an orphan?

Mirrors what the reduction does structurally: a worker (the child) spawns a
tool (the grandchild) and exits first, so the tool is reparented to PID 1. If
PID 1 does not wait() for it, the entry stays in the process table as a zombie.

Run twice, with and without `docker run --init`. Without it the answer must be
LEAKED and with it REAPED; a probe that cannot fail proves nothing.
"""

import subprocess
import time
from pathlib import Path

# The child exits immediately, the grandchild lingers a moment and then dies as
# an orphan already adopted by PID 1.
subprocess.run(['sh', '-c', '(sleep 1; exit 0) & exit 0'])
time.sleep(3)

zombies = []
for entry in Path('/proc').iterdir():
    if not entry.name.isdigit():
        continue
    try:
        fields = (entry / 'stat').read_text().rsplit(') ', 1)[1].split()
    except (FileNotFoundError, ProcessLookupError, IndexError):
        continue
    if fields[0] == 'Z':
        zombies.append(entry.name)

print(f'pid 1 is {Path("/proc/1/comm").read_text().strip()}')
print('LEAKED' if zombies else 'REAPED', f'({len(zombies)} zombies: {zombies})')
