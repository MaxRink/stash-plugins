import difflib
import json
import os
import re
import shutil
import sys
import time
import traceback
from datetime import datetime

import requests

try:
    import psutil  # pip install psutil

    MODULE_PSUTIL = True
except Exception:
    MODULE_PSUTIL = False

try:
    import unidecode  # pip install Unidecode

    MODULE_UNIDECODE = True
except Exception:
    MODULE_UNIDECODE = False

import log

try:
    from native_move import move_file as native_move_file
    from safe_move import move_noclobber
    from scene_lock import scene_lock
except ImportError:
    from .native_move import move_file as native_move_file
    from .safe_move import move_noclobber
    from .scene_lock import scene_lock

try:
    import config
except Exception:
    log.LogWarning("Could not import ROU config file, did you rename the template file to 'config.py'? Defaulting to template config file")
    import renamerOnUpdate_config as config


DB_VERSION_FILE_REFACTOR = 32
DB_VERSION_SCENE_STUDIO_CODE = 38

DRY_RUN = config.dry_run
DRY_RUN_FILE = None

if config.log_file:
    DRY_RUN_FILE = os.path.join(
        os.path.dirname(config.log_file), "renamerOnUpdate_dryrun.txt"
    )

if DRY_RUN:
    if DRY_RUN_FILE and not config.dry_run_append:
        if os.path.exists(DRY_RUN_FILE):
            os.remove(DRY_RUN_FILE)
    log.LogInfo("Dry mode on")

START_TIME = time.time()
FRAGMENT = json.loads(sys.stdin.read())

FRAGMENT_SERVER = FRAGMENT["server_connection"]
PLUGIN_DIR = FRAGMENT_SERVER["PluginDir"]


PLUGIN_ARGS = FRAGMENT["args"].get("mode")
SINGLE_DRY_RUN = False


def parse_single_scene_id(args):
    """Return the one explicitly requested scene ID for the bounded task.

    Stash versions have passed task argument maps through slightly different
    keys.  Accept only one scalar ID and fail closed for every other shape.
    """
    raw = args.get("scene_id", args.get("sceneId", args.get("id")))
    if isinstance(raw, bool) or raw is None:
        raise ValueError("single-scene task requires exactly one numeric scene_id")
    value = str(raw).strip()
    if not value.isdigit() or int(value) <= 0:
        raise ValueError("single-scene task requires exactly one numeric scene_id")
    return int(value)


def filesystem_target_occupied(target_path, current_path):
    """Reject a native move target already occupied outside Stash's DB."""
    if os.path.normcase(os.path.abspath(target_path)) == os.path.normcase(os.path.abspath(current_path)):
        return False
    return os.path.lexists(target_path)

# log.LogDebug("{}".format(FRAGMENT))


def callGraphQL(query, variables=None):
    # Session cookie for authentication
    graphql_port = str(FRAGMENT_SERVER["Port"])
    graphql_scheme = FRAGMENT_SERVER["Scheme"]
    graphql_cookies = {"session": FRAGMENT_SERVER["SessionCookie"]["Value"]}
    graphql_headers = {
        "Accept-Encoding": "gzip, deflate, br",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Connection": "keep-alive",
        "DNT": "1",
    }
    graphql_domain = FRAGMENT_SERVER["Host"]
    if graphql_domain == "0.0.0.0":
        graphql_domain = "localhost"
    # Stash GraphQL endpoint
    graphql_url = f"{graphql_scheme}://{graphql_domain}:{graphql_port}/graphql"

    json = {"query": query}
    if variables is not None:
        json["variables"] = variables
    try:
        response = requests.post(
            graphql_url,
            json=json,
            headers=graphql_headers,
            cookies=graphql_cookies,
            timeout=20,
        )
    except Exception as e:
        exit_plugin(err=f"[FATAL] Error with the graphql request {e}")
    if response.status_code == 200:
        result = response.json()
        if result.get("error"):
            for error in result["error"]["errors"]:
                raise Exception(f"GraphQL error: {error}")
            return None
        if result.get("data"):
            return result.get("data")
    elif response.status_code == 401:
        exit_plugin(err="HTTP Error 401, Unauthorised.")
    else:
        raise ConnectionError(
            f"GraphQL query failed: {response.status_code} - {response.content}"
        )


def graphql_getScene(scene_id):
    query = (
        """
    query FindScene($id: ID!, $checksum: String) {
        findScene(id: $id, checksum: $checksum) {
            ...SceneData
        }
    }
    fragment SceneData on Scene {
        id
        title
        date
        rating100
        stash_ids {
            endpoint
            stash_id
        }
        organized"""
        + FILE_QUERY
        + """
        studio {
            id
            name
            parent_studio {
                id
                name
            }
        }
        tags {
            id
            name
        }
        performers {
            id
            name
            gender
            favorite
            rating100
            stash_ids{
                endpoint
                stash_id
            }
        }
        movies {
            movie {
                name
                date
            }
            scene_index
        }
    }
    """
    )
    variables = {"id": scene_id}
    result = callGraphQL(query, variables)
    return result.get("findScene")


# used for bulk
def graphql_findScene(perPage, direc="DESC") -> dict:
    query = (
        """
    query FindScenes($filter: FindFilterType) {
        findScenes(filter: $filter) {
            count
            scenes {
                ...SlimSceneData
            }
        }
    }
    fragment SlimSceneData on Scene {
        id
        title
        date
        rating100
        organized
        stash_ids {
            endpoint
            stash_id
        }
    """
        + FILE_QUERY
        + """
        studio {
            id
            name
            parent_studio {
                id
                name
            }
        }
        tags {
            id
            name
        }
        performers {
            id
            name
            gender
            favorite
            rating100
            stash_ids{
                endpoint
                stash_id
            }
        }
        movies {
            movie {
                name
                date
            }
            scene_index
        }
    }
    """
    )
    # ASC DESC
    variables = {
        "filter": {
            "direction": direc,
            "page": 1,
            "per_page": perPage,
            "sort": "updated_at",
        }
    }
    result = callGraphQL(query, variables)
    return result.get("findScenes")


# used to find duplicate
def graphql_findScenebyPath(path, modifier) -> dict:
    query = """
    query FindScenes($filter: FindFilterType, $scene_filter: SceneFilterType) {
        findScenes(filter: $filter, scene_filter: $scene_filter) {
            count
            scenes {
                id
                title
            }
        }
    }
    """
    # ASC DESC
    variables = {
        "filter": {"direction": "ASC", "page": 1, "per_page": 40, "sort": "updated_at"},
        "scene_filter": {"path": {"modifier": modifier, "value": path}},
    }
    result = callGraphQL(query, variables)
    return result.get("findScenes")


def graphql_getConfiguration():
    query = """
        query Configuration {
            configuration {
                general {
                    databasePath
                }
            }
        }
    """
    result = callGraphQL(query)
    return result.get("configuration")


