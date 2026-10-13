"""Cross-process per-scene lock used by the update hook."""

import contextlib
import fcntl
import os
import re
import tempfile


@contextlib.contextmanager
def scene_lock(scene_id):
    lock_root = os.environ.get("ROU_LOCK_DIR", os.path.join(tempfile.gettempdir(), "stash-renamer-locks"))
    os.makedirs(lock_root, mode=0o700, exist_ok=True)
    lock_path = os.path.join(lock_root, "scene-{}.lock".format(re.sub(r"[^A-Za-z0-9_.-]", "_", str(scene_id))))
    with open(lock_path, "a+", encoding="ascii") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
