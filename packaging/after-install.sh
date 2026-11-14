#!/bin/sh
# Byte-compile the app for the system's Python, so it starts faster and never needs to write
# __pycache__ files itself (which it can't as a normal user, and which as root would be left
# behind on removal). Runs as the deb postinst and the rpm %post.
set -e
if command -v python3 >/dev/null 2>&1; then
  python3 -m compileall -q -f /usr/share/infinitime-builder/itbuilder >/dev/null 2>&1 || true
fi
exit 0
