import io
import json
import tarfile
from pathlib import Path

import pytest

from conftest import SCREEN_H
from itbuilder import firmware as fw
from itbuilder.manifest import ManifestError, load_package, parse_manifest
from itbuilder.project import Project, Settings
from itbuilder.sources import FetchError, SourceSpec, SourceStore, _safe_extract

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def test_catalog(tree):
    cat = fw.load_catalog(tree)
    assert cat.version == "1.15.0"
    assert [a.enum for a in cat.apps] == ["Timer", "Dice"]
    assert cat.apps[0].icon == ""
    assert cat.apps[1].icon == "D"
    assert [(f.enum, f.name) for f in cat.watchfaces] == [("Digital", "Digital face"), ("Analog", "Analog face")]
    assert cat.default_apps == ["builtin:Apps::Timer", "builtin:Apps::Dice"]
    assert cat.default_watchfaces == ["builtin:WatchFace::Digital"]
    assert cat.apps_per_page == 6


def test_inject(tree):
    items = []
    for d in ("hello-app", "bigclock-face"):
        items += load_package(EXAMPLES / d, f"local:{EXAMPLES / d}").items
    cat = fw.load_catalog(tree)
    assert fw.check_compat(cat, items) == ([], [])
    fw.inject(tree, items, log=lambda s: None)

    apps_in = (tree / fw.APPS_IN).read_text()
    assert "      Error,\n      HelloCounter,\n    };" in apps_in
    assert "      Analog,\n      BigClock,\n    };" in apps_in
    ua = (tree / fw.USER_APPS_H).read_text()
    assert '#include "external/00_examples_hello_app/HelloCounter.h"' in ua
    assert ua.index("HelloCounter.h") > ua.index("Controllers.h")
    cm = (tree / fw.SRC_CMAKE).read_text()
    assert cm.index("external/01_examples_bigclock_face/WatchFaceBigClock.cpp") < cm.index("add_executable")
    assert (tree / "src/external/00_examples_hello_app/HelloCounter.cpp").is_file()
    fonts = json.loads((tree / fw.FONTS_JSON).read_text())
    assert fonts[fw.LAUNCHER_FONT]["sources"][1]["range"].endswith(", 0xf118")
    # catalog of the injected tree sees the new items
    cat2 = fw.load_catalog(tree)
    assert "HelloCounter" in cat2.all_app_enums and "BigClock" in cat2.all_face_enums


def test_name_clash(tree):
    pkg = parse_manifest({"apps": [{"enum": "Timer", "header": "HelloCounter.h"}]}, "local:x", EXAMPLES / "hello-app")
    errors, _ = fw.check_compat(fw.load_catalog(tree), pkg.items)
    assert errors and "already exists" in errors[0]


def test_min_version_warning(tree):
    pkg = parse_manifest({"minInfiniTime": "9.0", "apps": [{"enum": "Foo", "header": "HelloCounter.h"}]},
                         "local:x", EXAMPLES / "hello-app")
    _, warnings = fw.check_compat(fw.load_catalog(tree), pkg.items)
    assert warnings


@pytest.mark.parametrize("bad", [
    {"apps": [{"enum": "lower", "header": "HelloCounter.h"}]},
    {"apps": [{"enum": "Foo", "header": "../evil.h"}]},
    {"apps": [{"enum": "Foo", "header": "/etc/passwd.h"}]},
    {"apps": [{"enum": "Foo", "header": "HelloCounter.h", "sources": ["missing.cpp"]}]},
    {"apps": [{"enum": "Foo", "header": "HelloCounter.h", "icon": "f118"}]},
    {"apps": [{"enum": "Foo", "header": "HelloCounter.h", "type": "widget"}]},
    {"apps": []},
    {"manifestVersion": 2, "apps": [{"enum": "Foo", "header": "HelloCounter.h"}]},
])
def test_invalid_manifests(bad):
    with pytest.raises(ManifestError):
        parse_manifest(bad, "local:x", EXAMPLES / "hello-app")


def test_source_spec_parsing(tmp_path):
    assert SourceSpec.parse("owner/repo").id == "github:owner/repo"
    assert SourceSpec.parse("https://github.com/owner/repo.git").location == "owner/repo"
    s = SourceSpec.parse("https://github.com/owner/repo/tree/v1.2")
    assert (s.location, s.ref) == ("owner/repo", "v1.2")
    assert SourceSpec.parse("owner/repo", ref="dev").ref == "dev"
    assert SourceSpec.parse(str(tmp_path)).kind == "local"
    with pytest.raises(FetchError):
        SourceSpec.parse("not a repo at all")