def graphql_getStudio(studio_id):
    query = """
        query FindStudio($id:ID!) {
            findStudio(id: $id) {
                id
                name
                parent_studio {
                    id
                    name
                }
            }
        }
    """
    variables = {"id": studio_id}
    result = callGraphQL(query, variables)
    return result.get("findStudio")


def graphql_move_file(file_id: str, destination_folder: str, destination_basename: str):
    return native_move_file(callGraphQL, file_id, destination_folder, destination_basename)


def graphql_removeScenesTag(id_scenes: list, id_tags: list):
    query = """
    mutation BulkSceneUpdate($input: BulkSceneUpdateInput!) {
        bulkSceneUpdate(input: $input) {
            id
        }
    }
    """
    variables = {
        "input": {"ids": id_scenes, "tag_ids": {"ids": id_tags, "mode": "REMOVE"}}
    }
    result = callGraphQL(query, variables)
    return result


def graphql_getBuild():
    query = """
        {
            systemStatus {
                databaseSchema
            }
        }
    """
    result = callGraphQL(query)
    return result["systemStatus"]["databaseSchema"]


def find_diff_text(a: str, b: str):
    addi = minus = stay = ""
    minus_ = addi_ = 0
    for _, s in enumerate(difflib.ndiff(a, b)):
        if s[0] == " ":
            stay += s[-1]
            minus += "*"
            addi += "*"
        elif s[0] == "-":
            minus += s[-1]
            minus_ += 1
        elif s[0] == "+":
            addi += s[-1]
            addi_ += 1
    if minus_ > 20 or addi_ > 20:
        log.LogDebug(f"Diff Checker: +{addi_}; -{minus_};")
        log.LogDebug(f"OLD: {a}")
        log.LogDebug(f"NEW: {b}")
    else:
        log.LogDebug(
            f"Original: {a}\n- Charac: {minus}\n+ Charac: {addi}\n  Result: {b}"
        )
    return


def has_handle(fpath, all_result=False):
    lst = []
    for proc in psutil.process_iter():
        try:
            for item in proc.open_files():
                if fpath == item.path:
                    if all_result:
                        lst.append(proc)
                    else:
                        return proc
        except Exception:
            pass
    return lst


def config_edit(name: str, state: bool):
    found = 0
    try:
        with open(config.__file__, "r", encoding="utf8") as file:
            config_lines = file.readlines()
        with open(config.__file__, "w", encoding="utf8") as file_w:
            for line in config_lines:
                if len(line.split("=")) > 1:
                    if name == line.split("=")[0].strip():
                        file_w.write(f"{name} = {state}\n")
                        found += 1
                        continue
                file_w.write(line)
    except PermissionError as err:
        log.LogError(f"You don't have the permission to edit config.py ({err})")
    return found


def check_longpath(path: str):
    # Trying to prevent error with long paths for Win10
    # https://docs.microsoft.com/en-us/windows/win32/fileio/maximum-file-path-limitation?tabs=cmd
    if len(path) > 240 and not IGNORE_PATH_LENGTH:
        log.LogError(
            f"The path is too long ({len(path)} > 240). You can look at 'order_field'/'ignore_path_length' in config."
        )
        return 1


def get_template_filename(scene: dict):
    template = None
    # Change by Studio
    if scene.get("studio") and config.studio_templates:
        template_found = False
        current_studio = scene.get("studio")
        if config.studio_templates.get(current_studio["name"]):
            template = config.studio_templates[current_studio["name"]]
            template_found = True
        # by first Parent found
        while current_studio.get("parent_studio") and not template_found:
            if config.studio_templates.get(
                current_studio.get("parent_studio").get("name")
            ):
                template = config.studio_templates[
                    current_studio["parent_studio"]["name"]
                ]
                template_found = True
            current_studio = graphql_getStudio(
                current_studio.get("parent_studio")["id"]
            )

    # Change by Tag
    tags = [x["name"] for x in scene["tags"]]
    if scene.get("tags") and config.tag_templates:
        for match, job in config.tag_templates.items():
            if match in tags:
                template = job
                break
    return template


def get_template_path(scene: dict):
    template = {"destination": "", "option": [], "opt_details": {}}
    # Change by Path
    if config.p_path_templates:
        for match, job in config.p_path_templates.items():
            if match in scene["path"]:
                template["destination"] = job
                break

    # Change by Studio
    if scene.get("studio") and config.p_studio_templates:
        if config.p_studio_templates.get(scene["studio"]["name"]):
            template["destination"] = config.p_studio_templates[scene["studio"]["name"]]
        # by Parent
        if scene["studio"].get("parent_studio"):
            if config.p_studio_templates.get(scene["studio"]["name"]):
                template["destination"] = config.p_studio_templates[
                    scene["studio"]["name"]
                ]

    # Change by Tag
    tags = [x["name"] for x in scene["tags"]]
    if scene.get("tags") and config.p_tag_templates:
        for match, job in config.p_tag_templates.items():
            if match in tags:
                template["destination"] = job
                break

    if scene.get("tags") and config.p_tag_option:
        for tag in scene["tags"]:
            if config.p_tag_option.get(tag["name"]):
                opt = config.p_tag_option[tag["name"]]
                template["option"].extend(opt)
                if "clean_tag" in opt:
                    if template["opt_details"].get("clean_tag"):
                        template["opt_details"]["clean_tag"].append(tag["id"])
                    else:
                        template["opt_details"] = {"clean_tag": [tag["id"]]}
    if not scene["organized"] and PATH_NON_ORGANIZED:
        template["destination"] = PATH_NON_ORGANIZED
    return template


def sort_performer(lst_use: list, lst_app=[]):
    for p in lst_use:
        lst_use[p].sort()
    for p in lst_use.values():
        for n in p:
            if n not in lst_app:
                lst_app.append(n)
    return lst_app


def sort_rating(d: dict):
    new_d = {}
    for i in sorted(d.keys(), reverse=True):
        new_d[i] = d[i]
    return new_d


