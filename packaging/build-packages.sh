#!/bin/bash
# Build .deb and .rpm packages of InfiniTime Builder: the .deb with dpkg-deb, the .rpm with fpm
# (https://fpm.readthedocs.io).
# Run from the repository root; packages are written to dist/. Used by .github/workflows/packages.yml.
#
# Usage: packaging/build-packages.sh [deb] [rpm]   (no arguments: both)
# Environment: ITERATION  package release number (default 1). CI uses 1 for tagged releases and
#                         0.<run number> otherwise, so development builds sort below the release.
#
# The app is pure Python, so the packages are architecture-independent. Qt comes from the
# distribution, through PySide6 or PyQt6 (itbuilder/qt.py works with either): Ubuntu 24.04 only
# ships PyQt6; Debian 13+, Ubuntu 25.04+ and Fedora ship both.
set -euo pipefail

FORMATS=("$@")
[ ${#FORMATS[@]} -gt 0 ] || FORMATS=(deb rpm)
for f in "${FORMATS[@]}"; do
  case $f in deb|rpm) ;; *) echo "unknown package format: $f (expected deb or rpm)" >&2; exit 2 ;; esac
done

NAME=infinitime-builder
APP_ID=io.github.ScanuNicco.InfiniTimeBuilder
MAINTAINER="InfiniTime Builder <scanuproductions@gmail.com>"
VERSION=$(python3 -c "import ast; t = ast.parse(open('itbuilder/__init__.py').read()); \
print(next(n.value.value for n in t.body if isinstance(n, ast.Assign) and n.targets[0].id == '__version__'))")
ITERATION=${ITERATION:-1}
URL=https://github.com/ScanuNicco/infinitime-builder
DESCRIPTION="Choose apps and watch faces for your PineTime and build InfiniTime firmware
Pick, order and add third-party apps and watch faces, then build a firmware
update with the official InfiniTime build container (Docker or Podman)."

# Reproducible packages: file times and compressed files are stamped with the last commit's time.
if [ -z "${SOURCE_DATE_EPOCH:-}" ]; then
  SOURCE_DATE_EPOCH=$(git log -1 --format=%ct 2>/dev/null || date +%s)
fi
export SOURCE_DATE_EPOCH

CHANGELOG_VERSION=$(python3 packaging/changelog.py version)
if [ "$CHANGELOG_VERSION" != "$VERSION" ]; then
  echo "CHANGELOG.md's newest release is $CHANGELOG_VERSION but itbuilder/__init__.py says $VERSION" >&2
  exit 1
fi

STAGE=$(mktemp -d)
META=$(mktemp -d)
trap 'rm -rf "$STAGE" "$META"' EXIT
APPDIR=$STAGE/usr/share/$NAME
DOCDIR=$STAGE/usr/share/doc/$NAME

install -d "$APPDIR" "$STAGE/usr/bin" "$STAGE/usr/share/applications" "$STAGE/usr/share/metainfo" \
  "$STAGE/usr/share/icons/hicolor/scalable/apps" "$STAGE/usr/share/man/man1" "$DOCDIR"
cp -r itbuilder "$APPDIR/"
find "$APPDIR" -name __pycache__ -prune -exec rm -rf {} +
find "$APPDIR" -type d -exec chmod 755 {} +
find "$APPDIR" -type f -exec chmod 644 {} +

for pair in "$NAME:itbuilder.gui" "$NAME-cli:itbuilder.cli"; do
  cat > "$STAGE/usr/bin/${pair%%:*}" <<LAUNCHER
