"""P149: files a resident creates in the shared village area become readable for the village group.

The runtime runs with umask 077 so that homes, traces and private notes stay private. Commands
inherit that umask, so a script or log a resident put into the shared area (ops/, storage/,
lineage entries, ...) was readable only by its creator: the explorer could not read the
operator's GPU log. After each executed cycle the runtime sweeps the shared area and adds
group read (and group execute where the owner may execute, like chmod g+rX) to entries the
resident itself owns. It never adds write permission, so no resident can change another's
work, and never follows symlinks. Homes and the runtime's own stores are excluded.
A file that must stay private belongs in the resident's home, not in the shared area.
"""
import os
import stat
from pathlib import Path
from typing import Dict, Optional

# Top-level entries of VILLAGE_ROOT that are private or managed by their own services.
EXCLUDED_TOP = frozenset({'users', 'memory', 'web', 'board', 'telemetry', 'run', 'signals', 'stewards'})
SWEEP_LIMIT_DEFAULT = 50000


def wanted_mode(mode: int) -> int:
    """Mode with group read added; group execute for directories and owner-executable files."""
    extra = stat.S_IRGRP
    if stat.S_ISDIR(mode) or mode & stat.S_IXUSR:
        extra |= stat.S_IXGRP
    return stat.S_IMODE(mode) | extra


def _fix(path: str, seen: os.stat_result, uid: int) -> bool:
    """chmod through a no-follow descriptor, only if it is still the inode that was inspected.
    Some shared directories are group-writable, so a peer could swap the name for a symlink
    to a private file between the scan and the chmod."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, 'O_CLOEXEC', 0))
    except OSError:
        return False
    try:
        now = os.fstat(fd)
        if (now.st_ino, now.st_dev, now.st_uid) != (seen.st_ino, seen.st_dev, uid):
            return False
        os.fchmod(fd, wanted_mode(now.st_mode))
        return True
    except OSError:
        return False
    finally:
        os.close(fd)


def share_group_readable(root: Path, uid: Optional[int] = None, limit: int = SWEEP_LIMIT_DEFAULT) -> Dict[str, int]:
    """Make the caller's own entries in the shared area group-readable. Returns counters."""
    uid = os.geteuid() if uid is None else uid
    counts = dict(scanned=0, fixed=0, errors=0)
    try:
        top = [entry for entry in os.scandir(root) if entry.name not in EXCLUDED_TOP]
    except OSError:
        counts['errors'] += 1
        return counts
    stack = list(top)
    while stack and counts['scanned'] < limit:
        entry = stack.pop()
        counts['scanned'] += 1
        try:
            info = entry.stat(follow_symlinks=False)
        except OSError:
            counts['errors'] += 1
            continue
        if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            continue  # symlinks, fifos, sockets: never touched
        if info.st_uid == uid and wanted_mode(info.st_mode) != stat.S_IMODE(info.st_mode):
            if _fix(entry.path, info, uid):
                counts['fixed'] += 1
            else:
                counts['errors'] += 1
        if stat.S_ISDIR(info.st_mode):
            try:
                stack.extend(os.scandir(entry.path))
            except OSError:
                counts['errors'] += 1
    return counts
