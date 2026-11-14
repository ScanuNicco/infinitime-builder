"""Building a customised firmware with the official InfiniTime docker image."""
from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import firmware as fw
from . import sizes
from .manifest import ExternalItem
from .sources import FetchError, clone_firmware, firmware_slot, host_git

HUB_REPO = "infinitime/infinitime-build"  # name on Docker Hub (its API)
# Fully qualified, so Podman doesn't ask which registry a short name means (it can't ask in this app).
IMAGE_REPO = f"docker.io/{HUB_REPO}"
IMAGE = f"{IMAGE_REPO}:latest"
ENV_TAG = "infinitime-builder-env"  # local tag for an image verified to match a firmware tree
DEFAULT_FIRMWARE_URL = "https://github.com/InfiniTimeOrg/InfiniTime.git"

Log = Callable[[str], None]


class BuildError(RuntimeError):
    pass


# --------------------------------------------------------------------------- container engine


@dataclass
class Engine:
    exe: str  # path to docker or podman
    name: str  # "docker" | "podman"

    @property
    def rootless_podman(self) -> bool:
        return self.name == "podman"

    def volume(self, host: Path, target: str) -> list[str]:
        opt = ":z" if platform.system() == "Linux" else ""  # SELinux relabel (Fedora etc.)
        return ["-v", f"{host}:{target}{opt}"]

    def user_args(self) -> list[str]:
        if platform.system() != "Linux" and platform.system() != "Darwin":
            return []
        if self.name == "podman":
            # Rootless Podman: the container's root is already the user, so files come out owned by
            # them. (--userns=keep-id would also work but makes Podman copy the whole 4 GB image.)
            return []
        if platform.system() == "Linux" and hasattr(os, "getuid"):
            return ["--user", f"{os.getuid()}:{os.getgid()}"]
        return []


