#!/usr/bin/env python3
"""Query and edit LEADER / MULTILEADER entities in a DWG file.

DWG <-> DXF goes through the ODA File Converter (via ezdxf.addons.odafc).
Edits are made on a cached working DXF in `.dwg_work/` next to the DWG and are
only written back to DWG by the `save` command.

Run `python dwg_leaders.py -h` for commands. Every command takes the DWG path
first. Add `--json` to `list`/`show`/`styles` for machine-readable output.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

import ezdxf
from ezdxf import colors
from ezdxf.addons import odafc
from ezdxf.document import Drawing
from ezdxf.entities import DXFEntity, Leader, MultiLeader
from ezdxf.math import Vec2, Vec3
from ezdxf.tools.text import plain_mtext

LEADER_QUERY = "LEADER MULTILEADER MLEADER"

ODA_CANDIDATES = [
    "/Applications/ODAFileConverter.app/Contents/MacOS/ODAFileConverter",
    str(Path.home() / "Applications/ODAFileConverter.app/Contents/MacOS/ODAFileConverter"),
]

# MULTILEADER property_override_flags bits (ODA DWG spec). Setting one of these
# attributes on the entity is ignored by AutoCAD unless its bit is set.
MLEADER_OVERRIDE_BITS = {
    "leader_type": 0,
    "leader_line_color": 1,
    "leader_linetype_handle": 2,
    "leader_lineweight": 3,
    "has_landing": 4,
    "has_dogleg": 6,
    "dogleg_length": 7,
    "arrow_head_handle": 8,
    "arrow_head_size": 9,
    "content_type": 10,
    "text_style_handle": 11,
    "text_left_attachment_type": 12,
    "text_angle_type": 13,
    "text_alignment_type": 14,
    "text_color": 15,
    "has_text_frame": 17,
    "block_record_handle": 19,
    "block_color": 20,
    "block_scale_vector": 21,
    "block_rotation": 22,
    "block_connection_type": 23,
    "text_right_attachment_type": 25,
    "text_attachment_direction": 27,
    "text_top_attachment_type": 28,
    "text_bottom_attachment_type": 29,
}
RAW_COLOR_ATTRIBS = {"leader_line_color", "text_color", "block_color"}


class ToolError(Exception):
    pass


# --------------------------------------------------------------------------
# ODA setup and working copy
# --------------------------------------------------------------------------

def setup_oda() -> None:
    if shutil.which("ODAFileConverter"):
        return
    for path in ODA_CANDIDATES:
        if Path(path).is_file():
            ezdxf.options.set("odafc-addon", "unix_exec_path", path)
            return
    raise ToolError(
        "ODA File Converter not found. Install it from "
        "https://www.opendesign.com/guestfiles/oda_file_converter"
    )


def work_paths(dwg: Path) -> dict[str, Path]:
    work_dir = dwg.parent / ".dwg_work"
    return {
        "dir": work_dir,
        "dxf": work_dir / f"{dwg.stem}.dxf",
        "meta": work_dir / f"{dwg.stem}.meta.json",
    }


def load_meta(dwg: Path) -> dict:
    meta_file = work_paths(dwg)["meta"]
    if meta_file.is_file():
        return json.loads(meta_file.read_text())
    return {}


def save_meta(dwg: Path, meta: dict) -> None:
    work_paths(dwg)["meta"].write_text(json.dumps(meta, indent=2))


def open_working(dwg: Path, fresh: bool = False) -> Drawing:
    """Return the working DXF document, converting from DWG when needed."""
    if not dwg.is_file():
        raise ToolError(f"No such file: {dwg}")
    paths = work_paths(dwg)
    meta = load_meta(dwg)
    src_mtime = dwg.stat().st_mtime
    has_cache = paths["dxf"].is_file() and meta
    if has_cache and not fresh and meta.get("source_mtime") != src_mtime:
        if meta.get("changes"):
            raise ToolError(
                "The DWG changed on disk after the working copy was made, and "
                "the working copy has unsaved edits. Run `save` or `reset` first."
            )
        print(f"WARNING: {dwg.name} was changed outside this tool (e.g. saved in AutoCAD). "
              "Reloaded it; earlier saved edits may have been overwritten.", file=sys.stderr)
        fresh = True
    if has_cache and not fresh:
        return ezdxf.readfile(paths["dxf"])

    setup_oda()
    paths["dir"].mkdir(exist_ok=True)
    doc = odafc.readfile(str(dwg))
    doc.saveas(paths["dxf"])
    save_meta(dwg, {
        "source": str(dwg),
        "source_mtime": src_mtime,
        "dxfversion": doc.dxfversion,
        "changes": [],
    })
    return doc


def commit(dwg: Path, doc: Drawing, changes: list[dict]) -> None:
    if not changes:
        return
    doc.saveas(work_paths(dwg)["dxf"])
    meta = load_meta(dwg)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    for c in changes:
        c["time"] = stamp
    meta.setdefault("changes", []).extend(changes)
    save_meta(dwg, meta)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def jsonable(value: Any) -> Any:
    if isinstance(value, (Vec2, Vec3)):
        return [round(v, 6) for v in value]
    if isinstance(value, float):
        return round(value, 6)
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items()}
    return value


def parse_value(raw: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def coerce_like(current: Any, value: Any) -> Any:
    if isinstance(current, Vec3) or (current is None and isinstance(value, list)):
        return Vec3(value)
    if isinstance(current, Vec2):
        return Vec2(value)
    if isinstance(current, bool):
        return bool(value)
    if isinstance(current, int) and not isinstance(value, str):
        return int(value)
    if isinstance(current, float) and not isinstance(value, str):
        return float(value)
    return value


def parse_color(value: Any) -> int:
    """ACI int, 'bylayer', 'byblock', [r,g,b] or '#rrggbb' -> raw color."""
    if isinstance(value, str):
        v = value.strip().lower()
        if v == "bylayer":
            return colors.BY_LAYER_RAW_VALUE
        if v == "byblock":
            return colors.BY_BLOCK_RAW_VALUE
        if v.startswith("#") and len(v) == 7:
            return colors.encode_raw_color(colors.RGB.from_hex(v))
        if v.lstrip("-").isdigit():
            value = int(v)
    if isinstance(value, list):
        return colors.encode_raw_color(colors.RGB(*value))
    if isinstance(value, int):
        if value < 0 or value > 257:  # already raw
            return value
        return colors.encode_raw_color(value)
    raise ToolError(f"Bad color: {value!r}")


def describe_raw_color(raw: int) -> str:
    try:
        kind, val = colors.decode_raw_color(raw)
    except ValueError:
        return str(raw)
    return {
        colors.COLOR_TYPE_BY_LAYER: "bylayer",
        colors.COLOR_TYPE_BY_BLOCK: "byblock",
        colors.COLOR_TYPE_WINDOW_BG: "window_bg",
        colors.COLOR_TYPE_ACI: f"aci:{val}",
        colors.COLOR_TYPE_RGB: "#{:02x}{:02x}{:02x}".format(*val) if isinstance(val, tuple) else str(val),
    }.get(kind, str(raw))


def handle_name(doc: Drawing, handle: str | None) -> str | None:
    if not handle or handle == "0":
        return None
    e = doc.entitydb.get(handle)
    if e is None:
        return None
    if e.dxftype() == "MLEADERSTYLE":
        # The dictionary key is the real style name; dxf.name can be stale.
        for key, style in doc.mleader_styles:
            if style is e:
                return key
    for attr in ("name", "tag"):
        if e.dxf.hasattr(attr):
            return e.dxf.get(attr)
    if e.dxftype() == "BLOCK_RECORD":
        return e.dxf.name
    return e.dxftype()


def space_names(doc: Drawing) -> dict[str, str]:
    """Map block_record handle -> 'Model' / layout name / 'block:<name>'."""
    names = {}
    for layout in doc.layouts:
        names[layout.block_record_handle] = layout.name
    for block in doc.blocks:
        h = block.block_record_handle
        if h not in names:
            names[h] = f"block:{block.name}"
    return names


def iter_leaders(doc: Drawing, include_blocks: bool = True):
    names = space_names(doc)
    for block in doc.blocks:
        space = names.get(block.block_record_handle, block.name)
        if not include_blocks and space.startswith("block:"):
            continue
        for e in block.query(LEADER_QUERY):
            yield space, e


def get_leader(doc: Drawing, handle: str) -> DXFEntity:
    e = doc.entitydb.get(handle.upper())
    if e is None or e.dxftype() not in LEADER_QUERY.split():
        raise ToolError(f"No LEADER/MULTILEADER with handle {handle}")
    return e


def leader_annotation(e: Leader) -> DXFEntity | None:
    h = e.dxf.get("annotation_handle")
    if not h or h == "0" or e.doc is None:
        return None
    return e.doc.entitydb.get(h)


def get_raw_text(e: DXFEntity) -> str | None:
    if isinstance(e, MultiLeader):
        if e.has_mtext_content:
            return e.get_mtext_content()
        if e.has_block_content:
            return json.dumps(e.get_block_content())
        return None
    ann = leader_annotation(e)
    if ann is None:
        return None
    if ann.dxftype() == "MTEXT":
        return ann.text
    if ann.dxf.hasattr("text"):
        return ann.dxf.text
    return None


def plain(raw: str | None) -> str | None:
    return None if raw is None else plain_mtext(raw)


def set_override_bit(e: MultiLeader, attr: str) -> None:
    bit = MLEADER_OVERRIDE_BITS.get(attr)
    if bit is not None:
        flags = e.dxf.get("property_override_flags", 0) or 0
        e.dxf.property_override_flags = flags | (1 << bit)


def arrow_points(e: DXFEntity) -> list:
    if isinstance(e, MultiLeader):
        pts = []
        for ld in e.context.leaders:
            for line in ld.lines:
                if line.vertices:
                    pts.append(line.vertices[0])
        return pts
    verts = list(e.vertices)
    return verts[:1]


# --------------------------------------------------------------------------
# Summaries
# --------------------------------------------------------------------------

def summary(doc: Drawing, space: str, e: DXFEntity) -> dict:
    raw = get_raw_text(e)
    d = {
        "handle": e.dxf.handle,
        "type": e.dxftype(),
        "space": space,
        "layer": e.dxf.get("layer"),
        "text": plain(raw) if not (isinstance(e, MultiLeader) and e.has_block_content) else raw,
        "arrow_points": jsonable([list(p)[:2] for p in arrow_points(e)]),
    }
    if isinstance(e, MultiLeader):
        d["style"] = handle_name(doc, e.dxf.get("style_handle"))
        d["content"] = "mtext" if e.has_mtext_content else "block" if e.has_block_content else "none"
        d["leaders"] = sum(len(ld.lines) for ld in e.context.leaders)
    else:
        d["dimstyle"] = e.dxf.get("dimstyle")
        ann = leader_annotation(e)
        d["annotation"] = f"{ann.dxftype()}:{ann.dxf.handle}" if ann else None
    return d


def detail(doc: Drawing, space: str, e: DXFEntity) -> dict:
    d = summary(doc, space, e)
    d["raw_text"] = get_raw_text(e)
    dxf = jsonable(e.dxf.all_existing_dxf_attribs())
    d["dxf"] = dxf
    resolved = {}
    for key, value in dxf.items():
        if key.endswith("_handle") and key != "handle" and key != "owner":
            resolved[key] = handle_name(doc, value)
        if key in RAW_COLOR_ATTRIBS and isinstance(value, int):
            resolved[key] = describe_raw_color(value)
    d["resolved"] = resolved

    if isinstance(e, MultiLeader):
        flags = e.dxf.get("property_override_flags", 0) or 0
        d["overridden"] = sorted(k for k, b in MLEADER_OVERRIDE_BITS.items() if flags & (1 << b))
        ctx = e.context
        c = {name: jsonable(getattr(ctx, name)) for name in ctx.ATTRIBS.values()}
        if ctx.mtext is not None:
            m = {name: jsonable(getattr(ctx.mtext, name)) for name in ctx.mtext.ATTRIBS.values()}
            m["style_name"] = handle_name(doc, ctx.mtext.style_handle)
            m["color_resolved"] = describe_raw_color(ctx.mtext.color)
            c["mtext"] = m
        if ctx.block is not None:
            b = ctx.block
            c["block"] = {
                "block_record_handle": b.block_record_handle,
                "block_name": handle_name(doc, b.block_record_handle),
                "insert": jsonable(b.insert),
                "scale": jsonable(b.scale),
                "rotation": jsonable(b.rotation),
                "color": describe_raw_color(b.color),
            }
            c["block_attribs"] = e.get_block_content()
        c["leaders"] = [
            {
                "index": i,
                "last_leader_point": jsonable(ld.last_leader_point),
                "dogleg_vector": jsonable(ld.dogleg_vector),
                "dogleg_length": jsonable(ld.dogleg_length),
                "has_dogleg_vector": ld.has_dogleg_vector,
                "attachment_direction": ld.attachment_direction,
                "lines": [
                    {
                        "index": j,
                        "color": describe_raw_color(ln.color),
                        "vertices": jsonable(ln.vertices),
                    }
                    for j, ln in enumerate(ld.lines)
                ],
            }
            for i, ld in enumerate(ctx.leaders)
        ]
        d["context"] = c
    else:
        d["vertices"] = jsonable(list(e.vertices))
        ovr = e.override()
        d["dimstyle_overrides"] = jsonable(dict(ovr.dimstyle_attribs))
        ann = leader_annotation(e)
        if ann is not None:
            d["annotation_dxf"] = jsonable(ann.dxf.all_existing_dxf_attribs())
    return d


# --------------------------------------------------------------------------
# Setting values
# --------------------------------------------------------------------------

def set_text(e: DXFEntity, value: str) -> tuple[Any, Any]:
    old = get_raw_text(e)
    if isinstance(e, MultiLeader):
        if e.has_block_content:
            content = value if isinstance(value, dict) else json.loads(value)
            e.set_block_content(content)
        elif e.has_mtext_content:
            e.set_mtext_content(value)
        else:
            raise ToolError(f"{e.dxf.handle}: MULTILEADER has no text content")
    else:
        ann = leader_annotation(e)
        if ann is None:
            raise ToolError(f"{e.dxf.handle}: LEADER has no annotation entity")
        if ann.dxftype() == "MTEXT":
            ann.text = value
        elif ann.dxf.hasattr("text"):
            ann.dxf.text = value
        else:
            raise ToolError(f"{e.dxf.handle}: annotation {ann.dxftype()} has no text")
    return old, get_raw_text(e)


def resolve_table_handle(doc: Drawing, table: str, name: str) -> str:
    if table == "mleaderstyle":
        style = doc.mleader_styles.get(name)
        if style is None:
            raise ToolError(f"No MLEADERSTYLE named {name!r}")
        return style.dxf.handle
    if table == "style":
        if name not in doc.styles:
            raise ToolError(f"No text style named {name!r}")
        return doc.styles.get(name).dxf.handle
    if table == "linetype":
        if name not in doc.linetypes:
            raise ToolError(f"No linetype named {name!r}")
        return doc.linetypes.get(name).dxf.handle
    if table == "block":
        br = doc.block_records.get(name) if name in doc.block_records else None
        if br is None:
            raise ToolError(f"No block named {name!r}")
        return br.dxf.handle
    raise ToolError(f"Unknown table {table}")


def set_path(doc: Drawing, e: DXFEntity, path: str, value: Any) -> tuple[Any, Any]:
    """Set one property. Returns (old, new). See `paths` command for syntax."""
    is_ml = isinstance(e, MultiLeader)

    if path == "text":
        return set_text(e, value if isinstance(value, (str, dict)) else str(value))

    if path in ("layer", "color", "linetype", "lineweight", "true_color"):
        path = f"dxf.{path}"

    if path == "style":
        if is_ml:
            h = resolve_table_handle(doc, "mleaderstyle", value)
            old = handle_name(doc, e.dxf.get("style_handle"))
            e.dxf.style_handle = h
            return old, value
        if value not in doc.dimstyles:
            raise ToolError(f"No DIMSTYLE named {value!r}")
        old = e.dxf.get("dimstyle")
        e.dxf.dimstyle = value
        return old, value

    if path == "text_style":
        h = resolve_table_handle(doc, "style", value)
        if is_ml:
            old = handle_name(doc, e.dxf.get("text_style_handle"))
            e.dxf.text_style_handle = h
            set_override_bit(e, "text_style_handle")
            if e.context.mtext is not None:
                e.context.mtext.style_handle = h
            return old, value
        ann = leader_annotation(e)
        if ann is None:
            raise ToolError("LEADER has no annotation")
        old = ann.dxf.get("style")
        ann.dxf.style = value
        return old, value

    if path == "text_color" and is_ml:
        raw = parse_color(value)
        old = describe_raw_color(e.dxf.get("text_color", colors.BY_BLOCK_RAW_VALUE))
        e.dxf.text_color = raw
        set_override_bit(e, "text_color")
        if e.context.mtext is not None:
            e.context.mtext.color = raw
        return old, describe_raw_color(raw)

    if path == "leader_color" and is_ml:
        raw = parse_color(value)
        old = describe_raw_color(e.dxf.get("leader_line_color", colors.BY_BLOCK_RAW_VALUE))
        e.dxf.leader_line_color = raw
        set_override_bit(e, "leader_line_color")
        for ld in e.context.leaders:
            for ln in ld.lines:
                ln.color = raw
        return old, describe_raw_color(raw)

    if path == "move" and is_ml:
        # Shift text and landings by [dx, dy] (model units); arrow tips stay put,
        # so the leader lines stretch to the new landing.
        d = Vec3(value)
        ctx = e.context
        old = jsonable(ctx.mtext.insert) if ctx.mtext else None
        if ctx.mtext is not None:
            ctx.mtext.insert = Vec3(ctx.mtext.insert) + d
        ctx.base_point = Vec3(ctx.base_point) + d
        for ld in ctx.leaders:
            ld.last_leader_point = Vec3(ld.last_leader_point) + d
        return old, jsonable(ctx.mtext.insert) if ctx.mtext else None

    if path == "scale" and is_ml:
        old = e.context.scale
        e.dxf.scale = float(value)
        e.context.scale = float(value)
        return old, float(value)

    if path.startswith("reset.") and is_ml:
        # Drop the per-entity override so the MLEADERSTYLE value applies again.
        name = path[6:]
        bit = MLEADER_OVERRIDE_BITS.get(name)
        if bit is None:
            raise ToolError(f"No override flag for {name!r}")
        style = doc.entitydb.get(e.dxf.get("style_handle"))
        old = jsonable(e.dxf.get(name))
        if style is not None and style.dxf.hasattr(name):
            e.dxf.set(name, style.dxf.get(name))
        flags = e.dxf.get("property_override_flags", 0) or 0
        e.dxf.property_override_flags = flags & ~(1 << bit)
        return old, jsonable(e.dxf.get(name))

    if path == "text_height":
        if is_ml:
            old = e.context.char_height
            e.context.char_height = float(value)
            return old, float(value)
        ann = leader_annotation(e)
        if ann is None or not ann.dxf.is_supported("char_height"):
            raise ToolError("LEADER annotation has no char_height")
        old = ann.dxf.get("char_height")
        ann.dxf.char_height = float(value)
        return old, float(value)

    if path == "arrow_size":
        if is_ml:
            old = e.context.arrow_head_size
            e.context.arrow_head_size = float(value)
            e.dxf.arrow_head_size = float(value)
            set_override_bit(e, "arrow_head_size")
            return old, float(value)
        path = "dimvar.dimasz"

    if path == "arrow_block":
        if is_ml:
            old = handle_name(doc, e.dxf.get("arrow_head_handle"))
            if value in ("", None, "closed_filled"):
                e.dxf.discard("arrow_head_handle")
            else:
                e.dxf.arrow_head_handle = resolve_table_handle(doc, "block", value)
            set_override_bit(e, "arrow_head_handle")
            return old, value
        path = "dimvar.dimldrblk"

    if path == "vertices" and not is_ml:
        old = jsonable(list(e.vertices))
        e.set_vertices([Vec3(v) for v in value])
        return old, jsonable(list(e.vertices))

    m = re.fullmatch(r"vertex\.(\d+)", path)
    if m and not is_ml:
        verts = list(e.vertices)
        i = int(m.group(1))
        old = jsonable(verts[i])
        verts[i] = Vec3(value)
        e.set_vertices(verts)
        return old, jsonable(verts[i])

    if path.startswith("dimvar.") and not is_ml:
        name = path.split(".", 1)[1]
        ovr = e.override()
        old = ovr.get(name)
        if name == "dimldrblk":
            ovr.set_arrows(ldrblk=value)
        else:
            ovr[name] = value
        ovr.commit()
        return jsonable(old), jsonable(e.override().get(name))

    if path.startswith("annotation.") and not is_ml:
        ann = leader_annotation(e)
        if ann is None:
            raise ToolError("LEADER has no annotation")
        name = path.split(".", 1)[1].removeprefix("dxf.")
        return set_dxf(ann, name, value)

    if path.startswith("dxf."):
        name = path[4:]
        old, new = set_dxf(e, name, value)
        if is_ml:
            set_override_bit(e, name)
        return old, new

    if is_ml:
        ctx = e.context
        if path.startswith("mtext."):
            if ctx.mtext is None:
                raise ToolError("MULTILEADER has no MTEXT content")
            return set_obj_attr(ctx.mtext, path[6:], value)
        if path.startswith("context."):
            return set_obj_attr(ctx, path[8:], value)
        m = re.fullmatch(r"leader\.(\d+)\.line\.(\d+)\.(vertices|color)", path)
        if m:
            line = ctx.leaders[int(m.group(1))].lines[int(m.group(2))]
            if m.group(3) == "color":
                old = describe_raw_color(line.color)
                line.color = parse_color(value)
                return old, describe_raw_color(line.color)
            old = jsonable(line.vertices)
            line.vertices = [Vec3(v) for v in value]
            return old, jsonable(line.vertices)
        m = re.fullmatch(r"leader\.(\d+)\.(\w+)", path)
        if m:
            return set_obj_attr(ctx.leaders[int(m.group(1))], m.group(2), value)
        m = re.fullmatch(r"block\.(.+)", path)
        if m:
            old = e.get_block_content().get(m.group(1))
            e.set_block_content({m.group(1): str(value)})
            return old, e.get_block_content().get(m.group(1))

    raise ToolError(f"Unknown or unsupported path {path!r} for {e.dxftype()}")


def set_dxf(e: DXFEntity, name: str, value: Any) -> tuple[Any, Any]:
    if not e.dxf.is_supported(name):
        raise ToolError(f"{e.dxftype()} has no DXF attribute {name!r}")
    old = e.dxf.get(name)
    if name in RAW_COLOR_ATTRIBS:
        value = parse_color(value)
    elif name == "layer" and value not in e.doc.layers:
        e.doc.layers.add(value)
    else:
        value = coerce_like(old if old is not None else e.dxf.get_default(name), value)
    e.dxf.set(name, value)
    return jsonable(old), jsonable(e.dxf.get(name))


def set_obj_attr(obj: Any, name: str, value: Any) -> tuple[Any, Any]:
    if not hasattr(obj, name) or name.startswith("_"):
        raise ToolError(f"{type(obj).__name__} has no attribute {name!r}")
    old = getattr(obj, name)
    if name in ("color", "bg_color"):
        value = parse_color(value)
    else:
        value = coerce_like(old, value)
    setattr(obj, name, value)
    return jsonable(old), jsonable(getattr(obj, name))


def apply_edits(doc: Drawing, edits: list[dict]) -> list[dict]:
    """edits: [{"handle": "1A2B", "set": {"path": value, ...}}, ...]"""
    changes = []
    for edit in edits:
        note = doc.entitydb.get(edit["handle"].upper())
        if note is not None and note.dxftype() in ("MTEXT", "TEXT"):
            # Plain notes: only "text" is supported. MTEXT takes \P line breaks.
            for path, value in edit["set"].items():
                if path != "text":
                    raise ToolError(f"{note.dxftype()} {note.dxf.handle}: only 'text' can be set")
                if note.dxftype() == "MTEXT":
                    old = note.text
                    note.text = value
                else:
                    old = note.dxf.text
                    note.dxf.text = value.replace("\\P", " ")
                changes.append({"handle": note.dxf.handle, "path": "text", "old": old, "new": value})
            continue
        e = get_leader(doc, edit["handle"])
        for path, value in edit["set"].items():
            old, new = set_path(doc, e, path, value)
            if isinstance(e, MultiLeader):
                e.proxy_graphic = None
            changes.append({"handle": e.dxf.handle, "path": path, "old": old, "new": new})
    return changes


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def out(data: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(data, indent=2, ensure_ascii=False, default=str))
    else:
        print(data)


def filter_leaders(doc: Drawing, args) -> list[tuple[str, DXFEntity]]:
    rows = []
    rx = re.compile(args.contains, re.I) if getattr(args, "contains", None) else None
    for space, e in iter_leaders(doc, include_blocks=not args.no_blocks):
        if args.type == "leader" and e.dxftype() != "LEADER":
            continue
        if args.type == "mleader" and e.dxftype() == "LEADER":
            continue
        if args.layer and e.dxf.get("layer") != args.layer:
            continue
        if args.space and space != args.space:
            continue
        if rx and not rx.search(plain(get_raw_text(e)) or ""):
            continue
        rows.append((space, e))
    return rows


def cmd_open(args) -> None:
    dwg = Path(args.dwg).resolve()
    doc = open_working(dwg, fresh=args.fresh)
    counts = Counter(e.dxftype() for _, e in iter_leaders(doc))
    meta = load_meta(dwg)
    out({
        "dwg": str(dwg),
        "working_dxf": str(work_paths(dwg)["dxf"]),
        "dxfversion": doc.dxfversion,
        "leaders": dict(counts),
        "pending_changes": len(meta.get("changes", [])),
        "layouts": [l.name for l in doc.layouts],
    }, True)


def cmd_list(args) -> None:
    doc = open_working(Path(args.dwg).resolve())
    rows = [summary(doc, s, e) for s, e in filter_leaders(doc, args)]
    if args.json:
        out(rows, True)
        return
    for r in rows:
        text = (r["text"] or "").replace("\n", " | ")
        if len(text) > 70:
            text = text[:67] + "..."
        style = r.get("style") or r.get("dimstyle") or ""
        print(f"{r['handle']:>8}  {r['type']:<11} {r['space']:<14} {r['layer']:<20} {style:<16} {text}")
    print(f"-- {len(rows)} leader(s)")


def cmd_show(args) -> None:
    doc = open_working(Path(args.dwg).resolve())
    names = space_names(doc)
    result = []
    for h in args.handles:
        e = get_leader(doc, h)
        result.append(detail(doc, names.get(e.dxf.owner, "?"), e))
    out(result if len(result) > 1 else result[0], True)


def cmd_set(args) -> None:
    dwg = Path(args.dwg).resolve()
    doc = open_working(dwg)
    sets = {}
    for item in args.assignments:
        if "=" not in item:
            raise ToolError(f"Expected path=value, got {item!r}")
        k, v = item.split("=", 1)
        sets[k] = v if k == "text" and not args.parse_text else parse_value(v)
    if "text" in sets and args.newlines:
        sets["text"] = sets["text"].replace("\n", "\\P").replace("\\n", "\\P")
    edits = [{"handle": h, "set": sets} for h in args.handles]
    changes = apply_edits(doc, edits)
    if args.dry_run:
        out({"dry_run": True, "changes": changes}, True)
        return
    commit(dwg, doc, changes)
    out({"changes": changes}, True)


def cmd_apply(args) -> None:
    dwg = Path(args.dwg).resolve()
    doc = open_working(dwg)
    edits = json.loads(Path(args.edits).read_text()) if args.edits != "-" else json.load(sys.stdin)
    changes = apply_edits(doc, edits)
    if args.dry_run:
        out({"dry_run": True, "changes": changes}, True)
        return
    commit(dwg, doc, changes)
    out({"changes": changes}, True)


def cmd_replace(args) -> None:
    """Find/replace inside leader text. Works on the raw MTEXT string."""
    dwg = Path(args.dwg).resolve()
    doc = open_working(dwg)
    flags = 0 if args.case else re.I
    pattern = re.compile(args.find if args.regex else re.escape(args.find), flags)
    edits = []
    for space, e in filter_leaders(doc, args):
        if isinstance(e, MultiLeader) and e.has_block_content:
            continue
        raw = get_raw_text(e)
        if raw is None or not pattern.search(raw):
            continue
        new = pattern.sub(args.replace, raw)
        if new != raw:
            edits.append({"handle": e.dxf.handle, "set": {"text": new}})
    changes = apply_edits(doc, edits)
    if args.dry_run:
        out({"dry_run": True, "changes": changes}, True)
        return
    commit(dwg, doc, changes)
    out({"changes": changes}, True)


STANDARD_SCALES = [1, 2, 5, 10, 20, 25, 40, 50, 75, 100, 200, 250, 500, 1000]
# Leader scale for a viewport scale when it is not the viewport scale itself.
# 1:100 notes plot at half size (1.25 mm); full size crowds those views.
LEADER_SCALE_FOR_VIEWPORT = {100: 50}


def viewport_scales(doc: Drawing) -> list[tuple[str, Vec2, float, float, float]]:
    """(sheet, model centre, model width, model height, scale) per sheet viewport."""
    out_ = []
    for lay in doc.layouts:
        if lay.name == "Model":
            continue
        for vp in lay.query("VIEWPORT"):
            if vp.dxf.get("status", 0) == 1 or not vp.dxf.height:
                continue
            h = vp.dxf.view_height
            scale = h / vp.dxf.height
            out_.append((lay.name, Vec2(vp.dxf.view_center_point), h * vp.dxf.width / vp.dxf.height, h, scale))
    return out_


def leader_model_point(doc: Drawing, space: str, e: DXFEntity) -> Vec2 | None:
    """Arrow point in model space, or None if the leader is not shown in model space."""
    pts = arrow_points(e)
    p = Vec2(pts[0]) if pts else Vec2(e.context.base_point)
    if space == "Model":
        return p
    if space.startswith("block:"):
        ins = next((i for i in doc.modelspace().query("INSERT") if i.dxf.name == space[6:]), None)
        return Vec2(ins.matrix44().transform(p.vec3)) if ins else None
    return None


def viewport_scale_for(doc: Drawing, space: str, e: DXFEntity, vps) -> float | None:
    """Scale of the most detailed viewport showing the leader, snapped to a standard scale."""
    p = leader_model_point(doc, space, e)
    if p is None:
        return None
    hits = [s for _, c, w, h, s in vps if abs(p.x - c.x) <= w / 2 and abs(p.y - c.y) <= h / 2]
    if not hits:
        return None
    s = min(hits)
    return min(STANDARD_SCALES, key=lambda k: abs(k - s) / k)


def normalize_edits(doc: Drawing, rows, style_name: str | None, keep: str) -> list[dict]:
    """Edits that put each MULTILEADER on style defaults and use only `scale`.

    keep="text"/"arrow": the new scale keeps the on-screen text height / arrow
    size. keep="viewport": scale = scale of the sheet viewport showing the
    leader (or its LEADER_SCALE_FOR_VIEWPORT entry, 1:100 -> 50), so notes
    plot at the style's text height, half that on 1:100 (leaders on no sheet
    are skipped). Text height, arrow size and landing gap become
    style value x scale. Geometry (text position, landing lengths) is untouched.
    """
    vps = viewport_scales(doc) if keep == "viewport" else None
    target = doc.mleader_styles.get(style_name) if style_name else None
    if style_name and target is None:
        raise ToolError(f"No MLEADERSTYLE named {style_name!r}")
    edits = []
    for space, e in rows:
        if not isinstance(e, MultiLeader) or not e.has_mtext_content:
            continue
        style = target or doc.entitydb.get(e.dxf.get("style_handle"))
        s = style.dxf
        ctx = e.context
        if keep == "viewport":
            scale = viewport_scale_for(doc, space, e, vps)
            if scale is None:
                continue  # not on any sheet: leave alone
            scale = LEADER_SCALE_FOR_VIEWPORT.get(scale, scale)
        elif keep == "text":
            scale = ctx.char_height / s.char_height
        else:
            scale = ctx.arrow_head_size / s.arrow_head_size
        scale = round(scale, 6)
        want = {
            "scale": scale,
            "context.char_height": round(s.char_height * scale, 6),
            "context.arrow_head_size": round(s.arrow_head_size * scale, 6),
            "context.landing_gap_size": round(s.landing_gap_size * scale, 6),
        }
        sets = {}
        if target is not None and e.dxf.get("style_handle") != target.dxf.handle:
            sets["style"] = style_name
        cur = {
            "scale": ctx.scale,
            "context.char_height": ctx.char_height,
            "context.arrow_head_size": ctx.arrow_head_size,
            "context.landing_gap_size": ctx.landing_gap_size,
        }
        for k, v in want.items():
            if abs(cur[k] - v) > 1e-6:
                sets[k] = v
        if ctx.mtext.width and abs(want["context.char_height"] - ctx.char_height) > 1e-6:
            # A fixed text column width would re-wrap the note; scale it with the text.
            sets["mtext.width"] = round(ctx.mtext.width * want["context.char_height"] / ctx.char_height, 6)
        flags = e.dxf.get("property_override_flags", 0) or 0
        if flags & (1 << MLEADER_OVERRIDE_BITS["arrow_head_size"]) or sets.get("style"):
            sets["reset.arrow_head_size"] = True
        if sets:
            edits.append({"handle": e.dxf.handle, "set": sets})
    return edits


def cmd_normalize(args) -> None:
    dwg = Path(args.dwg).resolve()
    doc = open_working(dwg)
    args.type = "mleader"
    edits = normalize_edits(doc, filter_leaders(doc, args), args.style, args.keep)
    changes = apply_edits(doc, edits)
    summary_rows = Counter(
        (c["path"], json.dumps(c["old"]), json.dumps(c["new"]))
        for c in changes if c["path"] in ("scale", "style")
    )
    result = {
        "leaders_changed": len(edits),
        "scale_and_style_changes": [
            {"path": p, "old": json.loads(o), "new": json.loads(n), "count": cnt}
            for (p, o, n), cnt in sorted(summary_rows.items(), key=lambda kv: -kv[1])
        ],
        "total_property_changes": len(changes),
    }
    if args.dry_run:
        result["dry_run"] = True
        out(result, True)
        return
    commit(dwg, doc, changes)
    out(result, True)


def cmd_mstyle(args) -> None:
    """Show or change MLEADERSTYLE defaults. Leaders pick the new values up
    through `normalize`; nothing on existing leaders changes here."""
    dwg = Path(args.dwg).resolve()
    doc = open_working(dwg)
    style = doc.mleader_styles.get(args.name)
    if style is None:
        raise ToolError(f"No MLEADERSTYLE named {args.name!r}")
    changes = []
    for item in args.set or []:
        k, v = item.split("=", 1)
        if not style.dxf.is_supported(k):
            raise ToolError(f"MLEADERSTYLE has no attribute {k!r}")
        old = style.dxf.get(k)
        style.dxf.set(k, coerce_like(old, parse_value(v)))
        changes.append({"handle": style.dxf.handle, "path": f"mstyle.{k}", "old": jsonable(old), "new": jsonable(style.dxf.get(k))})
    if changes:
        commit(dwg, doc, changes)
    out({"style": args.name, "changes": changes, "dxf": jsonable(style.dxf.all_existing_dxf_attribs())}, True)


def cmd_styles(args) -> None:
    doc = open_working(Path(args.dwg).resolve())
    ml = []
    for _, s in doc.mleader_styles:
        a = s.dxf
        ml.append({
            "name": a.get("name"),
            "handle": a.handle,
            "text_style": handle_name(doc, a.get("text_style_handle")),
            "char_height": a.get("char_height"),
            "arrow_head": handle_name(doc, a.get("arrow_head_handle")) or "closed_filled",
            "arrow_head_size": a.get("arrow_head_size"),
            "leader_type": a.get("leader_type"),
            "text_color": describe_raw_color(a.get("text_color", colors.BY_BLOCK_RAW_VALUE)),
            "leader_line_color": describe_raw_color(a.get("leader_line_color", colors.BY_BLOCK_RAW_VALUE)),
            "dogleg_length": a.get("dogleg_length"),
            "landing_gap_size": a.get("landing_gap_size"),
        })
    data = {
        "mleader_styles": ml,
        "dimstyles": [d.dxf.name for d in doc.dimstyles],
        "text_styles": [t.dxf.name for t in doc.styles],
        "layers": sorted(l.dxf.name for l in doc.layers),
    }
    out(data, True)


def cmd_dump(args) -> None:
    doc = open_working(Path(args.dwg).resolve())
    rows = [detail(doc, s, e) for s, e in filter_leaders(doc, args)]
    text = json.dumps(rows, indent=2, ensure_ascii=False, default=str)
    if args.output:
        Path(args.output).write_text(text)
        print(f"Wrote {len(rows)} leader(s) to {args.output}")
    else:
        print(text)


def cmd_changes(args) -> None:
    out(load_meta(Path(args.dwg).resolve()).get("changes", []), True)


def cmd_reset(args) -> None:
    dwg = Path(args.dwg).resolve()
    paths = work_paths(dwg)
    for p in (paths["dxf"], paths["meta"]):
        p.unlink(missing_ok=True)
    print("Working copy discarded.")


def leader_signature(e: DXFEntity) -> tuple:
    sig = (e.dxf.get("layer"), get_raw_text(e))
    if isinstance(e, MultiLeader):
        c = e.context
        sig += (e.dxf.get("style_handle"), round(c.scale, 6), round(c.char_height, 6),
                round(c.arrow_head_size, 6), round(c.landing_gap_size, 6))
    return sig


def entity_counts(doc: Drawing) -> Counter:
    return Counter(e.dxftype() for e in doc.entitydb.values() if e.is_alive)


def cmd_save(args) -> None:
    dwg = Path(args.dwg).resolve()
    paths = work_paths(dwg)
    if not paths["dxf"].is_file():
        raise ToolError("No working copy. Nothing to save.")
    meta = load_meta(dwg)
    setup_oda()
    doc = ezdxf.readfile(paths["dxf"])

    if args.overwrite:
        target = dwg
    else:
        target = Path(args.output).resolve() if args.output else dwg.with_name(f"{dwg.stem}-edited.dwg")
    # AutoCAD keeps <name>.dwl while a drawing is open; saving it there later
    # would silently overwrite whatever we write now.
    if target.with_suffix(".dwl").exists() and not args.force:
        raise ToolError(f"{target.name} is open in AutoCAD ({target.stem}.dwl exists). Close it first.")
    backup = None
    if args.overwrite:
        backup = paths["dir"] / f"{dwg.stem}.backup-{time.strftime('%Y%m%d-%H%M%S')}.dwg"
        shutil.copy2(dwg, backup)

    tmp_target = paths["dir"] / f"{target.stem}.saving.dwg"
    odafc.export_dwg(doc, str(tmp_target), version=args.version, replace=True)
    if not tmp_target.is_file():
        raise ToolError("ODA did not produce a DWG file.")

    # Verify: read the new DWG back and compare with the working copy.
    check = odafc.readfile(str(tmp_target))
    before, after = entity_counts(doc), entity_counts(check)
    lost = {k: before[k] - after[k] for k in before if after[k] < before[k]}
    mismatched = []
    for _, a in iter_leaders(doc):
        b = check.entitydb.get(a.dxf.handle)
        if b is None or leader_signature(a) != leader_signature(b):
            mismatched.append(a.dxf.handle)

    if (lost or mismatched) and not args.force:
        raise ToolError(
            f"Verification failed. Lost entities: {lost}. Leader mismatch: {mismatched[:20]}. "
            f"Unverified file left at {tmp_target}. Use --force to save anyway."
        )
    shutil.move(str(tmp_target), str(target))

    if args.overwrite:
        meta["source_mtime"] = target.stat().st_mtime
        meta["changes"] = []
        save_meta(dwg, meta)
    out({
        "saved": str(target),
        "backup": str(backup) if backup else None,
        "changes_written": len(meta.get("changes", [])) if not args.overwrite else "all",
        "lost_entities": lost,
        "text_mismatch": mismatched,
    }, True)


def sheets_for(doc: Drawing, p: Vec2) -> list[str]:
    """Paper space layouts whose viewports show model space point `p`."""
    hits = []
    for lay in doc.layouts:
        if lay.name == "Model":
            continue
        for vp in lay.query("VIEWPORT"):
            if vp.dxf.get("status", 0) == 1:  # the layout's own overall viewport
                continue
            c = Vec2(vp.dxf.view_center_point)
            h = vp.dxf.view_height
            w = h * vp.dxf.width / vp.dxf.height if vp.dxf.height else h
            if vp.dxf.get("view_twist_angle", 0):
                w = h = max(w, h)
            if abs(p.x - c.x) <= w / 2 and abs(p.y - c.y) <= h / 2:
                hits.append(lay.name)
                break
    return hits


def cmd_render(args) -> None:
    """Render a crop around each leader to PNG, leader in blue, circle at arrow.

    Works on an in-memory copy: proxy graphics are dropped and leader colors
    forced to ByLayer so they draw visibly. Nothing is saved.
    """
    from ezdxf import bbox
    from ezdxf.math import BoundingBox2d
    from ezdxf.addons.drawing import Frontend, RenderContext, svg, layout, config

    import logging
    logging.getLogger("ezdxf").setLevel(logging.ERROR)  # MULTILEADER linetype handle 0 floods warnings
    rsvg = shutil.which("rsvg-convert")
    if not rsvg:
        raise ToolError("rsvg-convert not found. Install it with: brew install librsvg")
    dwg = Path(args.dwg).resolve()
    doc = open_working(dwg)
    out_dir = Path(args.output).resolve() if args.output else dwg.parent / "renders"
    out_dir.mkdir(exist_ok=True)

    names = space_names(doc)
    if args.handles:
        targets = [(names.get(get_leader(doc, h).dxf.owner, "?"), get_leader(doc, h)) for h in args.handles]
    else:
        targets = filter_leaders(doc, args)
    if not targets:
        raise ToolError("No leaders to render.")

    for _, e in iter_leaders(doc):
        if isinstance(e, MultiLeader):
            e.proxy_graphic = None  # ODA proxy graphics render white
            e.dxf.leader_line_color = colors.BY_LAYER_RAW_VALUE
            for ld in e.context.leaders:
                for ln in ld.lines:
                    ln.color = colors.BY_LAYER_RAW_VALUE

    def paint(e, raw):
        if isinstance(e, MultiLeader):
            e.dxf.leader_line_color = raw
            e.dxf.text_color = raw
            if e.context.mtext is not None:
                e.context.mtext.color = raw
            for ld in e.context.leaders:
                for ln in ld.lines:
                    ln.color = raw
        else:
            e.dxf.color = 5 if raw != colors.BY_LAYER_RAW_VALUE else 256

    box_cache: dict[str, list] = {}

    def boxes(lay):
        if lay.name not in box_cache:
            items = []
            cache = bbox.Cache()
            for x in lay:
                try:
                    b = bbox.extents([x], fast=True, cache=cache)
                except Exception:
                    continue
                if b.has_data:
                    items.append((x, BoundingBox2d([Vec2(b.extmin), Vec2(b.extmax)])))
            box_cache[lay.name] = items
        return box_cache[lay.name]

    cfg = config.Configuration(
        background_policy=config.BackgroundPolicy.WHITE,
        color_policy=config.ColorPolicy.COLOR_SWAP_BW,
        lineweight_scaling=0.6,
    )
    msp = doc.modelspace()
    report = []
    for space, e in targets:
        text = (plain(get_raw_text(e)) or "").strip()
        parts = list(e.virtual_entities()) if isinstance(e, MultiLeader) else [e, leader_annotation(e)]
        lb = bbox.extents([p for p in parts if p is not None], fast=False)
        corners = [Vec2(lb.extmin), Vec2(lb.extmax), Vec2(lb.extmin.x, lb.extmax.y), Vec2(lb.extmax.x, lb.extmin.y)]
        arrows = arrow_points(e)
        p = Vec2(arrows[0]) if arrows else (corners[0] + corners[1]) / 2

        if space.startswith("block:"):
            # Show the leader where the block is placed in model space.
            inserts = [i for i in msp.query("INSERT") if i.dxf.name == space[6:]]
            if not inserts:
                report.append({"handle": e.dxf.handle, "text": text, "skipped": f"{space} is not placed in model space"})
                continue
            m = inserts[0].matrix44()
            corners = [Vec2(m.transform(c.vec3)) for c in corners]
            p = Vec2(m.transform(p.vec3))
            lay = msp
        else:
            lay = doc.layouts.get(space)

        box = BoundingBox2d(corners)
        size = max(box.size.x, box.size.y)
        pad = max(size * 0.5, args.pad)
        char_h = e.context.char_height if isinstance(e, MultiLeader) else size * 0.05
        longest = max((len(x) for x in text.splitlines()), default=0)
        pad_x = pad + longest * char_h * 0.75  # ezdxf underestimates right-aligned MTEXT extents
        view = BoundingBox2d([box.extmin - Vec2(pad_x, pad), box.extmax + Vec2(pad_x, pad)])

        paint(e, colors.encode_raw_color(5))
        circle = lay.add_circle(p, radius=max(size * 0.06, 30), dxfattribs={"color": 5, "lineweight": 100, "layer": "0"})
        keep = {id(x) for x, b in boxes(lay) if b.has_intersection(view)}
        keep.add(id(circle))
        backend = svg.SVGBackend()
        Frontend(RenderContext(doc), backend, config=cfg).draw_layout(
            lay, filter_func=lambda x: x.dxf.handle is None or id(x) in keep
        )
        page = layout.Page(280, 280 * view.size.y / view.size.x, layout.Units.mm)
        svg_str = backend.get_string(page, render_box=view)
        lay.delete_entity(circle)
        paint(e, colors.BY_LAYER_RAW_VALUE)

        png = out_dir / f"{e.dxf.handle}.png"
        svg_file = out_dir / f"{e.dxf.handle}.svg"
        svg_file.write_text(svg_str)
        subprocess.run([rsvg, "-w", str(args.width), "-b", "white", "-o", str(png), str(svg_file)], check=True)
        svg_file.unlink()
        sheets = sheets_for(doc, p) if lay is msp else [space]
        report.append({"handle": e.dxf.handle, "image": str(png), "sheets": sheets, "space": space, "text": text})
    out(report, True)


LAYOUT_ATTRIBS_NOT_COPIED = {
    "handle", "owner", "name", "taborder", "block_record_handle", "viewport_handle", "extmin", "extmax",
}


def new_sheet(doc: Drawing, name: str, template: str, attribs: dict[str, str]):
    """Create paper space layout `name` with the plot settings and non-viewport
    content (title block, standard notes) of layout `template`."""
    if name in doc.layouts:
        raise ToolError(f"Sheet {name} already exists")
    src = doc.layouts.get(template) if template in doc.layouts else None
    if src is None or src.name == "Model":
        raise ToolError(f"No sheet named {template!r}")
    settings = {
        k: v for k, v in src.dxf_layout.dxf.all_existing_dxf_attribs().items()
        if k not in LAYOUT_ATTRIBS_NOT_COPIED
    }
    settings["taborder"] = src.dxf_layout.dxf.taborder + 1
    for lay in doc.layouts:  # make room in the tab order right after the template
        if lay.name != "Model" and lay.dxf_layout.dxf.taborder >= settings["taborder"]:
            lay.dxf_layout.dxf.taborder += 1
    sheet = doc.layouts.new(name, dxfattribs=settings)
    main = sheet.add_new_main_viewport()
    src_main = next((v for v in src.query("VIEWPORT") if v.dxf.get("status", 0) == 1), None)
    if src_main is not None:
        for k in ("center", "width", "height", "view_center_point", "view_height"):
            main.dxf.set(k, src_main.dxf.get(k))
    for e in src:
        if e.dxftype() == "VIEWPORT":
            continue
        copy = e.copy()
        sheet.add_entity(copy)
        if copy.dxftype() == "INSERT":
            for a in copy.attribs:
                if a.dxf.tag in attribs:
                    a.dxf.text = attribs[a.dxf.tag]
    return sheet


def move_sheet_after(doc: Drawing, name: str, after: str) -> None:
    sheets = sorted((l for l in doc.layouts if l.name != "Model"), key=lambda l: l.dxf_layout.dxf.taborder)
    moving = next(l for l in sheets if l.name == name)
    sheets.remove(moving)
    idx = next((i for i, l in enumerate(sheets) if l.name == after), None)
    if idx is None:
        raise ToolError(f"No sheet named {after!r}")
    sheets.insert(idx + 1, moving)
    for n, lay in enumerate(sheets, start=1):
        lay.dxf_layout.dxf.taborder = n


def cmd_rename_sheet(args) -> None:
    dwg = Path(args.dwg).resolve()
    doc = open_working(dwg)
    if args.old not in doc.layouts:
        raise ToolError(f"No sheet named {args.old!r}")
    if args.new in doc.layouts:
        raise ToolError(f"Sheet {args.new} already exists")
    doc.layouts.rename(args.old, args.new)
    lay = doc.layouts.get(args.new)
    for ins in lay.query("INSERT"):
        for a in ins.attribs:
            if a.dxf.tag == "SHEETNUMBER" and a.dxf.text == args.old:
                a.dxf.text = args.new
    if args.after:
        move_sheet_after(doc, args.new, args.after)
    commit(dwg, doc, [{"handle": "-", "path": "rename-sheet", "old": args.old, "new": args.new}])
    order = [l.name for l in sorted(doc.layouts, key=lambda l: l.dxf_layout.dxf.taborder)]
    out({"renamed": [args.old, args.new], "tab_order": order}, True)


def cmd_new_sheet(args) -> None:
    dwg = Path(args.dwg).resolve()
    doc = open_working(dwg)
    attribs = {"SHEETNUMBER": args.name}
    for item in args.set or []:
        k, v = item.split("=", 1)
        attribs[k] = v
    new_sheet(doc, args.name, args.template, attribs)
    commit(dwg, doc, [{"handle": "-", "path": f"new-sheet {args.name}", "old": None, "new": args.template}])
    out({"created": args.name, "template": args.template, "attribs": attribs}, True)


def sweep_table_orphans(doc: Drawing) -> list[dict]:
    """Remove table data objects left without a live owner after deleting an
    ACAD_TABLE (ODA drops them on export, which would fail save verification)."""
    removed = []
    for obj in list(doc.objects):
        if obj.dxftype() not in ("TABLECONTENT", "TABLEGEOMETRY"):
            continue
        owner = doc.entitydb.get(obj.dxf.get("owner", "0"))
        if owner is None or not owner.is_alive:
            removed.append({"handle": obj.dxf.handle, "path": "delete", "old": obj.dxftype(), "new": None})
            doc.objects.delete_entity(obj)
    return removed


def cmd_attrib(args) -> None:
    """Find/replace in block attribute values (e.g. SCHEDULE_LABEL tags), in
    every layout and block definition. --block limits it to one block name."""
    dwg = Path(args.dwg).resolve()
    doc = open_working(dwg)
    pattern = re.compile(args.find if args.regex else re.escape(args.find))
    changes = []
    for blk in doc.blocks:
        for ins in blk.query("INSERT"):
            if args.block and ins.dxf.name != args.block:
                continue
            for a in ins.attribs:
                if pattern.search(a.dxf.text):
                    new = pattern.sub(args.replace, a.dxf.text)
                    changes.append({"handle": a.dxf.handle, "path": f"attrib.{a.dxf.tag}",
                                    "old": a.dxf.text, "new": new, "block": ins.dxf.name})
                    a.dxf.text = new
    if not args.dry_run:
        commit(dwg, doc, changes)
    out({"dry_run": args.dry_run, "count": len(changes),
         "summary": Counter(f'{c["old"]} -> {c["new"]}' for c in changes)}, True)


def cmd_delete(args) -> None:
    dwg = Path(args.dwg).resolve()
    doc = open_working(dwg)
    changes = []
    for h in args.handles:
        e = doc.entitydb.get(h.upper())
        if e is None or not e.is_alive:
            raise ToolError(f"No entity with handle {h}")
        kind = e.dxftype()
        table_block = e.dxf.get("geometry") if kind == "ACAD_TABLE" else None
        doc.layouts.get_layout_for_entity(e).delete_entity(e)
        if table_block and table_block in doc.blocks:
            doc.blocks.delete_block(table_block, safe=False)  # the table's graphics
        changes.append({"handle": h.upper(), "path": "delete", "old": kind, "new": None})
    changes += sweep_table_orphans(doc)
    commit(dwg, doc, changes)
    out({"deleted": changes}, True)


def wrap_cell(text: str, max_chars: int) -> list[str]:
    import textwrap
    lines = []
    for para in str(text).split("\n"):
        lines.extend(textwrap.wrap(para, max(max_chars, 1), break_long_words=False) or [""])
    return lines


SCHEDULE_DEFAULTS = {
    "text_height": 2.5,      # paper mm
    "title_scale": 1.4,
    "char_width": 0.9,       # average glyph width / text height, for sizing and wrapping
    "max_width": 380,        # usable sheet width above the title block
    "layer": "ds_text",
    "text_style": "Standard",
    "origin": [0, 280],      # top-left of the first table on a sheet
    "bottom": 30,            # lowest y a table may reach (top of title block strip)
    "gap": 12,               # vertical gap between stacked tables
}


def read_csv(path: Path) -> tuple[list[str], list[list[str]]]:
    import csv
    with open(path, newline="") as f:
        rows = list(csv.reader(f))
    if not rows:
        raise ToolError(f"{path} is empty")
    header = rows[0]
    data = [(r + [""] * len(header))[: len(header)] for r in rows[1:] if any(c.strip() for c in r)]
    return header, data


def write_csv(path: Path, header: list[str], data: list[list[str]]) -> None:
    import csv
    with open(path, "w", newline="") as f:
        csv.writer(f).writerows([header] + data)


def draw_table(doc: Drawing, spec: dict, opt: dict) -> dict:
    """Draw the CSV in `spec` into block spec["block"] (rebuilt from scratch).
    Block origin is the table's top-left corner; the table grows downward."""
    header, data = read_csv(spec["csv_path"])
    if spec.get("columns"):
        missing = [c for c in spec["columns"] if c not in header]
        if missing:
            raise ToolError(f"{spec['id']}: columns not in CSV: {missing}")
        idx = [header.index(c) for c in spec["columns"]]
        header = list(spec["columns"])
        data = [[r[i] for i in idx] for r in data]

    h = opt["text_height"]
    char_w = h * opt["char_width"]
    margin = h * 0.8
    line_gap = h * 5 / 3  # MTEXT default line spacing
    slack = h * 0.6  # room for wide glyphs (M, W) the average width misses
    ncols = len(header)

    def natural(i):
        return max(len(x) for x in [header[i]] + [r[i] for r in data]) * char_w + 2 * margin + slack

    def min_width(i):
        words = [w for x in [header[i]] + [r[i] for r in data] for w in x.split()] or [""]
        return max(len(w) for w in words) * char_w + 2 * margin + slack

    if spec.get("widths"):
        widths = [float(w) for w in spec["widths"]]
        if len(widths) != ncols:
            raise ToolError(f"{spec['id']}: widths needs {ncols} values")
    else:
        nat = [natural(i) for i in range(ncols)]
        floor = [min(min_width(i), nat[i]) for i in range(ncols)]
        if sum(nat) <= opt["max_width"]:
            widths = nat
        else:
            # Water-fill: every column starts at its longest word; spare width is
            # shared equally among columns that still want more, so no single
            # long column (e.g. NOTES) gets squeezed to one word per line.
            widths = list(floor)
            spare = opt["max_width"] - sum(widths)
            while spare > 0.01:
                hungry = [j for j in range(ncols) if nat[j] - widths[j] > 0.01]
                if not hungry:
                    break
                step = min(spare / len(hungry), min(nat[j] - widths[j] for j in hungry))
                for j in hungry:
                    widths[j] += step
                spare -= step * len(hungry)
        widths = [round(w + 0.49) for w in widths]
        while sum(widths) > opt["max_width"] and max(w - f for w, f in zip(widths, floor)) >= 1:
            i = max(range(ncols), key=lambda j: widths[j] - floor[j])  # undo rounding overshoot
            widths[i] -= 1

    cells = [[wrap_cell(r[c], int((widths[c] - 2 * margin - slack) / char_w)) for c in range(ncols)] for r in [header] + data]
    heights = [max(len(c) for c in row) * line_gap + 2 * margin - (line_gap - h) for row in cells]
    title = spec.get("title", "")
    title_h = h * opt["title_scale"]
    title_row = title_h + 2 * margin if title else 0

    layer = opt["layer"] if opt["layer"] in doc.layers else "0"
    if spec["block"] in doc.blocks:
        blk = doc.blocks.get(spec["block"])
        for e in list(blk):
            blk.delete_entity(e)
    else:
        blk = doc.blocks.new(spec["block"])
    attrs = {"layer": layer, "color": 256}
    total_w, total_h = sum(widths), title_row + sum(heights)

    xs = [0.0]
    for w in widths:
        xs.append(xs[-1] + w)
    ys = [0.0] + ([-title_row] if title else [])
    for rh in heights:
        ys.append(ys[-1] - rh)
    for y in ys:
        blk.add_line((0, y), (total_w, y), dxfattribs=attrs)
    for x in xs:
        top = -title_row if (title and 0 < x < total_w) else 0.0
        blk.add_line((x, top), (x, -total_h), dxfattribs=attrs)

    text_attrs = {**attrs, "style": opt["text_style"]}
    if title:
        blk.add_mtext(title, dxfattribs={**text_attrs, "char_height": title_h}).set_location(
            (margin, -margin), attachment_point=1)
    y = -title_row
    for r, row in enumerate(cells):
        for c, lines in enumerate(row):
            text = "\\P".join(lines)
            if not text.strip():
                continue
            if r == 0:
                text = "{\\L" + text + "}"  # underline the header row
            blk.add_mtext(text, dxfattribs={**text_attrs, "char_height": h}).set_location(
                (xs[c] + margin, y - margin), attachment_point=1)
        y -= heights[r]
    return {"rows": len(data), "widths": widths, "size": (total_w, total_h), "layer": layer}


