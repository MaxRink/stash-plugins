"""Fake GraphQL hook-flow regression for concurrent/repeated Scene.Update.Post."""
import multiprocessing
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))
from native_move import move_file
from scene_lock import scene_lock


def _hook(lock_root, state_path, calls):
    os.environ["ROU_LOCK_DIR"] = str(lock_root)
    with scene_lock("scene-7"):
        # Refreshed scene read/no-op gate: a second post hook must see the
        # destination and avoid another move.
        if Path(state_path).read_text() == "/library/organized/scene.mkv":
            calls.put("noop")
            return

        def fake_graphql(query, variables):
            assert "mutation MoveFiles" in query
            assert variables["input"]["ids"] == ["77"]
            assert variables["input"]["destination_folder"] == "/library/organized"
            assert variables["input"]["destination_basename"] == "scene.mkv"
            Path(state_path).write_text("/library/organized/scene.mkv")
            calls.put("move")
            return {"moveFiles": True}

        move_file(fake_graphql, "77", "/library/organized", "scene.mkv")


def test_repeated_post_hooks_refresh_and_noop(tmp_path):
    ctx = multiprocessing.get_context("fork" if "fork" in multiprocessing.get_all_start_methods() else "spawn")
    state_path = tmp_path / "state"
    state_path.write_text("/library/inbox/scene.mkv")
    calls = ctx.Queue()
    args = (tmp_path, state_path, calls)
    first = ctx.Process(target=_hook, args=args)
    second = ctx.Process(target=_hook, args=args)
    first.start(); first.join(5)
    assert first.exitcode == 0
    second.start(); second.join(5)
    assert second.exitcode == 0
    outcomes = sorted(calls.get(timeout=1) for _ in (first, second))
    assert outcomes == ["move", "noop"]
    assert state_path.read_text() == "/library/organized/scene.mkv"