#!/bin/sh
export PYTHONPATH="/usr/share/$NAME\${PYTHONPATH:+:\$PYTHONPATH}"
exec python3 -m ${pair#*:} "\$@"
LAUNCHER
  chmod 755 "$STAGE/usr/bin/${pair%%:*}"
done

install -m 644 "packaging/$APP_ID.desktop" "$STAGE/usr/share/applications/"
python3 packaging/changelog.py metainfo "packaging/$APP_ID.metainfo.xml.in" > "$STAGE/usr/share/metainfo/$APP_ID.metainfo.xml"
chmod 644 "$STAGE/usr/share/metainfo/$APP_ID.metainfo.xml"
install -m 644 itbuilder/icon.svg "$STAGE/usr/share/icons/hicolor/scalable/apps/$APP_ID.svg"
install -m 644 README.md MANIFEST.md docs/app-repositories.md "$DOCDIR/"
for page in packaging/man/*.1; do
  gzip -9 -n -c "$page" > "$STAGE/usr/share/man/man1/$(basename "$page").gz"
  chmod 644 "$STAGE/usr/share/man/man1/$(basename "$page").gz"
done

python3 packaging/changelog.py rpm "$MAINTAINER" "$ITERATION" > "$META/rpm-changelog"

mkdir -p dist

# Everything needed to build firmware right after installing, with no setup:
# - Podman, because it works rootless out of the box (Docker needs its daemon running and the user
#   in the docker group). The app still uses Docker instead if Docker is installed and usable.
#   Ubuntu 24.04's podman package doesn't pull in uidmap (newuidmap), which rootless Podman needs.
# - git, to download InfiniTime, and ca-certificates for HTTPS (git only recommends it on Debian).
# - Qt's SVG plugin, for the window icon.
# The rpm must list the app's own directories (--directories) or rpm doesn't own them and leaves
# them behind on uninstall; the deb records every directory itself.
# Both run after-install.sh / before-remove.sh to manage the app's byte-code.
for f in "${FORMATS[@]}"; do
  case $f in
    deb)
      # Built with dpkg-deb rather than fpm: fpm adds non-Debian control fields (License, Vendor)
      # and timestamped gzip files. Debian wants the licence as a machine-readable copyright file
      # (the GPL text itself is in /usr/share/common-licenses), the Debian changelog as
      # changelog.Debian.gz and the upstream CHANGELOG.md as changelog.gz.
      DEB=$META/deb
      rm -rf "$DEB"
      cp -a "$STAGE" "$DEB"
      rm -f "$DEB/usr/share/doc/$NAME/LICENSE" "$DEB/usr/share/doc/$NAME/CHANGELOG.md"  # if the rpm was built first
      install -m 644 packaging/copyright "$DEB/usr/share/doc/$NAME/copyright"
      python3 packaging/changelog.py debian "$NAME" "$MAINTAINER" "$ITERATION" \
        | gzip -9 -n > "$DEB/usr/share/doc/$NAME/changelog.Debian.gz"
      gzip -9 -n < CHANGELOG.md > "$DEB/usr/share/doc/$NAME/changelog.gz"
      chmod 644 "$DEB/usr/share/doc/$NAME/"*.gz
      # Released outside the Debian archive, so archive-only checks are overridden (see the file).
      install -D -m 644 packaging/lintian-overrides "$DEB/usr/share/lintian/overrides/$NAME"
      install -d -m 755 "$DEB/DEBIAN"
      cat > "$DEB/DEBIAN/control" <<CONTROL
Package: $NAME
Version: $VERSION-$ITERATION
Architecture: all
Maintainer: $MAINTAINER
Installed-Size: $(du -sk --apparent-size "$DEB/usr" | cut -f1)
Depends: python3 (>= 3.10), python3-pyside6.qtwidgets | python3-pyqt6, libqt6svg6, podman, uidmap, git, ca-certificates
Suggests: docker.io
Section: utils
Priority: optional
Homepage: $URL
Description: $(printf '%s\n' "$DESCRIPTION" | head -1)
$(printf '%s\n' "$DESCRIPTION" | tail -n +2 | sed 's/^/ /')
CONTROL
      install -m 755 packaging/after-install.sh "$DEB/DEBIAN/postinst"
      install -m 755 packaging/before-remove.sh "$DEB/DEBIAN/prerm"
      (cd "$DEB" && find usr -type f -print0 | LC_ALL=C sort -z | xargs -0 md5sum) > "$DEB/DEBIAN/md5sums"
      chmod 644 "$DEB/DEBIAN/control" "$DEB/DEBIAN/md5sums"
      dpkg-deb --root-owner-group -Zxz --build "$DEB" "dist/${NAME}_${VERSION}-${ITERATION}_all.deb" ;;
    rpm)
      install -m 644 LICENSE CHANGELOG.md "$DOCDIR/"
      fpm -s dir -C "$STAGE" -t rpm -p dist/ -n "$NAME" -v "$VERSION" --iteration "$ITERATION" -a all \
        --license GPL-3.0-or-later --url "$URL" --description "$DESCRIPTION" \
        --maintainer "$MAINTAINER" --vendor "InfiniTime Builder" --force \
        --after-install packaging/after-install.sh --before-remove packaging/before-remove.sh \
        --category Applications/System --rpm-changelog "$META/rpm-changelog" \
        -d "python3 >= 3.10" -d "(python3-pyside6 or python3-pyqt6)" -d qt6-qtsvg \
        -d podman -d git-core -d ca-certificates \
        --directories "/usr/share/$NAME" --directories "/usr/share/doc/$NAME" \
        usr ;;
  esac
done

ls -l dist/
