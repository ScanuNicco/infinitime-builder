#!/usr/bin/env python3
"""Turn CHANGELOG.md into the Debian changelog, the RPM %changelog and AppStream <releases>.

Usage (from the repository root, as packaging/build-packages.sh does):
    changelog.py debian  NAME MAINTAINER ITERATION   > changelog
    changelog.py rpm     MAINTAINER ITERATION        > rpm-changelog
    changelog.py metainfo TEMPLATE                   > metainfo.xml   (fills in @RELEASES@)
    changelog.py version                             # newest version, for checks

ITERATION is the package release number of the newest version; older versions are given 1.
"""
from __future__ import annotations

import datetime as dt
import re
import sys
from dataclasses import dataclass
from email.utils import format_datetime
from pathlib import Path
from xml.sax.saxutils import escape

HEADING_RE = re.compile(r"^##\s+\[?(?P<version>\d+\.\d+\.\d+[^\]\s]*)\]?\s+-\s+(?P<date>\d{4}-\d{2}-\d{2})\s*$")


@dataclass
class Release:
    version: str
    date: dt.date
    changes: list[str]


def parse(text: str) -> list[Release]:
    """Releases in file order (newest first). Bullets may wrap onto indented lines."""
    releases: list[Release] = []
    cur: Release | None = None
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.startswith("## "):
            m = HEADING_RE.match(line)
            if not m:
                raise ValueError(f"CHANGELOG.md: bad release heading {line!r} (expected '## [1.2.3] - YYYY-MM-DD')")
            cur = Release(m["version"], dt.date.fromisoformat(m["date"]), [])
            releases.append(cur)
        elif cur is None or line.startswith("#") or not line.strip():
            continue
        elif line.startswith("- "):
            cur.changes.append(line[2:].strip())
        elif line.startswith("  ") and cur.changes:
            cur.changes[-1] += " " + line.strip()
        else:
            raise ValueError(f"CHANGELOG.md: unexpected line in {cur.version}: {line!r}")
    for r in releases:
        if not r.changes:
            raise ValueError(f"CHANGELOG.md: release {r.version} lists no changes")
    if not releases:
        raise ValueError("CHANGELOG.md: no releases found")
    return releases


def _wrap(text: str, width: int, first: str, rest: str) -> list[str]:
    out, line = [], first
    for word in text.split():
        if len(line) + len(word) + 1 > width and line.strip() not in ("", first.strip()):
            out.append(line.rstrip())
            line = rest
        line += word + " "
    out.append(line.rstrip())
    return out


def debian(releases: list[Release], name: str, maintainer: str, iteration: str) -> str:
    blocks = []
    for i, r in enumerate(releases):
        rev = iteration if i == 0 else "1"
        when = format_datetime(dt.datetime.combine(r.date, dt.time(12, 0), tzinfo=dt.timezone.utc))
        lines = [f"{name} ({r.version}-{rev}) unstable; urgency=medium", ""]
        for c in r.changes:
            lines += _wrap(c, 79, "  * ", "    ")
        lines += ["", f" -- {maintainer}  {when}"]
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + "\n"


def rpm(releases: list[Release], maintainer: str, iteration: str) -> str:
    blocks = []
    for i, r in enumerate(releases):
        rev = iteration if i == 0 else "1"
        lines = [f"* {r.date.strftime('%a %b %d %Y')} {maintainer} - {r.version}-{rev}"]
        for c in r.changes:
            lines += _wrap(c.replace("%", "%%"), 79, "- ", "  ")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + "\n"


def appstream(releases: list[Release]) -> str:
    out = ["<releases>"]
    for r in releases:
        out.append(f'    <release version="{escape(r.version)}" date="{r.date.isoformat()}">')
        out.append("      <description>")
        out.append("        <ul>")
        out += [f"          <li>{escape(c)}</li>" for c in r.changes]
        out.append("        </ul>")
        out.append("      </description>")
        out.append("    </release>")
    out.append("  </releases>")
    return "\n".join(out)


def main(argv: list[str]) -> int:
    releases = parse(Path("CHANGELOG.md").read_text(encoding="utf-8"))
    cmd, args = (argv[0], argv[1:]) if argv else ("", [])
    if cmd == "debian" and len(args) == 3:
        sys.stdout.write(debian(releases, *args))
    elif cmd == "rpm" and len(args) == 2:
        sys.stdout.write(rpm(releases, *args))
    elif cmd == "metainfo" and len(args) == 1:
        template = Path(args[0]).read_text(encoding="utf-8")
        if "@RELEASES@" not in template:
            raise SystemExit(f"{args[0]}: no @RELEASES@ placeholder")
        sys.stdout.write(template.replace("@RELEASES@", appstream(releases)))
    elif cmd == "version" and not args:
        print(releases[0].version)
    else:
        print(__doc__, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
