"""Read host capacity without network calls or optional Python dependencies."""

from __future__ import annotations

import ctypes
import json
import os
import platform
import shutil
import subprocess
from pathlib import Path
from typing import Any


def _command(argv: list[str]) -> str:
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    ).stdout.strip()


def hardware_info() -> dict[str, Any]:
    total = available = None
    cpu = platform.processor() or platform.machine()
    gpus: list[dict[str, Any]] = []
    try:
        if os.name == "nt":

            class MemoryStatus(ctypes.Structure):
                _fields_ = [
                    ("length", ctypes.c_ulong),
                    ("load", ctypes.c_ulong),
                    *[
                        (n, ctypes.c_ulonglong)
                        for n in (
                            "total",
                            "avail",
                            "page",
                            "availpage",
                            "virtual",
                            "availvirtual",
                            "extended",
                        )
                    ],
                ]

            mem = MemoryStatus()
            mem.length = ctypes.sizeof(mem)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(mem)):
                total, available = mem.total / 1024**3, mem.avail / 1024**3
            info = json.loads(
                _command(
                    [
                        "powershell",
                        "-NoProfile",
                        "-NonInteractive",
                        "-Command",
                        "$c=(Get-CimInstance Win32_Processor | Select-Object -First 1).Name; "
                        "$g=@(Get-CimInstance Win32_VideoController | Select-Object Name,AdapterRAM); "
                        "@{cpu=$c;gpus=$g} | ConvertTo-Json -Compress",
                    ]
                )
            )
            cpu = info["cpu"].strip()
            gpus = [
                {
                    "name": g["Name"],
                    "reported_adapter_gb": round((g.get("AdapterRAM") or 0) / 1024**3, 2),
                }
                for g in info["gpus"]
            ]
        elif platform.system() == "Darwin":
            total = int(_command(["sysctl", "-n", "hw.memsize"])) / 1024**3
            cpu = _command(["sysctl", "-n", "machdep.cpu.brand_string"])
            if platform.machine() == "arm64":
                gpus = [{"name": "Apple Silicon / Metal", "unified_memory": True}]
        else:
            values = {
                line.split(":")[0]: int(line.split()[1])
                for line in Path("/proc/meminfo").read_text().splitlines()
                if len(line.split()) >= 2
            }
            total = values["MemTotal"] / 1024**2
            available = values.get("MemAvailable", 0) / 1024**2
            for line in Path("/proc/cpuinfo").read_text().splitlines():
                if line.startswith("model name"):
                    cpu = line.split(":", 1)[1].strip()
                    break
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        pass
    if shutil.which("nvidia-smi"):
        try:
            gpus = []
            for line in _command(
                [
                    "nvidia-smi",
                    "--query-gpu=name,memory.total,memory.free",
                    "--format=csv,noheader,nounits",
                ]
            ).splitlines():
                name, memory, free = (part.strip() for part in line.split(","))
                gpus.append(
                    {
                        "name": name,
                        "vram_gb": round(float(memory) / 1024, 2),
                        "available_vram_gb": round(float(free) / 1024, 2),
                        "backend": "cuda",
                    }
                )
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    return {
        "cpu": cpu,
        "logical_cores": os.cpu_count(),
        "ram_gb": round(total, 1) if total else None,
        "available_ram_gb": round(available, 1) if available is not None else None,
        "gpus": gpus,
        "platform": platform.system(),
        "architecture": platform.machine(),
        "memory_note": "Capacity estimates are advisory. Integrated GPUs share system memory. Benchmark the selected backend.",
    }