def extract_info(scene: dict, template: None):
    # Grabbing things from Stash
    scene_information = {}

    scene_information["current_path"] = str(scene["path"])
    # note: contain the dot (.mp4)
    scene_information["file_extension"] = os.path.splitext(
        scene_information["current_path"]
    )[1]
    # note: basename contains the extension
    scene_information["current_filename"] = os.path.basename(
        scene_information["current_path"]
    )
    scene_information["current_directory"] = os.path.dirname(
        scene_information["current_path"]
    )
    scene_information["oshash"] = scene.get("oshash")
    scene_information["checksum"] = scene.get("checksum")
    scene_information["studio_code"] = scene.get("code")

    if scene.get("stash_ids"):
        # todo support other db that stashdb ?
        scene_information["stashid_scene"] = scene["stash_ids"][0]["stash_id"]

    if template.get("path"):
        if "^*" in template["path"]["destination"]:
            template["path"]["destination"] = template["path"]["destination"].replace(
                "^*", scene_information["current_directory"]
            )
        scene_information["template_split"] = os.path.normpath(
            template["path"]["destination"]
        ).split(os.sep)
    scene_information["current_path_split"] = os.path.normpath(
        scene_information["current_path"]
    ).split(os.sep)

    if FILENAME_ASTITLE and not scene.get("title"):
        scene["title"] = scene_information["current_filename"]

    # Grab Title (without extension if present)
    if scene.get("title"):
        # Removing extension if present in title
        scene_information["title"] = re.sub(
            rf"{scene_information['file_extension']}$", "", scene["title"]
        )
        if PREPOSITIONS_REMOVAL:
            for word in PREPOSITIONS_LIST:
                scene_information["title"] = re.sub(
                    rf"^{word}[\s_-]", "", scene_information["title"]
                )

    # Grab Date
    scene_information["date"] = scene.get("date")
    if scene_information["date"]:
        # Handle partial dates: Stash may return only YYYY or YYYY-MM instead of YYYY-MM-DD
        raw_date = scene_information["date"]
        date_scene = None
        for fmt in (r"%Y-%m-%d", r"%Y-%m", r"%Y"):
            try:
                date_scene = datetime.strptime(raw_date, fmt)
                break
            except ValueError:
                continue
        if date_scene:
            scene_information["date_format"] = datetime.strftime(
                date_scene, config.date_format
            )
        else:
            log.LogWarning(f"Could not parse date '{raw_date}', skipping date formatting")
            scene_information["date"] = None

    # Grab Duration
    scene_information["duration"] = scene["file"]["duration"]
    if config.duration_format:
        scene_information["duration"] = time.strftime(
            config.duration_format, time.gmtime(scene_information["duration"])
        )
    else:
        scene_information["duration"] = str(scene_information["duration"])

    # Grab Rating
    if scene.get("rating100"):
        scene_information["rating"] = RATING_FORMAT.format(scene["rating100"])

    # Grab Performer
    scene_information["performer_path"] = None
    if scene.get("performers"):
        perf_list = []
        perf_list_stashid = []
        perf_rating = {"0": []}
        perf_favorite = {"yes": [], "no": []}
        for perf in scene["performers"]:
            if perf.get("gender"):
                if perf["gender"] in PERFORMER_IGNOREGENDER:
                    continue
            elif "UNDEFINED" in PERFORMER_IGNOREGENDER:
                continue
            # path related
            if template.get("path"):
                if "inverse_performer" in template["path"]["option"]:
                    perf["name"] = re.sub(
                        r"([a-zA-Z]+)(\s)([a-zA-Z]+)", r"\3 \1", perf["name"]
                    )
            perf_list.append(perf["name"])
            if perf.get("rating100"):
                if perf_rating.get(str(perf["rating100"])) is None:
                    perf_rating[str(perf["rating100"])] = []
                perf_rating[str(perf["rating100"])].append(perf["name"])
            else:
                perf_rating["0"].append(perf["name"])
            if perf.get("favorite"):
                perf_favorite["yes"].append(perf["name"])
            else:
                perf_favorite["no"].append(perf["name"])
            # if the path already contains the name we keep this one
            if (
                perf["name"] in scene_information["current_path_split"]
                and scene_information.get("performer_path") is None
                and PATH_KEEP_ALRPERF
            ):
                scene_information["performer_path"] = perf["name"]
                log.LogDebug(
                    f"[PATH] Keeping the current name of the performer '{perf['name']}'"
                )
        perf_rating = sort_rating(perf_rating)
        # sort performer
        if PERFORMER_SORT == "rating":
            # sort alpha
            perf_list = sort_performer(perf_rating)
        elif PERFORMER_SORT == "favorite":
            perf_list = sort_performer(perf_favorite)
        elif PERFORMER_SORT == "mix":
            perf_list = []
            for p in perf_favorite:
                perf_favorite[p].sort()
            for p in perf_favorite.get("yes"):
                perf_list.append(p)
            perf_list = sort_performer(perf_rating, perf_list)
        elif PERFORMER_SORT == "mixid":
            perf_list = []
            for p in perf_favorite.get("yes"):
                perf_list.append(p)
            for p in perf_rating.values():
                for n in p:
                    if n not in perf_list:
                        perf_list.append(n)
        elif PERFORMER_SORT == "name":
            perf_list.sort()
        if not scene_information["performer_path"] and perf_list:
            scene_information["performer_path"] = perf_list[0]
        if len(perf_list) > PERFORMER_LIMIT:
            if not PERFORMER_LIMIT_KEEP:
                log.LogInfo(
                    f"More than {PERFORMER_LIMIT} performer(s). Ignoring $performer"
                )
                perf_list = []
            else:
                log.LogInfo(f"Limited the amount of performer to {PERFORMER_LIMIT}")
                perf_list = perf_list[0:PERFORMER_LIMIT]
        scene_information["performer"] = PERFORMER_SPLITCHAR.join(perf_list)
        if perf_list:
            for p in perf_list:
                for perf in scene["performers"]:
                    # todo support other db that stashdb ?
                    if p == perf["name"] and perf.get("stash_ids"):
                        perf_list_stashid.append(perf["stash_ids"][0]["stash_id"])
                        break
            scene_information["stashid_performer"] = PERFORMER_SPLITCHAR.join(
                perf_list_stashid
            )
        if not PATH_ONEPERFORMER:
            scene_information["performer_path"] = PERFORMER_SPLITCHAR.join(perf_list)
    elif PATH_NOPERFORMER_FOLDER:
        scene_information["performer_path"] = "NoPerformer"

    # Grab Studio name
    if scene.get("studio"):
        if SQUEEZE_STUDIO_NAMES:
            scene_information["studio"] = scene["studio"]["name"].replace(" ", "")
        else:
            scene_information["studio"] = scene["studio"]["name"]
        scene_information["studio_family"] = scene_information["studio"]
        studio_hierarchy = [scene_information["studio"]]
        # Grab Parent name
        if scene["studio"].get("parent_studio"):
            if SQUEEZE_STUDIO_NAMES:
                scene_information["parent_studio"] = scene["studio"]["parent_studio"][
                    "name"
                ].replace(" ", "")
            else:
                scene_information["parent_studio"] = scene["studio"]["parent_studio"][
                    "name"
                ]
            scene_information["studio_family"] = scene_information["parent_studio"]

            studio_p = scene["studio"]
            while studio_p.get("parent_studio"):
                studio_p = graphql_getStudio(studio_p["parent_studio"]["id"])
                if studio_p:
                    if SQUEEZE_STUDIO_NAMES:
                        studio_hierarchy.append(studio_p["name"].replace(" ", ""))
                    else:
                        studio_hierarchy.append(studio_p["name"])
            studio_hierarchy.reverse()
        scene_information["studio_hierarchy"] = studio_hierarchy
    # Grab Tags
    if scene.get("tags"):
        tag_list = []
        for tag in scene["tags"]:
            # ignore tag in blacklist
            if tag["name"] in TAGS_BLACKLIST:
                continue
            # check if there is a whilelist
            if len(TAGS_WHITELIST) > 0:
                if tag["name"] in TAGS_WHITELIST:
                    tag_list.append(tag["name"])
            else:
                tag_list.append(tag["name"])
        scene_information["tags"] = TAGS_SPLITCHAR.join(tag_list)

    # Grab Height (720p,1080p,4k...)
    scene_information["bit_rate"] = str(
        round(int(scene["file"]["bit_rate"]) / 1000000, 2)
    )
    scene_information["resolution"] = "SD"
    scene_information["height"] = f"{scene['file']['height']}p"
    if scene["file"]["height"] >= 720:
        scene_information["resolution"] = "HD"
    if scene["file"]["height"] >= 2160:
        scene_information["height"] = "4k"
        scene_information["resolution"] = "UHD"
    if scene["file"]["height"] >= 2880:
        scene_information["height"] = "5k"
    if scene["file"]["height"] >= 3384:
        scene_information["height"] = "6k"
    if scene["file"]["height"] >= 4320:
        scene_information["height"] = "8k"
    # For Phone ?
    if scene["file"]["height"] > scene["file"]["width"]:
        scene_information["resolution"] = "VERTICAL"

    if scene.get("movies"):
        scene_information["movie_title"] = scene["movies"][0]["movie"]["name"]
        if scene["movies"][0]["movie"].get("date"):
            scene_information["movie_year"] = scene["movies"][0]["movie"]["date"][0:4]
        if scene["movies"][0].get("scene_index"):
            scene_information["movie_index"] = scene["movies"][0]["scene_index"]
            scene_information["movie_scene"] = (
                f"scene {scene_information['movie_index']}"
            )

    # Grab Video and Audio codec
    scene_information["video_codec"] = scene["file"]["video_codec"].upper()
    scene_information["audio_codec"] = scene["file"]["audio_codec"].upper()

    if scene_information.get("date"):
        scene_information["year"] = scene_information["date"][0:4]

    if FIELD_WHITESPACE_SEP:
        for key, value in scene_information.items():
            if key in [
                "current_path",
                "current_filename",
                "current_directory",
                "current_path_split",
                "template_split",
            ]:
                continue
            if type(value) is str:
                scene_information[key] = value.replace(" ", FIELD_WHITESPACE_SEP)
            elif type(value) is list:
                scene_information[key] = [
                    x.replace(" ", FIELD_WHITESPACE_SEP) for x in value
                ]
    return scene_information


