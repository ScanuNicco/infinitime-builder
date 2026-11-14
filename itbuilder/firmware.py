"""Reading an InfiniTime source tree and preparing a customised copy of it.

Everything here is derived from the source tree itself, so forks and future
versions of InfiniTime work as long as they keep the upstream app registration
mechanism (Apps.h.in + AppTraits/WatchFaceTraits + ENABLE_USERAPPS).
"""
from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from .manifest import ExternalItem, version_tuple

APPS_IN = Path("src/displayapp/apps/Apps.h.in")
APPS_CMAKE = Path("src/displayapp/apps/CMakeLists.txt")
USER_APPS_H = Path("src/displayapp/UserApps.h")
SRC_CMAKE = Path("src/CMakeLists.txt")
FONTS_JSON = Path("src/displayapp/fonts/fonts.json")
RESOURCE_FONTS_JSON = Path("src/resources/fonts.json")
RESOURCE_IMAGES_JSON = Path("src/resources/images.json")
SYMBOLS_H = Path("src/displayapp/screens/Symbols.h")
APP_LIST_H = Path("src/displayapp/screens/ApplicationList.h")
WF_SETTING_H = Path("src/displayapp/screens/settings/SettingWatchFace.h")
FA_FONT = Path("src/displayapp/fonts/FontAwesome5-Solid+Brands+Regular.woff")
LAUNCHER_FONT = "jetbrains_mono_bold_20"
EXTERNAL_DIR = "external"  # relative to src/


class FirmwareError(RuntimeError):
    pass


@dataclass
class BuiltinItem:
    kind: str  # "app" | "watchface"
    enum: str
    name: str
    icon: str | None = None  # literal UTF-8 text for the launcher tile, if known
    default: bool = False
    header: str = ""  # header declaring the traits, relative to src/
    needs_resources: bool = False  # only works once the resource pack is on the watch

    def object_files(self) -> list[str]:
        """Source files (relative to src/) that belong to this item: the .cpp next to its header."""
        return [str(Path(self.header).with_suffix(".cpp"))] if self.header else []

    @property
    def key(self) -> str:
        return f"builtin:{self.qualified}"

    @property
    def qualified(self) -> str:
        return ("Apps::" if self.kind == "app" else "WatchFace::") + self.enum


@dataclass
class Catalog:
    version: str
    apps: list[BuiltinItem]
    watchfaces: list[BuiltinItem]
    default_apps: list[str]  # keys in default order
    default_watchfaces: list[str]
    apps_per_page: int = 6
    faces_per_page: int = 4
    all_app_enums: set[str] = field(default_factory=set)
    all_face_enums: set[str] = field(default_factory=set)


def _read(root: Path, rel: Path) -> str:
    p = root / rel
    if not p.is_file():
        raise FirmwareError(
            f"{rel} not found - this does not look like a supported InfiniTime source tree "
            "(needs the ENABLE_USERAPPS build system, InfiniTime 1.14 or later)"
        )
    return p.read_text(encoding="utf-8")


def _enum_body(text: str, enum_name: str) -> tuple[int, int]:
    m = re.search(r"enum\s+class\s+" + enum_name + r"\s*:\s*\w+\s*\{", text)
    if not m:
        raise FirmwareError(f"could not find 'enum class {enum_name}' in Apps.h.in")
    start = m.end()
    end = text.index("}", start)
    return start, end


def _enum_values(text: str, enum_name: str) -> list[str]:
    s, e = _enum_body(text, enum_name)
    body = re.sub(r"//.*", "", text[s:e])
    return [v.strip().split("=")[0].strip() for v in body.split(",") if v.strip()]


def _parse_symbols(text: str) -> dict[str, str]:
    out = {}
    for m in re.finditer(r'const\s+char\s*\*\s*(\w+)\s*=\s*"((?:[^"\\]|\\.)*)"', text):
        out[m.group(1)] = _c_string_to_text(m.group(2))
    return out


def _c_string_to_text(s: str) -> str:
    raw = bytearray()
    i = 0
    while i < len(s):
        if s[i] == "\\" and i + 1 < len(s):
            if s[i + 1] == "x":
                j = i + 2
                while j < len(s) and j < i + 4 and s[j] in "0123456789abcdefABCDEF":
                    j += 1
                raw.append(int(s[i + 2 : j], 16))
                i = j
                continue
            raw.extend(s[i + 1].encode())
            i += 2
            continue
        raw.extend(s[i].encode())
        i += 1
    return raw.decode("utf-8", errors="replace")


def _parse_default_list(cmake: str, var: str) -> list[str]:
    vals = []
    for line in cmake.splitlines():
        line = line.strip()
        if line.startswith("#"):
            continue
        m = re.match(r'set\(\s*' + var + r'\s+"(.*)"\s*\)', line)
        if m:
            vals += [v.strip() for v in m.group(1).replace("${" + var + "}", "").split(",") if v.strip()]
    return vals


