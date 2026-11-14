"""Fetching InfiniTime itself and third-party packages."""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tarfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .manifest import MANIFEST_NAME, Package, load_package

USER_AGENT = "infinitime-builder"
GITHUB_RE = re.compile(
    r"^(?:https?://github\.com/|git@github\.com:|github:)?"
    r"(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+?)(?:\.git)?"
    r"(?:/tree/(?P<ref>[^\s]+?))?/?$"
)
DISCOVERY_TOPIC = "infinitime-app"


class FetchError(RuntimeError):
    pass


@dataclass(frozen=True)
class SourceSpec:
    """A user-added package source: a GitHub repo (optionally pinned) or a local folder."""

    kind: str  # "github" | "local"
    location: str  # "owner/repo" or absolute path
    ref: str = ""  # branch / tag / commit; empty = default branch

    @property
    def id(self) -> str:
        if self.kind == "local":
            return f"local:{self.location}"
        return f"github:{self.location}" + (f"@{self.ref}" if self.ref else "")

    def to_json(self) -> dict:
        return {"kind": self.kind, "location": self.location, "ref": self.ref}

    @staticmethod
    def from_json(d: dict) -> "SourceSpec":
        return SourceSpec(d["kind"], d["location"], d.get("ref", ""))

    @staticmethod
    def parse(text: str, ref: str = "") -> "SourceSpec":
        text = text.strip()
        p = Path(text).expanduser()
        if text.startswith(("/", "~", ".")) or (len(text) > 2 and text[1] == ":") or p.is_dir():
            if not p.is_dir():
                raise FetchError(f"folder not found: {text}")
            return SourceSpec("local", str(p.resolve()))
        m = GITHUB_RE.match(text)
        if not m:
            raise FetchError("enter a GitHub repository (owner/repo or its URL) or a local folder")
        return SourceSpec("github", f"{m['owner']}/{m['repo']}", ref.strip() or (m["ref"] or ""))


def _http(url: str, timeout: float = 60) -> bytes:
    headers = {"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"}
    # Optional token for private repositories and a higher rate limit; only ever sent to GitHub's API.
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token and urllib.parse.urlsplit(url).hostname == "api.github.com":
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            hint = "" if os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") else \
                " (if the repository is private, set GITHUB_TOKEN)"
            raise FetchError(f"not found: {url}{hint}") from e
        if e.code == 403:
            raise FetchError("GitHub rate limit reached, try again in a while") from e
        raise FetchError(f"HTTP {e.code} fetching {url}") from e
    except urllib.error.URLError as e:
        raise FetchError(f"network error fetching {url}: {e.reason}") from e


def _safe_extract(data: bytes, dest: Path) -> Path:
    """Extract a GitHub tarball into dest, returning the single top-level directory."""
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
        for m in tf.getmembers():
            if not (m.isfile() or m.isdir()):
                continue  # skip links/devices entirely
            name = Path(m.name)
            if name.is_absolute() or ".." in name.parts:
                raise FetchError(f"unsafe path in archive: {m.name}")
        members = [m for m in tf.getmembers() if m.isfile() or m.isdir()]
        if hasattr(tarfile, "data_filter"):
            tf.extractall(dest, members=members, filter="data")
        else:  # Python without extraction filters; paths were validated above
            tf.extractall(dest, members=members)
    tops = [p for p in dest.iterdir()]
    if len(tops) != 1 or not tops[0].is_dir():
        raise FetchError("unexpected archive layout")
    return tops[0]