def replace_text(text: str):
    for old, new in FILENAME_REPLACEWORDS.items():
        if type(new) is str:
            new = [new]
        if len(new) > 1:
            if new[1] == "regex":
                tmp = re.sub(old, new[0], text)
                if tmp != text:
                    log.LogDebug(f"Regex matched: {text} -> {tmp}")
            else:
                if new[1] == "word":
                    tmp = re.sub(rf"([\s_-])({old})([\s_-])", f"\\1{new[0]}\\3", text)
                elif new[1] == "any":
                    tmp = text.replace(old, new[0])
                if tmp != text:
                    log.LogDebug(f"'{old}' changed with '{new[0]}'")
        else:
            tmp = re.sub(rf"([\s_-])({old})([\s_-])", f"\\1{new[0]}\\3", text)
            if tmp != text:
                log.LogDebug(f"'{old}' changed with '{new[0]}'")
        text = tmp
    return tmp


def cleanup_text(text: str):
    text = re.sub(r"\(\W*\)|\[\W*\]|{[^a-zA-Z0-9]*}", "", text)
    text = re.sub(r"[{}]", "", text)
    text = remove_consecutive_nonword(text)
    return text.strip(" -_.")


def remove_consecutive_nonword(text: str):
    for _ in range(0, 10):
        m = re.findall(r"(\W+)\1+", text)
        if m:
            text = re.sub(r"(\W+)\1+", r"\1", text)
        else:
            break
    return text


def field_replacer(text: str, scene_information: dict):
    field_found = re.findall(r"\$\w+", text)
    result = text
    title = None
    replaced_word = ""
    if field_found:
        field_found.sort(key=len, reverse=True)
    for i in range(0, len(field_found)):
        f = field_found[i].replace("$", "").strip("_")
        # If $performer is before $title, prevent having duplicate text.
        if (
            f == "performer"
            and len(field_found) > i + 1
            and scene_information.get("performer")
        ):
            if (
                field_found[i + 1] == "$title"
                and scene_information.get("title")
                and PREVENT_TITLE_PERF
            ):
                if re.search(
                    f"^{scene_information['performer'].lower()}",
                    scene_information["title"].lower(),
                ):
                    log.LogDebug(
                        "Ignoring the performer field because it's already in start of title"
                    )
                    result = result.replace("$performer", "")
                    continue
        replaced_word = scene_information.get(f)
        if not replaced_word:
            replaced_word = ""
        if FIELD_REPLACER.get(f"${f}"):
            replaced_word = replaced_word.replace(
                FIELD_REPLACER[f"${f}"]["replace"], FIELD_REPLACER[f"${f}"]["with"]
            )
        if f == "title":
            title = replaced_word.strip()
            continue
        if replaced_word == "":
            result = result.replace(field_found[i], replaced_word)
        else:
            result = result.replace(f"${f}", replaced_word)
    return result, title


def makeFilename(scene_information: dict, query: str) -> str:
    new_filename = str(query)
    r, t = field_replacer(new_filename, scene_information)
    if FILENAME_REPLACEWORDS:
        r = replace_text(r)
    if not t:
        r = r.replace("$title", "")
    r = cleanup_text(r)
    if t:
        r = r.replace("$title", t)
    # Replace spaces with splitchar
    r = r.replace(" ", FILENAME_SPLITCHAR)
    return r


def makePath(scene_information: dict, query: str) -> str:
    new_filename = str(query)
    new_filename = new_filename.replace("$performer", "$performer_path")
    r, t = field_replacer(new_filename, scene_information)
    if not t:
        r = r.replace("$title", "")
    r = cleanup_text(r)
    if t:
        r = r.replace("$title", t)
    return r


