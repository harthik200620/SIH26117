"""Linux single-command resource meter, launched as an isolated stdlib interpreter.

The report descriptor belongs to the supervisor and is never inherited by the
untrusted command. This process has exactly one child, so its reaped-child usage
cannot include inference engines or other sandbox jobs.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys


def main() -> int:
    fd = int(sys.argv[1])
    limits = json.loads(sys.argv[2])
    resource = importlib.import_module("resource")
    cpu = max(1, int(limits["cpu_s"]))
    memory = int(limits["max_rss_mb"]) * 1024 * 1024
    pids = int(limits["max_pids"])
    resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu + 5))
    resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
    resource.setrlimit(resource.RLIMIT_NOFILE, (max(512, fd + 1), max(512, fd + 1)))
    resource.setrlimit(resource.RLIMIT_NPROC, (pids, pids))
    with subprocess.Popen(sys.argv[3:], close_fds=True) as child:
        code = child.wait()
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    with os.fdopen(fd, "w") as report:
        json.dump(
            {"cpu_s": usage.ru_utime + usage.ru_stime, "peak_rss_mb": usage.ru_maxrss / 1024},
            report,
        )
    return code if code >= 0 else 128 - code


if __name__ == "__main__":
    raise SystemExit(main())
