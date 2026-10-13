import multiprocessing
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))
from scene_lock import scene_lock


def _worker(lock_root, entered, release):
    import os
    os.environ["ROU_LOCK_DIR"] = str(lock_root)
    with scene_lock("42"):
        entered.value += 1
        release.wait(10)


def test_same_scene_hooks_are_serialized(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    entered = ctx.Value("i", 0)
    release = ctx.Event()
    first = ctx.Process(target=_worker, args=(tmp_path, entered, release))
    second = ctx.Process(target=_worker, args=(tmp_path, entered, release))
    first.start()
    deadline = time.time() + 5
    while entered.value != 1 and time.time() < deadline:
        time.sleep(0.01)
    assert entered.value == 1
    second.start()
    time.sleep(0.2)
    assert entered.value == 1
    release.set()
    first.join(5)
    second.join(5)
    assert first.exitcode == 0
    assert second.exitcode == 0
