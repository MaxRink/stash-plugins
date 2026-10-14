"""Fail-closed validation for the bounded native single-scene task."""

import ast
from pathlib import Path


SOURCE = Path(__file__).parents[1] / "renamerOnUpdate.py"


def _parser():
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    fn = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "parse_single_scene_id")
    namespace = {}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), str(SOURCE), "exec"), namespace)
    return namespace["parse_single_scene_id"]


def test_single_scene_id_accepts_only_one_positive_numeric_id():
    parse = _parser()
    assert parse({"scene_id": "4441"}) == 4441
    assert parse({"sceneId": 4441}) == 4441
    for args in ({}, {"scene_id": ""}, {"scene_id": "4441,4442"},
                 {"scene_id": 0}, {"scene_id": -1}, {"scene_id": True},
                 {"scene_id": [4441]}):
        try:
            parse(args)
        except ValueError:
            pass
        else:
            raise AssertionError(f"accepted invalid single-scene arguments: {args!r}")
