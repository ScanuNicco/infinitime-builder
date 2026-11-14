from itbuilder import sizes

MAP = """Archive member included to satisfy reference by file (symbol)

Linker script and memory map

.text           0x0000000000000000     0x1000
 .text.main     0x0000000000000000      0x100 CMakeFiles/pinetime-app.dir/main.cpp.o
 .text._ZN4DiceC2Ev
                0x0000000000000100      0x200 CMakeFiles/pinetime-app.dir/displayapp/screens/Dice.cpp.o
 .text.lv_chart 0x0000000000000300      0x400 src/libs/liblvgl.a(lv_chart.c.o)
 .text.timer    0x0000000000000700       0x80 CMakeFiles/pinetime-app.dir/displayapp/screens/Timer.cpp.o
 .rodata.font   0x0000000000000780       0x40 src/displayapp/fonts/libinfinitime_fonts.a(jetbrains.c.o)
.data           0x0000000020000000       0x10 load address 0x0000000000001000
 .data.x        0x0000000020000000       0x10 CMakeFiles/pinetime-app.dir/main.cpp.o
.bss            0x0000000020000010      0x100
 .bss.y         0x0000000020000010      0x100 CMakeFiles/pinetime-app.dir/main.cpp.o
.comment        0x0000000000000000       0x49
 .comment       0x0000000000000000       0x49 CMakeFiles/pinetime-app.dir/main.cpp.o
.mem_section_dummy_rom
                0x0000000000001010        0x0

Cross Reference Table

Symbol                                            File
Dice::Dice()                                      CMakeFiles/pinetime-app.dir/displayapp/screens/Dice.cpp.o
                                                  CMakeFiles/pinetime-app.dir/displayapp/DisplayApp.cpp.o
lv_chart_create                                   src/libs/liblvgl.a(lv_chart.c.o)
                                                  CMakeFiles/pinetime-app.dir/displayapp/screens/Dice.cpp.o
font                                              src/displayapp/fonts/libinfinitime_fonts.a(jetbrains.c.o)
                                                  CMakeFiles/pinetime-app.dir/displayapp/screens/Timer.cpp.o
                                                  CMakeFiles/pinetime-app.dir/main.cpp.o
Timer::Timer()                                    CMakeFiles/pinetime-app.dir/displayapp/screens/Timer.cpp.o
                                                  CMakeFiles/pinetime-app.dir/displayapp/DisplayApp.cpp.o
"""


def test_object_sizes():
    o = sizes.object_sizes(MAP)
    assert o == {
        "main.cpp": 0x110,  # .text + .data (copied from flash); .bss and .comment are not flash
        "displayapp/screens/Dice.cpp": 0x200,
        "liblvgl.a(lv_chart.c.o)": 0x400,
        "displayapp/screens/Timer.cpp": 0x80,
        "libinfinitime_fonts.a(jetbrains.c.o)": 0x40,
    }


def test_reference_graph():
    g = sizes.reference_graph(MAP)
    assert g["displayapp/DisplayApp.cpp"] == {"displayapp/screens/Dice.cpp", "displayapp/screens/Timer.cpp"}
    assert g["displayapp/screens/Dice.cpp"] == {"liblvgl.a(lv_chart.c.o)"}
    assert g["main.cpp"] == {"libinfinitime_fonts.a(jetbrains.c.o)"}


def test_profile_estimates_follow_dependencies():
    own = {"builtin:Apps::Dice": ["displayapp/screens/Dice.cpp"], "builtin:Apps::Timer": ["displayapp/screens/Timer.cpp"]}
    prof = sizes.build_profile(MAP, 0x1000 + 0x10, own)
    everything = {"builtin:Apps::Dice", "builtin:Apps::Timer"}
    assert prof.estimate(everything)[0] == 0x1010
    # Turning Dice off also drops lv_chart, which only Dice uses
    no_dice, unknown = prof.estimate({"builtin:Apps::Timer"})
    assert no_dice == 0x1010 - 0x200 - 0x400 - sizes.ITEM_OVERHEAD and unknown == []
    # The font is shared with main.cpp, so turning Timer off only saves Timer's own code
    assert prof.cost("builtin:Apps::Timer", everything) == 0x80 + sizes.ITEM_OVERHEAD
    assert prof.estimate({"ext:new#App"})[1] == ["ext:new#App"]


