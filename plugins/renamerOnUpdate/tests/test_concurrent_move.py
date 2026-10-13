import multiprocessing
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))
from safe_move import move_noclobber
from scene_lock import scene_lock


def _move_once(root, barrier, results):
    os.environ["ROU_LOCK_DIR"] = str(root / "locks")
    source = root / "source.bin"
    target = root / "target.bin"
    barrier.wait()
    try:
        with scene_lock("scene-1"):
            move_noclobber(str(source), str(target))
        results.put("moved")
    except (FileExistsError, FileNotFoundError):
        results.put("rejected")


def test_two_hook_invocations_cannot_overwrite_target(tmp_path):
    source = tmp_path / "source.bin"
    source.write_bytes(b"stable bytes")
    ctx = multiprocessing.get_context("spawn")
    barrier = ctx.Barrier(2)
    results = ctx.Queue()
    workers = [ctx.Process(target=_move_once, args=(tmp_path, barrier, results)) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(5)
    assert all(worker.exitcode == 0 for worker in workers)
    outcomes = sorted(results.get(timeout=1) for _ in workers)
    assert outcomes == ["moved", "rejected"]
    assert (tmp_path / "target.bin").read_bytes() == b"stable bytes"