def _humanise(enum: str) -> str:
    return re.sub(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", enum)


def _braced(text: str, open_pos: int) -> str:
    """The text between the brace at open_pos and its matching closing brace."""
    depth = 0
    for i in range(open_pos, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[open_pos + 1 : i]
    return text[open_pos + 1 :]


class _TraitMatch:
    def __init__(self, m: re.Match, body: str):
        self._m, self._body = m, body

    def group(self, i: int):
        return self._body if i == 4 else self._m.group(i)


def _traits(text: str):
    """AppTraits / WatchFaceTraits specialisations in a header, with their full bodies."""
    for m in re.finditer(r"struct\s+(AppTraits<Apps::(\w+)>|WatchFaceTraits<WatchFace::(\w+)>)\s*\{", text):
        yield _TraitMatch(m, _braced(text, m.end() - 1))


def fs_geometry(root: Path) -> tuple[int, int]:
    """(block size, block count) of the watch's external flash file system, from components/fs/FS.h."""
    from .sizes import DEFAULT_FS_BLOCK_COUNT, DEFAULT_FS_BLOCK_SIZE

    p = root / "src/components/fs/FS.h"
    try:
        text = p.read_text(encoding="utf-8")
        size = int(re.search(r"\bsize\s*=\s*(0x[0-9a-fA-F]+|\d+)", text).group(1), 0)
        block = int(re.search(r"\bblockSize\s*=\s*(0x[0-9a-fA-F]+|\d+)", text).group(1), 0)
        return block, size // block
    except (OSError, AttributeError, ValueError):
        return DEFAULT_FS_BLOCK_SIZE, DEFAULT_FS_BLOCK_COUNT


def project_version(root: Path) -> str:
    m = re.search(r"project\(\s*pinetime\s+VERSION\s+([\d.]+)", _read(root, Path("CMakeLists.txt")))
    return m.group(1) if m else "0.0.0"


def load_catalog(root: Path) -> Catalog:
    apps_in = _read(root, APPS_IN)
    apps_cmake = _read(root, APPS_CMAKE)
    symbols = _parse_symbols((root / SYMBOLS_H).read_text(encoding="utf-8")) if (root / SYMBOLS_H).is_file() else {}

    app_traits: dict[str, str | None] = {}
    face_names: dict[str, str] = {}
    headers: dict[str, str] = {}
    needs_resources: set[str] = set()
    for h in (root / "src/displayapp").rglob("*.h"):
        text = h.read_text(encoding="utf-8", errors="replace")
        if "Traits<" not in text:
            continue
        for m in _traits(text):
            body = m.group(4)
            headers[m.group(2) or m.group(3)] = h.relative_to(root / "src").as_posix()
            # Items whose IsAvailable() checks the filesystem need files from the resource pack.
            avail = re.search(r"IsAvailable\([^)]*\)\s*\{", body)
            if avail and not re.fullmatch(r"\s*return\s+true\s*;\s*", _braced(body, avail.end() - 1)):
                needs_resources.add(m.group(2) or m.group(3))
            if m.group(2):
                icon = None
                im = re.search(r"icon\s*=\s*(?:Screens::)?Symbols::(\w+)\s*;", body)
                if im:
                    icon = symbols.get(im.group(1))
                else:
                    im = re.search(r'icon\s*=\s*"((?:[^"\\]|\\.)*)"\s*;', body)
                    if im:
                        icon = _c_string_to_text(im.group(1))
                app_traits[m.group(2)] = icon
            else:
                nm = re.search(r'name\s*=\s*"([^"]*)"', body)
                face_names[m.group(3)] = nm.group(1) if nm else _humanise(m.group(3))

    app_enums = _enum_values(apps_in, "Apps")
    face_enums = _enum_values(apps_in, "WatchFace")
    def_apps = [v.split("::")[-1] for v in _parse_default_list(apps_cmake, "DEFAULT_USER_APP_TYPES")]
    def_faces = [v.split("::")[-1] for v in _parse_default_list(apps_cmake, "DEFAULT_WATCHFACE_TYPES")]

    apps = [
        BuiltinItem("app", e, _humanise(e), icon=app_traits[e], default=e in def_apps, header=headers.get(e, ""),
                    needs_resources=e in needs_resources)
        for e in app_enums
        if e in app_traits
    ]
    faces = [
        BuiltinItem("watchface", e, face_names.get(e, _humanise(e)), default=e in def_faces, header=headers.get(e, ""),
                    needs_resources=e in needs_resources)
        for e in face_enums
        if e in face_names
    ]

    def per_page(rel: Path, var: str, fallback: int) -> int:
        p = root / rel
        if p.is_file():
            m = re.search(var + r"\s*=\s*(\d+)", p.read_text(encoding="utf-8"))
            if m:
                return int(m.group(1))
        return fallback

    return Catalog(
        version=project_version(root),
        apps=apps,
        watchfaces=faces,
        default_apps=[f"builtin:Apps::{e}" for e in def_apps if e in app_traits],
        default_watchfaces=[f"builtin:WatchFace::{e}" for e in def_faces if e in face_names],
        apps_per_page=per_page(APP_LIST_H, "appsPerScreen", 6),
        faces_per_page=per_page(WF_SETTING_H, "settingsPerScreen", 4),
        all_app_enums=set(app_enums),
        all_face_enums=set(face_enums),
    )


def check_compat(catalog: Catalog, items: list[ExternalItem]) -> tuple[list[str], list[str]]:
    """Returns (errors, warnings) for injecting `items` into a firmware described by `catalog`."""
    errors, warnings = [], []
    taken = {"app": set(catalog.all_app_enums), "watchface": set(catalog.all_face_enums)}
    for it in items:
        if it.enum in taken[it.kind]:
            errors.append(f"'{it.name}' ({it.package.name}) uses the name {it.qualified}, which already exists")
        taken[it.kind].add(it.enum)
        req = it.package.min_infinitime
        if req and version_tuple(catalog.version) < version_tuple(req):
            warnings.append(f"'{it.name}' needs InfiniTime {req} or newer, firmware source is {catalog.version}")
    return errors, warnings


# --------------------------------------------------------------------------- preparing a build tree


def copy_tree(src: Path, dst: Path) -> None:
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst, symlinks=True, ignore=shutil.ignore_patterns("build", ".git", "node_modules"))


def _insert_enum_values(text: str, enum_name: str, values: list[str]) -> str:
    s, e = _enum_body(text, enum_name)
    body = text[s:e].rstrip()
    if body and not body.endswith(","):
        body += ","
    indent = re.search(r"\n([ \t]*)\w", text[s:e])
    pad = indent.group(1) if indent else "      "
    body += "".join(f"\n{pad}{v}," for v in values) + "\n"
    close_indent = re.search(r"([ \t]*)$", text[:e]).group(1)
    return text[:s] + body + close_indent + text[e:]


def _add_glyphs(fonts: dict, codepoints: list[str]) -> None:
    font = fonts.get(LAUNCHER_FONT)
    if not font:
        raise FirmwareError(f"font {LAUNCHER_FONT} not found in fonts.json")
    fa = next((s for s in font["sources"] if "FontAwesome" in s["file"]), None)
    if fa is None:
        raise FirmwareError(f"no FontAwesome source in {LAUNCHER_FONT}")
    have = {r.strip().lower() for r in fa["range"].split(",")}
    new = [c for c in codepoints if c.lower() not in have]
    if new:
        fa["range"] = fa["range"] + ", " + ", ".join(new)


def set_app_lists(tree: Path, apps: list[str], faces: list[str]) -> None:
    """Force the launcher app list and watch face list (in order) for this tree.

    Done in the tree rather than via the ENABLE_USERAPPS env var, because the docker build.sh of
    older releases (e.g. 1.16.1) does not pass that variable through to CMake.
    """
    p = tree / APPS_CMAKE
    text = p.read_text(encoding="utf-8")
    header = (
        "# Selection made with InfiniTime Builder\n"
        f'set(ENABLE_USERAPPS "{", ".join(apps)}")\n'
        f'set(ENABLE_WATCHFACES "{", ".join(faces)}")\n\n'
    )
    p.write_text(header + text, encoding="utf-8")


def generated_lists(build_dir: Path) -> tuple[list[str], list[str]] | None:
    """Read back the app / watch face lists from the generated Apps.h after a build."""
    p = build_dir / "src/displayapp/apps/Apps.h"
    if not p.is_file():
        return None
    text = p.read_text(encoding="utf-8")
    a = re.search(r"UserAppTypes\s*=\s*TypeList<([^>]*)>", text)
    f = re.search(r"UserWatchFaceTypes\s*=\s*WatchFaceTypeList<([^>]*)>", text)
    split = lambda m: [x.strip() for x in m.group(1).split(",") if x.strip()] if m else []  # noqa: E731
    return split(a), split(f)


def inject(tree: Path, items: list[ExternalItem], log=print) -> dict[str, list[str]]:
    """Copy third-party packages into `tree` and register their apps / watch faces.

    Returns {item key: [its source files relative to src/]}.
    """
    if not items:
        return {}
    ext_root = tree / "src" / EXTERNAL_DIR
    ext_root.mkdir(parents=True, exist_ok=True)

    pkg_dirs: dict[int, str] = {}
    for it in items:
        pkg = it.package
        if id(pkg) not in pkg_dirs:
            ident = pkg.source_id.split(":", 1)[-1].split("@")[0].replace("\\", "/").rstrip("/")
            ident = "_".join(ident.split("/")[-2:])
            safe = re.sub(r"[^A-Za-z0-9_]+", "_", ident).strip("_")[:40] or "pkg"
            dirname = f"{len(pkg_dirs):02d}_{safe}"
            shutil.copytree(pkg.root, ext_root / dirname, ignore=shutil.ignore_patterns(".git", "build"))
            pkg_dirs[id(pkg)] = dirname
            log(f"Added package {pkg.name} -> src/{EXTERNAL_DIR}/{dirname}")

    # 1. enum values
    apps_in = (tree / APPS_IN).read_text(encoding="utf-8")
    new_apps = [it.enum for it in items if it.kind == "app"]
    new_faces = [it.enum for it in items if it.kind == "watchface"]
    if new_apps:
        apps_in = _insert_enum_values(apps_in, "Apps", new_apps)
    if new_faces:
        apps_in = _insert_enum_values(apps_in, "WatchFace", new_faces)
    (tree / APPS_IN).write_text(apps_in, encoding="utf-8")

    # 2. traits headers
    ua = (tree / USER_APPS_H).read_text(encoding="utf-8")
    includes = "".join(
        f'#include "{EXTERNAL_DIR}/{pkg_dirs[id(it.package)]}/{it.header}"\n' for it in items
    )
    last_inc = list(re.finditer(r"^#include .*\n", ua, re.M))
    if not last_inc:
        raise FirmwareError("could not find includes in UserApps.h")
    pos = last_inc[-1].end()
    ua = ua[:pos] + "// Third-party apps added by InfiniTime Builder\n" + includes + ua[pos:]
    (tree / USER_APPS_H).write_text(ua, encoding="utf-8")

    # 3. sources
    srcs = []
    for it in items:
        for s in it.sources:
            p = f"{EXTERNAL_DIR}/{pkg_dirs[id(it.package)]}/{s}"
            if p not in srcs:
                srcs.append(p)
    cm = (tree / SRC_CMAKE).read_text(encoding="utf-8")
    anchor = re.search(r"^add_executable\(\$\{EXECUTABLE_NAME\}\s+\$\{SOURCE_FILES\}\)", cm, re.M)
    if not anchor:
        raise FirmwareError("could not find the app executable definition in src/CMakeLists.txt")
    block = "# Third-party apps added by InfiniTime Builder\nlist(APPEND SOURCE_FILES\n" + "".join(
        f"        {s}\n" for s in srcs
    ) + ")\n\n"
    cm = cm[: anchor.start()] + block + cm[anchor.start() :]
    (tree / SRC_CMAKE).write_text(cm, encoding="utf-8")

    # 4. icon glyphs
    glyphs = []
    for it in items:
        glyphs += [g for g in it.all_glyphs() if g not in glyphs]
    if glyphs:
        fonts = json.loads((tree / FONTS_JSON).read_text(encoding="utf-8"))
        _add_glyphs(fonts, glyphs)
        (tree / FONTS_JSON).write_text(json.dumps(fonts, indent=3) + "\n", encoding="utf-8")
        log(f"Added icon glyphs {', '.join(glyphs)} to {LAUNCHER_FONT}")

    # 5. resource pack files (fonts / images for the watch's external flash)
    if any(it.needs_resources for it in items):
        for rel, kind in ((RESOURCE_FONTS_JSON, "fonts"), (RESOURCE_IMAGES_JSON, "images")):
            path = tree / rel
            if not path.is_file():
                raise FirmwareError("this InfiniTime version has no resource pack support (src/resources), "
                                    "so watch faces with custom fonts or images cannot be added")
            table = json.loads(path.read_text(encoding="utf-8"))
            for it in items:
                for name, spec in (it.fonts if kind == "fonts" else it.images).items():
                    if name in table:
                        raise FirmwareError(f"'{it.name}' ({it.package.name}) uses the resource name '{name}', "
                                            "which is already taken")
                    spec = json.loads(json.dumps(spec))  # copy
                    base = f"../{EXTERNAL_DIR}/{pkg_dirs[id(it.package)]}/"  # relative to src/resources
                    if kind == "fonts":
                        for src in spec["sources"]:
                            src["file"] = base + src["file"]
                    else:
                        spec["sources"] = base + spec["sources"]
                    table[name] = spec
                    log(f"Added resource /{kind}/{name}.bin for {it.name}")
            path.write_text(json.dumps(table, indent=3) + "\n", encoding="utf-8")

    for it in items:
        log(f"Registered {it.qualified} ({it.name})")
    return {it.key: [f"{EXTERNAL_DIR}/{pkg_dirs[id(it.package)]}/{s}" for s in it.sources] for it in items}