def test_profile_roundtrip(tmp_path):
    prof = sizes.build_profile(MAP, 1234, {"a": ["main.cpp"]})
    prof.save(tmp_path / "p.json")
    again = sizes.SizeProfile.load(tmp_path / "p.json")
    assert again.estimate({"a"}) == prof.estimate({"a"})


def test_memory_usage():
    lines = [
        "Memory region         Used Size  Region Size  %age Used",
        "           FLASH:      167760 B     474632 B     35.35%",
        "             RAM:       18652 B        64 KB     28.46%",
        "post build steps for pinetime-mcuboot-recovery-1.16.0",
        "[ 69%] Building CXX object x.o",
        "Memory region         Used Size  Region Size  %age Used",
        "           FLASH:      393232 B     474632 B     82.85%",
        "     SPARE_SPACE:           0 B        12 KB      0.00%",
        "             RAM:       22920 B        64 KB     34.97%",
        "post build steps for pinetime-mcuboot-app-1.16.0",
    ]
    assert sizes.memory_usage(lines) == {"FLASH": (393232, 474632), "SPARE_SPACE": (0, 12288), "RAM": (22920, 65536)}


def test_resource_blocks_match_littlefs():
    # Checked against littlefs itself (InfiniTime's FS config) on real resource packs:
    # 8 files / 51 KB -> 24 blocks, 16 files / 124 KB -> 46, 55 files / 239 KB -> 100.
    assert sizes.resource_blocks([]) == 2 + 2 * 2
    assert sizes.resource_blocks([100]) == 1 + 6  # a small file still takes a whole block
    assert sizes.resource_blocks([4088]) == 1 + 6
    assert sizes.resource_blocks([4089]) == 2 + 6
    assert sizes.resource_capacity(844) == 844 - sizes.FS_RESERVE_BLOCKS


def test_profile_resource_estimate():
    prof = sizes.build_profile(MAP, 1000, {"a": ["main.cpp"], "b": []},
                               resource_sizes={"teko": 800, "face_font": 5000, "face_bg": 60000},
                               resource_owner={"face_font": "b", "face_bg": "b"})
    nbytes, blocks, capacity = prof.estimate_resources({"a"})
    assert nbytes == 800 and blocks == sizes.resource_blocks([800])  # built-in resources are always included
    nbytes, blocks, _ = prof.estimate_resources({"a", "b"})
    assert nbytes == 65800 and blocks == sizes.resource_blocks([800, 5000, 60000])
    assert capacity == sizes.resource_capacity(sizes.DEFAULT_FS_BLOCK_COUNT)
    assert prof.resource_cost("b") == 65000 and prof.resource_cost("a") is None


def test_old_profiles_are_measured_again(tmp_path):
    import json

    old = {"linked_total": 1, "objects": {}, "graph": {}, "roots": [], "ownership": {}}
    (tmp_path / "p.json").write_text(json.dumps(old))
    assert sizes.SizeProfile.load(tmp_path / "p.json") is None


def test_resource_pack_usage_and_warning(tmp_path):
    import json
    import zipfile

    from itbuilder import builder

    zp = tmp_path / "infinitime-resources-1.0.0.zip"
    files = {"a.bin": 5000, "b.bin": 300}
    with zipfile.ZipFile(zp, "w") as z:
        for name, size in files.items():
            z.writestr(name, b"x" * size)
        z.writestr("resources.json", json.dumps({"resources": [
            {"filename": n, "path": f"/fonts/{n}"} for n in files], "obsolete_files": []}))
    nbytes, blocks, capacity = builder.resource_pack_usage(zp, 4096, 844)
    assert (nbytes, blocks) == (5300, sizes.resource_blocks([5000, 300]))
    nbytes, blocks, capacity = builder.resource_pack_usage(zp, 4096, 10)
    assert blocks > capacity
    assert "larger than" in builder.resource_warning(blocks, capacity, nbytes)


def test_fs_geometry(tmp_path):
    from itbuilder import firmware as fw

    h = tmp_path / "src/components/fs/FS.h"
    h.parent.mkdir(parents=True)
    h.write_text("static constexpr size_t startAddress = 0x0B4000;\\n"
                 "static constexpr size_t size = 0x34C000;\\nstatic constexpr size_t blockSize = 4096;\\n")
    assert fw.fs_geometry(tmp_path) == (4096, 844)
    assert fw.fs_geometry(tmp_path / "missing") == (4096, 844)  # sensible default
