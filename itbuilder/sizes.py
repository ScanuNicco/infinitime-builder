"""Flash size estimation.

A *calibration build* compiles the firmware once with every known app and watch face enabled, with
the flash limit in the linker script raised so it links even if that would not fit on a real watch,
and with a cross reference table in the linker map. From the map we get:

* how many bytes of flash each object file takes after unused code was garbage collected, and
* which object files reference which (an object-level dependency graph).

To estimate a selection we re-run the linker's garbage collection on that graph: start from the
objects that are always linked plus those of the enabled items, follow references (ignoring
references into other items' code, which only exist because of the app list) and add up what is
reachable. Measured against real builds this is within about 1%, and errs on the high side.
After a real build the exact size is known and checked against the limits.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

# The PineTime's limits for the MCUboot application image (gcc_nrf52-mcuboot.ld, InfiniTime 1.11+):
# 0x7c000 (scratch) - 0x8000 (bootloader) - 0x20 header - 0x1b0 trailer - 0x28 TLV
DEFAULT_FLASH_LIMIT = 474632
DEFAULT_RAM_LIMIT = 64 * 1024
ITEM_OVERHEAD = 64  # launcher entry + Create() thunk per item, roughly

_SECTION_RE = re.compile(r"^ (\S+)?\s+0x([0-9a-fA-F]+)\s+0x([0-9a-fA-F]+)\s+(\S.*?)\s*$")
_OUT_RE = re.compile(r"^(\.\S+)\s+0x([0-9a-fA-F]+)\s+0x([0-9a-fA-F]+)")
_RAM_ONLY = {".bss", ".heap", ".stack_dummy", ".noinit"}
_NOT_LOADED = (".debug", ".comment", ".stab", ".ARM.attributes", ".group", ".note.GNU-stack")


def object_sizes(map_text: str) -> dict[str, int]:
    """Flash bytes per object file (path relative to src/, e.g. 'displayapp/screens/Dice.cpp')."""
    start = map_text.find("Linker script and memory map")
    if start < 0:
        raise ValueError("not a GNU ld map file")
    out: dict[str, int] = {}
    in_flash = False
    pending: str | None = None
    out_pending: str | None = None
    for line in map_text[start:].splitlines():
        if out_pending is not None:  # output section whose address is on the next line
            mm = re.match(r"^\s+0x([0-9a-fA-F]+)\s+0x([0-9a-fA-F]+)", line)
            if mm:
                line = f"{out_pending} {line.strip()}"
            out_pending = None
        if re.match(r"^\.\S+$", line):
            out_pending = line
            in_flash = False
            continue
        m = _OUT_RE.match(line)
        if m:
            name, addr = m.group(1), int(m.group(2), 16)
            in_flash = (name not in _RAM_ONLY and not name.startswith(_NOT_LOADED)
                        and (addr < 0x20000000 or name == ".data"))
            continue
        if line.startswith(" ") and not line.startswith("  ") and len(line.split()) == 1:
            pending = line.strip()  # input section name on its own line
            continue
        m = _SECTION_RE.match(line)
        if not m:
            continue
        if m.group(1) is None and pending is None:
            continue
        pending = None
        if not in_flash:
            continue
        size, obj = int(m.group(3), 16), m.group(4)
        if not size or obj.startswith("load address"):
            continue
        key = _obj_key(obj)
        out[key] = out.get(key, 0) + size
    return out


def memory_usage(lines: list[str], target: str = "pinetime-mcuboot-app") -> dict[str, tuple[int, int]]:
    """Parse the 'Memory region' table printed by the linker for `target`.

    Returns {"FLASH": (used, size), "RAM": (used, size)} for the last table printed before the
    post build line of that target.
    """
    tables: list[dict[str, tuple[int, int]]] = []
    cur: dict[str, tuple[int, int]] | None = None
    for line in lines:
        if line.startswith("Memory region"):
            cur = {}
            tables.append(cur)
            continue
        m = re.match(r"\s*(\w+):\s+(\d+(?:\.\d+)?)\s*(B|KB|MB)\s+(\d+(?:\.\d+)?)\s*(B|KB|MB)", line)
        if m and cur is not None:
            mult = {"B": 1, "KB": 1024, "MB": 1024 * 1024}
            cur[m.group(1)] = (int(float(m.group(2)) * mult[m.group(3)]), int(float(m.group(4)) * mult[m.group(5)]))
            continue
        if cur is not None and re.match(rf"post build (?:\(DFU\) )?steps for {target}-[\d.]+$", line.strip()):
            return cur
        if line.strip() and not line.startswith(" ") and cur is not None and "Memory region" not in line \
                and not re.match(r"\s*\w+:", line):
            cur = None
    return {}


# ---------------------------------------------------------------------------------------------
# The resource pack lives on the watch's external flash, in a littlefs file system
# (components/fs/FS.h: 0x34C000 bytes in 4 KiB blocks on InfiniTime 1.11+).

DEFAULT_FS_BLOCK_SIZE = 4096
DEFAULT_FS_BLOCK_COUNT = 0x34C000 // 4096  # 844
# Blocks kept free for the watch's own files (settings, alarms, ...) and littlefs' working space.
FS_RESERVE_BLOCKS = 32
RESOURCE_DIRS = 2  # /fonts and /images


def resource_blocks(file_sizes: list[int], block_size: int = DEFAULT_FS_BLOCK_SIZE, dirs: int = RESOURCE_DIRS) -> int:
    """Blocks the resource files take on the watch.

    InfiniTime's littlefs cache is 16 bytes, so no resource file is small enough to be stored
    inline: each takes whole blocks, minus a few bytes per block for littlefs' pointers. Each
    directory is a metadata pair (2 blocks, more when it holds many files), and the superblock
    is another pair.
    """
    data = sum(-(-size // (block_size - 8)) for size in file_sizes if size > 0)
    per_dir = 2 * (1 + len(file_sizes) // (60 * max(dirs, 1)))  # metadata pairs split when full
    return data + 2 + dirs * per_dir


def resource_capacity(block_count: int = DEFAULT_FS_BLOCK_COUNT) -> int:
    """Blocks available for the resource pack."""
    return block_count - FS_RESERVE_BLOCKS


@dataclass
class SizeProfile:
    """Result of a calibration build: everything needed to estimate any selection's flash use."""

    linked_total: int  # flash bytes with every calibrated item enabled, as reported by the linker
    objects: dict[str, int]
    graph: dict[str, list[str]]
    roots: list[str]
    ownership: dict[str, list[str]]  # item key -> its objects
    flash_limit: int = DEFAULT_FLASH_LIMIT
    fingerprint: str = ""
    failed: list[str] = field(default_factory=list)  # item keys that could not be measured
    # resource pack: bytes per resource file, and which item (if any) each belongs to; files that
    # belong to no item (InfiniTime's own) are always in the pack
    resource_sizes: dict[str, int] = field(default_factory=dict)
    resource_owner: dict[str, str] = field(default_factory=dict)
    fs_block_size: int = DEFAULT_FS_BLOCK_SIZE
    fs_block_count: int = DEFAULT_FS_BLOCK_COUNT

    def _model(self) -> "ReachModel":
        m = getattr(self, "_m", None)
        if m is None:
            m = ReachModel(self.objects, self.graph, self.roots, self.ownership)
            object.__setattr__(self, "_m", m)
            object.__setattr__(self, "_full", m.size(set(self.ownership)))
        return m

    def known(self, key: str) -> bool:
        return key in self.ownership

    def estimate(self, enabled: set[str]) -> tuple[int, list[str]]:
        """Estimated flash use for a selection, and the enabled keys whose size is unknown."""
        m = self._model()
        known = {k for k in enabled if k in self.ownership}
        size = self.linked_total - (self._full - m.size(known))
        return size, sorted(k for k in enabled if k not in self.ownership)

    def cost(self, key: str, enabled: set[str]) -> int | None:
        """Flash this item adds (or would add) on top of the rest of the selection."""
        if key not in self.ownership:
            return None
        others = set(enabled) - {key}
        return self.estimate(others | {key})[0] - self.estimate(others)[0]

    def estimate_resources(self, enabled: set[str]) -> tuple[int, int, int]:
        """(bytes, blocks used, blocks available) for the resource pack of a selection."""
        files = [size for name, size in self.resource_sizes.items()
                 if name not in self.resource_owner or self.resource_owner[name] in enabled]
        return sum(files), resource_blocks(files, self.fs_block_size), resource_capacity(self.fs_block_count)

    def resource_cost(self, key: str) -> int | None:
        """Bytes of resource-pack files that belong to an item, if it has any measured."""
        files = [self.resource_sizes.get(name, 0) for name, owner in self.resource_owner.items() if owner == key]
        return sum(files) if files else None

    def save(self, path: Path) -> None:
        from .fsutil import atomic_write_text

        data = {k: v for k, v in self.__dict__.items() if not k.startswith("_")}
        atomic_write_text(path, json.dumps(data))

    @staticmethod
    def load(path: Path) -> "SizeProfile | None":
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if "resource_sizes" not in data:
                return None  # measured by an older version: measure again
            return SizeProfile(**data)
        except (OSError, ValueError, TypeError):
            return None


