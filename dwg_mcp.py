#!/usr/bin/env python3
"""MCP server (stdio) that exposes dwg_edit.py to Claude Desktop.

Each tool runs dwg_edit.py as a subprocess, so the server holds no drawing
state: the working DXF in `.dwg_work/` is the only state, same as the CLI.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Literal

from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "dwg_edit.py"
# Relative DWG paths resolve against this folder.
DWG_HOME = Path(os.environ.get("DWG_HOME", HERE)).expanduser()
MAX_IMAGES = 8
# Claude Desktop starts servers with a minimal PATH; render needs Homebrew's rsvg-convert.
ENV = {**os.environ, "PATH": f"/opt/homebrew/bin:/usr/local/bin:{os.environ.get('PATH', '/usr/bin:/bin')}"}

READ = ToolAnnotations(read_only_hint=True)
EDIT = ToolAnnotations(read_only_hint=False, destructive_hint=False)
DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True)

INSTRUCTIONS = """\
Edits DWG drawings (MULTILEADERs, sheets, schedule tables) via a working DXF copy.
Edits never touch the DWG until dwg_save. Review with dwg_changes before saving.

House rules:
- The user only works with MULTILEADERs. Old-style LEADERs have no linked text.
- Every multileader uses MLEADERSTYLE `SebStyle` (2.5 text and arrow) sized only by `scale`, and
  scale = the scale of the sheet viewport it shows in, so every note plots at 2.5 mm.
  Exception: on 1:100 viewports scale = 50, so those notes plot at 1.25 mm.
  dwg_normalize(style="SebStyle", keep="viewport") enforces this (dry run first).
- To move a note use dwg_set with {"move": [dx, dy]} (model units; arrow tip stays). Check with
  dwg_render: bigger text can collide or get clipped at the viewport edge.
- Moving MULTILEADER text does not move its leader lines. Move both.
- Before dwg_save with overwrite=true, tell the user to close the drawing in AutoCAD without
  saving. A drawing left open and saved later silently undoes the edits.