def load_config(path: str) -> tuple[Path, dict]:
    cfg_path = Path(path).resolve()
    if not cfg_path.is_file():
        raise ToolError(f"No schedule config at {cfg_path}")
    cfg = json.loads(cfg_path.read_text())
    for spec in cfg["schedules"]:
        spec["csv_path"] = (cfg_path.parent / spec["csv"]).resolve()
    return cfg_path, cfg


def get_spec(cfg: dict, sid: str) -> dict:
    for spec in cfg["schedules"]:
        if spec["id"] == sid:
            return spec
    raise ToolError(f"No schedule {sid!r}. Have: {[s['id'] for s in cfg['schedules']]}")


def cmd_schedules(args) -> None:
    """Build every schedule in the config onto its sheet.

    Sheets that do not exist are created from cfg["sheet_template"] and placed
    after cfg["sheets_after"]. Tables already placed on their sheet keep their
    position; new ones are stacked below the others on that sheet.
    """
    dwg = Path(args.dwg).resolve()
    doc = open_working(dwg)
    _, cfg = load_config(args.config)
    opt = {**SCHEDULE_DEFAULTS, **cfg.get("defaults", {})}
    specs = [s for s in cfg["schedules"] if not args.only or s["id"] in args.only]

    last_sheet = cfg.get("sheets_after")
    for sheet_name in dict.fromkeys(s["sheet"] for s in cfg["schedules"]):
        if sheet_name not in doc.layouts:
            if not cfg.get("sheet_template"):
                raise ToolError(f"No sheet {sheet_name!r} and no sheet_template in config")
            new_sheet(doc, sheet_name, cfg["sheet_template"],
                      {"SHEETNUMBER": sheet_name, **cfg.get("sheet_attribs", {})})
            if last_sheet:
                move_sheet_after(doc, sheet_name, last_sheet)
        last_sheet = sheet_name

    report = []
    built = {s["id"]: draw_table(doc, s, {**opt, **s.get("options", {})}) for s in specs}
    bottoms: dict[str, float] = {}  # lowest table edge so far, per sheet
    for spec in cfg["schedules"]:
        sheet = doc.layouts.get(spec["sheet"])
        for lay in doc.layouts:  # a schedule moved to another sheet: drop the old placement
            if lay.name not in ("Model", spec["sheet"]):
                for i in [i for i in lay.query("INSERT") if i.dxf.name == spec["block"]]:
                    lay.delete_entity(i)
        info = built.get(spec["id"])
        height = info["size"][1] if info else bbox_height(doc, spec["block"])
        placed = [i for i in sheet.query("INSERT") if i.dxf.name == spec["block"]]
        if placed:
            top = placed[0].dxf.insert.y
        else:
            top = bottoms.get(spec["sheet"], opt["origin"][1] + opt["gap"]) - opt["gap"]
            sheet.add_blockref(spec["block"], (opt["origin"][0], top), dxfattribs={"layer": opt["layer"] if opt["layer"] in doc.layers else "0"})
        bottoms[spec["sheet"]] = min(bottoms.get(spec["sheet"], 1e9), top - height)
        if info:
            row = {"id": spec["id"], "sheet": spec["sheet"], "rows": info["rows"],
                   "size_mm": [round(v, 1) for v in info["size"]], "top_y": round(top, 1)}
            if top - height < opt["bottom"]:
                row["warning"] = "runs into the title block; move it or split the sheet"
            report.append(row)
    commit(dwg, doc, [{"handle": "-", "path": "schedules", "old": None, "new": [r["id"] for r in report]}])
    out(report, True)


