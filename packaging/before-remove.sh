#!/bin/sh
# Remove the byte-code written by after-install.sh, which the package manager doesn't track.
# Runs as the deb prerm ("remove", "upgrade", ...) and the rpm %preun ($1 = number of copies left
# installed: 0 on removal, 1 on upgrade, when the new version's %post has already compiled).
set -e
case "$1" in
  1) exit 0 ;;  # rpm upgrade: keep the new version's byte-code
esac
find /usr/share/infinitime-builder -depth -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
exit 0
