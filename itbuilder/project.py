"""The user's configuration: firmware source, package sources and the chosen apps/watch faces."""
from __future__ import annotations

import json
import os
import platform
from dataclasses import dataclass, field
from pathlib import Path

from . import firmware as fw
from .builder import DEFAULT_FIRMWARE_URL, BuildRequest
from .manifest import ExternalItem, ManifestError, Package
from .sources import FetchError, SourceSpec, SourceStore

APP_NAME = "infinitime-builder"
# Screenshots of InfiniTime's own watch faces (taken in the InfiniEmu emulator), by enum name.
BUILTIN_SCREENSHOTS = Path(__file__).resolve().parent / "screenshots"

# What a fresh install starts with: the bare minimum, everything else is opt-in.
MINIMAL_APPS = ["builtin:Apps::Timer", "builtin:Apps::StopWatch", "builtin:Apps::HeartRate"]
MINIMAL_WATCHFACES = ["builtin:WatchFace::Digital", "builtin:WatchFace::Analog"]

# Package sources a fresh install starts with (their apps start switched off).
DEFAULT_SOURCES = [("github", "ScanuNicco/infinitime-sky-games"), ("github", "ScanuNicco/infinitime-concept-faces")]


def default_sources() -> list[SourceSpec]:
    return [SourceSpec(kind, location) for kind, location in DEFAULT_SOURCES]


def data_dir() -> Path:
    # INFINITIME_BUILDER_HOME moves only the builder's own files (portable installs, tests); unlike
    # XDG_DATA_HOME it doesn't also move other programs' data, such as Podman's image store.
    override = os.environ.get("INFINITIME_BUILDER_HOME")
    if override:
        return Path(override)
    system = platform.system()
    if system == "Windows":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
    elif system == "Darwin":
        base = Path.home() / "Library/Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    return base / APP_NAME


def default_output_dir() -> Path:
    return Path.home() / "InfiniTime Builds"


@dataclass
class Settings:
    firmware_url: str = DEFAULT_FIRMWARE_URL
    firmware_ref: str = ""  # empty -> latest release, resolved at load time
    sources: list[SourceSpec] = field(default_factory=default_sources)
    # Default sources this install has already been given. A default is added once; if the user
    # removes it, it is not added again. Lets new defaults reach existing installs too.
    seen_defaults: list[str] = field(default_factory=lambda: [s.id for s in default_sources()])
    apps: list[str] | None = None  # ordered keys; None -> firmware defaults
    watchfaces: list[str] | None = None
    output_dir: str = str(default_output_dir())
    engine: str = ""

    def to_json(self, include_local: bool = True) -> dict:
        return {
            "format": "infinitime-builder-profile",
            "version": 1,
            "firmware": {"url": self.firmware_url, "ref": self.firmware_ref},
            "sources": [s.to_json() for s in self.sources if include_local or s.kind != "local"],
            "apps": self.apps,
            "watchfaces": self.watchfaces,
            **({"output_dir": self.output_dir, "engine": self.engine, "seen_defaults": self.seen_defaults}
               if include_local else {}),
        }

    @staticmethod
    def from_json(d: dict, base: "Settings | None" = None) -> "Settings":
        s = Settings() if base is None else Settings(**{**base.__dict__})
        fwd = d.get("firmware", {})
        s.firmware_url = fwd.get("url", s.firmware_url)
        s.firmware_ref = fwd.get("ref", s.firmware_ref)
        if "sources" in d:
            s.sources = [SourceSpec.from_json(x) for x in d["sources"]]
        s.apps = d.get("apps", s.apps)
        s.watchfaces = d.get("watchfaces", s.watchfaces)
        s.output_dir = d.get("output_dir", s.output_dir)
        s.engine = d.get("engine", s.engine)
        if "seen_defaults" in d:
            s.seen_defaults = list(d["seen_defaults"])
        elif base is None:
            s.seen_defaults = []  # saved before default sources existed: none offered yet
        return s

    def offer_default_sources(self) -> bool:
        """Add default sources this install hasn't been given yet. Returns True if anything changed."""
        changed = False
        for spec in default_sources():
            if spec.id in self.seen_defaults:
                continue
            self.seen_defaults.append(spec.id)
            if spec not in self.sources:
                self.sources.append(spec)
            changed = True
        return changed


def settings_path() -> Path:
    return data_dir() / "settings.json"


def load_settings() -> Settings:
    p = settings_path()
    if p.is_file():
        try:
            s = Settings.from_json(json.loads(p.read_text(encoding="utf-8")))
        except (ValueError, KeyError, TypeError):
            return Settings()
        if s.offer_default_sources():
            save_settings(s)
        return s
    return Settings()


def save_settings(s: Settings) -> None:
    p = settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(s.to_json(), indent=2), encoding="utf-8")


@dataclass
class Entry:
    """Something that can be put on the watch: a built-in or external app/watch face."""

    key: str
    kind: str
    name: str
    qualified: str
    origin: str  # "Built-in" or package name
    icon: str | None = None
    description: str = ""
    external: ExternalItem | None = None
    needs_resources: bool = False
    screenshot: Path | None = None