def find_engine(preferred: str = "") -> Engine:
    """Find a working container engine. Raises BuildError with a helpful message otherwise."""
    order = [preferred] if preferred else []
    order += [e for e in ("docker", "podman") if e not in order]
    problems = []
    for name in order:
        exe = shutil.which(name)
        if not exe:
            continue
        try:
            r = subprocess.run([exe, "info"], capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as e:
            problems.append(f"{name}: {e}")
            continue
        if r.returncode == 0:
            # `docker` may be podman's compatibility shim
            real = "podman" if "podman" in (r.stdout + r.stderr).lower() and name == "docker" else name
            return Engine(exe, real)
        err = (r.stderr or r.stdout).strip().splitlines()
        problems.append(f"{name}: {err[-1] if err else 'not working'}")
    if not problems:
        raise BuildError(
            "Docker is not installed. Install Docker Desktop (Windows/macOS) or Docker/Podman (Linux), "
            "then restart InfiniTime Builder."
        )
    hint = ""
    if any("permission denied" in p.lower() for p in problems):
        hint = "\nYour user may need to be in the 'docker' group (then log out and back in)."
    elif any("daemon" in p.lower() or "connect" in p.lower() for p in problems):
        hint = "\nIs Docker running? Start Docker Desktop or the docker service."
    raise BuildError("Could not use a container engine:\n" + "\n".join(problems) + hint)


def stream(cmd: list[str], log: Log, cancel: Callable[[], bool] | None = None, cwd: Path | None = None) -> tuple[int, list[str]]:
    """Run a command, forwarding output lines to log. Returns (exit code, all lines)."""
    proc = subprocess.Popen(
        cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace", bufsize=1
    )
    lines = []
    assert proc.stdout
    for line in proc.stdout:
        line = line.rstrip("\n")
        lines.append(line)
        log(line)
        if cancel and cancel():
            proc.terminate()
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                proc.kill()
            raise BuildError("Build cancelled")
    return proc.wait(), lines


def image_present(engine: Engine, image: str = IMAGE) -> bool:
    return subprocess.run([engine.exe, "image", "inspect", image], capture_output=True).returncode == 0


def pull_image(engine: Engine, log: Log, cancel=None, image: str = IMAGE, output: Log | None = None) -> None:
    log(f"Downloading the official build image {image} (about 1 GB, only needed once per toolchain)...")
    code, _ = stream([engine.exe, "pull", image], output or log, cancel)
    if code != 0:
        raise BuildError("Could not download the build image. Check your internet connection.")


def container_git(engine: Engine, log: Log):
    def run(args: list[str], cwd: Path) -> None:
        cmd = [engine.exe, "run", "--rm", *engine.user_args(), *engine.volume(cwd, "/work"),
               "-e", "HOME=/tmp", "-w", "/work", "--entrypoint", "git", IMAGE, *args]
        code, lines = stream(cmd, log)
        if code != 0:
            raise FetchError("git clone failed: " + "\n".join(lines[-5:]))
    return run


# --------------------------------------------------------------------------- choosing the build image
#
# The build image's toolchain must match the firmware version (e.g. InfiniTime 1.16.1 needs
# GCC 10.3, while the current `latest` image ships GCC 15 which rejects 1.16.1's code).
# The image's /opt/build.sh is a copy of docker/build.sh from the commit it was built from,
# so an image is a match when the two files are identical.


def _image_build_sh(engine: Engine, image: str) -> str | None:
    r = subprocess.run([engine.exe, "run", "--rm", "--entrypoint", "cat", image, "/opt/build.sh"],
                       capture_output=True, text=True, errors="replace")
    return r.stdout if r.returncode == 0 else None


def _candidate_tags(firmware_url: str, ref: str, log: Log) -> list[str]:
    """Docker Hub tags likely to match, best first, using the date of the last docker/ change."""
    from .sources import _http

    tags: list[str] = []
    m = re.match(r"^(?:https?://github\.com/|git@github\.com:)([^/]+/[^/]+?)(?:\.git)?/?$", firmware_url.strip())
    try:
        hub = json.loads(_http(f"https://hub.docker.com/v2/repositories/{HUB_REPO}/tags?page_size=100", timeout=30))
        hub_tags = [(t["last_updated"][:10], t["name"]) for t in hub.get("results", []) if t["name"].startswith("sha-")]
    except (FetchError, ValueError, KeyError):
        hub_tags = []
    if m and hub_tags:
        try:
            q = f"https://api.github.com/repos/{m.group(1)}/commits?path=docker&per_page=1" + (f"&sha={ref}" if ref else "")
            date = json.loads(_http(q, timeout=30))[0]["commit"]["committer"]["date"][:10]
            tags += [name for d, name in sorted(hub_tags) if d >= date][:2]
        except (FetchError, ValueError, KeyError, IndexError):
            pass
    if "latest" not in tags:
        tags.append("latest")
    return tags


def select_image(engine: Engine, tree: Path, firmware_url: str, ref: str, log: Log, cancel=None,
                 output: Log | None = None) -> str:
    """Return a build image whose toolchain matches the firmware source in `tree`."""
    wanted = (tree / "docker" / "build.sh").read_text(encoding="utf-8", errors="replace") \
        if (tree / "docker" / "build.sh").is_file() else None
    if wanted is None:
        log("This firmware has no docker/build.sh; using the latest official build image.")
        if not image_present(engine, IMAGE):
            pull_image(engine, log, cancel, IMAGE, output)
        return IMAGE
    h = hashlib.sha256(wanted.encode()).hexdigest()[:12]
    env = f"{ENV_TAG}:{h}"
    if image_present(engine, env):
        return env

    def adopt(image: str) -> bool:
        if _image_build_sh(engine, image) == wanted:
            subprocess.run([engine.exe, "tag", image, env], check=True, capture_output=True)
            log(f"Using build image {image}")
            return True
        return False

    # 1. images already downloaded
    r = subprocess.run([engine.exe, "images", IMAGE_REPO, "--format", "{{.Repository}}:{{.Tag}}"],
                       capture_output=True, text=True)
    for image in [l for l in r.stdout.split() if not l.endswith(":<none>")]:
        if adopt(image):
            return env
    # 2. official images from Docker Hub
    log("Finding the official build image for this firmware version...")
    for tag in _candidate_tags(firmware_url, ref, log):
        image = f"{IMAGE_REPO}:{tag}"
        try:
            pull_image(engine, log, cancel, image, output)
        except BuildError:
            continue
        if adopt(image):
            return env
    # 3. build it from the firmware's own Dockerfile (always matches; slower, needs internet)
    log("No matching prebuilt image found; building the build environment from this firmware's "
        "Dockerfile. This can take 10+ minutes the first time.")
    code, _ = stream([engine.exe, "build", "-t", env, str(tree / "docker")], output or log, cancel)
    if code != 0:
        raise BuildError("Could not create the build environment (docker build failed). See the log.")
    return env


# --------------------------------------------------------------------------- build


@dataclass
class BuildRequest:
    apps: list[str]  # qualified names in launcher order, e.g. "Apps::Timer"
    watchfaces: list[str]
    external: list[ExternalItem]
    firmware_dir: Path
    firmware_url: str = DEFAULT_FIRMWARE_URL
    firmware_ref: str = ""


@dataclass
class BuildResult:
    output_dir: Path
    dfu: Path
    resources: Path | None
    flash_used: int | None = None
    flash_size: int | None = None
    ram_used: int | None = None
    seconds: float = 0
    files: list[Path] = field(default_factory=list)
    resource_bytes: int = 0
    resource_blocks: int = 0
    resource_capacity: int = 0  # blocks

    @property
    def resources_fit(self) -> bool:
        return self.resource_blocks <= self.resource_capacity


def ensure_firmware(cache_dir: Path, url: str, ref: str, engine: Engine | None, log: Log, refresh: bool = False) -> Path:
    slot = firmware_slot(cache_dir, url, ref)
    marker = slot / "src/displayapp/apps/Apps.h.in"
    if refresh or not marker.is_file() or not (slot / "src/libs/lvgl/lvgl.h").is_file():
        if shutil.which("git"):
            runner = lambda a, c: host_git(a, c)  # noqa: E731
        elif engine:
            runner = container_git(engine, log)
        else:
            raise BuildError("git or docker is needed to download InfiniTime")
        clone_firmware(url, ref, slot, runner, log)
    return slot


def _parse_size(lines: list[str]) -> tuple[int | None, int | None, int | None]:
    used = size = ram = None
    for i, line in enumerate(lines):
        m = re.match(r"\s*(\d+)\s+(\d+)\s+(\d+)\s+\d+\s+[0-9a-f]+\s+pinetime-mcuboot-app-[\d.]+\.out\s*$", line)
        if m:
            used = int(m.group(1)) + int(m.group(2))
            ram = int(m.group(2)) + int(m.group(3))
        m = re.match(r"\s*FLASH:\s+\d+ B\s+(\d+) B", line)
        if m:
            size = max(size or 0, int(m.group(1)))
    return used, size, ram


def explain_failure(lines: list[str], request: "BuildRequest | None" = None) -> str:
    text = "\n".join(lines)
    m = re.search(r"region [`'\"]?FLASH[`'\"]? overflowed by (\d+) bytes", text)
    if m:
        return (f"The firmware is {int(m.group(1)):,} bytes too big for the watch's flash. "
                "Remove some apps or watch faces and build again.")
    m = re.search(r"region [`'\"]?RAM[`'\"]? overflowed by (\d+) bytes", text)
    if m:
        return f"The firmware needs {int(m.group(1)):,} bytes more RAM than the watch has. Remove some apps."
    errs =[l for l in lines if re.search(r"\berror\b", l, re.I) and "app_error" not in l]
    ext_errs = [l for l in errs if f"/{fw.EXTERNAL_DIR}/" in l]
    if ext_errs:
        return ("A third-party app failed to compile against this InfiniTime version. "
                "It may need updating by its author, or try a different firmware version.\n\n" + "\n".join(ext_errs[:6]))
    if errs:
        return "The build failed:\n\n" + "\n".join(errs[:8])
    return "The build failed. See the log for details."


def build(
    request: BuildRequest,
    engine: Engine,
    work_root: Path,
    output_root: Path,
    log: Log,
    cancel: Callable[[], bool] | None = None,
    output: Log | None = None,
) -> BuildResult:
    """Build firmware. `log` receives progress messages, `output` the raw tool output (default: log)."""
    output = output or log
    if not request.watchfaces:
        raise BuildError("At least one watch face is required.")
    if not request.apps:
        raise BuildError("At least one app is required.")
    t0 = time.time()
    image = select_image(engine, request.firmware_dir, request.firmware_url, request.firmware_ref, log, cancel, output)

    tree = work_root / "tree"
    log("Preparing source tree...")
    fw.copy_tree(request.firmware_dir, tree)
    fw.inject(tree, request.external, log)
    fw.set_app_lists(tree, request.apps, request.watchfaces)

    cmd = [engine.exe, "run", "--rm", *engine.user_args(), *engine.volume(tree, "/sources"), image]
    log("Compiling firmware (this takes a minute or two)...")
    output("$ " + " ".join(cmd))
    code, lines = stream(cmd, output, cancel)
    if code != 0:
        raise BuildError(explain_failure(lines, request))

    got = fw.generated_lists(tree / "build")
    if got != (request.apps, request.watchfaces):
        raise BuildError("The firmware built, but its app list does not match your selection, so it was not saved.\n"
                         f"Expected apps: {request.apps}\nGot: {got[0] if got else '?'}")

    out = tree / "build" / "output"
    dfus = sorted(out.glob("pinetime-mcuboot-app-dfu-*.zip"))
    if not dfus:
        raise BuildError("The build finished but no DFU file was produced.")
    mem = sizes.memory_usage(lines)
    flash_used, flash_size = mem.get("FLASH", (None, None))
    ram_used, ram_size = mem.get("RAM", (None, None))
    if flash_used is None:  # older toolchains may not print the table
        flash_used, flash_size, ram_used = _parse_size(lines)
        flash_size = flash_size or sizes.DEFAULT_FLASH_LIMIT
    check_limits(flash_used, flash_size, ram_used, ram_size)  # before anything is saved

    stamp = time.strftime("%Y-%m-%d_%H-%M-%S")
    dest = output_root / f"infinitime-{stamp}"
    dest.mkdir(parents=True, exist_ok=True)
    files = []
    for f in [*dfus, *out.glob("infinitime-resources-*.zip")]:
        shutil.copy2(f, dest / f.name)
        files.append(dest / f.name)
    res = next((f for f in files if f.name.startswith("infinitime-resources")), None)
    result = BuildResult(dest, dest / dfus[-1].name, res, flash_used, flash_size, ram_used, time.time() - t0, files)
    if res is not None:
        result.resource_bytes, result.resource_blocks, result.resource_capacity = resource_pack_usage(
            res, *fw.fs_geometry(request.firmware_dir))
        if not result.resources_fit:
            log(resource_warning(result.resource_blocks, result.resource_capacity, result.resource_bytes))
    return result


def resource_pack_usage(zip_path: Path, block_size: int, block_count: int) -> tuple[int, int, int]:
    """(bytes, blocks used, blocks available) of a resource pack once installed on the watch."""
    import zipfile

    with zipfile.ZipFile(zip_path) as z:
        manifest = json.loads(z.read("resources.json"))
        file_sizes = [z.getinfo(r["filename"]).file_size for r in manifest.get("resources", [])]
    return sum(file_sizes), sizes.resource_blocks(file_sizes, block_size), sizes.resource_capacity(block_count)


def resource_warning(blocks: int, capacity: int, nbytes: int, block_size: int = sizes.DEFAULT_FS_BLOCK_SIZE) -> str:
    over = (blocks - capacity) * block_size
    return (f"The resource pack ({nbytes / 1024:.0f} KB of files, {blocks * block_size / 1024:.0f} KB once stored) is "
            f"{over / 1024:.0f} KB larger than the {capacity * block_size / 1024:.0f} KB the watch has room for. "
            "It will not install completely: switch off some watch faces or apps that use large fonts or images.")


def check_limits(flash_used, flash_size, ram_used, ram_size) -> None:
    """Refuse firmware that does not fit on a PineTime (normally the linker already fails)."""
    if flash_used and flash_size and flash_used > flash_size:
        raise BuildError(f"The firmware needs {flash_used:,} bytes of flash but the PineTime only has room for "
                         f"{flash_size:,}. Turn off some apps or watch faces.")
    if ram_used and ram_size and ram_used > ram_size:
        raise BuildError(f"The firmware needs {ram_used:,} bytes of RAM, more than the PineTime's {ram_size:,}.")


# --------------------------------------------------------------------------- size calibration


def calibrate(
    firmware_dir: Path,
    catalog: fw.Catalog,
    external: list[ExternalItem],
    engine: Engine,
    work_root: Path,
    log: Log,
    cancel: Callable[[], bool] | None = None,
    output: Log | None = None,
    firmware_url: str = DEFAULT_FIRMWARE_URL,
    firmware_ref: str = "",
) -> sizes.SizeProfile:
    """Measure how much flash every app / watch face costs (see sizes.py)."""
    output = output or (lambda line: None)
    image = select_image(engine, firmware_dir, firmware_url, firmware_ref, log, cancel, output)
    failed: list[str] = []
    for attempt_external in ([external, []] if external else [[]]):
        tree = work_root / "calibrate"
        fw.copy_tree(firmware_dir, tree)
        ownership = fw.inject(tree, attempt_external, output)
        for it in catalog.apps + catalog.watchfaces:
            ownership[it.key] = it.object_files()
        apps = [a.qualified for a in catalog.apps] + [e.qualified for e in attempt_external if e.kind == "app"]
        faces = [f.qualified for f in catalog.watchfaces] + [e.qualified for e in attempt_external if e.kind == "watchface"]
        fw.set_app_lists(tree, apps, faces)
        cm = tree / fw.SRC_CMAKE  # cross reference table in the map, for the reference graph
        cm.write_text(cm.read_text(encoding="utf-8").replace(
            "-Wl,-Map=${EXECUTABLE_FILE_NAME}.map", "-Wl,-Map=${EXECUTABLE_FILE_NAME}.map -Wl,--cref"), encoding="utf-8")
        ld = tree / "gcc_nrf52.ld"
        text = ld.read_text(encoding="utf-8")
        text, n = re.subn(r"^APP_SIZE\s*=.*?;", "APP_SIZE = 0x100000; /* calibration: no limit */", text, flags=re.M)
        if n:
            ld.write_text(text, encoding="utf-8")
        cmd = [engine.exe, "run", "--rm", *engine.user_args(), *engine.volume(tree, "/sources"),
               "-e", "DISABLE_POSTBUILD=true", image, "/opt/build.sh", "pinetime-app"]
        log("Measuring app sizes" + (" (including third-party apps)..." if attempt_external else "..."))
        code, lines = stream(cmd, output, cancel)
        maps = sorted((tree / "build" / "src").glob("pinetime-app-*.map"))
        if code == 0 and maps:
            mem = sizes.memory_usage(lines, "pinetime-app")
            total = mem["FLASH"][0] if mem.get("FLASH") else None
            if not n:
                log("Note: unknown linker layout, assuming the standard PineTime flash size.")
            block_size, block_count = fw.fs_geometry(firmware_dir)
            res_dir = tree / "build" / "src" / "resources"
            resource_sizes = {}
            for table in (fw.RESOURCE_FONTS_JSON, fw.RESOURCE_IMAGES_JSON):
                if (tree / table).is_file():
                    for name in json.loads((tree / table).read_text(encoding="utf-8")):
                        f = res_dir / f"{name}.bin"
                        if f.is_file():
                            resource_sizes[name] = f.stat().st_size
            resource_owner = {name: it.key for it in attempt_external for name in [*it.fonts, *it.images]}
            return sizes.build_profile(maps[-1].read_text(encoding="utf-8", errors="replace"), total, ownership,
                                       failed=failed, resource_sizes=resource_sizes, resource_owner=resource_owner,
                                       fs_block_size=block_size, fs_block_count=block_count)
        if attempt_external:
            failed = [e.key for e in attempt_external]
            log("A third-party app failed to compile; measuring the built-in apps only.")
    raise BuildError("Could not measure app sizes:\n" + explain_failure(lines, None))
