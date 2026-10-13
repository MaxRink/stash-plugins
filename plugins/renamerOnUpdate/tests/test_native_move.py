import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))
from native_move import move_file


def test_native_move_sends_transactional_file_target():
    calls = []
    def fake_graphql(query, variables):
        calls.append((query, variables))
        return {"moveFiles": True}
    move_file(fake_graphql, "42", "/library/organized", "scene.mkv")
    query, variables = calls[0]
    assert "mutation MoveFiles" in query
    assert variables == {"input": {"ids": ["42"], "destination_folder": "/library/organized", "destination_basename": "scene.mkv"}}


def test_native_move_fails_on_rejected_transaction():
    try:
        move_file(lambda *_: {"moveFiles": False}, "42", "/library", "scene.mkv")
    except RuntimeError:
        pass
    else:
        raise AssertionError("rejected native move was accepted")
