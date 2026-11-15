# External app repositories: required structure

This is the reference for repositories that provide third-party apps and watch faces to InfiniTime
Builder. It describes exactly what the builder accepts (as implemented in `itbuilder/manifest.py`,
`itbuilder/sources.py` and `itbuilder/firmware.py`) and how the repository ends up in the firmware.
For a shorter walkthrough, see [MANIFEST.md](../MANIFEST.md).

Check a repository at any time with:

```sh
infinitime-builder-cli validate path/to/repo
```

## 1. How the builder uses a repository

1. **Adding it.** Users add a repository as `owner/repo`, `https://github.com/owner/repo`,
   `git@github.com:owner/repo.git`, or `https://github.com/owner/repo/tree/<ref>`, optionally with a
   branch, tag or commit. Without a ref, the default branch is used. A local folder can be added the
   same way for development. If a bare `owner/repo` is also a folder (containing a manifest) in
   the current directory, the builder asks the user to write `github:owner/repo` or
   `./owner/repo` instead.
2. **Discovery.** *Sources > Search GitHub* lists public repositories with the topic
   **`infinitime-app`**, sorted by stars (top 50). Without the topic, people can still add the
   repository by name, but they won't find it by searching.
3. **Downloading.** The builder asks GitHub for the commit the ref currently points to and
   downloads that commit's tarball. Every time the source is loaded, it checks for a new commit and
   downloads the tarball again if there is one. From the tarball:
   - only regular files and directories are kept. **Symlinks, hard links and device files are
     dropped**, so don't rely on them;
   - **Git submodules are not included** (GitHub tarballs don't contain them). Vendor what you need.
4. **Building.** For each item the user switches on, the builder:
   - copies the **whole repository** (except `.git/` and `build/` directories) to
     `src/external/<NN>_<owner>_<repo>/` in the InfiniTime tree;
   - adds your `enum` to `enum class Apps` or `enum class WatchFace` in `Apps.h.in`;
   - adds `#include "external/<NN>_<owner>_<repo>/<header>"` to `src/displayapp/UserApps.h`;
   - appends your `sources` to `SOURCE_FILES` in `src/CMakeLists.txt`;
   - adds your `icon` and `glyphs` to the launcher font (`jetbrains_mono_bold_20`);
   - adds your `resources` to `src/resources/fonts.json` / `images.json` (the resource pack).
5. **Compiling.** The firmware is compiled in the official InfiniTime build container **with no
   network access**, with InfiniTime's warning flags and `-Werror`.

Because the whole repository is copied into every build, keep it small. Put large files that the
firmware doesn't need (videos, design files, emulator binaries) somewhere else.

## 2. Repository layout

The only fixed location is the manifest, which **must be at the repository root**. Everything else
can go anywhere inside the repository, as long as the manifest points to it.

```
my-infinitime-apps/
├── infinitime-app.json          # required, at the root
├── README.md                    # recommended: what it is, screenshots, supported InfiniTime versions
├── LICENSE                      # recommended: InfiniTime is GPL-3.0, so use a compatible licence
├── src/
│   ├── MyApp.h                  # "header": declares AppTraits<Apps::MyApp>
│   ├── MyApp.cpp                # "sources"
│   ├── WatchFaceMyFace.h        # "header": declares WatchFaceTraits<WatchFace::MyFace>
│   └── WatchFaceMyFace.cpp
├── fonts/MyFont.ttf             # optional: resources for the resource pack
├── images/background.png
└── screenshots/myface.png       # optional: 240x240 preview shown in the builder
```

One repository can provide any number of apps and watch faces (one manifest entry each). They are
offered separately, and users switch each one on or off on its own.

## 3. `infinitime-app.json`

UTF-8 JSON. The builder ignores keys it doesn't know, so **a misspelt optional key is silently
ignored**. Run `validate` and check the output.

### 3.1 Top level

| Key | Type | Required | Default | Rules / meaning |
|---|---|---|---|---|
| `manifestVersion` | integer | no | `1` | Must be `1`. Any other value is rejected. Always set it explicitly. |
| `name` | string | no | repository folder name | Package name shown in the builder. |
| `author` | string | no | `""` | Shown in the builder. |
| `description` | string | no | `""` | Shown for items that have no `description` of their own. |
| `minInfiniTime` | string | no | none | `MAJOR`, `MAJOR.MINOR` or `MAJOR.MINOR.PATCH` (digits only, e.g. `"1.14.0"`). Users building older firmware get a **warning**; the build isn't blocked. |
| `homepage` | string | no | none | An `http://` or `https://` URL. Shown and opened from the builder's source list (defaults to the GitHub page). |
| `apps` | array of objects | **yes** | — | Must not be empty. One entry per app / watch face (see 3.2). |

### 3.2 Entries in `apps`

| Key | Type | Required | Default | Rules / meaning |
|---|---|---|---|---|
| `type` | string | no | `"app"` | `"app"` or `"watchface"`. |
| `enum` | string | **yes** | — | Matches `^[A-Z][A-Za-z0-9_]{0,47}$`: a C++ identifier of 1–48 characters starting with a capital letter. Becomes `Apps::<enum>` or `WatchFace::<enum>`. Must be unique in the manifest and must not clash with InfiniTime's own enumerators or any other selected package's (a clash is a build error), so make it distinctive (e.g. `SkySnake`, not `Snake`). |
| `name` | string | no | `enum` | Display name in the builder. (On the watch, a face's name comes from `WatchFaceTraits::name`.) |
| `description` | string | no | package `description` | Shown in the builder. |
| `header` | path | **yes** | — | Header that defines the traits (see 4). It's included from `UserApps.h`. |
| `sources` | array of paths | no | `[]` | Files to compile. Can be empty for a header-only item. Duplicate paths across items are compiled once. |
| `icon` | codepoint | no | none | FontAwesome 5 codepoint for the launcher tile, e.g. `"0xf118"`. Added to the launcher font, and used for the builder's preview. Apps only (it has no effect on watch faces). |
| `glyphs` | array of codepoints | no | `[]` | Extra FontAwesome 5 codepoints your code draws with the 20 px launcher font (`jetbrains_mono_bold_20`). |
| `resources` | object | no | none | Fonts and images for the resource pack (see 3.3). |
| `screenshot` | path | no | none | PNG, ideally 240x240, shown in the builder's watch preview (most useful for watch faces). |

**Paths** are relative to the repository root and use `/` (a `\` is treated as `/`). They can't be
absolute or contain `..`, and the file must exist. `header` and `sources` must end in `.h`, `.hpp`,
`.c`, `.cc` or `.cpp` (case-sensitive). Resource files and screenshots are checked
case-insensitively.

**Codepoints** are strings of the form `0x` followed by 2–6 hex digits (`^0x[0-9a-fA-F]{2,6}$`).
Bare numbers are rejected.

### 3.3 `resources`

Big fonts and images go into the **resource pack** (`infinitime-resources-*.zip`), which users
install on the watch's external flash after the firmware. An item's resources are added only when
that item is switched on.

```json
"resources": {
  "fonts": {
    "myface_time": {
      "sources": [{ "file": "fonts/MyFont.ttf", "symbols": "0123456789:" }],
      "size": 54,
      "bpp": 2
    }
  },
  "images": {
    "myface_bg": { "sources": "images/background.png", "color_format": "CF_TRUE_COLOR_ALPHA" }
  }
}
```

`resources` may only contain `fonts` and `images`.

**Resource names** (the keys under `fonts` and `images`) must match `^[a-z][a-z0-9_]{1,40}$`
(2–41 characters, lower case). They become `/fonts/<name>.bin` and `/images/<name>.bin` on the
watch. They must be unique across InfiniTime and every package a user might select, so **prefix
them with your item's name**. A clash fails the build.

| Font key | Type | Required | Rules |
|---|---|---|---|
| `sources` | array of objects | **yes** | Not empty. Each object has a `file` (`.ttf`, `.otf` or `.woff`) and `symbols` and/or `range`. |
| `sources[].symbols` | string | one of the two | The characters to include, e.g. `"0123456789:"`. Must not start with `-`. |
| `sources[].range` | string | one of the two | Codepoint ranges in lv_font_conv syntax, e.g. `"0x30-0x39"`. Must not start with `-`. |
| `size` | integer | **yes** | Pixel height, 6–200. |
| `bpp` | integer | no (default `1`) | Bits per pixel: `1`, `2` or `4`. |

| Image key | Type | Required | Rules |
|---|---|---|---|
| `sources` | path (string) | **yes** | A single `.png` file. |
| `color_format` | string | no (default `CF_TRUE_COLOR_ALPHA`) | `CF_TRUE_COLOR_ALPHA` (full colour) or `CF_INDEXED_1_BIT` (white on transparent; tint it with `image_recolor`). These are the only formats InfiniTime's converter supports. |

Budget:
- **RAM.** Fonts loaded with `lv_font_load` live on the heap. Around 8 KB of fonts per face works;
  16 KB crashed the watch in testing. Include only the glyphs you draw.
- **Flash.** The watch's file system holds about 3.3 MB, and each file takes whole 4 KB blocks.
  The builder shows each item's share, and warns when the pack won't fit.
- Images are drawn from flash and cost no RAM, so use images rather than fonts for large icon sets.

## 4. C++ requirements

Write the app the same way as a built-in InfiniTime app (see InfiniTime's
[Apps.md](https://github.com/InfiniTimeOrg/InfiniTime/blob/main/doc/code/Apps.md)). The `header`
must define the traits specialisation for the manifest's `enum`, inside
`namespace Pinetime::Applications`:

```cpp
// App
template <>
struct AppTraits<Apps::MyApp> {
  static constexpr Apps app = Apps::MyApp;
  static constexpr const char* icon = "\xEF\x84\x98";  // UTF-8 of the manifest "icon" (0xf118)
  static Screens::Screen* Create(AppControllers& controllers);
  static bool IsAvailable(Pinetime::Controllers::FS& filesystem);
};

// Watch face
template <>
struct WatchFaceTraits<WatchFace::MyFace> {
  static constexpr WatchFace watchFace = WatchFace::MyFace;
  static constexpr const char* name = "My Face";       // shown in Settings > Watch face
  static Screens::Screen* Create(AppControllers& controllers);
  static bool IsAvailable(Pinetime::Controllers::FS& filesystem);
};
```

Rules:
- **`enum` must match the traits exactly.** `validate` warns if the header doesn't contain
  `AppTraits<Apps::<enum>>` / `WatchFaceTraits<WatchFace::<enum>>`.
- **The header is included from `UserApps.h`** for every build that includes your item. It must be
  self-contained (`#pragma once`, include everything it uses) and must not define non-inline
  functions or variables, or the build fails with duplicate symbols.
- **Includes.** Include your own files with quotes, relative to the including file
  (`#include "MyApp.h"`). Include InfiniTime with its usual paths
  (`#include "displayapp/screens/Screen.h"`, `#include "displayapp/apps/Apps.h"`,
  `#include "displayapp/Controllers.h"`, `#include <lvgl/lvgl.h>`). Your files are copied to
  `src/external/<dir>/`, which is not on the include path.
- **No warnings.** InfiniTime builds with `-Werror`.
- **Use only what `AppControllers` exposes.** Apps that need new system or BLE services still need a
  fork of InfiniTime.
- **No network at build time.** The build container has no network, and no extra build steps run.
  Commit generated code.
- **Resources.** If the item uses resource-pack files, `IsAvailable()` must check that they exist,
  so the item is greyed out until the pack is installed. Load fonts with
  `lv_font_load("F:/fonts/<name>.bin")` and free them with `lv_font_free` after `lv_obj_clean`. Use
  images with `lv_img_set_src(img, "F:/images/<name>.bin")`.
- **Icons.** `AppTraits::icon` is the UTF-8 encoding of the manifest's `icon` codepoint. You can
  also use an existing `Symbols::` icon or short plain text (then leave `icon` out of the manifest).
- **Touch.** `OnTouchEvent(uint16_t x, uint16_t y)` is called continuously while a finger is down.
  For taps and buttons, use `OnTouchEvent(TouchEvents event)`.

## 5. Versioning and compatibility

- InfiniTime 1.14 or later is required (the `ENABLE_USERAPPS` build system). An app that compiles
  on one InfiniTime version may not compile on another. Set `minInfiniTime`, and say in the README
  which versions you've tested.
- **Users who add your repository without a ref follow your default branch** and get every new
  commit. Keep the default branch buildable, and publish tags (e.g. `v1.2.0`) for people who want
  to pin a version.
- Renaming an `enum` or a resource name breaks users' saved selections, and changes which face a
  watch shows (faces are stored on the watch by number). Treat both as a breaking change.
- `manifestVersion` will be increased only for changes that older builders can't read. A builder
  rejects manifest versions it doesn't know.

## 6. Checklist before publishing

- [ ] `infinitime-app.json` is at the repository root, and `infinitime-builder-cli validate .` reports `OK` with no warnings.
- [ ] Each `enum` and resource name is distinctive (prefixed with your project or item name).
- [ ] Each header defines the matching `AppTraits` / `WatchFaceTraits` and compiles without warnings.
- [ ] Built and run with *Sources > Add local folder…*, and tested in [InfiniEmu](https://github.com/pipe01/InfiniEmu) or on a watch.
- [ ] Watch faces have a 240x240 `screenshot`.
- [ ] No symlinks, submodules or large unrelated files.
- [ ] The repository has the `infinitime-app` topic, a README and a GPL-compatible licence.

## 7. Examples

- [`examples/hello-app`](../examples/hello-app): a minimal app with a launcher icon.
- [`examples/bigclock-face`](../examples/bigclock-face): a minimal watch face.
- [ScanuNicco/infinitime-sky-games](https://github.com/ScanuNicco/infinitime-sky-games): two apps in one repository.
- [ScanuNicco/infinitime-concept-faces](https://github.com/ScanuNicco/infinitime-concept-faces): watch faces with fonts and images in the resource pack.
- [ScanuNicco/infinitime-pebble-ports](https://github.com/ScanuNicco/infinitime-pebble-ports): Pebble watch faces ported to InfiniTime, with on-watch settings menus, artwork generated into the firmware, and a generator script for the manifest.
