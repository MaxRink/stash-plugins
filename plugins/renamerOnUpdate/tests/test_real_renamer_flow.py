"""Run the real renamer entry point against a fake GraphQL transport.

This catches stale bulk dictionaries, repeated hooks, target churn and missing
native file IDs without a live Stash or filesystem mutation.
"""
import json
import pathlib
import sys
import tempfile
import types
import importlib.util
import os

PLUGIN = pathlib.Path(__file__).parents[1]
sys.path.insert(0, str(PLUGIN))


class _Config(types.ModuleType):
    def __getattr__(self, name):
        values = {
            "dry_run": False, "dry_run_append": False, "log_file": "", "enable_hook": True,
            "batch_number_scene": 1, "p_path_templates": {"/library/inbox": "/library/organized"},
            "p_studio_templates": {}, "p_tag_templates": {}, "p_tag_option": {},
            "p_use_default_template": True, "p_default_template": "/library/organized",
            "use_default_template": True, "default_template": "$title", "associated_extension": [],
            "field_whitespaceSeperator": " ", "field_replacer": {}, "filename_as_title": False,
            "lowercase_Filename": False, "titlecase_Filename": False, "filename_splitchar": " ",
            "removecharac_Filename": "", "replace_words": {}, "performer_splitchar": ", ",
            "performer_limit": 99, "performer_limit_keep": True, "performer_sort": "name",
            "performer_ignoreGender": False, "prevent_title_performer": False,
            "duplicate_suffix": ["", "_1", "_2"], "prepositions_list": [],
            "prepositions_removal": False, "squeeze_studio_names": False, "rating_format": "",
            "tags_splitchar": ", ", "tags_whitelist": [], "tags_blacklist": [],
            "ignore_path_length": False, "prevent_consecutive": False, "remove_emptyfolder": False,
            "process_kill_attach": False, "process_getall": False, "use_ascii": False,
            "order_field": [], "alt_diff_display": False, "path_noperformer_folder": "",
            "path_keep_alrperf": False, "p_non_organized": "", "path_one_performer": False,
            "exclude_enabled": False, "exclude_tag_patterns": {}, "exclude_studio_patterns": {},
            "exclude_path_patterns": {}, "require_fields": False, "date_format": "%Y-%m-%d",
            "duration_format": "%H-%M-%S",
        }
        return values.get(name, False)


def _scene():
    return {
        "id": "1", "title": "Scene", "date": "2024-01-01", "organized": True,
        "tags": [], "studio": None, "performers": [], "stash_ids": [],
        "files": [{"id": "77", "path": "/library/inbox/Scene.mkv",
                   "video_codec": "h264", "audio_codec": "aac", "width": 1920,
                   "height": 1080, "frame_rate": 30.0, "duration": 60.0,
                   "bit_rate": 1000000, "fingerprints": [{"type": "oshash", "value": "abc"}]}],
    }


def test_real_entry_refreshes_stale_dict_and_noops(tmp_path, monkeypatch):
    state_path = tmp_path / "scene.json"
    state_path.write_text(json.dumps(_scene()))
    os.environ["ROU_LOCK_DIR"] = str(tmp_path / "locks")
    config = _Config("config")
    log = types.ModuleType("log")
    log.__getattr__ = lambda _name: (lambda *_args, **_kwargs: None)
    monkeypatch.setitem(sys.modules, "config", config)
    monkeypatch.setitem(sys.modules, "log", log)

    class Response:
        status_code = 200
        content = b""
        def __init__(self, data): self.data = data
        def json(self): return self.data

    class Requests(types.ModuleType):
        def post(self, _url, json=None, **_kwargs):
            query = json["query"]
            if "systemStatus" in query:
                return Response({"data": {"systemStatus": {"databaseSchema": 40}}})
            if "configuration" in query:
                return Response({"data": {"configuration": {"general": {"databasePath": "/db/stash.sqlite"}}}})
            scene = __import__("json").loads(state_path.read_text())
            if "findScenes" in query:
                if "scene_filter" in query:
                    return Response({"data": {"findScenes": {"count": 0, "scenes": []}}})
                return Response({"data": {"findScenes": {"count": 1, "scenes": [scene]}}})
            if "findScene" in query:
                return Response({"data": {"findScene": scene}})
            if "moveFiles" in query:
                move_input = json["variables"]["input"]
                selected = move_input["ids"][0]
                for file_info in scene["files"]:
                    if file_info["id"] == selected:
                        file_info["path"] = move_input["destination_folder"] + "/" + move_input["destination_basename"]
                count_path = state_path.with_suffix(".moves")
                count = int(count_path.read_text() or "0") + 1 if count_path.exists() else 1
                # Simulate a primary-file change: after the first complete
                # multi-file hook, Stash returns the files in reverse order.
                if count == 2 and len(scene["files"]) > 1:
                    scene["files"] = list(reversed(scene["files"]))
                state_path.write_text(__import__("json").dumps(scene))
                count_path.write_text(str(count))
                return Response({"data": {"moveFiles": True}})
            return Response({"data": {}})

    monkeypatch.setitem(sys.modules, "requests", Requests("requests"))
    fragment = {"server_connection": {"Port": 9999, "Scheme": "http", "Host": "localhost",
                "SessionCookie": {"Value": "test"}, "PluginDir": str(PLUGIN)},
                "args": {"mode": "bulk", "hookContext": {"type": "Scene.Update.Post", "id": "1"}}}
    monkeypatch.setattr(sys, "stdin", types.SimpleNamespace(read=lambda: json.dumps(fragment)))
    spec = importlib.util.spec_from_file_location("renamer_real_test", PLUGIN / "renamerOnUpdate.py")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "renamer_real_test", module)
    try:
        spec.loader.exec_module(module)
    except SystemExit:
        pass

    stale = _scene()
    stale["files"][0]["path"] = "/library/inbox/stale.mkv"
    before = json.loads(json.dumps(stale))
    module.renamer(stale)
    assert stale == before
    assert json.loads(state_path.read_text())["files"][0]["path"] == "/library/organized/Scene.mkv"
    assert int(state_path.with_suffix(".moves").read_text()) == 1

