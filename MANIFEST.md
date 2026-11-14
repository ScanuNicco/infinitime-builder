# Publishing an app or watch face

Write your app the same way as a built-in InfiniTime app (see
[Apps.md](https://github.com/InfiniTimeOrg/InfiniTime/blob/main/doc/code/Apps.md)), put it in a
GitHub repository with an `infinitime-app.json` at the root, and add the **`infinitime-app`**
topic to the repository so people can find it from the builder.

See [`examples/hello-app`](examples/hello-app) and [`examples/bigclock-face`](examples/bigclock-face).

## `infinitime-app.json`

```json
{
  "manifestVersion": 1,
  "name": "Hello Counter",
  "author": "you",
  "description": "Shown in the builder",
  "minInfiniTime": "1.14.0",
  "apps": [
    {
      "type": "app",
      "enum": "HelloCounter",
      "name": "Hello Counter",
      "description": "Tap to count",
      "header": "HelloCounter.h",
      "sources": ["HelloCounter.cpp"],
      "icon": "0xf118",
      "glyphs": []
    }
  ]
}
```

| Field | |
|---|---|
| `apps` | One or more apps / watch faces in this repository. |
| `type` | `"app"` (default) or `"watchface"`. |
| `enum` | Enumerator added to `Apps` or `WatchFace`. Must start with a capital letter and not clash with existing names, so make it distinctive. |
| `header` | Header that defines `AppTraits<Apps::<enum>>` or `WatchFaceTraits<WatchFace::<enum>>`, including `Create` and `IsAvailable`. It's included from `UserApps.h`. |
| `sources` | `.cpp` files to compile. Paths are relative to the repository root and may not leave it. |
| `icon` | Optional FontAwesome 5 codepoint for the launcher tile. It's added to the launcher font, and the builder uses it for its preview. Your `AppTraits::icon` must be the UTF-8 encoding of the same codepoint (e.g. `0xf118` → `"\xEF\x84\x98"`). You can also use an existing `Symbols::` icon or plain text, like the built-in "2" for Twos. |
| `glyphs` | Optional extra FontAwesome codepoints your app draws in the 20px launcher font. |
| `minInfiniTime` | Optional minimum version; users get a warning on older firmware. |
| `resources` | Optional fonts and images for the watch's external flash; see below. |
| `screenshot` | Optional 240x240 PNG of the item. For watch faces the builder shows it on its watch preview. An emulator such as [InfiniEmu](https://github.com/pipe01/InfiniEmu) is handy for taking it. |

## Fonts and images (the resource pack)

Big fonts and images don't fit in the firmware's flash, so InfiniTime keeps them on the watch's
external flash and ships them as a separate `infinitime-resources-*.zip` that users send to the
watch after the firmware. An item can add its own:

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

- The format is InfiniTime's own `src/resources/fonts.json` / `images.json`: fonts accept
  `symbols` and/or `range` (always limit them to what you draw — fonts are loaded into RAM), `size`
  in pixels and `bpp` (1, 2 or 4); images are PNGs with an LVGL `color_format`.
- They end up on the watch as `/fonts/<name>.bin` and `/images/<name>.bin`. Names must be
  `lower_case` and unique across all packages, so prefix them with your item's name.
- In your code, check that the files exist in `IsAvailable()` (the face is greyed out until the
  resource pack is installed), load fonts with `lv_font_load("F:/fonts/<name>.bin")` (free them
  with `lv_font_free` after `lv_obj_clean`) and use images with
  `lv_img_set_src(img, "F:/images/<name>.bin")`.
- They are only added to the resource pack when the item is switched on, and the builder tells
  users to install the pack.
- Mind the RAM: fonts are loaded into the watch's heap. Around 8 KB of fonts per face works; a face
  loading 16 KB crashed the watch in testing (`lv_font_load` does not survive running out of
  memory). Images are drawn from flash and cost no RAM, so use images for large icon sets.
- Only `CF_TRUE_COLOR_ALPHA` and `CF_INDEXED_1_BIT` images are supported by InfiniTime's converter.
  A 1-bit image is white on transparent; tint it with `image_recolor` if needed.

See [ScanuNicco/infinitime-concept-faces](https://github.com/ScanuNicco/infinitime-concept-faces)
for a complete example.

## Notes

Your files are copied to `src/external/<package>/`, so include your own headers with quotes
relative to your files (`#include "HelloCounter.h"`) and InfiniTime's with their usual paths
(`#include "displayapp/screens/Screen.h"`). InfiniTime compiles with `-Werror`, so your code must
be free of warnings.

Tips:
- `OnTouchEvent(uint16_t x, uint16_t y)` is called continuously while the finger is down (it's meant
  for drawing). For buttons and taps use `OnTouchEvent(TouchEvents event)` with `TouchEvents::Tap`.
- Check your manifest with `infinitime-builder-cli validate path/to/repo`.
- Test with *App sources > Add local folder...*, then run the result in
  [InfiniEmu](https://github.com/pipe01/InfiniEmu).
