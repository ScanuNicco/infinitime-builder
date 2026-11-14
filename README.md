# InfiniTime Builder

Build your own [InfiniTime](https://github.com/InfiniTimeOrg/InfiniTime) firmware for the PineTime
without forking or touching code:

![InfiniTime Builder: choosing apps and watch faces, then building](docs/demo.gif)

- **Switch apps and watch faces on or off** and drag them into the order you want in the
  launcher, with a live preview of the watch screen. A fresh install starts with the bare minimum:
  the Digital and Analog faces, plus Timer, Stopwatch and Heart Rate.
- **See whether it will fit before building.** A flash meter shows how much of the PineTime's
  464 KB your selection needs, and each item shows its own cost. Selections that clearly won't fit
  can't be built, and every build is checked exactly against the watch's limits.
- **Add third-party apps and watch faces** straight from GitHub repositories that contain an
  [`infinitime-app.json`](MANIFEST.md) manifest, or from a local folder
  ([full repository reference](docs/app-repositories.md)).
- Press **Build firmware** to get a `pinetime-mcuboot-app-dfu-*.zip` file you can install with
  Gadgetbridge, InfiniLink, Siglo, ITD or Amazfish.

Building uses the official `infinitime/infinitime-build` Docker image, so nothing else needs to be
installed. The builder picks the image tag that matches the InfiniTime version you build (see below).

**WARNING:** When installing multiple third-party apps, there is no guarantee that your specific combination has been tested by anyone, nor that a given app has been tested at all. InfiniTime provides multiple built-in recovery features, but if you have a sealed watch there is still some risk. **I am not responsible for bricked watches.**

## Requirements

- Linux: Debian 13+, Ubuntu 24.04+ or Fedora with the packages, or any system with Python 3.10+
  from source
- About 3 GB of disk for the build image
- From source you also need Docker or Podman, and git

## Install & run

**Packages:** download the `.deb` or `.rpm` from the
[releases](https://github.com/ScanuNicco/infinitime-builder/releases) (or from the latest
[workflow run](https://github.com/ScanuNicco/infinitime-builder/actions), where the `.deb` and the
`.rpm` are separate downloads), then

```sh
sudo apt install ./infinitime-builder_*.deb      # Debian 13+, Ubuntu 24.04+
sudo dnf install ./infinitime-builder-*.rpm      # Fedora
```

Then start **InfiniTime Builder** from your applications menu (or `infinitime-builder`; there is
also `infinitime-builder-cli` for headless builds and validating manifests).

**From source:** `pip install .` (or `pipx install .`), which brings PySide6 with it.

That's all: the packages bring everything with them, including **Podman** to run the official
InfiniTime build container (rootless, so there's no daemon to start and no group to join). If you
already use Docker and it works for your user, the builder uses Docker instead. On first use it
downloads the build image itself (about 1 GB, once), choosing the version that matches the firmware
you build. Firmware builds run without network access.

## Using it

1. **Apps**: flip the switch to put an app on the watch or take it off. Drag the items under
   *On your watch* to set the launcher order (6 apps per page; the watch preview on the right
   shows the pages, click it to flip).
2. **Watch faces**: use the switch to enable/disable just like apps. Click a face to see a screenshot of it on the
   watch preview; click the watch to step through the faces you've switched on. (Built-in faces use
   screenshots bundled with the builder; third-party faces use the `screenshot` from their
   manifest.) You pick the active face on the watch under Settings > Watch face.
3. **Sources**: add an external GitHub repo (`owner/repo`, a URL, optionally a branch or tag), a local
   folder, or search GitHub for repos tagged `infinitime-app`. Any watchfaces and apps they contain will appear under the appropriate tab for you to enable. The app comes with two small demo repos containing [two games](https://github.com/ScanuNicco/infinitime-sky-games) and [ten watchfaces](https://github.com/ScanuNicco/infinitime-concept-faces).

   **NOTE:** The bundled watchfaces and games are considered proof of concept apps primarily intended to demo InfiniTime Builder. They have all been tested on real hardware, but I don't consider them production quality at this point.
5. Watch the **flash meter** at the bottom, then press **Build firmware**. The first build downloads
   InfiniTime and the build image; later builds take about a minute.
6. Send the DFU zip to your watch, then press *Validate* under Settings > Firmware on the watch.

The firmware button at the top right chooses the InfiniTime version (default: the latest release) or a fork.
**File > Export profile** saves your selection and sources as JSON to share, or to use with
`infinitime-builder-cli build --profile my.json`. *File* can also reset to the minimal selection
or to InfiniTime's own default selection.

## Will it fit?

The PineTime has about 64 KB of RAM, 464 KB of internal flash for the firmware, and 3.3MB of external flash for fonts and images.

- **Before building:** after loading a firmware version, and whenever sources change, the builder
  does a one-off *calibration build* in the background. It compiles everything
  with the flash limit lifted and reads the linker map: how much flash each object file takes, and
  which files depend on which. For any selection it then replays the linker's garbage collection on
  that graph. Against real builds this was within 0.4 to 0.7% and always slightly high (e.g.
  318,397 B estimated vs 316,096 B actual). Within 1.5% of the limit you can still build;
  further over, the Build button is disabled.
- **After building:** flash and RAM use are read from the linker output and compared with the
  limits *before* anything is saved, so a DFU that doesn't fit is never produced.
- **CLI:** `infinitime-builder-cli sizes` lists every item's cost. `build` refuses a selection
  that is clearly too big unless `--force` is given.

**The resource pack** Every file takes whole 4 KB blocks, so the builder counts blocks rather than bytes; this model matches littlefs exactly
on real resource packs. The size is shown under the flash meter (and per item, "… in resource pack"),
in red when it won't fit, and checked again on the real zip after every build. In real-world use the speed of the exernal flash is more of a limiter than the size. It took ~15min to upload a ~300KB resource pack to my watch. Watchfaces that load a lot of resources from external storage open very slowly.

**RAM** is not something that the app can currently check. It depends
on what the app does on screen, not on what's compiled in.

## How it works

InfiniTime already chooses its apps at build time: `src/displayapp/apps/CMakeLists.txt` turns
`ENABLE_USERAPPS` / `ENABLE_WATCHFACES` into the `UserAppTypes` list, and that list's order is the
launcher order. For each build the builder:

1. copies a cached shallow clone of the chosen InfiniTime version;
2. for each selected third-party item, copies its package into `src/external/`, adds its enum value
   to `Apps.h.in`, includes its header from `UserApps.h`, appends its sources to `SOURCE_FILES`
   and adds any icon glyphs to the launcher font;
3. writes the chosen app and watch-face lists into the tree. The env vars aren't used because the
   `build.sh` in releases such as 1.16.1 doesn't pass them through to CMake;
4. runs the official build image. The image is matched to the firmware by comparing its
   `/opt/build.sh` with the tree's `docker/build.sh`. Releases need the toolchain they were made
   for: 1.16.1 needs GCC 10.3, and the current `latest` image (GCC 15) can't compile it. If no
   published image matches (for example a fork with its own Dockerfile), the image is built from
   the tree's `docker/` folder;
5. checks that the generated `Apps.h` contains exactly the requested lists, then copies the DFU
   and resource zips to `~/InfiniTime Builds/`.

Settings and caches are stored in `~/.local/share/infinitime-builder` (Linux),
`~/Library/Application Support/infinitime-builder` (macOS) or `%LOCALAPPDATA%\infinitime-builder` (Windows).

## Limits

- Third-party apps can only use what InfiniTime exposes to apps (`AppControllers`). Apps that need
  new system services or BLE services still need a fork, which you can select under *Firmware*.
- An app written for one InfiniTime version may not compile on another. The manifest's
  `minInfiniTime` triggers a warning, and compile errors in third-party code are shown as such.
- The watch stores the selected watch face as a number. Adding or removing third-party faces between
  builds can switch your face to a different one; just pick it again.
- Large resource packs can take a long time to upload, and some companion apps fail before finishing the upload. I found that ITD was the most reliable for uploading resource packs.

## Possible Future Enhancements
- Bundle ITD as a git submodule to support direct flashing from systems with bluetooth support.
- Bundle InfiniEmu to allow users to test a generated firmware before flashing to their watch.

## AI Use
AI was used under strong human oversight to accelerate development of this app. I believe that AI should not be used for a task the developer doesn't know how to do manually. I have written InfiniTime apps and mods by hand in the past and feel I have sufficient knowledge of it to have made this without the use of AI. The demo apps and watchfaces were written with less oversight, which is why I consider them not to be production quality at this point.

## Testing a build in an emulator

[InfiniEmu](https://github.com/pipe01/InfiniEmu) runs real firmware. Its headless `previewer`
(built with the `builder` stage of its Dockerfile) takes the ELF from the build tree
(`<data dir>/work/tree/build/src/pinetime-app-*.out`, the same code as the DFU but linked
without the bootloader) and a script such as:

```
run 3s
swipe up
run 500ms
screenshot
tap 40 70
run 500ms
screenshot
```

`previewer -screenshot out.png pinetime-app-1.16.1.out script.txt`

## Development

```sh
python -m venv --system-site-packages .venv && .venv/bin/pip install -e '.[test]'
.venv/bin/pytest
```

The GUI tests run offscreen when PySide6 or PyQt6 is installed and are skipped otherwise.

To build the packages locally you need `dpkg-deb` for the .deb and
[fpm](https://fpm.readthedocs.io) 1.18 plus `rpmbuild` for the .rpm:
`packaging/build-packages.sh [deb] [rpm]` writes them to `dist/`.

### Releasing

1. Set the new version in `itbuilder/__init__.py` (the only place it is defined).
2. Add a `## [x.y.z] - YYYY-MM-DD` section at the top of `CHANGELOG.md`. The packages' changelogs
   and the AppStream release list are generated from it, and CI fails if it doesn't match the
   version.
3. Commit, then tag and push: `git tag vx.y.z && git push origin vx.y.z`. CI builds, tests and
   publishes the .deb, the .rpm and `SHA256SUMS` as a GitHub release.
