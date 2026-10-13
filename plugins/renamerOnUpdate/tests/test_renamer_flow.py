"""Fake GraphQL hook-flow regression for concurrent/repeated Scene.Update.Post."""
import multiprocessing
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))
from native_move import move_file
from scene_lock import scene_lock


def _hook(lock_root, state, calls, ready):
    os.environ["ROU_LOCK_DIR"] = str(lock_root)
    with scene_lock("scene-7"):
        # This is the renamer's refreshed scene read/no-op gate.  A second
        # post hook must see the destination and avoid another move.
        if state["path"] == "/library/organized/scene.mkv":
            calls.append("noop")
            ready.set()
            return

        def fake_graphql(query, variables):
            assert "mutation MoveFiles" in query
            assert variables["input"]["ids"] == ["77"]
            assert variables["input"]["destination_folder"] == "/library/organized"
            assert variables["input"]["destination_basename"] == "scene.mkv"
            state["path"] = "/library/organized/scene.mkv"
            calls.append("move")
            return {"moveFiles": True}

        move_file(fake_graphql, "77", "/library/organized", "scene.mkv")
        ready.set()


def test_repeated_post_hooks_refresh_and_noop(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    manager = ctx.Manager()
    state = manager.dict(path="/library/inbox/scene.mkv")
    calls = manager.list()
    ready = manager.Event()
    args = (tmp_path, state, calls, ready)
    first = ctx.Process(target=_hook, args=args)
    second = ctx.Process(target=_hook, args=args)
    first.start(); first.join(5)
    assert first.exitcode == 0
    second.start(); second.join(5)
    assert second.exitcode == 0
    assert list(calls) == ["move", "noop"]
    assert state["path"] == "/library/organized/scene.mkv"
    manager.shutdown()