def _tarball(entries):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in entries:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def test_safe_extract(tmp_path):
    top = _safe_extract(_tarball([("repo-abc/infinitime-app.json", b"{}")]), tmp_path / "x")
    assert (top / "infinitime-app.json").is_file()
    with pytest.raises(FetchError):
        _safe_extract(_tarball([("repo-abc/../../evil", b"x")]), tmp_path / "y")


def test_project_build_request(tree, tmp_path):
    settings = Settings(sources=[SourceSpec("local", str(EXAMPLES / "hello-app"))])
    project = Project(settings, SourceStore(tmp_path / "cache"))
    project.set_firmware(tree)
    project.load_all_sources()
    # fresh install: only the minimal set (of which the fixture has Timer, Digital and Analog)
    assert project.selected("app") == ["builtin:Apps::Timer"]
    assert project.selected("watchface") == ["builtin:WatchFace::Digital", "builtin:WatchFace::Analog"]
    project.use_infinitime_defaults()
    assert project.selected("app") == ["builtin:Apps::Timer", "builtin:Apps::Dice"]
    hello = next(k for k in project.entries("app") if k.startswith("ext:"))
    project.set_selected("app", [hello, "builtin:Apps::Dice", "builtin:Apps::Gone"])
    req, errors, warnings = project.build_request()
    assert errors == []
    assert req.apps == ["Apps::HelloCounter", "Apps::Dice"]
    assert req.watchfaces == ["WatchFace::Digital"]
    assert [i.enum for i in req.external] == ["HelloCounter"]
    assert len(warnings) == 1  # Gone is skipped

    project.set_selected("watchface", [])
    _, errors, _ = project.build_request()
    assert errors


def test_profile_roundtrip():
    s = Settings(sources=[SourceSpec("github", "a/b", "v1"), SourceSpec("local", "/x")], apps=["builtin:Apps::Timer"])
    exported = s.to_json(include_local=False)
    assert [x["location"] for x in exported["sources"]] == ["a/b"]
    assert "output_dir" not in exported
    s2 = Settings.from_json(json.loads(json.dumps(exported)))
    assert s2.sources == [SourceSpec("github", "a/b", "v1")] and s2.apps == ["builtin:Apps::Timer"]


def test_set_app_lists_and_readback(tree, tmp_path):
    fw.set_app_lists(tree, ["Apps::Dice", "Apps::Timer"], ["WatchFace::Analog"])
    text = (tree / fw.APPS_CMAKE).read_text()
    assert text.startswith("# Selection made with InfiniTime Builder\nset(ENABLE_USERAPPS \"Apps::Dice, Apps::Timer\")")
    assert 'set(ENABLE_WATCHFACES "WatchFace::Analog")' in text
    gen = tmp_path / "b/src/displayapp/apps/Apps.h"
    gen.parent.mkdir(parents=True)
    gen.write_text("using UserAppTypes = TypeList<Apps::Dice, Apps::Timer>;\n"
                   "using UserWatchFaceTypes = WatchFaceTypeList<WatchFace::Analog>;\n")
    assert fw.generated_lists(tmp_path / "b") == (["Apps::Dice", "Apps::Timer"], ["WatchFace::Analog"])


def test_token_only_sent_to_github_api(monkeypatch):
    from itbuilder import sources

    seen = []

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"{}"

    monkeypatch.setattr(sources._opener, "open", lambda req, timeout: (seen.append(req), Resp())[1])
    monkeypatch.setenv("GITHUB_TOKEN", "secret")
    sources._http("https://api.github.com/repos/a/b")
    sources._http("https://hub.docker.com/v2/repositories/x")
    assert seen[0].get_header("Authorization") == "Bearer secret"
    assert seen[1].get_header("Authorization") is None


def test_default_sources():
    from itbuilder.project import default_sources

    fresh = Settings()
    assert SourceSpec("github", "ScanuNicco/infinitime-sky-games") in fresh.sources
    assert fresh.sources == default_sources()
    # a saved configuration keeps its own list, including when the user removed the default
    assert Settings.from_json({"sources": []}).sources == []
    # fresh installs don't switch third-party apps on
    assert fresh.apps is None