def build_profile(map_text: str, linked_total: int | None, ownership: dict[str, list[str]], **kw) -> SizeProfile:
    objects = object_sizes(map_text)
    graph = reference_graph(map_text)
    owned = {o for objs in ownership.values() for o in objs}
    # Objects given to the linker directly (not from a library) are always linked, apart from items.
    roots = [o for o in objects if "(" not in o and o not in owned]
    return SizeProfile(linked_total or sum(objects.values()), objects, {k: sorted(v) for k, v in graph.items()},
                       roots, {k: [o for o in v if o in objects] for k, v in ownership.items()}, **kw)


def _obj_key(obj: str) -> str:
    m = re.search(r"\.dir/(.+?)\.o$", obj)
    if m:
        return m.group(1)
    return obj.rsplit("/", 1)[-1]  # archive member: 'liblvgl.a(lv_obj.c.o)'


def reference_graph(map_text: str) -> dict[str, set[str]]:
    """Object-level 'references' graph from the map's cross reference table (needs -Wl,--cref).

    Edge A -> B means object A references a symbol defined in object B.
    """
    start = map_text.find("Cross Reference Table")
    if start < 0:
        return {}
    graph: dict[str, set[str]] = {}
    definer: str | None = None
    obj_re = re.compile(r"(\S+\.o\)?)\s*$")
    for line in map_text[start:].splitlines()[3:]:
        m = obj_re.search(line)
        if not m:
            definer = None
            continue
        obj = _obj_key(m.group(1))
        if line[:1].isspace():
            if definer and obj != definer:
                graph.setdefault(obj, set()).add(definer)
        else:
            definer = obj
    return graph


@dataclass
class ReachModel:
    """Object-level model of the linker's garbage collection, built from a calibration build."""

    objects: dict[str, int]  # flash bytes per object (all items enabled)
    graph: dict[str, list[str]]
    roots: list[str]  # objects always linked
    ownership: dict[str, list[str]]  # item key -> objects belonging to it

    def size(self, enabled: set[str]) -> int:
        owned = {o for objs in self.ownership.values() for o in objs}
        start = set(self.roots) | {o for k in enabled for o in self.ownership.get(k, [])}
        seen, stack = set(), list(start)
        while stack:
            o = stack.pop()
            if o in seen:
                continue
            seen.add(o)
            for t in self.graph.get(o, ()):
                # references into another item's code only exist because of the app list
                if t not in seen and (t not in owned or t in start):
                    stack.append(t)
        return sum(self.objects.get(o, 0) for o in seen) + ITEM_OVERHEAD * len(enabled)