def capitalizeWords(s: str) -> str:
    """
    Converts a filename to title case. Capitalizes all words except for certain
    conjunctions, prepositions, and articles, unless they are the first or
    last word of a segment of the filename. Recognizes standard apostrophes, right
    single quotation marks (U+2019), and left single quotation marks (U+2018) within words.

    Ignores all caps words and abbreviations, e.g., MILF, BBW, VR, PAWGs.
    Ignores words with mixed case, e.g., LaSirena69, VRCosplayX, xHamster.
    Ignores resolutions, e.g., 1080p, 4k.

    Args:
        s (str): The string to capitalize.

    Returns:
        str: The capitalized string.

    Raises:
        ValueError: If the input is not a string.

    About the regex:
        The first \b marks the starting word boundary.
        [A-Z]? allows for an optional initial uppercase letter.
        [a-z\'\u2019\u2018]+ matches one or more lowercase letters, apostrophes, right single quotation marks, or left single quotation marks.
            If a word contains multiple uppercase letters, it does not match.
        The final \b marks the ending word boundary, ensuring the expression matches whole words.
    """
    if not isinstance(s, str):
        raise ValueError("Input must be a string.")

    # Function to capitalize words based on their position and value.
    def process_word(match):
        word = match.group(0)
        preceding_char, following_char = None, None

        # List of words to avoid capitalizing if found between other words.
        exceptions = {"and", "of", "the"}

        # Find the nearest non-space character before the current word
        if match.start() > 0:
            for i in range(match.start() - 1, -1, -1):
                if not match.string[i].isspace():
                    preceding_char = match.string[i]
                    break

        # Find the nearest non-space character after the current word
        if match.end() < len(s):
            for i in range(match.end(), len(s)):
                if not match.string[i].isspace():
                    following_char = match.string[i]
                    break

        # Determine capitalization based on the position and the exception rules
        if (
            match.start() == 0
            or match.end() == len(s)
            or word.lower() not in exceptions
            or (preceding_char and not preceding_char.isalnum())
            or (following_char and not following_char.isalnum())
        ):
            return word.capitalize()
        else:
            return word.lower()

    # Apply the regex pattern and the process_word function.
    return re.sub(r"\b[A-Z]?[a-z\'\u2019\u2018]+\b", process_word, s)


def create_new_filename(scene_info: dict, template: str):
    new_filename = (
        makeFilename(scene_info, template)
        + DUPLICATE_SUFFIX[scene_info["file_index"]]
        + scene_info["file_extension"]
    )
    if FILENAME_LOWER:
        new_filename = new_filename.lower()
    if FILENAME_TITLECASE:
        new_filename = capitalizeWords(new_filename)
    # Remove illegal character for Windows
    new_filename = re.sub('[\\/:"*?<>|]+', "", new_filename)

    if FILENAME_REMOVECHARACTER:
        new_filename = re.sub(f"[{FILENAME_REMOVECHARACTER}]+", "", new_filename)

    # Trying to remove non standard character
    if MODULE_UNIDECODE and UNICODE_USE:
        new_filename = unidecode.unidecode(new_filename, errors="preserve")
    else:
        # Using typewriter for Apostrophe
        new_filename = re.sub("[’‘”“]+", "'", new_filename)
    return new_filename


def remove_consecutive(liste: list):
    new_list = []
    for i in range(0, len(liste)):
        if i != 0 and liste[i] == liste[i - 1]:
            continue
        new_list.append(liste[i])
    return new_list


def create_new_path(scene_info: dict, template: dict):
    # Create the new path
    # Split the template path
    path_split = scene_info["template_split"]
    path_list = []
    for part in path_split:
        if ":" in part and path_split[0]:
            path_list.append(part)
        elif part == "$studio_hierarchy":
            if not scene_info.get("studio_hierarchy"):
                continue
            for p in scene_info["studio_hierarchy"]:
                path_list.append(re.sub('[\\/:"*?<>|]+', "", p).strip())
        else:
            path_list.append(
                re.sub('[\\/:"*?<>|]+', "", makePath(scene_info, part)).strip()
            )
    # Remove blank, empty string
    path_split = [x for x in path_list if x]
    # The first character was a seperator, so put it back.
    if path_list[0] == "":
        path_split.insert(0, "")

    if PREVENT_CONSECUTIVE:
        # remove consecutive (/FolderName/FolderName/video.mp4 -> FolderName/video.mp4
        path_split = remove_consecutive(path_split)

    if "^*" in template["path"]["destination"]:
        if scene_info["current_directory"] != os.sep.join(path_split):
            path_split.pop(len(scene_info["current_directory"]))

    path_edited = os.sep.join(path_split)

    if FILENAME_REMOVECHARACTER:
        path_edited = re.sub(f"[{FILENAME_REMOVECHARACTER}]+", "", path_edited)

    # Using typewriter for Apostrophe
    new_path = re.sub("[’‘”“]+", "'", path_edited)

    return new_path


def checking_duplicate_db(scene_info: dict):
    """Reject an exact path already represented by another Stash scene.

    Final file conflicts are also checked by native moveFiles in its
    transaction; this query only selects a deterministic suffix before that
    mutation.
    """
    scenes = graphql_findScenebyPath(scene_info["final_path"], "EQUALS")
    if scenes["count"] > 0:
        log.LogError("Duplicate path detected")
        for dupl_row in scenes["scenes"]:
            log.LogWarning(f"Identical path: [{dupl_row['id']}]")
        return 1
    scenes = graphql_findScenebyPath(scene_info["new_filename"], "EQUALS")
    if scenes["count"] > 0:
        for dupl_row in scenes["scenes"]:
            if dupl_row["id"] != scene_info["scene_id"]:
                log.LogWarning(f"Duplicate filename: [{dupl_row['id']}]")
    return 0


def associated_rename(scene_info: dict):
    if ASSOCIATED_EXT:
        for ext in ASSOCIATED_EXT:
            p = os.path.splitext(scene_info["current_path"])[0] + "." + ext
            p_new = os.path.splitext(scene_info["final_path"])[0] + "." + ext
            if os.path.isfile(p):
                try:
                    move_noclobber(p, p_new)
                except Exception as err:
                    log.LogError(
                        f"Something prevents renaming this file '{p}' - err: {err}"
                    )
                    continue
            if os.path.isfile(p_new):
                log.LogInfo(f"[OS] Associate file renamed ({p_new})")
                if LOGFILE:
                    try:
                        with open(LOGFILE, "a", encoding="utf-8") as f:
                            f.write(f"{scene_info['scene_id']}|{p}|{p_new}\n")
                    except Exception as err:
                        move_noclobber(p_new, p)
                        log.LogError(
                            f"Restoring the original name, error writing the logfile: {err}"
                        )


def get_missing_required_fields(template_str: str, scene_info: dict) -> list:
    """
    Return a list of required field names that are missing (null/empty) in scene_info.

    Fields inside optional {} groups are excluded from this check — they are
    designed to be silently dropped when empty. Only fields outside {} are
    considered required.

    Example: "{[$studio] }{$date - }$title"
        - $studio and $date are optional (inside {})
        - $title is required (outside {})
    """
    # Strip optional groups so only required field references remain
    required_part = re.sub(r"\{[^{}]*\}", "", template_str)
    required_fields = re.findall(r"\$(\w+)", required_part)
    missing = []
    for field in required_fields:
        # Normalise: strip trailing underscore variants (e.g. performer_path)
        key = field.strip("_")
        if not scene_info.get(key):
            missing.append(f"${field}")
    return missing


def _matches_pattern(value: str, pattern_config: dict) -> bool:
    """Check if a value matches a pattern config dict with type (exact|regex) and pattern."""
    import re as _re
    ptype = pattern_config.get("type", "exact")
    pattern = pattern_config.get("pattern", "")
    if ptype == "regex":
        return bool(_re.search(pattern, value, _re.IGNORECASE))
    # exact
    return value.lower() == pattern.lower()


