"""Tests for robustness: settings persistence, locking, HTTP handling, the CLI and the build flow."""
import os
import stat
import subprocess
import sys
import threading
import time
import urllib.request
from email.message import Message
from pathlib import Path

import pytest

from itbuilder import __version__
from itbuilder import builder as bld
from itbuilder import cli, sources
from itbuilder.fsutil import FileLock, LockBusy, atomic_write_text
from itbuilder.manifest import ManifestError, parse_manifest
from itbuilder.project import Settings, load_settings, save_settings
from itbuilder.sources import FetchError, SourceSpec

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("INFINITIME_BUILDER_HOME", str(tmp_path / "home"))
    return tmp_path / "home"


# --------------------------------------------------------------------------- settings


def test_settings_roundtrip(home):
    s = Settings()
    s.apps = ["builtin:Apps::Timer"]
    save_settings(s)
    assert load_settings().apps == ["builtin:Apps::Timer"]
    assert not list(home.glob(".settings.json.*.tmp"))  # no temporary files left behind


def test_corrupt_settings_are_kept_and_reported(home):
    home.mkdir()
    (home / "settings.json").write_text('{"apps": ["builtin:Apps::Ti')  # truncated by a crash
    s = load_settings()
    assert s.load_warning and "could not be read" in s.load_warning
    assert s.apps is None  # defaults
    backups = list(home.glob("settings.json.broken-*"))
    assert len(backups) == 1 and backups[0].read_text().startswith('{"apps"')
    assert not (home / "settings.json").exists()
    assert "load_warning" not in s.to_json()


@pytest.mark.parametrize("bad", [
    [],
    {"apps": "builtin:Apps::Timer"},
    {"apps": [1, 2]},
    {"firmware": "x"},
    {"firmware": {"url": 5}},
    {"sources": [{"kind": "ftp", "location": "x"}]},
    {"sources": [{"kind": "github"}]},
])
def test_malformed_settings_are_rejected(bad):
    with pytest.raises(ValueError):
        Settings.from_json(bad)


def test_atomic_write_keeps_old_content_on_failure(tmp_path, monkeypatch):
    p = tmp_path / "f.json"
    p.write_text("old")

    def boom(*a):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        atomic_write_text(p, "new")
    assert p.read_text() == "old"
    assert [x.name for x in tmp_path.iterdir()] == ["f.json"]


# --------------------------------------------------------------------------- locks


def test_file_lock_excludes_other_holders(tmp_path):
    lock = tmp_path / "x.lock"
    with FileLock(lock):
        with pytest.raises(LockBusy):
            with FileLock(lock, blocking=False):
                pass
    with FileLock(lock, blocking=False):  # free again
        pass


def test_file_lock_across_processes(tmp_path):
    lock = tmp_path / "x.lock"
    code = ("import sys, time; sys.path.insert(0, sys.argv[2]); from itbuilder.fsutil import FileLock\n"
            "with FileLock(sys.argv[1]):\n    print('locked', flush=True); time.sleep(3)\n")
    p = subprocess.Popen([sys.executable, "-c", code, str(lock), str(ROOT)], stdout=subprocess.PIPE, text=True)
    try:
        assert p.stdout.readline().strip() == "locked"
        with pytest.raises(LockBusy):
            with FileLock(lock, blocking=False):
                pass
    finally:
        p.kill()
        p.wait()


def test_concurrent_builds_are_refused(tmp_path, tree):
    req = bld.BuildRequest(["Apps::Timer"], ["WatchFace::Digital"], [], tree)
    with FileLock(tmp_path / "work" / "tree.lock"):
        with pytest.raises(bld.BuildError, match="already running"):
            bld.build(req, bld.Engine("/bin/false", "docker"), tmp_path / "work", tmp_path / "out", lambda m: None)


# --------------------------------------------------------------------------- HTTP


def test_token_is_not_forwarded_off_the_api_host(monkeypatch):
    handler = sources._StripAuthOnRedirect()
    req = urllib.request.Request("https://api.github.com/repos/a/b/tarball/x",
                                 headers={"Authorization": "Bearer secret", "User-Agent": "t"})
    off = handler.redirect_request(req, None, 302, "Found", {}, "https://codeload.github.com/a/b/tar.gz/x?token=t")
    assert off.get_header("Authorization") is None and off.get_header("User-agent") == "t"
    same = handler.redirect_request(req, None, 301, "Moved", {}, "https://api.github.com/repositories/1/tarball/x")
    assert same.get_header("Authorization") == "Bearer secret"


def _headers(**kv):
    m = Message()
    for k, v in kv.items():
        m[k.replace("_", "-")] = v
    return m


