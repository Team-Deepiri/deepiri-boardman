"""Measure the machine Boardman is running on — no self-reported labels.

`team_assignments.yml`'s per-person `tier` (light/standard/heavy) has always been
something a human typed in once and nobody updates when they buy a new laptop.
This measures the same thing from the OS/hardware directly, so it can be reported
automatically (`boardman capability report`) instead of hand-maintained.

Stdlib + optional `nvidia-smi` presence only — no new dependency, works on any box
this CLI already runs on, including ones with no GPU driver at all.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass


@dataclass(frozen=True)
class HardwareSnapshot:
    cores: int
    ram_gb: float
    has_gpu: bool
    gpu_name: str = ""


def _ram_gb() -> float:
    # Linux: /proc/meminfo is universal and needs no dependency. Fall back to 0.0
    # (unknown) on platforms without it rather than guessing.
    try:
        with open("/proc/meminfo", encoding="ascii") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    kb = int(line.split()[1])
                    return round(kb / (1024 * 1024), 1)
    except (OSError, ValueError, IndexError):
        pass
    return 0.0


def _gpu() -> tuple[bool, str]:
    # nvidia-smi's presence on PATH is itself the signal — no driver, no binary.
    # This deliberately does not try to detect AMD/Intel GPUs: the tier policy
    # this feeds (see capability_tier below) only distinguishes "has a real GPU
    # for heavy/ML work" vs not, and nvidia-smi is the common case for that.
    exe = shutil.which("nvidia-smi")
    if not exe:
        return False, ""
    try:
        out = subprocess.run(
            [exe, "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        name = out.stdout.strip().splitlines()[0].strip() if out.stdout.strip() else ""
        return True, name
    except (OSError, subprocess.SubprocessError, IndexError):
        return True, ""  # binary exists but query failed — still "has a GPU"


def measure_hardware() -> HardwareSnapshot:
    cores = os.cpu_count() or 1
    ram = _ram_gb()
    has_gpu, gpu_name = _gpu()
    return HardwareSnapshot(cores=cores, ram_gb=ram, has_gpu=has_gpu, gpu_name=gpu_name)


# Thresholds are policy (how many cores/GB count as "heavy"), not per-person data —
# tune these, never hand-assign a tier to a specific name.
def capability_tier(snap: HardwareSnapshot) -> str:
    """Map a measured snapshot to the light/standard/heavy vocabulary qa_picker.py
    already understands (see boardman/assignment/config.py TeamMember.tier)."""
    if snap.has_gpu and snap.ram_gb >= 16 and snap.cores >= 8:
        return "heavy"
    if snap.ram_gb >= 8 and snap.cores >= 4:
        return "standard"
    return "light"