def is_excluded(stash_scene: dict) -> bool:
    """Return True if the scene should be excluded from renaming based on config patterns."""
    if not EXCLUDE_ENABLED:
        return False

    # Check tag patterns
    if EXCLUDE_TAG_PATTERNS:
        scene_tags = [t.get("name", "") for t in stash_scene.get("tags", [])]
        for _key, pat in EXCLUDE_TAG_PATTERNS.items():
            for tag_name in scene_tags:
                if _matches_pattern(tag_name, pat):
                    log.LogDebug(f"Scene excluded by tag pattern '{pat['pattern']}' (matched '{tag_name}')")
                    return True

    # Check studio patterns (studio + parent studio)
    if EXCLUDE_STUDIO_PATTERNS:
        studios_to_check = []
        if stash_scene.get("studio"):
            studios_to_check.append(stash_scene["studio"].get("name", ""))
            parent = stash_scene["studio"].get("parent_studio")
            if parent:
                studios_to_check.append(parent.get("name", ""))
        for _key, pat in EXCLUDE_STUDIO_PATTERNS.items():
            for studio_name in studios_to_check:
                if studio_name and _matches_pattern(studio_name, pat):
                    log.LogDebug(f"Scene excluded by studio pattern '{pat['pattern']}' (matched '{studio_name}')")
                    return True

    # Check path patterns
    if EXCLUDE_PATH_PATTERNS:
        current_path = stash_scene.get("path") or ""
        if not current_path and stash_scene.get("files"):
            current_path = stash_scene["files"][0].get("path", "")
        for _key, pat in EXCLUDE_PATH_PATTERNS.items():
            if current_path and _matches_pattern(current_path, pat):
                log.LogDebug(f"Scene excluded by path pattern '{pat['pattern']}' (matched '{current_path}')")
                return True

    return False


def renamer(scene_id, db_conn=None):
    # The complete scene read, target selection and native move must be one
    # critical section. Scene.Update.Post may be delivered concurrently.
    # Always refresh by ID, including bulk callers that pass a slim/stale dict;
    # this also prevents _renamer from mutating the caller's object.
    lock_id = scene_id.get("id") if isinstance(scene_id, dict) else scene_id
    with scene_lock(lock_id):
        fresh_scene = graphql_getScene(lock_id)
        if not fresh_scene:
            raise RuntimeError(f"scene {lock_id} disappeared during rename")
        return _renamer(fresh_scene, db_conn)