_REAL_MODULE = None


def _invoke_real_renamer(scene, outcomes, barrier):
    try:
        barrier.wait()
        _REAL_MODULE.renamer(scene)
        outcomes.put("ok")
    except Exception as exc:  # pragma: no cover - surfaced by assertion
        outcomes.put(f"error:{type(exc).__name__}")


def test_real_entry_serializes_simultaneous_hooks(tmp_path, monkeypatch):
    """Two real renamer calls must produce one move and one stable no-op."""
    # Reuse the setup path by invoking the preceding test's fixture logic in a
    # fresh isolated module would obscure the concurrency assertion.  The
    # compact fixture below stubs only the same GraphQL operations.
    state_path = tmp_path / "scene.json"
    state_path.write_text(json.dumps(_scene()))
    os.environ["ROU_LOCK_DIR"] = str(tmp_path / "locks")
    config = _Config("config")
    log = types.ModuleType("log")
    log.__getattr__ = lambda _name: (lambda *_args, **_kwargs: None)
    monkeypatch.setitem(sys.modules, "config", config)
    monkeypatch.setitem(sys.modules, "log", log)

    class Response:
        status_code = 200
        content = b""
        def __init__(self, data): self.data = data
        def json(self): return self.data

    class Requests(types.ModuleType):
        def post(self, _url, json=None, **_kwargs):
            query = json["query"]
            if "systemStatus" in query:
                return Response({"data": {"systemStatus": {"databaseSchema": 40}}})
            if "configuration" in query:
                return Response({"data": {"configuration": {"general": {"databasePath": "/db/stash.sqlite"}}}})
            scene = __import__("json").loads(state_path.read_text())
            if "findScenes" in query:
                if "scene_filter" in query:
                    return Response({"data": {"findScenes": {"count": 0, "scenes": []}}})
                return Response({"data": {"findScenes": {"count": 1, "scenes": [scene]}}})
            if "findScene" in query:
                return Response({"data": {"findScene": scene}})
            if "moveFiles" in query:
                move_input = json["variables"]["input"]
                selected = move_input["ids"][0]
                for file_info in scene["files"]:
                    if file_info["id"] == selected:
                        file_info["path"] = move_input["destination_folder"] + "/" + move_input["destination_basename"]
                count_path = state_path.with_suffix(".moves")
                count = int(count_path.read_text() or "0") + 1 if count_path.exists() else 1
                # Simulate a primary-file change: after the first complete
                # multi-file hook, Stash returns the files in reverse order.
                if count == 2 and len(scene["files"]) > 1:
                    scene["files"] = list(reversed(scene["files"]))
                state_path.write_text(__import__("json").dumps(scene))
                count_path.write_text(str(count))
                return Response({"data": {"moveFiles": True}})
            return Response({"data": {}})

    monkeypatch.setitem(sys.modules, "requests", Requests("requests"))
    fragment = {"server_connection": {"Port": 9999, "Scheme": "http", "Host": "localhost",
                "SessionCookie": {"Value": "test"}, "PluginDir": str(PLUGIN)},
                "args": {"mode": "bulk", "hookContext": {"type": "Scene.Update.Post", "id": "1"}}}
    monkeypatch.setattr(sys, "stdin", types.SimpleNamespace(read=lambda: json.dumps(fragment)))
    spec = importlib.util.spec_from_file_location("renamer_concurrent_test", PLUGIN / "renamerOnUpdate.py")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "renamer_concurrent_test", module)
    try:
        spec.loader.exec_module(module)
    except SystemExit:
        pass
    global _REAL_MODULE
    _REAL_MODULE = module
    # The import above performs one bulk canary move; start the concurrent
    # assertion from a fresh source and clear that canary's count.
    multi = _scene()
    multi["files"].append(dict(multi["files"][0], id="78", path="/library/inbox/Scene_2.mkv"))
    multi["primary_file_id"] = "78"
    multi["files"] = list(reversed(multi["files"]))
    state_path.write_text(json.dumps(multi))
    state_path.with_suffix(".moves").write_text("0")
    monkeypatch.setattr(sys, "stdin", sys.__stdin__)
    ctx = __import__("multiprocessing").get_context("fork" if "fork" in __import__("multiprocessing").get_all_start_methods() else "spawn")
    outcomes = ctx.Queue()
    barrier = ctx.Barrier(2)
    stale = _scene()
    workers = [ctx.Process(target=_invoke_real_renamer, args=(stale, outcomes, barrier)) for _ in range(2)]
    for worker in workers: worker.start()
    for worker in workers: worker.join(5)
    assert all(worker.exitcode == 0 for worker in workers)
    assert sorted(outcomes.get(timeout=1) for _ in workers) == ["ok", "ok"]
    final = json.loads(state_path.read_text())
    assert sorted(file_info["path"] for file_info in final["files"]) == [
        "/library/organized/Scene.mkv", "/library/organized/Scene_1.mkv"
    ]
    assert int(state_path.with_suffix(".moves").read_text()) == 2
