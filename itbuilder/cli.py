"""Command line interface: headless builds and manifest validation for package authors."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import builder as bld
from . import firmware as fw
from . import sizes
from .manifest import ManifestError, load_package
from .project import Project, Settings, data_dir, load_settings, resolve_ref
from .sources import FetchError, SourceSpec, SourceStore


TOLERANCE = 0.015  # estimates are slightly pessimistic


def _project(args) -> tuple[Project, bld.Engine | None]:
    settings = load_settings()
    if getattr(args, "profile", None):
        settings = Settings.from_json(json.loads(Path(args.profile).read_text()), base=settings)
    if getattr(args, "ref", None):
        settings.firmware_ref = args.ref
    for src in getattr(args, "add", None) or []:
        spec = SourceSpec.parse(src)
        if spec not in settings.sources:
            settings.sources.append(spec)
    project = Project(settings, SourceStore(data_dir() / "cache"))
    engine = None
    try:
        engine = bld.find_engine(settings.engine)
    except bld.BuildError:
        if getattr(args, "needs_engine", False):
            raise
    ref = resolve_ref(settings)
    fw_dir = bld.ensure_firmware(data_dir() / "cache", settings.firmware_url, ref, engine, print)
    project.set_firmware(fw_dir, ref)
    project.load_all_sources()
    return project, engine


def cmd_list(args) -> int:
    project, _ = _project(args)
    print(f"InfiniTime {project.catalog.version}  ({project.firmware_dir})")
    for kind, title in (("app", "Apps"), ("watchface", "Watch faces")):
        entries = project.entries(kind)
        sel = project.selected(kind)
        print(f"\n{title} (selected, in order):")
        for i, k in enumerate(sel, 1):
            e = entries.get(k)
            print(f"  {i:2}. {e.name if e else k}  [{e.origin if e else 'missing'}]")
        rest = [e for k, e in entries.items() if k not in sel]
        if rest:
            print("  available: " + ", ".join(e.name for e in rest))
    return 0


def cmd_build(args) -> int:
    args.needs_engine = True
    project, engine = _project(args)
    req, errors, warnings = project.build_request()
    for w in warnings:
        print("warning:", w, file=sys.stderr)
    if errors:
        for e in errors:
            print("error:", e, file=sys.stderr)
        return 1
    prof = sizes.SizeProfile.load(project.size_profile_path())
    if prof:
        enabled = set(project.selected("app")) | set(project.selected("watchface"))
        est, unknown = prof.estimate(enabled)
        print(f"Estimated flash: {est / 1024:.0f} KB of {prof.flash_limit / 1024:.0f} KB"
              + (f" (+{len(unknown)} items not measured)" if unknown else ""))
        if est > prof.flash_limit * (1 + TOLERANCE) and not args.force:
            print(f"error: this selection is about {(est - prof.flash_limit) / 1024:.0f} KB too big for the "
                  "PineTime. Remove some apps, or pass --force to try anyway.", file=sys.stderr)
            return 1
    out = Path(args.output or project.settings.output_dir).expanduser()
    quiet = lambda line: None  # noqa: E731
    try:
        res = bld.build(req, engine, data_dir() / "work", out, print, output=print if args.verbose else quiet)
    except bld.BuildError as e:
        print("\nBUILD FAILED:", e, file=sys.stderr)
        return 1
    print(f"\nBuilt in {res.seconds:.0f}s -> {res.dfu}")
    if res.resources is not None:
        print(f"Resource pack: {res.resource_blocks * 4096 / 1024:.0f} KB once stored on the watch "
              f"(room for {res.resource_capacity * 4096 / 1024:.0f} KB)")
        if not res.resources_fit:
            print("warning: " + bld.resource_warning(res.resource_blocks, res.resource_capacity, res.resource_bytes),
                  file=sys.stderr)
    users = project.resource_users()
    if users and res.resources:
        print(f"{', '.join(users)} need{'s' if len(users) == 1 else ''} the resource pack: "
              f"also send {res.resources} to the watch.")
    if res.flash_used and res.flash_size:
        print(f"Flash: {res.flash_used/1024:.0f} KB of {res.flash_size/1024:.0f} KB ({100*res.flash_used/res.flash_size:.0f}%)")
    return 0


def cmd_sizes(args) -> int:
    args.needs_engine = True
    project, engine = _project(args)
    prof = None if args.remeasure else sizes.SizeProfile.load(project.size_profile_path())
    if prof is None:
        prof = bld.calibrate(project.firmware_dir, project.catalog, project.external_items(), engine,
                             data_dir() / "work", print, firmware_url=project.settings.firmware_url,
                             firmware_ref=project.firmware_ref)
        prof.save(project.size_profile_path())
    enabled = set(project.selected("app")) | set(project.selected("watchface"))
    est, _ = prof.estimate(enabled)
    print(f"\nSelected: about {est / 1024:.1f} KB of {prof.flash_limit / 1024:.0f} KB flash")
    _, blocks, capacity = prof.estimate_resources(enabled)
    print(f"Resource pack: about {blocks * prof.fs_block_size / 1024:.0f} KB of the "
          f"{capacity * prof.fs_block_size / 1024:.0f} KB the watch has room for"
          + ("  <-- TOO BIG" if blocks > capacity else "") + "\n")
    rows = []
    for kind in ("app", "watchface"):
        for key, e in project.entries(kind).items():
            rows.append((prof.cost(key, enabled), key in enabled, e))
    for cost, on, e in sorted(rows, key=lambda r: -(r[0] or 0)):
        print(f"  {'on ' if on else 'off'}  {('~%.1f KB' % (cost / 1024)) if cost is not None else '   ?   ':>9}  "
              f"{e.name} ({'face' if e.kind == 'watchface' else 'app'}, {e.origin})")
    return 0


def cmd_validate(args) -> int:
    root = Path(args.path).resolve()
    try:
        pkg = load_package(root, f"local:{root}")
    except ManifestError as e:
        print("invalid:", e, file=sys.stderr)
        return 1
    print(f"OK: {pkg.name} by {pkg.author or 'unknown'}")
    for it in pkg.items:
        print(f"  {it.kind:9} {it.qualified:28} header={it.header} sources={', '.join(it.sources) or '-'}"
              + (f" icon={it.icon}" if it.icon else ""))
        hdr = (root / it.header).read_text(errors="replace")
        trait = "AppTraits<Apps::" if it.kind == "app" else "WatchFaceTraits<WatchFace::"
        if f"{trait}{it.enum}>" not in hdr:
            print(f"  warning: {it.header} does not seem to define {trait}{it.enum}>", file=sys.stderr)
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="infinitime-builder-cli", description="Build customised InfiniTime firmware.")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name, fn, hlp in (("list", cmd_list, "show available and selected apps"),
                          ("build", cmd_build, "build a DFU file")):
        sp = sub.add_parser(name, help=hlp)
        sp.add_argument("--profile", help="profile JSON exported from the GUI (default: saved GUI settings)")
        sp.add_argument("--ref", help="InfiniTime branch or tag to build")
        sp.add_argument("--add", action="append", metavar="SOURCE", help="extra package source (owner/repo or folder)")
        sp.set_defaults(fn=fn)
        if name == "build":
            sp.add_argument("--force", action="store_true", help="build even if the estimate says it will not fit")
            sp.add_argument("-o", "--output", help="output directory")
            sp.add_argument("-v", "--verbose", action="store_true", help="show the full compiler log")
    sp = sub.add_parser("sizes", help="estimate flash use of each app / watch face")
    sp.add_argument("--profile")
    sp.add_argument("--ref")
    sp.add_argument("--add", action="append", metavar="SOURCE")
    sp.add_argument("--remeasure", action="store_true", help="ignore the cached measurement")
    sp.set_defaults(fn=cmd_sizes)
    sp = sub.add_parser("validate", help="check a package's infinitime-app.json (for app authors)")
    sp.add_argument("path", nargs="?", default=".")
    sp.set_defaults(fn=cmd_validate)
    args = p.parse_args(argv)
    try:
        return args.fn(args)
    except (bld.BuildError, FetchError, ManifestError, fw.FirmwareError) as e:
        print("error:", e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
