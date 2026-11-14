"""Shared fixtures: a minimal InfiniTime source tree."""
import json
from pathlib import Path

import pytest

from itbuilder import firmware as fw

APPS_IN = """#pragma once
namespace Pinetime {
  namespace Applications {
    enum class Apps : uint8_t {
      None,
      Launcher,
      Timer,
      Dice,
      Error
    };

    enum class WatchFace : uint8_t {
      Digital,
      Analog,
    };
    using UserAppTypes = TypeList<@USERAPP_TYPES@>;
  }
}
"""
APPS_CMAKE = """if(DEFINED ENABLE_USERAPPS)
else ()
    set(DEFAULT_USER_APP_TYPES "Apps::Timer")
    set(DEFAULT_USER_APP_TYPES "${DEFAULT_USER_APP_TYPES}, Apps::Dice")
    #set(DEFAULT_USER_APP_TYPES "${DEFAULT_USER_APP_TYPES}, Apps::Motion")
endif ()
if(DEFINED ENABLE_WATCHFACES)
else()
    set(DEFAULT_WATCHFACE_TYPES "WatchFace::Digital")
endif()
"""
SCREEN_H = """
namespace Pinetime { namespace Applications {
    template <>
    struct AppTraits<Apps::Timer> {
      static constexpr const char* icon = Screens::Symbols::hourGlass;
      static Screens::Screen* Create(AppControllers& controllers) {
        return nullptr;
      };
    };
    template <>
    struct AppTraits<Apps::Dice> {
      static constexpr const char* icon = "D";
    };
    template <>
    struct WatchFaceTraits<WatchFace::Digital> {
      static constexpr const char* name = "Digital face";
    };
    template <>
    struct WatchFaceTraits<WatchFace::Analog> {
      static constexpr const char* name = "Analog face";
    };
}}
"""


@pytest.fixture
def tree(tmp_path) -> Path:
    root = tmp_path / "InfiniTime"
    files = {
        "CMakeLists.txt": "project(pinetime VERSION 1.15.0 LANGUAGES C CXX ASM)\n",
        str(fw.APPS_IN): APPS_IN,
        str(fw.APPS_CMAKE): APPS_CMAKE,
        str(fw.USER_APPS_H): '#pragma once\n#include "displayapp/apps/Apps.h"\n#include "Controllers.h"\n\nnamespace X {}\n',
        str(fw.SRC_CMAKE): "list(APPEND SOURCE_FILES\n        main.cpp\n)\n\nadd_executable(${EXECUTABLE_NAME} ${SOURCE_FILES})\n"
                           "add_executable(${EXECUTABLE_MCUBOOT_NAME} ${SOURCE_FILES})\n",
        str(fw.FONTS_JSON): json.dumps({fw.LAUNCHER_FONT: {"sources": [
            {"file": "JetBrainsMono-Bold.ttf", "range": "0x20-0x7e"},
            {"file": "FontAwesome5-Solid+Brands+Regular.woff", "range": "0xf294, 0xf252"}]}}),
        str(fw.SYMBOLS_H): 'static constexpr const char* hourGlass = "\\xEF\\x89\\x92";\n',
        "src/displayapp/screens/Things.h": SCREEN_H,
        str(fw.APP_LIST_H): "static constexpr int appsPerScreen = 6;\n",
    }
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return root