def _renamer(scene_id, db_conn=None):
    option_dryrun = False
    if type(scene_id) is dict:
        stash_scene = scene_id
        scene_id = stash_scene["id"]
    elif type(scene_id) is int:
        stash_scene = graphql_getScene(scene_id)

    if (
        config.only_organized
        and not stash_scene["organized"]
        and not PATH_NON_ORGANIZED
    ):
        log.LogDebug(f"[{scene_id}] Scene ignored (not organized)")
        return

    if is_excluded(stash_scene):
        log.LogDebug(f"[{scene_id}] Scene excluded by exclude pattern")
        return

    # refractor file support
    fingerprint = []
    if stash_scene.get("path"):
        stash_scene["file"]["path"] = stash_scene["path"]
        if stash_scene.get("checksum"):
            fingerprint.append({"type": "md5", "value": stash_scene["checksum"]})
        if stash_scene.get("oshash"):
            fingerprint.append({"type": "oshash", "value": stash_scene["oshash"]})
        stash_scene["file"]["fingerprints"] = fingerprint
        scene_files = [stash_scene["file"]]
        del stash_scene["path"]
        del stash_scene["file"]
    elif stash_scene.get("files"):
        scene_files = stash_scene["files"]
        del stash_scene["files"]
    else:
        scene_files = []
    stash_db = None
    claimed_target_indices = set()
    for i in range(0, len(scene_files)):
        scene_file = scene_files[i]
        # refractor file support
        for f in scene_file["fingerprints"]:
            if f.get("oshash"):
                stash_scene["oshash"] = f["oshash"]
            if f.get("md5"):
                stash_scene["checksum"] = f["md5"]
            if f.get("checksum"):
                stash_scene["checksum"] = f["checksum"]
        stash_scene["path"] = scene_file["path"]
        stash_scene["file"] = scene_file
        if scene_file.get("bit_rate"):
            stash_scene["file"]["bit_rate"] = scene_file["bit_rate"]
        if scene_file.get("frame_rate"):
            stash_scene["file"]["framerate"] = scene_file["frame_rate"]

        # Tags > Studios > Default
        template = {}
        template["filename"] = get_template_filename(stash_scene)
        template["path"] = get_template_path(stash_scene)
        if not template["path"].get("destination"):
            if config.p_use_default_template:
                log.LogDebug("[PATH] Using default template")
                template["path"] = {
                    "destination": config.p_default_template,
                    "option": [],
                    "opt_details": {},
                }
            else:
                template["path"] = None
        else:
            if template["path"].get("option"):
                if "dry_run" in template["path"]["option"] and not DRY_RUN:
                    log.LogInfo("Dry-Run on (activate by option)")
                    option_dryrun = True
        if not template["filename"] and config.use_default_template:
            log.LogDebug("[FILENAME] Using default template")
            template["filename"] = config.default_template

        if not template["filename"] and not template["path"]:
            log.LogWarning(f"[{scene_id}] No template for this scene.")
            return

        # log.LogDebug("Using this template: {}".format(filename_template))
        scene_information = extract_info(stash_scene, template)
        log.LogDebug(f"[{scene_id}] Scene information: {scene_information}")
        log.LogDebug(f"[{scene_id}] Template: {template}")

        scene_information["scene_id"] = scene_id
        scene_information["file_index"] = i

        # Check that all required (non-optional) template fields are present
        if REQUIRE_FIELDS:
            missing = []
            if template.get("filename"):
                missing += get_missing_required_fields(template["filename"], scene_information)
            if template.get("path") and template["path"].get("destination"):
                missing += get_missing_required_fields(template["path"]["destination"], scene_information)
            if missing:
                log.LogWarning(
                    f"[{scene_id}] Skipping rename: required field(s) {missing} are empty. "
                    f"Set require_fields = False in config to rename anyway."
                )
                continue

        for removed_field in ORDER_SHORTFIELD:
            if removed_field:
                if scene_information.get(removed_field.replace("$", "")):
                    del scene_information[removed_field.replace("$", "")]
                    log.LogWarning(f"removed {removed_field} to reduce the length path")
                else:
                    continue
            if template["filename"]:
                scene_information["new_filename"] = create_new_filename(
                    scene_information, template["filename"]
                )
            else:
                scene_information["new_filename"] = scene_information[
                    "current_filename"
                ]
            if template.get("path"):
                scene_information["new_directory"] = create_new_path(
                    scene_information, template
                )
            else:
                scene_information["new_directory"] = scene_information[
                    "current_directory"
                ]
            scene_information["final_path"] = os.path.join(
                scene_information["new_directory"], scene_information["new_filename"]
            )
            if IGNORE_PATH_LENGTH or len(scene_information["final_path"]) <= 240:
                break

        # Reserve destinations already occupied by later files in this scene
        # before assigning a newly imported file.  Without this pre-pass an
        # import that appears first in Stash's file order can select the
        # canonical destination and make the later canonical file collide.
        if len(scene_files) > 1 and template.get("filename"):
            for candidate_index in range(len(DUPLICATE_SUFFIX)):
                candidate_information = dict(scene_information)
                candidate_information["file_index"] = candidate_index
                candidate_name = create_new_filename(candidate_information, template["filename"])
                candidate_dir = (
                    create_new_path(candidate_information, template)
                    if template.get("path")
                    else candidate_information["current_directory"]
                )
                candidate_path = os.path.normpath(os.path.join(candidate_dir, candidate_name))
                if any(
                    other is not scene_file
                    and os.path.normpath(other.get("path", "")) == candidate_path
                    for other in scene_files
                ):
                    claimed_target_indices.add(candidate_index)

        # Preserve an already-correct file-to-suffix mapping when Stash changes
        # the order of a scene's files (for example after a primary file switch).
        # Loop-index-only naming would swap two correct paths on the next hook
        # and churn forever.  This runs after path shortening so short paths
        # retain all configured metadata fields.
        if len(scene_files) > 1 and template.get("filename"):
            matched_existing_target = False
            for candidate_index in range(len(DUPLICATE_SUFFIX)):
                scene_information["file_index"] = candidate_index
                candidate_name = create_new_filename(scene_information, template["filename"])
                candidate_dir = (
                    create_new_path(scene_information, template)
                    if template.get("path")
                    else scene_information["current_directory"]
                )
                candidate_path = os.path.join(candidate_dir, candidate_name)
                # A pre-reserved index may belong to this file's own existing
                # path.  Allow that exact match; only skip it when it would
                # assign a new file to a target reserved by another file.
                if (
                    candidate_index in claimed_target_indices
                    and os.path.normpath(candidate_path)
                    != os.path.normpath(scene_information["current_path"])
                ):
                    scene_information["file_index"] = i
                    continue
                if os.path.normpath(candidate_path) == os.path.normpath(scene_information["current_path"]):
                    scene_information["new_filename"] = candidate_name
                    scene_information["new_directory"] = candidate_dir
                    scene_information["final_path"] = candidate_path
                    claimed_target_indices.add(candidate_index)
                    matched_existing_target = True
                    break
                scene_information["file_index"] = i

            # A newly imported file has no existing suffix mapping.  Its loop
            # index can nevertheless already be reserved by an earlier file
            # (for example an existing ``_1`` followed by a new file).  Pick
            # the first free suffix rather than sending a known collision to
            # the native mover.  Existing mappings above always win.
            if not matched_existing_target and scene_information["file_index"] in claimed_target_indices:
                for candidate_index in range(len(DUPLICATE_SUFFIX)):
                    if candidate_index in claimed_target_indices:
                        continue
                    scene_information["file_index"] = candidate_index
                    scene_information["new_filename"] = create_new_filename(
                        scene_information, template["filename"]
                    )
                    scene_information["new_directory"] = (
                        create_new_path(scene_information, template)
                        if template.get("path")
                        else scene_information["current_directory"]
                    )
                    scene_information["final_path"] = os.path.join(
                        scene_information["new_directory"], scene_information["new_filename"]
                    )
                    break

        if check_longpath(scene_information["final_path"]):
            if (DRY_RUN or option_dryrun) and LOGFILE:
                with open(DRY_RUN_FILE, "a", encoding="utf-8") as f:
                    f.write(
                        f"[LENGTH LIMIT] {scene_information['scene_id']}|{scene_information['final_path']}\n"
                    )
            continue

        # log.LogDebug(f"Filename: {scene_information['current_filename']} -> {scene_information['new_filename']}")
        # log.LogDebug(f"Path: {scene_information['current_directory']} -> {scene_information['new_directory']}")

        if scene_information["final_path"] == scene_information["current_path"]:
            log.LogInfo(f"Everything is ok. ({scene_information['current_filename']})")
            continue

        if scene_information["current_directory"] != scene_information["new_directory"]:
            log.LogInfo("File will be moved to another directory")
            log.LogDebug(f"[OLD path] {scene_information['current_path']}")
            log.LogDebug(f"[NEW path] {scene_information['final_path']}")

        if scene_information["current_filename"] != scene_information["new_filename"]:
            log.LogInfo("The filename will be changed")
            if ALT_DIFF_DISPLAY:
                find_diff_text(
                    scene_information["current_filename"],
                    scene_information["new_filename"],
                )
            else:
                log.LogDebug(f"[OLD filename] {scene_information['current_filename']}")
                log.LogDebug(f"[NEW filename] {scene_information['new_filename']}")

        if (DRY_RUN or option_dryrun) and LOGFILE:
            with open(DRY_RUN_FILE, "a", encoding="utf-8") as f:
                f.write(
                    f"{scene_information['scene_id']}|{scene_information['current_path']}|{scene_information['final_path']}\n"
                )
            continue
        # check if there is already a file where the new path is
        err = checking_duplicate_db(scene_information)
        fs_err = filesystem_target_occupied(
            scene_information["final_path"], scene_information["current_path"]
        )
        while (err or fs_err) and scene_information["file_index"] <= len(DUPLICATE_SUFFIX):
            log.LogDebug("Duplicate filename detected, increasing file index")
            scene_information["file_index"] = scene_information["file_index"] + 1
            scene_information["new_filename"] = create_new_filename(
                scene_information, template["filename"]
            )
            scene_information["final_path"] = os.path.join(
                scene_information["new_directory"], scene_information["new_filename"]
            )
            log.LogDebug(f"[NEW filename] {scene_information['new_filename']}")
            log.LogDebug(f"[NEW path] {scene_information['final_path']}")
            err = checking_duplicate_db(scene_information)
            fs_err = filesystem_target_occupied(
                scene_information["final_path"], scene_information["current_path"]
            )
        # abort
        if err or fs_err:
            raise Exception("duplicate")
        # Reserve the suffix selected for this file for the rest of this
        # scene's pass, including files that are actually moved.  This keeps
        # later files from selecting the same target after a duplicate check
        # or a primary-file reorder.
        claimed_target_indices.add(scene_information["file_index"])
        try:
            # Stash's native mover performs the filesystem move and database
            # update in one transaction, including conflict checks.  Keep the
            # scene lock around this call so concurrent hooks cannot select
            # the same target.
            file_id = scene_file.get("id")
            if not file_id:
                raise RuntimeError("scene file has no native file id")
            graphql_move_file(
                file_id,
                scene_information["new_directory"],
                scene_information["new_filename"],
            )
            if i == 0:
                associated_rename(scene_information)
            if template.get("path"):
                if "clean_tag" in template["path"]["option"]:
                    graphql_removeScenesTag(
                        [scene_information["scene_id"]],
                        template["path"]["opt_details"]["clean_tag"],
                    )
        except Exception as err:
            log.LogError(f"Error during native move operation ({err})")
            continue


