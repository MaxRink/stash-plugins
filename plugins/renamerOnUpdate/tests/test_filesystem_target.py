"""Filesystem collision guard tests for orphaned/untracked destination files."""

import ast
import os
from pathlib import Path


SOURCE = Path(__file__).parents[1] / "renamerOnUpdate.py"


def _guard():
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    fn = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "filesystem_target_occupied")
    namespace = {"os": os}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), str(SOURCE), "exec"), namespace)
    return namespace["filesystem_target_occupied"]


def test_orphan_destination_is_occupied(tmp_path):
    guard = _guard()
    current = tmp_path / "incoming.mkv"
    target = tmp_path / "organized.mkv"
    current.write_bytes(b"same content")
    os.link(current, target)
    assert guard(str(target), str(current)) is True
    assert guard(str(current), str(current)) is False