def test_default_sources_reach_existing_installs(tmp_path, monkeypatch):
    from itbuilder import project

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setattr(project.platform, "system", lambda: "Linux")
    path = project.settings_path()
    path.parent.mkdir(parents=True)
    games = SourceSpec("github", "ScanuNicco/infinitime-sky-games")
    faces = SourceSpec("github", "ScanuNicco/infinitime-concept-faces")

    # saved before default sources existed -> the defaults are added once
    path.write_text(json.dumps({"sources": [], "apps": ["builtin:Apps::Timer"]}))
    s = project.load_settings()
    assert s.sources == [games, faces] and s.apps == ["builtin:Apps::Timer"]
    assert json.loads(path.read_text())["seen_defaults"] == [games.id, faces.id]

    # the user removes one -> it stays removed
    s.sources.remove(games)
    project.save_settings(s)
    assert project.load_settings().sources == [faces]

    # an install that has seen only the first default gets the newer one added
    path.write_text(json.dumps({"sources": [], "seen_defaults": [games.id]}))
    assert project.load_settings().sources == [faces]

    # profiles shared with others don't carry it
    assert "seen_defaults" not in s.to_json(include_local=False)


def _resource_pkg(tmp_path, resources, enum="Face"):
    root = tmp_path / "pkg"
    (root / "fonts").mkdir(parents=True, exist_ok=True)
    (root / "images").mkdir(exist_ok=True)
    (root / "Face.h").write_text("// traits")
    (root / "Face.cpp").write_text("// code")
    (root / "fonts" / "My.ttf").write_bytes(b"ttf")
    (root / "images" / "bg.png").write_bytes(b"png")
    data = {"apps": [{"type": "watchface", "enum": enum, "header": "Face.h", "sources": ["Face.cpp"],
                      "resources": resources}]}
    return parse_manifest(data, f"local:{root}", root)


GOOD_RESOURCES = {
    "fonts": {"face_big": {"sources": [{"file": "fonts/My.ttf", "symbols": "0123456789:"}], "size": 60, "bpp": 2}},
    "images": {"face_bg": {"sources": "images/bg.png", "color_format": "CF_TRUE_COLOR_ALPHA"}},
}


def _add_resource_tables(tree):
    res = tree / "src/resources"
    res.mkdir(parents=True, exist_ok=True)
    (res / "fonts.json").write_text(json.dumps({"teko": {"sources": [{"file": "fonts/Teko.ttf", "symbols": "0"}],
                                                        "bpp": 1, "size": 28, "format": "bin",
                                                        "target_path": "/fonts/"}}))
    (res / "images.json").write_text("{}")


def test_resources_parsed_and_injected(tree, tmp_path):
    pkg = _resource_pkg(tmp_path, GOOD_RESOURCES)
    item = pkg.items[0]
    assert item.needs_resources
    assert item.fonts["face_big"]["target_path"] == "/fonts/"
    _add_resource_tables(tree)
    fw.inject(tree, pkg.items, log=lambda s: None)
    fonts = json.loads((tree / fw.RESOURCE_FONTS_JSON).read_text())
    images = json.loads((tree / fw.RESOURCE_IMAGES_JSON).read_text())
    assert "teko" in fonts  # built-in resources are kept
    big = fonts["face_big"]
    assert big["size"] == 60 and big["bpp"] == 2 and big["format"] == "bin"
    # paths are relative to src/resources, where InfiniTime's generators resolve them
    font_path = big["sources"][0]["file"]
    assert font_path.startswith("../external/") and font_path.endswith("/fonts/My.ttf")
    assert (tree / "src/resources" / font_path).is_file()
    assert (tree / "src/resources" / images["face_bg"]["sources"]).is_file()
    assert images["face_bg"]["binary_format"] == "ARGB8565_RBSWAP"


def test_resource_name_clash(tree, tmp_path):
    clash = {"fonts": {"teko": GOOD_RESOURCES["fonts"]["face_big"]}}
    pkg = _resource_pkg(tmp_path, clash)
    _add_resource_tables(tree)
    with pytest.raises(fw.FirmwareError, match="teko"):
        fw.inject(tree, pkg.items, log=lambda s: None)