def test_http_error_messages(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    msg = sources._http_error_message
    assert "rate limit" in msg("u", 403, _headers(X_RateLimit_Remaining="0", X_RateLimit_Reset="0"))
    assert "rate limit" in msg("u", 429, _headers())
    denied = msg("u", 403, _headers(X_RateLimit_Remaining="55"))
    assert "access denied" in denied and "rate limit" not in denied
    assert "private" in msg("u", 404, _headers())
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    assert "GITHUB_TOKEN is valid" in msg("u", 401, _headers())


def test_refs_are_escaped_in_urls(monkeypatch):
    seen = []
    monkeypatch.setattr(sources, "_http", lambda url, timeout=60: (seen.append(url), b'{"sha": "abc"}')[1])
    sources.latest_commit(SourceSpec("github", "o/r", "feature/x?y#z"))
    assert seen[-1] == "https://api.github.com/repos/o/r/commits/feature/x%3Fy%23z"


def test_bare_owner_repo_that_is_also_a_folder_is_ambiguous(tmp_path, monkeypatch):
    (tmp_path / "owner" / "repo").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    assert SourceSpec.parse("owner/repo").kind == "github"  # a folder without a manifest: not a package
    (tmp_path / "owner" / "repo" / "infinitime-app.json").write_text("{}")
    with pytest.raises(FetchError, match="github:owner/repo"):
        SourceSpec.parse("owner/repo")
    assert SourceSpec.parse("github:owner/repo").kind == "github"
    assert SourceSpec.parse("https://github.com/owner/repo").kind == "github"
    assert SourceSpec.parse("./owner/repo").kind == "local"


# --------------------------------------------------------------------------- manifest


def test_homepage_is_validated():
    item = {"enum": "Foo", "header": "HelloCounter.h"}
    pkg = parse_manifest({"homepage": "https://example.org/x", "apps": [item]}, "local:x", EXAMPLES / "hello-app")
    assert pkg.homepage == "https://example.org/x"
    for bad in (5, "example.org", "javascript:alert(1)", "https://"):
        with pytest.raises(ManifestError, match="homepage"):
            parse_manifest({"homepage": bad, "apps": [item]}, "local:x", EXAMPLES / "hello-app")


# --------------------------------------------------------------------------- stream / cancel


def test_stream_can_cancel_a_silent_command():
    cancel = threading.Event()
    threading.Timer(0.3, cancel.set).start()
    t0 = time.monotonic()
    with pytest.raises(bld.BuildError, match="cancelled"):
        bld.stream([sys.executable, "-c", "import time; time.sleep(30)"], lambda line: None, cancel.is_set, poll=0.05)
    assert time.monotonic() - t0 < 10


def test_stream_returns_output_and_code():
    code, lines = bld.stream([sys.executable, "-c", "print('a'); print('b'); raise SystemExit(3)"], lambda l: None)
    assert (code, lines) == (3, ["a", "b"])


def test_stream_reports_missing_program():
    with pytest.raises(bld.BuildError, match="Could not run"):
        bld.stream(["/nonexistent/program"], lambda l: None)


# --------------------------------------------------------------------------- build flow (fake container engine)

FAKE_ENGINE = r'''#!{python}
"""Stands in for docker/podman: `run ... -v <tree>:/sources[:z] <image>` acts like the build image."""
import re, sys, zipfile
from pathlib import Path

args = sys.argv[1:]
if args[:1] != ["run"]:
    sys.exit(0)
tree = Path(next(a for a in args if a.endswith(":/sources") or a.endswith(":/sources:z")).split(":/sources")[0])
cmake = (tree / "src/displayapp/apps/CMakeLists.txt").read_text()
apps = re.search(r'set\(ENABLE_USERAPPS "(.*)"\)', cmake).group(1)
faces = re.search(r'set\(ENABLE_WATCHFACES "(.*)"\)', cmake).group(1)
gen = tree / "build/src/displayapp/apps/Apps.h"
gen.parent.mkdir(parents=True, exist_ok=True)
gen.write_text(f"using UserAppTypes = TypeList<{{apps}}>;\nusing UserWatchFaceTypes = WatchFaceTypeList<{{faces}}>;\n")
out = tree / "build/output"
out.mkdir(parents=True, exist_ok=True)
with zipfile.ZipFile(out / "pinetime-mcuboot-app-dfu-1.15.0.zip", "w") as z:
    z.writestr("image.bin", b"fw")
print("Memory region         Used Size  Region Size  %age Used")
print("           FLASH:      {flash} B     474632 B     35.35%")
print("             RAM:       18652 B        64 KB     28.46%")
print("post build steps for pinetime-mcuboot-app-1.15.0")
'''


def _fake_engine(tmp_path, flash=167760) -> bld.Engine:
    exe = tmp_path / "fake-engine"
    exe.write_text(FAKE_ENGINE.format(python=sys.executable, flash=flash))
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    return bld.Engine(str(exe), "podman")


def test_build_flow(tmp_path, tree, monkeypatch):
    monkeypatch.setattr(bld, "select_image", lambda *a, **k: "img")
    req = bld.BuildRequest(["Apps::Timer"], ["WatchFace::Digital"], [], tree)
    out = tmp_path / "out"
    r1 = bld.build(req, _fake_engine(tmp_path), tmp_path / "work", out, lambda m: None)
    assert r1.dfu.is_file() and r1.flash_used == 167760 and r1.flash_size == 474632 and r1.ram_used == 18652
    r2 = bld.build(req, _fake_engine(tmp_path), tmp_path / "work", out, lambda m: None)
    assert r1.output_dir != r2.output_dir  # two builds in the same second don't share a folder


def test_build_refuses_firmware_that_does_not_fit(tmp_path, tree, monkeypatch):
    monkeypatch.setattr(bld, "select_image", lambda *a, **k: "img")
    req = bld.BuildRequest(["Apps::Timer"], ["WatchFace::Digital"], [], tree)
    with pytest.raises(bld.BuildError, match="flash"):
        bld.build(req, _fake_engine(tmp_path, flash=500000), tmp_path / "work", tmp_path / "out", lambda m: None)
    assert not (tmp_path / "out").exists()


def test_failed_image_tag_is_a_build_error(tmp_path, tree, monkeypatch):
    (tree / "docker").mkdir()
    (tree / "docker/build.sh").write_text("build")
    exe = tmp_path / "engine"
    exe.write_text("#!/bin/sh\ncase \"$1\" in\n  images) echo docker.io/infinitime/infinitime-build:x ;;\n"
                   "  run) echo build ;;\n  tag) echo 'tag failed' >&2; exit 1 ;;\n  *) exit 1 ;;\nesac\n")
    exe.chmod(0o755)
    monkeypatch.setattr(bld, "_image_build_sh", lambda engine, image: "build")
    with pytest.raises(bld.BuildError, match="tag failed"):
        bld.select_image(bld.Engine(str(exe), "docker"), tree, "", "", lambda m: None)


# --------------------------------------------------------------------------- CLI


def test_cli_version(capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["--version"])
    assert e.value.code == 0 and __version__ in capsys.readouterr().out


def test_cli_validate(capsys):
    assert cli.main(["validate", str(EXAMPLES / "hello-app")]) == 0
    assert "OK: Hello Counter" in capsys.readouterr().out
    assert cli.main(["validate", str(ROOT / "tests")]) == 1
    assert "invalid:" in capsys.readouterr().err


def test_cli_reports_bad_profile(tmp_path, home, capsys):
    bad = tmp_path / "p.json"
    bad.write_text("{not json")
    assert cli.main(["list", "--profile", str(bad)]) == 1
    assert "could not read profile" in capsys.readouterr().err
    assert cli.main(["list", "--profile", str(tmp_path / "missing.json")]) == 1
    assert "could not read profile" in capsys.readouterr().err


def test_version_is_single_sourced():
    tomllib = pytest.importorskip("tomllib")  # Python 3.11+

    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert "version" not in pyproject["project"] and "version" in pyproject["project"]["dynamic"]
    assert pyproject["tool"]["setuptools"]["dynamic"]["version"] == {"attr": "itbuilder.__version__"}


# --------------------------------------------------------------------------- packaging metadata


def test_changelog_matches_version():
    sys.path.insert(0, str(ROOT / "packaging"))
    try:
        import changelog
    finally:
        sys.path.pop(0)
    releases = changelog.parse((ROOT / "CHANGELOG.md").read_text(encoding="utf-8"))
    assert releases and releases[0].version == __version__
    deb = changelog.debian(releases, "infinitime-builder", "Maint <m@example.org>", "1")
    assert deb.splitlines()[0] == f"infinitime-builder ({__version__}-1) unstable; urgency=medium"
    assert " -- Maint <m@example.org>  " in deb
    rpm = changelog.rpm(releases, "Maint <m@example.org>", "1")
    assert rpm.startswith("* ") and f"- {__version__}-1" in rpm.splitlines()[0]
    assert f'<release version="{__version__}"' in changelog.appstream(releases)



def test_make_progress_lines():
    assert bld.make_progress("[ 42%] Building CXX object src/CMakeFiles/x.dir/a.cpp.o") == 42
    assert bld.make_progress("[100%] Built target pinetime-app") == 100
    assert bld.make_progress("[  0%] Generating fonts") == 0
    assert bld.make_progress("Scanning dependencies of target lvgl") is None
    assert bld.make_progress("warning: [ 42%] in the middle") is None


def test_changelog_releases_on_the_same_day():
    sys.path.insert(0, str(ROOT / "packaging"))
    try:
        import changelog
    finally:
        sys.path.pop(0)
    text = "## [0.2.0] - 2026-11-14\n\n- Second.\n\n## [0.1.0] - 2026-11-14\n\n- First.\n"
    deb = changelog.debian(changelog.parse(text), "x", "M <m@example.org>", "1")
    stamps = [line.split("  ", 1)[1] for line in deb.splitlines() if line.startswith(" -- ")]
    # Debian (lintian latest-changelog-entry-without-new-date) needs the newer entry strictly later
    assert stamps == ["Sat, 14 Nov 2026 13:00:00 +0000", "Sat, 14 Nov 2026 12:00:00 +0000"]
    with pytest.raises(ValueError, match="dated before"):
        changelog.parse("## [0.2.0] - 2026-11-13\n\n- Second.\n\n## [0.1.0] - 2026-11-14\n\n- First.\n")