def bbox_height(doc: Drawing, block: str) -> float:
    from ezdxf import bbox
    if block not in doc.blocks:
        return 0.0
    b = bbox.extents(doc.blocks.get(block), fast=True)
    return b.size.y if b.has_data else 0.0


def parse_assignments(items: list[str], header: list[str]) -> dict[str, str]:
    out_ = {}
    for item in items or []:
        if "=" not in item:
            raise ToolError(f"Expected COLUMN=VALUE, got {item!r}")
        k, v = item.split("=", 1)
        match = [c for c in header if c.upper() == k.strip().upper()]
        if not match:
            raise ToolError(f"No column {k!r}. Columns: {header}")
        out_[match[0]] = v
    return out_


def cmd_table(args) -> None:
    """Edit schedule CSVs by row key (first column). No DWG involved; run
    `schedules` afterwards to redraw."""
    _, cfg = load_config(args.config)
    if args.action == "list":
        out([{"id": s["id"], "title": s.get("title"), "sheet": s["sheet"], "csv": s["csv"],
              "rows": len(read_csv(s["csv_path"])[1])} for s in cfg["schedules"]], True)
        return
    spec = get_spec(cfg, args.id)
    header, data = read_csv(spec["csv_path"])
    key = header[0]
    find = lambda k: next((i for i, r in enumerate(data) if r[0].upper() == k.upper()), None)
    if args.action == "show":
        out([dict(zip(header, r)) for r in data if not args.key or r[0].upper() == args.key.upper()], True)
        return
    if args.action == "set":
        i = find(args.key)
        if i is None:
            raise ToolError(f"No row with {key} = {args.key}")
        old = dict(zip(header, data[i]))
        for col, val in parse_assignments(args.values, header).items():
            data[i][header.index(col)] = val
        result = {"old": old, "new": dict(zip(header, data[i]))}
    elif args.action == "add":
        if find(args.key) is not None:
            raise ToolError(f"Row {args.key} already exists")
        row = [""] * len(header)
        row[0] = args.key
        for col, val in parse_assignments(args.values, header).items():
            row[header.index(col)] = val
        pos = len(data) if not args.after else (find(args.after) + 1 if find(args.after) is not None else len(data))
        data.insert(pos, row)
        result = {"added": dict(zip(header, row))}
    elif args.action == "remove":
        i = find(args.key)
        if i is None:
            raise ToolError(f"No row with {key} = {args.key}")
        result = {"removed": dict(zip(header, data.pop(i)))}
    elif args.action == "rename":
        i = find(args.key)
        if i is None:
            raise ToolError(f"No row with {key} = {args.key}")
        data[i][0] = args.values[0]
        result = {"renamed": [args.key, args.values[0]]}
    write_csv(spec["csv_path"], header, data)
    out(result, True)