@pytest.mark.parametrize("bad", [
    {"fonts": {"Bad-Name": GOOD_RESOURCES["fonts"]["face_big"]}},
    {"fonts": {"f": {"sources": [{"file": "fonts/My.ttf", "symbols": "0"}], "size": 60}}},  # name too short
    {"fonts": {"face_big": {"sources": [{"file": "fonts/My.ttf"}], "size": 60}}},  # no symbols/range
    {"fonts": {"face_big": {"sources": [{"file": "fonts/My.ttf", "symbols": "-0123"}], "size": 60}}},  # lv_font_conv option
    {"fonts": {"face_big": {"sources": [{"file": "../x.ttf", "symbols": "0"}], "size": 60}}},
    {"fonts": {"face_big": {"sources": [{"file": "fonts/missing.ttf", "symbols": "0"}], "size": 60}}},
    {"fonts": {"face_big": {"sources": [{"file": "fonts/My.ttf", "symbols": "0"}], "size": 60, "bpp": 3}}},
    {"images": {"face_bg": {"sources": "images/bg.png", "color_format": "CF_RAINBOW"}}},
    {"images": {"face_bg": {"sources": "images/bg.png", "color_format": "CF_INDEXED_4_BIT"}}},  # not in InfiniTime's converter
    {"images": {"face_bg": {"sources": "images/bg.jpg"}}},
    {"sounds": {}},
])
def test_invalid_resources(tmp_path, bad):
    with pytest.raises(ManifestError):
        _resource_pkg(tmp_path, bad)


def test_builtin_resource_detection(tree):
    p = tree / "src/displayapp/screens/Fancy.h"
    p.write_text("""
    template <>
    struct WatchFaceTraits<WatchFace::Analog> {
      static constexpr const char* name = "Analog face";
      static Screens::Screen* Create(AppControllers& controllers) {
        return nullptr;
      };
      static bool IsAvailable(Pinetime::Controllers::FS& filesystem) {
        return Screens::Fancy::IsAvailable(filesystem);
      }
    };
    """)
    (tree / "src/displayapp/screens/Things.h").write_text(SCREEN_H.replace(
        "struct WatchFaceTraits<WatchFace::Analog>", "struct Unused"))
    cat = fw.load_catalog(tree)
    flags = {i.enum: i.needs_resources for i in cat.apps + cat.watchfaces}
    assert flags == {"Timer": False, "Dice": False, "Digital": False, "Analog": True}


def test_screenshot_field(tmp_path):
    root = tmp_path / "pkg"
    (root / "shots").mkdir(parents=True)
    (root / "Face.h").write_text("//")
    (root / "shots" / "face.png").write_bytes(b"png")
    item = {"type": "watchface", "enum": "Face", "header": "Face.h", "screenshot": "shots/face.png"}
    pkg = parse_manifest({"apps": [item]}, f"local:{root}", root)
    assert pkg.items[0].screenshot == "shots/face.png"
    for bad in ("shots/missing.png", "../face.png", "shots/face.jpg"):
        with pytest.raises(ManifestError):
            parse_manifest({"apps": [{**item, "screenshot": bad}]}, f"local:{root}", root)


def test_entries_have_screenshots(tree, tmp_path):
    from itbuilder.project import BUILTIN_SCREENSHOTS

    project = Project(Settings(sources=[SourceSpec("local", str(EXAMPLES / "hello-app"))]), SourceStore(tmp_path / "c"))
    project.set_firmware(tree)
    project.load_all_sources()
    faces = project.entries("watchface")
    assert faces["builtin:WatchFace::Digital"].screenshot == BUILTIN_SCREENSHOTS / "Digital.png"
    assert faces["builtin:WatchFace::Digital"].screenshot.is_file()
    assert project.entries("app")["builtin:Apps::Timer"].screenshot is None


def test_github_sources_update_when_the_repo_changes(tmp_path, monkeypatch):
    from itbuilder import sources

    head = {"sha": "aaa"}
    downloads = []
    manifest = json.dumps({"apps": [{"enum": "Foo", "header": "Foo.h"}]}).encode()

    def fake_http(url, timeout=60):
        if "/commits/" in url:
            if head["sha"] is None:
                raise FetchError("offline")
            return json.dumps(head).encode()
        downloads.append(url)
        return _tarball([("o-r-x/infinitime-app.json", manifest), ("o-r-x/Foo.h", b"//")])

    monkeypatch.setattr(sources, "_http", fake_http)
    store = SourceStore(tmp_path)
    spec = SourceSpec("github", "o/r")
    store.fetch(spec)
    assert downloads[-1].endswith("/tarball/aaa") and len(downloads) == 1
    store.fetch(spec)                      # same commit: the cached copy is used
    assert len(downloads) == 1
    head["sha"] = "bbb"                     # new commit: downloaded again
    store.fetch(spec)
    assert len(downloads) == 2 and downloads[-1].endswith("/tarball/bbb")
    head["sha"] = None                      # offline: the cached copy is used
    assert store.fetch(spec).items[0].enum == "Foo" and len(downloads) == 2
