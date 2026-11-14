"""Third-party package manifests (``infinitime-app.json``).

A package repository contains one manifest at its root describing one or more
apps / watch faces. See MANIFEST.md for the format.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

MANIFEST_NAME = "infinitime-app.json"
SUPPORTED_MANIFEST_VERSION = 1

IDENT_RE = re.compile(r"^[A-Z][A-Za-z0-9_]{0,47}$")
# Resource names become /fonts/<name>.bin or /images/<name>.bin on the watch (littlefs name_max = 50).
RESOURCE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,40}$")
# The only formats InfiniTime's resource image converter (src/resources/lv_img_conv.py) implements.
IMAGE_COLOR_FORMATS = {"CF_TRUE_COLOR_ALPHA", "CF_INDEXED_1_BIT"}
CODEPOINT_RE = re.compile(r"^0x[0-9a-fA-F]{2,6}$")
VERSION_RE = re.compile(r"^\d+(\.\d+){0,2}$")
HOMEPAGE_RE = re.compile(r"^https?://[^\s/]+\.[^\s]*$")


class ManifestError(ValueError):
    pass


@dataclass
class ExternalItem:
    """One app or watch face provided by a third-party package."""

    kind: str  # "app" | "watchface"
    enum: str  # C++ enumerator name, e.g. "Hello" -> Apps::Hello
    name: str  # display name
    header: str  # header (relative to package root) declaring the traits
    sources: list[str]
    description: str = ""
    icon: str | None = None  # FontAwesome codepoint shown in launcher, e.g. "0xf1b0"
    glyphs: list[str] = field(default_factory=list)  # extra codepoints for jetbrains_mono_bold_20
    # Files for the watch's external flash, in InfiniTime's src/resources/{fonts,images}.json format
    # (paths relative to the package). They go into the infinitime-resources-*.zip.
    fonts: dict[str, dict] = field(default_factory=dict)
    images: dict[str, dict] = field(default_factory=dict)
    screenshot: str | None = None  # PNG of the face/app (relative to the package), shown by the builder
    package: "Package | None" = None

    @property
    def needs_resources(self) -> bool:
        return bool(self.fonts or self.images)

    @property
    def key(self) -> str:
        assert self.package is not None
        return f"ext:{self.package.source_id}#{self.enum}"

    @property
    def qualified(self) -> str:
        return ("Apps::" if self.kind == "app" else "WatchFace::") + self.enum

    def all_glyphs(self) -> list[str]:
        out = list(self.glyphs)
        if self.icon and self.icon not in out:
            out.append(self.icon)
        return out


@dataclass
class Package:
    source_id: str  # e.g. "github:owner/repo@ref" or "local:/path"
    root: Path  # directory containing the manifest
    name: str
    author: str
    description: str
    min_infinitime: str | None
    homepage: str | None
    items: list[ExternalItem]


def _rel_path(value, what: str) -> str:
    if not isinstance(value, str) or not value:
        raise ManifestError(f"{what} must be a non-empty string")
    p = PurePosixPath(value.replace("\\", "/"))
    if p.is_absolute() or ".." in p.parts:
        raise ManifestError(f"{what} '{value}' must be a relative path inside the package")
    if p.suffix not in {".h", ".hpp", ".cpp", ".cc", ".c"}:
        raise ManifestError(f"{what} '{value}' must be a C/C++ source or header file")
    return str(p)


def _codepoint(value, what: str) -> str:
    if not isinstance(value, str) or not CODEPOINT_RE.match(value):
        raise ManifestError(f"{what} must be a hex codepoint string like \"0xf1b0\", got {value!r}")
    return "0x" + value[2:].lower()


def _resource_path(value, what: str, suffixes: set[str]) -> str:
    if not isinstance(value, str) or not value:
        raise ManifestError(f"{what} must be a non-empty string")
    p = PurePosixPath(value.replace("\\", "/"))
    if p.is_absolute() or ".." in p.parts:
        raise ManifestError(f"{what} '{value}' must be a relative path inside the package")
    if p.suffix.lower() not in suffixes:
        raise ManifestError(f"{what} '{value}' must be one of: {', '.join(sorted(suffixes))}")
    return str(p)


def _parse_resources(raw, where: str) -> tuple[dict, dict]:
    if raw is None:
        return {}, {}
    if not isinstance(raw, dict) or set(raw) - {"fonts", "images"}:
        raise ManifestError(f"{where}.resources may only contain 'fonts' and 'images'")
    fonts, images = {}, {}
    for name, spec in (raw.get("fonts") or {}).items():
        w = f"{where}.resources.fonts.{name}"
        if not RESOURCE_NAME_RE.match(name):
            raise ManifestError(f"{w}: resource names must be lower_case_with_underscores (2-41 characters)")
        if not isinstance(spec, dict) or not isinstance(spec.get("sources"), list) or not spec["sources"]:
            raise ManifestError(f"{w} needs a 'sources' list")
        sources = []
        for i, src in enumerate(spec["sources"]):
            if not isinstance(src, dict):
                raise ManifestError(f"{w}.sources[{i}] must be an object")
            entry = {"file": _resource_path(src.get("file"), f"{w}.sources[{i}].file", {".ttf", ".otf", ".woff"})}
            if not (src.get("symbols") or src.get("range")):
                raise ManifestError(f"{w}.sources[{i}] needs 'symbols' or 'range' (only the glyphs you use)")
            for k in ("symbols", "range"):
                if k in src:
                    if not isinstance(src[k], str):
                        raise ManifestError(f"{w}.sources[{i}].{k} must be a string")
                    if src[k].startswith("-"):
                        raise ManifestError(f"{w}.sources[{i}].{k} must not start with '-' (the font converter would "
                                            "read it as an option); put the '-' later in the string")
                    entry[k] = src[k]
            sources.append(entry)
        size, bpp = spec.get("size"), spec.get("bpp", 1)
        if not isinstance(size, int) or not 6 <= size <= 200:
            raise ManifestError(f"{w}.size must be a number of pixels between 6 and 200")
        if bpp not in (1, 2, 4):
            raise ManifestError(f"{w}.bpp must be 1, 2 or 4")
        fonts[name] = {"sources": sources, "bpp": bpp, "size": size, "format": "bin", "target_path": "/fonts/"}
    for name, spec in (raw.get("images") or {}).items():
        w = f"{where}.resources.images.{name}"
        if not RESOURCE_NAME_RE.match(name):
            raise ManifestError(f"{w}: resource names must be lower_case_with_underscores (2-41 characters)")
        if not isinstance(spec, dict):
            raise ManifestError(f"{w} must be an object")
        cf = spec.get("color_format", "CF_TRUE_COLOR_ALPHA")
        if cf not in IMAGE_COLOR_FORMATS:
            raise ManifestError(f"{w}.color_format must be CF_TRUE_COLOR_ALPHA (full colour) or CF_INDEXED_1_BIT "
                                "(two colours) - the only formats InfiniTime's image converter supports")
        images[name] = {
            "sources": _resource_path(spec.get("sources"), f"{w}.sources", {".png"}),
            "color_format": cf,
            "output_format": "bin",
            "binary_format": "ARGB8565_RBSWAP",
            "target_path": "/images/",
        }
    return fonts, images


def _parse_item(raw: dict, idx: int) -> ExternalItem:
    where = f"apps[{idx}]"
    if not isinstance(raw, dict):
        raise ManifestError(f"{where} must be an object")
    kind = raw.get("type", "app")
    if kind not in ("app", "watchface"):
        raise ManifestError(f"{where}.type must be 'app' or 'watchface'")
    enum = raw.get("enum")
    if not isinstance(enum, str) or not IDENT_RE.match(enum):
        raise ManifestError(f"{where}.enum must be a C++ identifier starting with a capital letter (e.g. \"MyApp\")")
    name = raw.get("name") or enum
    header = _rel_path(raw.get("header"), f"{where}.header")
    sources = raw.get("sources", [])
    if not isinstance(sources, list):
        raise ManifestError(f"{where}.sources must be a list")
    sources = [_rel_path(s, f"{where}.sources[{i}]") for i, s in enumerate(sources)]
    icon = raw.get("icon")
    if icon is not None:
        icon = _codepoint(icon, f"{where}.icon")
    glyphs = [_codepoint(g, f"{where}.glyphs[{i}]") for i, g in enumerate(raw.get("glyphs", []))]
    fonts, images = _parse_resources(raw.get("resources"), where)
    screenshot = raw.get("screenshot")
    if screenshot is not None:
        screenshot = _resource_path(screenshot, f"{where}.screenshot", {".png"})
    return ExternalItem(
        kind=kind,
        enum=enum,
        name=str(name),
        header=header,
        sources=sources,
        description=str(raw.get("description", "")),
        icon=icon,
        glyphs=glyphs,
        fonts=fonts,
        images=images,
        screenshot=screenshot,
    )


def parse_manifest(data: dict, source_id: str, root: Path) -> Package:
    if not isinstance(data, dict):
        raise ManifestError("manifest must be a JSON object")
    ver = data.get("manifestVersion", 1)
    if ver != SUPPORTED_MANIFEST_VERSION:
        raise ManifestError(f"unsupported manifestVersion {ver} (this builder supports {SUPPORTED_MANIFEST_VERSION})")
    raw_items = data.get("apps")
    if raw_items is None:
        raise ManifestError("manifest must contain an 'apps' list")
    if not isinstance(raw_items, list) or not raw_items:
        raise ManifestError("'apps' must be a non-empty list")
    homepage = data.get("homepage")
    if homepage is not None and (not isinstance(homepage, str) or not HOMEPAGE_RE.match(homepage)):
        raise ManifestError("homepage must be an http:// or https:// URL")
    min_it = data.get("minInfiniTime")
    if min_it is not None and (not isinstance(min_it, str) or not VERSION_RE.match(min_it)):
        raise ManifestError("minInfiniTime must look like \"1.15.0\"")
    pkg = Package(
        source_id=source_id,
        root=root,
        name=str(data.get("name") or root.name),
        author=str(data.get("author", "")),
        description=str(data.get("description", "")),
        min_infinitime=min_it,
        homepage=homepage,
        items=[],
    )
    seen = set()
    for i, raw in enumerate(raw_items):
        item = _parse_item(raw, i)
        if item.enum in seen:
            raise ManifestError(f"duplicate enum '{item.enum}' in manifest")
        seen.add(item.enum)
        resource_files = [s["file"] for f in item.fonts.values() for s in f["sources"]]
        resource_files += [i["sources"] for i in item.images.values()]
        resource_files += [item.screenshot] if item.screenshot else []
        for rel in [item.header, *item.sources, *resource_files]:
            if not (root / rel).is_file():
                raise ManifestError(f"file '{rel}' listed in manifest does not exist in the package")
        item.package = pkg
        pkg.items.append(item)
    return pkg


def load_package(root: Path, source_id: str) -> Package:
    path = root / MANIFEST_NAME
    if not path.is_file():
        raise ManifestError(f"no {MANIFEST_NAME} found at the root of the package")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ManifestError(f"{MANIFEST_NAME} is not valid JSON: {e}") from e
    return parse_manifest(data, source_id, root)


def version_tuple(v: str) -> tuple[int, ...]:
    parts = [int(x) for x in re.findall(r"\d+", v)[:3]]
    return tuple(parts + [0] * (3 - len(parts)))
