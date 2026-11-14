# Changelog

All notable changes to InfiniTime Builder. The newest release comes first; its version must match
`itbuilder/__version__`. Packaging turns this file into the Debian changelog, the RPM `%changelog`
and the AppStream release list, so keep to the format: `## [version] - YYYY-MM-DD` headings
followed by `- ` bullet points (`###` subheadings are allowed and ignored).

## [0.1.0] - 2026-11-14

- First release: choose, order and add third-party apps and watch faces, and build InfiniTime
  firmware in the official build container (Podman or Docker).
- Flash and resource-pack size estimates before building, and exact checks after.
- Third-party packages from GitHub repositories or local folders, described by
  `infinitime-app.json` (see docs/app-repositories.md).
- Command line interface for headless builds and manifest validation.
- .deb and .rpm packages for Debian 13+, Ubuntu 24.04+ and Fedora.
