"""Permission normalization is opt-in and never part of a normal move."""

import ast
from pathlib import Path


SOURCE = Path(__file__).parents[1] / "renamerOnUpdate.py"


def _load_normalize_permissions(os_module, enabled):
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    node = next(
        item for item in tree.body
        if isinstance(item, ast.FunctionDef) and item.name == "normalize_permissions"
    )
    namespace = {"os": os_module, "NORMALIZE_PERMISSIONS": enabled}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(SOURCE), "exec"), namespace)
    return namespace["normalize_permissions"]


class _FakeOS:
    def __init__(self):
        self.calls = []

    def chown(self, *args):
        self.calls.append(("chown", args))

    def chmod(self, *args):
        self.calls.append(("chmod", args))


def test_normalization_is_disabled_by_default_without_touching_acl():
    fake_os = _FakeOS()
    normalize = _load_normalize_permissions(fake_os, enabled=False)

    assert normalize("/media/file.mp4", "/media") is False
    assert fake_os.calls == []


def test_explicit_normalization_preserves_legacy_values():
    fake_os = _FakeOS()
    normalize = _load_normalize_permissions(fake_os, enabled=True)

    assert normalize("/media/file.mp4", "/media") is True
    assert fake_os.calls == [
        ("chown", ("/media/file.mp4", 99, 100)),
        ("chmod", ("/media/file.mp4", 0o664)),
        ("chown", ("/media", 99, 100)),
        ("chmod", ("/media", 0o775)),
    ]
