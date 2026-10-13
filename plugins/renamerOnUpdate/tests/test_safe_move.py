import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))
from safe_move import move_noclobber


def test_move_preserves_inode_and_rejects_existing_target(tmp_path):
    source = tmp_path / "source.bin"
    target = tmp_path / "target.bin"
    source.write_bytes(b"payload")
    inode = source.stat().st_ino
    move_noclobber(str(source), str(target))
    assert not source.exists()
    assert target.read_bytes() == b"payload"
    assert target.stat().st_ino == inode

    other = tmp_path / "other.bin"
    other.write_bytes(b"other")
    try:
        move_noclobber(str(other), str(target))
    except FileExistsError:
        pass
    else:
        raise AssertionError("existing target was overwritten")
    assert target.read_bytes() == b"payload"
    assert other.read_bytes() == b"other"


def test_move_missing_source_fails_closed(tmp_path):
    try:
        move_noclobber(str(tmp_path / "missing"), str(tmp_path / "target"))
    except FileNotFoundError:
        pass
    else:
        raise AssertionError("missing source unexpectedly moved")
