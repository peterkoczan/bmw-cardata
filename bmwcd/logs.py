"""Size-based rotation for the launchd logs.

Copy-truncate, not rename-and-recreate. launchd opens StandardOutPath itself and
holds an append-mode descriptor on that inode for the life of the job, so a
renamed file keeps receiving every subsequent line and the "current" log stays
empty forever. Truncating in place keeps the inode, and the next write lands at
offset zero.

The tradeoff is the usual one: lines written between the copy and the truncate
are lost. For a progress log that is a fine price; the telemetry itself is in the
raw JSONL, which is never touched by this.
"""

import os
import shutil
from pathlib import Path

from .config import ROOT

# The launchd plists and the systemd units both hardcode <repo>/data/logs as
# their output path, so that is where the logs are regardless of data_dir.
LOG_DIR = ROOT / "data" / "logs"


def _shift(path: Path, keep: int) -> None:
    """stream.log.2 -> stream.log.3, and so on downwards."""
    for index in range(keep - 1, 0, -1):
        src = path.with_name(f"{path.name}.{index}")
        if src.exists():
            os.replace(src, path.with_name(f"{path.name}.{index + 1}"))
    dropped = path.with_name(f"{path.name}.{keep + 1}")
    if dropped.exists():
        dropped.unlink()


def rotate(path: Path, max_bytes: int, keep: int = 3) -> bool:
    try:
        if not path.exists() or path.stat().st_size <= max_bytes:
            return False
        _shift(path, keep)
        shutil.copyfile(path, path.with_name(f"{path.name}.1"))
        with path.open("r+") as fh:
            fh.truncate(0)
        return True
    except OSError:
        # Logging must never be the thing that takes the streamer down.
        return False


def rotate_all(cfg) -> list[str]:
    """Rotate every launchd log that has grown past the limit."""
    # Not cfg.data_dir / "logs": with any relocated data_dir -- a documented,
    # supported setting -- that directory does not exist, the is_dir() guard
    # returned [] from both the 30s in-stream check and the nightly prune, and
    # stream.log grew without bound while everything reported nothing to do.
    directory = LOG_DIR
    if not directory.is_dir():
        return []
    limit = int(cfg.log_max_mb * 1024 * 1024)
    rotated = []
    for path in sorted(directory.iterdir()):
        if path.suffix in {".log", ".err"} and rotate(path, limit, cfg.log_keep):
            rotated.append(path.name)
    return rotated