- If output says "changed outside this tool", the working copy was reloaded; re-check earlier edits.
- Leaders inside unused anonymous blocks (A$C...) are invisible leftovers; ignore them.
- Use dwg_render to look at leaders before and after moving them.
- Window/skylight sizes come from the CAD (SCHEDULE_LABEL blocks), not the BASIX or Materials PDFs.
"""

mcp = MCPServer("dwg", instructions=INSTRUCTIONS)


def dwg_path(dwg: str) -> str:
    p = Path(dwg).expanduser()
    if not p.is_absolute():
        p = DWG_HOME / p
    if p.suffix.lower() != ".dwg":
        raise ToolError(f"Not a .dwg file: {p}")
    if not p.is_file():
        raise ToolError(f"No such file: {p}")
    return str(p)


def run(*args: str, stdin: str | None = None) -> str:
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        input=stdin, capture_output=True, text=True, cwd=DWG_HOME, env=ENV, timeout=900,
    )
    err = proc.stderr.strip()
    if proc.returncode != 0:
        raise ToolError(err or f"dwg_edit.py exited with {proc.returncode}")
    out = proc.stdout.strip()
    return f"{err}\n{out}" if err else out


def filter_args(type: str, contains: str | None, layer: str | None, space: str | None,
                include_blocks: bool) -> list[str]:
    a = ["--type", type]
    if contains:
        a += ["--contains", contains]
    if layer:
        a += ["--layer", layer]
    if space:
        a += ["--space", space]
    if not include_blocks:
        a.append("--no-blocks")
    return a


LeaderType = Literal["mleader", "leader", "all"]


@mcp.tool(annotations=READ)
def dwg_files(folder: str | None = None) -> str:
    """List DWG files in a folder (default: the DWG home folder)."""
    base = Path(folder).expanduser() if folder else DWG_HOME
    if not base.is_absolute():
        base = DWG_HOME / base
    files = sorted(p for p in base.glob("*.dwg"))
    return json.dumps({"folder": str(base), "files": [p.name for p in files]}, indent=2)


@mcp.tool(annotations=READ)
def dwg_open(dwg: str, fresh: bool = False) -> str:
    """Open a DWG (converts to a working copy or reuses it). Returns leader counts,
    layouts and pending change count. fresh=true discards the working copy and its edits."""
    return run("open", dwg_path(dwg), *(["--fresh"] if fresh else []))


@mcp.tool(annotations=READ)
def dwg_styles(dwg: str) -> str:
    """MLEADERSTYLEs (with sizes), DIMSTYLEs, text styles and layers."""
    return run("styles", dwg_path(dwg))


@mcp.tool(annotations=READ)
def dwg_list(dwg: str, contains: str | None = None, type: LeaderType = "mleader",
             layer: str | None = None, space: str | None = None, include_blocks: bool = True) -> str:
    """List leaders: handle, space, layer, style, plain text, arrow points.
    contains: case-insensitive regex on the text. space: "Model", a layout name or "block:<name>"."""
    return run("list", dwg_path(dwg), "--json", *filter_args(type, contains, layer, space, include_blocks))


@mcp.tool(annotations=READ)
def dwg_show(dwg: str, handles: list[str]) -> str:
    """Full properties of leaders by handle (DXF attribs, overrides, text position, leader line vertices)."""
    return run("show", dwg_path(dwg), *handles)


@mcp.tool(annotations=READ, structured_output=False)
def dwg_render(dwg: str, handles: list[str] | None = None, contains: str | None = None,
               pad: float = 500, width: int = 1200) -> list:
    """PNG crop around each leader (leader drawn blue, circle at the arrow), plus the sheets it
    appears on. Give handles, or a contains regex. At most 8 images per call.
    pad: minimum margin in drawing units. Renders the working copy, so it shows unsaved edits."""
    path = dwg_path(dwg)
    if handles:
        args = ["render", path, *handles]
    elif contains:
        args = ["render", path, "--contains", contains]
    else:
        raise ToolError("Give handles or contains.")
    report = _json_tail(run(*args, "--pad", str(pad), "--width", str(width)))
    result: list[Any] = [json.dumps(report, indent=2, ensure_ascii=False)]
    images = [r for r in report if r.get("image")]
    for r in images[:MAX_IMAGES]:
        result.append(f"{r['handle']}: {r.get('text', '')}")
        result.append(Image(path=r["image"]))
    if len(images) > MAX_IMAGES:
        result.append(f"{len(images) - MAX_IMAGES} more image(s) not shown; render them by handle.")
    return result


def _json_tail(text: str) -> Any:
    """Parse the JSON that follows any warning lines in `text`."""
    start = min((i for i in (text.find("["), text.find("{")) if i >= 0), default=-1)
    if start < 0:
        raise ToolError(text)
    return json.loads(text[start:])


@mcp.tool(annotations=READ)
def dwg_paths() -> str:
    """Property paths accepted by dwg_set / dwg_apply."""
    return run("paths")


def edit_value(path: str, value: Any) -> Any:
    if path == "text" and isinstance(value, str):
        return value.replace("\r\n", "\n").replace("\n", "\\P")
    return value


@mcp.tool(annotations=EDIT)
def dwg_set(dwg: str, handles: list[str], values: dict[str, Any], dry_run: bool = True) -> str:
    """Set properties on one or more leaders in the working copy, e.g.
    {"text": "LINE 1\\nLINE 2", "scale": 50, "mtext.insert": [x, y, 0]}.
    See dwg_paths for every path. Newlines in text become MTEXT \\P.
    Runs as a dry run unless dry_run=false."""
    edits = [{"handle": h, "set": {k: edit_value(k, v) for k, v in values.items()}} for h in handles]
    return run("apply", dwg_path(dwg), "-", *(["--dry-run"] if dry_run else []), stdin=json.dumps(edits))


@mcp.tool(annotations=EDIT)
def dwg_apply(dwg: str, edits: list[dict[str, Any]], dry_run: bool = True) -> str:
    """Apply different edits to many leaders at once: [{"handle": "1A2B", "set": {path: value}}, ...].
    Runs as a dry run unless dry_run=false."""
    for e in edits:
        e["set"] = {k: edit_value(k, v) for k, v in e.get("set", {}).items()}
    return run("apply", dwg_path(dwg), "-", *(["--dry-run"] if dry_run else []), stdin=json.dumps(edits))


@mcp.tool(annotations=EDIT)
def dwg_replace(dwg: str, find: str, replace: str, regex: bool = False, case_sensitive: bool = False,
                dry_run: bool = True, type: LeaderType = "mleader", layer: str | None = None,
                space: str | None = None, include_blocks: bool = True) -> str:
    """Find/replace in leader text (raw MTEXT). Runs as a dry run unless dry_run=false."""
    a = ["replace", dwg_path(dwg), find, replace, *filter_args(type, None, layer, space, include_blocks)]
    if regex:
        a.append("--regex")
    if case_sensitive:
        a.append("--case")
    if dry_run:
        a.append("--dry-run")
    return run(*a)


@mcp.tool(annotations=EDIT)
def dwg_normalize(dwg: str, style: str | None = "SebStyle", keep: Literal["viewport", "text", "arrow"] = "viewport",
                  dry_run: bool = True, contains: str | None = None, layer: str | None = None,
                  space: str | None = None) -> str:
    """Put multileaders on `style` at default sizes, sized only by scale. keep="viewport" sets each
    leader's scale to the scale of the sheet viewport showing it (all notes plot at the style text
    height; on 1:100 viewports scale 50, half height; leaders on no sheet are skipped). keep="text"/"arrow" preserve that on-screen size.
    Runs as a dry run unless dry_run=false."""
    a = ["normalize", dwg_path(dwg), "--keep", keep, *filter_args("mleader", contains, layer, space, True)]
    if style:
        a += ["--style", style]
    if dry_run:
        a.append("--dry-run")
    return run(*a)


@mcp.tool(annotations=EDIT)
def dwg_mstyle(dwg: str, name: str = "SebStyle", values: dict[str, Any] | None = None) -> str:
    """Show an MLEADERSTYLE, or set its defaults (e.g. {"char_height": 2.5, "arrow_head_size": 2.5}).
    Existing leaders pick up new sizes only after dwg_normalize."""
    a = ["mstyle", dwg_path(dwg), name]
    if values:
        a += ["--set", *[f"{k}={json.dumps(v) if not isinstance(v, str) else v}" for k, v in values.items()]]
    return run(*a)


@mcp.tool(annotations=EDIT)
def dwg_attrib(dwg: str, find: str, replace: str, block: str | None = "SCHEDULE_LABEL",
               regex: bool = False, dry_run: bool = True) -> str:
    """Find/replace in block attribute values, e.g. tag labels DR-01 -> DR01. block limits it to one
    block name (default SCHEDULE_LABEL, the tag labels). Runs as a dry run unless dry_run=false."""
    a = ["attrib", dwg_path(dwg), find, replace]
    if block:
        a += ["--block", block]
    if regex:
        a.append("--regex")
    if dry_run:
        a.append("--dry-run")
    return run(*a)


@mcp.tool(annotations=DESTRUCTIVE)
def dwg_delete(dwg: str, handles: list[str]) -> str:
    """Delete entities by handle in the working copy (ACAD_TABLE also drops its graphics)."""
    return run("delete", dwg_path(dwg), *handles)


@mcp.tool(annotations=READ)
def dwg_changes(dwg: str) -> str:
    """Pending (unsaved) edits in the working copy."""
    return run("changes", dwg_path(dwg))


@mcp.tool(annotations=DESTRUCTIVE)
def dwg_reset(dwg: str) -> str:
    """Discard the working copy and every pending edit."""
    return run("reset", dwg_path(dwg))


@mcp.tool(annotations=DESTRUCTIVE)
def dwg_save(dwg: str, output: str | None = None, overwrite: bool = False, version: str | None = None) -> str:
    """Write the working copy to DWG and verify it by reading it back.
    Default target: <name>-edited.dwg. overwrite=true replaces the original (backup kept in .dwg_work/);
    ask the user to close the drawing in AutoCAD without saving first. Refuses if the drawing is open."""
    a = ["save", dwg_path(dwg)]
    if overwrite:
        a.append("--overwrite")
    elif output:
        out = Path(output).expanduser()
        a += ["-o", str(out if out.is_absolute() else DWG_HOME / out)]
    if version:
        a += ["--version", version]
    return run(*a)


@mcp.tool(annotations=EDIT)
def dwg_new_sheet(dwg: str, name: str, template: str, attribs: dict[str, str] | None = None) -> str:
    """Create a sheet from a template sheet (title block and notes, no viewports).
    attribs: title block attribute values, e.g. {"SHEETTITLE": "..."}."""
    a = ["new-sheet", dwg_path(dwg), name, "--template", template]
    if attribs:
        a += ["--set", *(f"{k}={v}" for k, v in attribs.items())]
    return run(*a)


@mcp.tool(annotations=EDIT)
def dwg_rename_sheet(dwg: str, old: str, new: str, after: str | None = None) -> str:
    """Rename a sheet (and its SHEETNUMBER). after: put the tab right after this sheet."""
    return run("rename-sheet", dwg_path(dwg), old, new, *(["--after", after] if after else []))


@mcp.tool(annotations=EDIT)
def dwg_schedules(dwg: str, only: list[str] | None = None) -> str:
    """Redraw schedule tables from schedules/schedules.json and its CSVs onto their sheets.
    Creates missing sheets. Placed tables keep their position. only: schedule ids to rebuild."""
    return run("schedules", dwg_path(dwg), *(["--only", *only] if only else []))


@mcp.tool(annotations=READ)
def schedule_tables(id: str | None = None, key: str | None = None) -> str:
    """List schedules (no id), or show a schedule's rows (id), or one row (id + key)."""
    if not id:
        return run("table", "list")
    return run("table", "show", id, *([key] if key else []))


@mcp.tool(annotations=EDIT)
def schedule_edit(action: Literal["set", "add", "remove", "rename"], id: str, key: str,
                  values: dict[str, str] | None = None, new_key: str | None = None,
                  after: str | None = None) -> str:
    """Edit a schedule CSV row by key (first column). set/add take values {COLUMN: value};
    add can go after another key; rename needs new_key. Run dwg_schedules afterwards to redraw."""
    a = ["table", action, id, key]
    if action == "rename":
        if not new_key:
            raise ToolError("rename needs new_key")
        a.append(new_key)
    else:
        a += [f"{k}={v}" for k, v in (values or {}).items()]
    if after:
        a += ["--after", after]
    return run(*a)


if __name__ == "__main__":
    mcp.run()
