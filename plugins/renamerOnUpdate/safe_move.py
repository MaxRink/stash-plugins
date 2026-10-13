"""Fail-closed file moves for renamerOnUpdate.

The renamer runs from multiple Stash hook invocations.  A plain rename/move can
silently replace a target that another invocation selected.  Linking first with
an exclusive target gives us no-clobber semantics and preserves the source
inode; cross-filesystem moves are rejected rather than falling back to an
unsafe copy/overwrite.
"""

import errno
import os


def move_noclobber(source: str, target: str) -> None:
    """Move *source* to an absent *target* without replacing either path.

    Both paths must be on the same filesystem.  The target is created as a
    hardlink before the source is unlinked, so a failed unlink leaves both
    names pointing to the same bytes and callers can recover safely.
    """
    source_stat = os.stat(source, follow_symlinks=False)
    if not os.path.isfile(source):
        raise FileNotFoundError(source)
    if os.path.lexists(target):
        raise FileExistsError(errno.EEXIST, "target already exists", target)
    os.link(source, target, follow_symlinks=False)
    try:
        os.unlink(source)
    except Exception:
        # Remove only the link we just created.  Do not touch a pre-existing
        # target: os.link above fails closed if another process won the race.
        try:
            if os.path.samefile(source, target):
                os.unlink(target)
        except (FileNotFoundError, OSError):
            pass
        raise
    target_stat = os.stat(target, follow_symlinks=False)
    if (source_stat.st_dev, source_stat.st_ino) != (target_stat.st_dev, target_stat.st_ino):
        raise RuntimeError("source/target inode changed during no-clobber move")
