"""Fail-closed no-clobber moves for renamerOnUpdate."""

import errno
import os


def move_noclobber(source: str, target: str) -> None:
    """Move source to an absent target without replacing either path.

    The plugin's media paths are on one filesystem.  Hardlinking first is
    atomic and preserves the source inode; cross-filesystem moves fail closed.
    """
    fd = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        source_stat = os.fstat(fd)
        if not os.path.isfile(source):
            raise FileNotFoundError(source)
        if os.path.lexists(target):
            raise FileExistsError(errno.EEXIST, "target already exists", target)
        os.link(source, target, follow_symlinks=False)
        try:
            current_stat = os.stat(source, follow_symlinks=False)
            if (source_stat.st_dev, source_stat.st_ino, source_stat.st_size) != (
                current_stat.st_dev, current_stat.st_ino, current_stat.st_size
            ):
                raise RuntimeError("source changed during no-clobber move")
            os.unlink(source)
        except Exception:
            # Only remove the link just created.  If source was externally
            # replaced, samefile is false and that replacement is preserved.
            try:
                if os.path.samefile(source, target):
                    os.unlink(target)
            except (FileNotFoundError, OSError):
                pass
            raise
        target_stat = os.stat(target, follow_symlinks=False)
        if (source_stat.st_dev, source_stat.st_ino) != (target_stat.st_dev, target_stat.st_ino):
            raise RuntimeError("source/target inode changed during no-clobber move")
    finally:
        os.close(fd)