def cmd_paths(args) -> None:
    print(PATHS_HELP)


PATHS_HELP = """\
Paths for `set` (path=value). Values are parsed as JSON when possible
(numbers, [x,y,z] lists, true/false), else used as plain strings.

Both types:
  text            Leader text (raw MTEXT: use \\P for new line, or pass --newlines)
  layer, color, linetype, lineweight, true_color
  style           MULTILEADER: MLEADERSTYLE name.  LEADER: DIMSTYLE name.
  text_style      Text style name
  text_height     Text height
  arrow_size      Arrowhead size
  arrow_block     Arrowhead block name (e.g. _DOT, _ARCHTICK, closed_filled)
  dxf.<attr>      Any raw DXF attribute (see `show` -> "dxf")

LEADER only:
  vertices        [[x,y,z], ...]  (first vertex = arrow point)
  vertex.<i>      [x,y,z]
  dimvar.<name>   Dimstyle override, e.g. dimvar.dimasz=2.5, dimvar.dimclrd=1
  annotation.<attr>  DXF attr of the linked MTEXT, e.g. annotation.insert=[x,y,0]

MULTILEADER only:
  text_color, leader_color   ACI int, "bylayer", "byblock", "#rrggbb" or [r,g,b]
  move            [dx,dy] shift text + landings (model units); arrow tips stay
  scale           Overall scale (sets entity and context scale)
  reset.<attr>    Drop an override so the style value applies, e.g. reset.arrow_head_size=1
  context.<attr>  e.g. context.char_height, context.base_point
  mtext.<attr>    e.g. mtext.insert, mtext.width, mtext.rotation (radians)
  leader.<i>.<attr>           e.g. leader.0.last_leader_point, leader.0.dogleg_length
  leader.<i>.line.<j>.vertices  [[x,y,z], ...]  (first vertex = arrow point)
  leader.<i>.line.<j>.color
  block.<TAG>     Attribute value for block-content multileaders

Note: moving text does not move the leader lines and vice versa. Move both.
"""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_filters(sp):
        sp.add_argument("--type", choices=["all", "leader", "mleader"], default="mleader")
        sp.add_argument("--layer")
        sp.add_argument("--space", help="Model, a layout name, or block:<name>")
        sp.add_argument("--contains", help="Regex matched against plain text (case-insensitive)")
        sp.add_argument("--no-blocks", action="store_true", help="Skip leaders inside block definitions")

    sp = sub.add_parser("open", help="Convert DWG to working copy (or reuse it) and print a summary")
    sp.add_argument("dwg")
    sp.add_argument("--fresh", action="store_true", help="Discard working copy and re-convert")
    sp.set_defaults(func=cmd_open)

    sp = sub.add_parser("list", help="List leaders")
    sp.add_argument("dwg")
    add_filters(sp)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_list)

    sp = sub.add_parser("show", help="Full details of leaders by handle")
    sp.add_argument("dwg")
    sp.add_argument("handles", nargs="+")
    sp.set_defaults(func=cmd_show)

    sp = sub.add_parser("dump", help="Full details of all (filtered) leaders as JSON")
    sp.add_argument("dwg")
    add_filters(sp)
    sp.add_argument("-o", "--output")
    sp.set_defaults(func=cmd_dump)

    sp = sub.add_parser("set", help="Set properties: set FILE H1 [H2 ...] --to path=value ...")
    sp.add_argument("dwg")
    sp.add_argument("handles", nargs="+")
    sp.add_argument("--to", dest="assignments", nargs="+", required=True, metavar="PATH=VALUE")
    sp.add_argument("--newlines", action="store_true", help="Turn newlines / \\n in text into \\P")
    sp.add_argument("--parse-text", action="store_true", help="JSON-parse the text value (block content dict)")
    sp.add_argument("--dry-run", action="store_true")
    sp.set_defaults(func=cmd_set)

    sp = sub.add_parser("apply", help='Apply a JSON edit list: [{"handle": "..", "set": {path: value}}]')
    sp.add_argument("dwg")
    sp.add_argument("edits", help="JSON file, or - for stdin")
    sp.add_argument("--dry-run", action="store_true")
    sp.set_defaults(func=cmd_apply)

    sp = sub.add_parser("replace", help="Find/replace in leader text")
    sp.add_argument("dwg")
    sp.add_argument("find")
    sp.add_argument("replace")
    sp.add_argument("--regex", action="store_true")
    sp.add_argument("--case", action="store_true", help="Case-sensitive")
    sp.add_argument("--dry-run", action="store_true")
    add_filters(sp)
    sp.set_defaults(func=cmd_replace)

    sp = sub.add_parser("normalize", help="Reset MULTILEADER sizes to style defaults; size only via scale")
    sp.add_argument("dwg")
    sp.add_argument("--style", help="Also move every leader to this MLEADERSTYLE")
    sp.add_argument("--keep", choices=["text", "arrow", "viewport"], default="text",
                    help="Which on-screen size the new scale preserves")
    sp.add_argument("--dry-run", action="store_true")
    add_filters(sp)
    sp.set_defaults(func=cmd_normalize)

    sp = sub.add_parser("mstyle", help="Show or set MLEADERSTYLE values: mstyle FILE NAME --set char_height=2.5 ...")
    sp.add_argument("dwg")
    sp.add_argument("name")
    sp.add_argument("--set", nargs="*", metavar="ATTR=VALUE")
    sp.set_defaults(func=cmd_mstyle)

    sp = sub.add_parser("styles", help="List MLEADERSTYLEs, DIMSTYLEs, text styles, layers")
    sp.add_argument("dwg")
    sp.set_defaults(func=cmd_styles)

    sp = sub.add_parser("changes", help="List pending (unsaved) edits")
    sp.add_argument("dwg")
    sp.set_defaults(func=cmd_changes)

    sp = sub.add_parser("reset", help="Discard working copy and all pending edits")
    sp.add_argument("dwg")
    sp.set_defaults(func=cmd_reset)

    sp = sub.add_parser("save", help="Write working copy back to DWG (default: <name>-edited.dwg)")
    sp.add_argument("dwg")
    sp.add_argument("-o", "--output")
    sp.add_argument("--overwrite", action="store_true", help="Replace the original (a backup is kept)")
    sp.add_argument("--version", default=None, help="DWG version, e.g. R2018 (default: same as source)")
    sp.add_argument("--force", action="store_true", help="Save even if verification fails")
    sp.set_defaults(func=cmd_save)

    sp = sub.add_parser("render", help="PNG crop around leaders (by handle or filters), leader in blue")
    sp.add_argument("dwg")
    sp.add_argument("handles", nargs="*")
    sp.add_argument("-o", "--output", help="Output folder (default: renders/ next to the DWG)")
    sp.add_argument("--pad", type=float, default=500, help="Minimum margin around the leader, drawing units")
    sp.add_argument("--width", type=int, default=1600, help="PNG width in pixels")
    add_filters(sp)
    sp.set_defaults(func=cmd_render)

    sp = sub.add_parser("schedules", help="Build all schedule tables from a config onto their sheets")
    sp.add_argument("dwg")
    sp.add_argument("--config", default="schedules/schedules.json")
    sp.add_argument("--only", nargs="*", help="Rebuild only these schedule ids")
    sp.set_defaults(func=cmd_schedules)

    sp = sub.add_parser("table", help="Edit schedule CSVs: list | show ID [KEY] | set ID KEY COL=VAL... | "
                                      "add ID KEY [COL=VAL...] [--after KEY] | remove ID KEY | rename ID KEY NEWKEY")
    sp.add_argument("action", choices=["list", "show", "set", "add", "remove", "rename"])
    sp.add_argument("id", nargs="?")
    sp.add_argument("key", nargs="?")
    sp.add_argument("values", nargs="*")
    sp.add_argument("--after")
    sp.add_argument("--config", default="schedules/schedules.json")
    sp.set_defaults(func=cmd_table)

    sp = sub.add_parser("new-sheet", help="Create a sheet from a template sheet (title block, notes; no viewports)")
    sp.add_argument("dwg")
    sp.add_argument("name")
    sp.add_argument("--template", required=True)
    sp.add_argument("--set", nargs="*", metavar="TAG=VALUE", help="Title block attributes")
    sp.set_defaults(func=cmd_new_sheet)

    sp = sub.add_parser("rename-sheet", help="Rename a sheet (and its SHEETNUMBER), optionally move its tab")
    sp.add_argument("dwg")
    sp.add_argument("old")
    sp.add_argument("new")
    sp.add_argument("--after", help="Put the tab right after this sheet")
    sp.set_defaults(func=cmd_rename_sheet)

    sp = sub.add_parser("attrib", help="Find/replace in block attribute values (tags), e.g. DR-01 -> DR01")
    sp.add_argument("dwg")
    sp.add_argument("find")
    sp.add_argument("replace")
    sp.add_argument("--block", help="Only INSERTs of this block, e.g. SCHEDULE_LABEL")
    sp.add_argument("--regex", action="store_true")
    sp.add_argument("--dry-run", action="store_true")
    sp.set_defaults(func=cmd_attrib)

    sp = sub.add_parser("delete",help="Delete entities by handle (ACAD_TABLE also drops its graphics block)")
    sp.add_argument("dwg")
    sp.add_argument("handles", nargs="+")
    sp.set_defaults(func=cmd_delete)

    sp = sub.add_parser("paths", help="Show the property paths accepted by `set`")
    sp.set_defaults(func=cmd_paths)
    return p


def main() -> int:
    args = build_parser().parse_args()
    try:
        args.func(args)
    except (ToolError, odafc.ODAFCError, FileNotFoundError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