class SourceStore:
    """Caches downloaded packages under `cache_dir`."""

    def __init__(self, cache_dir: Path):
        self.cache_dir = cache_dir
        self.pkg_dir = cache_dir / "packages"

    def _slot(self, spec: SourceSpec) -> Path:
        h = hashlib.sha1(spec.id.encode()).hexdigest()[:12]
        return self.pkg_dir / f"{spec.location.replace('/', '_')}-{h}"

    def fetch(self, spec: SourceSpec, refresh: bool = False, check_updates: bool = True) -> Package:
        """Load a package, downloading it if it isn't cached or (for GitHub sources) has new commits.

        With check_updates, one small request asks GitHub for the source's latest commit; the package
        is downloaded again only if that changed. Without a connection, the cached copy is used.
        """
        if spec.kind == "local":
            return load_package(Path(spec.location), spec.id)
        slot = self._slot(spec)
        root = slot / "src"
        cached = (root / MANIFEST_NAME).is_file()
        stored = (slot / "commit").read_text().strip() if (slot / "commit").is_file() else ""
        latest = ""
        if refresh or not cached or check_updates:
            try:
                latest = latest_commit(spec)
            except FetchError:
                if not cached:
                    raise
        if refresh or not cached or (latest and latest != stored):
            ref = latest or spec.ref or ""
            url = f"https://api.github.com/repos/{spec.location}/tarball/{ref}".rstrip("/")
            top = _safe_extract(_http(url, timeout=120), slot / "extract")
            if root.exists():
                shutil.rmtree(root)
            top.rename(root)
            shutil.rmtree(slot / "extract", ignore_errors=True)
            (slot / "commit").write_text(latest)
        return load_package(root, spec.id)

    def remove(self, spec: SourceSpec) -> None:
        if spec.kind == "github":
            shutil.rmtree(self._slot(spec), ignore_errors=True)


def latest_commit(spec: SourceSpec) -> str:
    """The commit a GitHub source's branch or tag (default branch if none) currently points to."""
    data = json.loads(_http(f"https://api.github.com/repos/{spec.location}/commits/{spec.ref or 'HEAD'}", timeout=20))
    sha = data.get("sha") if isinstance(data, dict) else None
    if not isinstance(sha, str) or not sha:
        raise FetchError(f"could not read the latest commit of {spec.location}")
    return sha


def discover(query: str = "") -> list[dict]:
    """Search GitHub for repositories tagged with the infinitime-app topic."""
    q = f"topic:{DISCOVERY_TOPIC}" + (f" {query}" if query else "")
    url = "https://api.github.com/search/repositories?per_page=50&sort=stars&q=" + urllib.parse.quote(q)
    data = json.loads(_http(url))
    return [
        {
            "full_name": r["full_name"],
            "description": r.get("description") or "",
            "stars": r.get("stargazers_count", 0),
            "url": r["html_url"],
        }
        for r in data.get("items", [])
    ]


def latest_release(repo: str) -> str | None:
    try:
        return json.loads(_http(f"https://api.github.com/repos/{repo}/releases/latest", timeout=20)).get("tag_name")
    except (FetchError, ValueError):
        return None


# --------------------------------------------------------------------------- InfiniTime source


def firmware_slot(cache_dir: Path, url: str, ref: str) -> Path:
    h = hashlib.sha1(f"{url}@{ref}".encode()).hexdigest()[:12]
    return cache_dir / "firmware" / h


def clone_firmware(url: str, ref: str, dest: Path, git_runner, log) -> None:
    """Shallow-clone InfiniTime (with submodules) into dest.

    `git_runner(args, cwd)` runs git either on the host or inside the build container.
    """
    tmp = dest.with_name(dest.name + ".partial")
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.parent.mkdir(parents=True, exist_ok=True)
    args = ["clone", "--depth", "1", "--recurse-submodules", "--shallow-submodules"]
    if ref:
        args += ["--branch", ref]
    log(f"Downloading InfiniTime source ({url} {ref or 'default branch'})...")
    git_runner(args + [url, tmp.name], tmp.parent)
    if dest.exists():
        shutil.rmtree(dest)
    tmp.rename(dest)


def host_git(args: list[str], cwd: Path) -> None:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise FetchError(f"git {' '.join(args[:1])} failed:\n{proc.stderr.strip()}")