class Project:
    def __init__(self, settings: Settings, store: SourceStore):
        self.settings = settings
        self.store = store
        self.catalog: fw.Catalog | None = None
        self.firmware_dir: Path | None = None
        self.firmware_ref = ""
        self.packages: dict[str, Package] = {}  # source id -> package
        self.package_errors: dict[str, str] = {}

    # -- loading
    def set_firmware(self, firmware_dir: Path, ref: str = "") -> None:
        self.firmware_dir = firmware_dir
        self.firmware_ref = ref
        self.catalog = fw.load_catalog(firmware_dir)

    def load_source(self, spec: SourceSpec, refresh: bool = False) -> Package:
        try:
            pkg = self.store.fetch(spec, refresh=refresh)
        except (FetchError, ManifestError, OSError) as e:
            self.package_errors[spec.id] = str(e)
            self.packages.pop(spec.id, None)
            raise
        self.package_errors.pop(spec.id, None)
        self.packages[spec.id] = pkg
        return pkg

    def load_all_sources(self, log=print) -> None:
        for spec in self.settings.sources:
            try:
                self.load_source(spec)
            except (FetchError, ManifestError, OSError) as e:
                log(f"Could not load {spec.id}: {e}")

    # -- entries
    def entries(self, kind: str) -> dict[str, Entry]:
        out: dict[str, Entry] = {}
        if self.catalog:
            for b in self.catalog.apps if kind == "app" else self.catalog.watchfaces:
                shot = BUILTIN_SCREENSHOTS / f"{b.enum}.png"
                out[b.key] = Entry(b.key, kind, b.name, b.qualified, "Built-in", b.icon,
                                   needs_resources=b.needs_resources, screenshot=shot if shot.is_file() else None)
        for pkg in self.packages.values():
            for it in pkg.items:
                if it.kind == kind:
                    icon = chr(int(it.icon, 16)) if it.icon else None
                    out[it.key] = Entry(it.key, kind, it.name, it.qualified, pkg.name, icon,
                                        it.description or pkg.description, it, it.needs_resources,
                                        pkg.root / it.screenshot if it.screenshot else None)
        return out

    def selected(self, kind: str) -> list[str]:
        sel = self.settings.apps if kind == "app" else self.settings.watchfaces
        if sel is None:
            available = self.entries(kind)
            sel = [k for k in (MINIMAL_APPS if kind == "app" else MINIMAL_WATCHFACES) if k in available]
        return list(sel)

    def use_infinitime_defaults(self) -> None:
        """Select what the official firmware ships with."""
        if self.catalog:
            self.settings.apps = list(self.catalog.default_apps)
            self.settings.watchfaces = list(self.catalog.default_watchfaces)

    # -- sizes
    def external_items(self) -> list[ExternalItem]:
        return [it for pkg in self.packages.values() for it in pkg.items]

    def size_fingerprint(self) -> str:
        """Identifies the set of code a size calibration was made for."""
        import hashlib

        h = hashlib.sha256(str(self.firmware_dir).encode())
        for it in sorted(self.external_items(), key=lambda i: i.key):
            h.update(it.key.encode())
            for rel in [it.header, *it.sources]:
                try:
                    h.update((it.package.root / rel).read_bytes())
                except OSError:
                    pass
        return h.hexdigest()[:16]

    def size_profile_path(self) -> Path:
        return data_dir() / "cache" / "sizes" / f"{self.size_fingerprint()}.json"

    def set_selected(self, kind: str, keys: list[str]) -> None:
        if kind == "app":
            self.settings.apps = keys
        else:
            self.settings.watchfaces = keys

    # -- build
    def resource_users(self) -> list[str]:
        """Names of selected items that need the resource pack installed on the watch."""
        out = []
        for kind in ("app", "watchface"):
            entries = self.entries(kind)
            out += [entries[k].name for k in self.selected(kind) if k in entries and entries[k].needs_resources]
        return out

    def build_request(self) -> tuple[BuildRequest, list[str], list[str]]:
        """Returns (request, errors, warnings)."""
        assert self.catalog and self.firmware_dir
        errors, warnings = [], []
        apps, faces, external = [], [], []
        for kind, target in (("app", apps), ("watchface", faces)):
            avail = self.entries(kind)
            for key in self.selected(kind):
                e = avail.get(key)
                if e is None:
                    warnings.append(f"Skipping {key.split('#')[-1].split('::')[-1]}: not available in this firmware/package set")
                    continue
                target.append(e.qualified)
                if e.external:
                    external.append(e.external)
        errs, warns = fw.check_compat(self.catalog, external)
        errors += errs
        warnings += warns
        if not faces:
            errors.append("Select at least one watch face.")
        if not apps:
            errors.append("Select at least one app.")
        req = BuildRequest(apps, faces, external, self.firmware_dir, self.settings.firmware_url, self.firmware_ref)
        return req, errors, warnings


def github_repo_of(url: str) -> str | None:
    import re

    m = re.match(r"^(?:https?://github\.com/|git@github\.com:)([^/]+/[^/]+?)(?:\.git)?/?$", url.strip())
    return m.group(1) if m else None


def resolve_ref(settings: Settings) -> str:
    """The firmware ref to build: the configured one, else the latest release, else the default branch."""
    if settings.firmware_ref:
        return settings.firmware_ref
    from .sources import latest_release

    repo = github_repo_of(settings.firmware_url)
    return (latest_release(repo) if repo else None) or ""
