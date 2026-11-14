#!/bin/bash
# Build .deb and .rpm packages of InfiniTime Builder with fpm (https://fpm.readthedocs.io).
# Run from the repository root; packages are written to dist/. Used by .github/workflows/packages.yml.
#
# The app is pure Python, so the packages are architecture-independent. Qt comes from the
# distribution, through PySide6 or PyQt6 (itbuilder/qt.py works with either): Ubuntu 24.04 only
# ships PyQt6; Debian 13+, Ubuntu 25.04+ and Fedora ship both.
set -euo pipefail

NAME=infinitime-builder
APP_ID=io.github.ScanuNicco.InfiniTimeBuilder
VERSION=$(python3 -c "import tomllib; print(tomllib.load(open('pyproject.toml', 'rb'))['project']['version'])")
ITERATION=${ITERATION:-1}   # package release number; CI passes the run number
URL=https://github.com/ScanuNicco/infinitime-builder
DESCRIPTION="Choose apps and watch faces for your PineTime and build InfiniTime firmware.
Pick, order and add third-party apps and watch faces, then build a firmware
update with the official InfiniTime build container (Docker or Podman)."

STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT
APPDIR=$STAGE/usr/share/$NAME

install -d "$APPDIR" "$STAGE/usr/bin" "$STAGE/usr/share/applications" \
  "$STAGE/usr/share/icons/hicolor/scalable/apps" "$STAGE/usr/share/doc/$NAME"
cp -r itbuilder "$APPDIR/"
find "$APPDIR" -name __pycache__ -prune -exec rm -rf {} +

for pair in "$NAME:itbuilder.gui" "$NAME-cli:itbuilder.cli"; do
  cat > "$STAGE/usr/bin/${pair%%:*}" <<LAUNCHER
#!/bin/sh
export PYTHONPATH="/usr/share/$NAME\${PYTHONPATH:+:\$PYTHONPATH}"
exec python3 -m ${pair#*:} "\$@"
LAUNCHER
  chmod 755 "$STAGE/usr/bin/${pair%%:*}"
done

install -m 644 "packaging/$APP_ID.desktop" "$STAGE/usr/share/applications/"
install -m 644 itbuilder/icon.svg "$STAGE/usr/share/icons/hicolor/scalable/apps/$APP_ID.svg"
install -m 644 README.md MANIFEST.md LICENSE "$STAGE/usr/share/doc/$NAME/"

mkdir -p dist
COMMON=(-s dir -C "$STAGE" -n "$NAME" -v "$VERSION" --iteration "$ITERATION" -a all
        --license GPL-3.0-or-later --url "$URL" --description "$DESCRIPTION"
        --maintainer "InfiniTime Builder" --vendor "InfiniTime Builder" --force)

fpm "${COMMON[@]}" -t deb -p dist/ \
  -d "python3 (>= 3.10)" -d "python3-pyside6.qtwidgets | python3-pyqt6" \
  --deb-recommends "docker.io | podman" --deb-recommends git \
  usr

fpm "${COMMON[@]}" -t rpm -p dist/ \
  -d "python3 >= 3.10" -d "(python3-pyside6 or python3-pyqt6)" \
  --rpm-tag "Recommends: podman" --rpm-tag "Recommends: git" \
  usr

ls -l dist/