def exit_plugin(msg=None, err=None):
    if msg is None and err is None:
        msg = "plugin ended"
    log.LogDebug("Execution time: {}s".format(round(time.time() - START_TIME, 5)))
    output_json = {"output": msg, "error": err}
    print(json.dumps(output_json))
    sys.exit()


if PLUGIN_ARGS:
    log.LogDebug("--Starting Plugin 'Renamer'--")
    if "bulk" not in PLUGIN_ARGS:
        if "enable" in PLUGIN_ARGS:
            log.LogInfo("Enable hook")
            success = config_edit("enable_hook", True)
        elif "disable" in PLUGIN_ARGS:
            log.LogInfo("Disable hook")
            success = config_edit("enable_hook", False)
        elif "dryrun" in PLUGIN_ARGS:
            if config.dry_run:
                log.LogInfo("Disable dryrun")
                success = config_edit("dry_run", False)
            else:
                log.LogInfo("Enable dryrun")
                success = config_edit("dry_run", True)
        elif "single" in PLUGIN_ARGS:
            try:
                SINGLE_SCENE_ID = parse_single_scene_id(FRAGMENT["args"])
            except ValueError as err:
                exit_plugin(err=str(err))
            SINGLE_DRY_RUN = FRAGMENT["args"].get("dry_run", False) is True
        if "single" not in PLUGIN_ARGS and not success:
            log.LogError("Script failed to change the value")
            exit_plugin("script finished")
        if "single" not in PLUGIN_ARGS:
            exit_plugin("script finished")
else:
    if not config.enable_hook:
        exit_plugin("Hook disabled")
    log.LogDebug("--Starting Hook 'Renamer'--")
    FRAGMENT_HOOK_TYPE = FRAGMENT["args"]["hookContext"]["type"]
    FRAGMENT_SCENE_ID = FRAGMENT["args"]["hookContext"]["id"]

LOGFILE = config.log_file

# Gallery.Update.Post
# if FRAGMENT_HOOK_TYPE == "Scene.Update.Post":


STASH_CONFIG = graphql_getConfiguration()
STASH_DATABASE = STASH_CONFIG["general"]["databasePath"]

# READING CONFIG

ASSOCIATED_EXT = config.associated_extension

FIELD_WHITESPACE_SEP = config.field_whitespaceSeperator
FIELD_REPLACER = config.field_replacer

FILENAME_ASTITLE = config.filename_as_title
FILENAME_LOWER = config.lowercase_Filename
FILENAME_TITLECASE = config.titlecase_Filename
FILENAME_SPLITCHAR = config.filename_splitchar
FILENAME_REMOVECHARACTER = config.removecharac_Filename
FILENAME_REPLACEWORDS = config.replace_words

PERFORMER_SPLITCHAR = config.performer_splitchar
PERFORMER_LIMIT = config.performer_limit
PERFORMER_LIMIT_KEEP = config.performer_limit_keep
PERFORMER_SORT = config.performer_sort
PERFORMER_IGNOREGENDER = config.performer_ignoreGender
PREVENT_TITLE_PERF = config.prevent_title_performer

DUPLICATE_SUFFIX = config.duplicate_suffix

PREPOSITIONS_LIST = config.prepositions_list
PREPOSITIONS_REMOVAL = config.prepositions_removal

SQUEEZE_STUDIO_NAMES = config.squeeze_studio_names

RATING_FORMAT = config.rating_format

TAGS_SPLITCHAR = config.tags_splitchar
TAGS_WHITELIST = config.tags_whitelist
TAGS_BLACKLIST = config.tags_blacklist

IGNORE_PATH_LENGTH = config.ignore_path_length

PREVENT_CONSECUTIVE = config.prevent_consecutive
REMOVE_EMPTY_FOLDER = config.remove_emptyfolder

PROCESS_KILL = config.process_kill_attach
PROCESS_ALLRESULT = config.process_getall
UNICODE_USE = config.use_ascii

ORDER_SHORTFIELD = config.order_field
ORDER_SHORTFIELD.insert(0, None)

ALT_DIFF_DISPLAY = config.alt_diff_display

PATH_NOPERFORMER_FOLDER = config.path_noperformer_folder
PATH_KEEP_ALRPERF = config.path_keep_alrperf
PATH_NON_ORGANIZED = config.p_non_organized
PATH_ONEPERFORMER = config.path_one_performer

# Exclude settings - safe getattr for backwards compatibility with existing config.py files
EXCLUDE_ENABLED = getattr(config, "exclude_enabled", False)
EXCLUDE_TAG_PATTERNS = getattr(config, "exclude_tag_patterns", {})
EXCLUDE_STUDIO_PATTERNS = getattr(config, "exclude_studio_patterns", {})
EXCLUDE_PATH_PATTERNS = getattr(config, "exclude_path_patterns", {})

# Require all template fields setting
REQUIRE_FIELDS = getattr(config, "require_fields", True)

DB_VERSION = graphql_getBuild()
if DB_VERSION >= DB_VERSION_FILE_REFACTOR:
    FILE_QUERY = """
            files {
                id
                path
                video_codec
                audio_codec
                width
                height
                frame_rate
                duration
                bit_rate
                phash: fingerprint(type: "phash")
                oshash: fingerprint(type: "oshash")
                checksum: fingerprint(type: "checksum")
                fingerprints {
                    type
                    value
                }
            }
    """
else:
    FILE_QUERY = """
            path
            file {
                video_codec
                audio_codec
                width
                height
                framerate
                bitrate
                duration
            }
    """
if DB_VERSION >= DB_VERSION_SCENE_STUDIO_CODE:
    FILE_QUERY = f"        code{FILE_QUERY}"

if PLUGIN_ARGS:
    if "bulk" in PLUGIN_ARGS:
        scenes = graphql_findScene(config.batch_number_scene, "ASC")
        log.LogDebug(f"Count scenes: {len(scenes['scenes'])}")
        progress = 0
        progress_step = 1 / len(scenes["scenes"])
        stash_db = None
        for scene in scenes["scenes"]:
            log.LogDebug(f"** Checking scene: {scene['title']} - {scene['id']} **")
            try:
                renamer(scene, stash_db)
            except Exception as err:
                log.LogError(f"main function error: {err}")
            progress += progress_step
            log.LogProgress(progress)
        # Native moveFiles owns each transaction.
    elif "single" in PLUGIN_ARGS:
        try:
            if SINGLE_DRY_RUN:
                DRY_RUN = True
            renamer(SINGLE_SCENE_ID)
        except Exception as err:
            log.LogError(f"single-scene task failed: {err}")
            exit_plugin(err=str(err))
else:
    try:
        renamer(FRAGMENT_SCENE_ID)
    except Exception as err:
        log.LogError(f"main function error: {err}")
        traceback.print_exc()

exit_plugin("Successful!")
