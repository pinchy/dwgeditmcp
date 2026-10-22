---
name: dwg-edit
description: Query and edit AutoCAD DWG files - MULTILEADER notes (text, style, scale, position), sheets/layouts and drawn schedule tables. Use when the user wants to read, change, normalise or render leaders/notes, rename or add sheets, or build door/window/finish schedules in a .dwg.
---

# DWG editing

`TOOL` below is the folder that holds this file (`${CLAUDE_SKILL_DIR}` when loaded as a skill). Run:

```bash
TOOL/.venv/bin/python TOOL/dwg_edit.py <command> <file.dwg> ...
```

On Windows the Python is `TOOL\.venv\Scripts\python.exe`. Run from the folder that holds the drawings. `-h` on any command lists its options. If `.venv` or ODA File Converter is missing, follow `TOOL/README.md`.

## Before you start

Read `TOOL/rules.toml` (or `TOOL/rules.template.toml` if there is no `rules.toml`). Its `[claude] notes` are the user's house rules. Follow them. `[leaders] style` is the MLEADERSTYLE every note should use.

## How it works

- Edits go to a working DXF in `.dwg_work/` next to the DWG. Nothing touches the DWG until `save`.
- `save` writes `<name>-edited.dwg` (`-o` for another name) and verifies it by reading it back. `--overwrite` replaces the original and keeps a backup in `.dwg_work/`.
- `save` refuses while AutoCAD has the drawing open (`<name>.dwl`). Before an overwrite, tell the user to close the drawing in AutoCAD without saving. A drawing left open and saved later silently undoes the edits.
- If output says "changed outside this tool", the working copy was reloaded from the DWG. Re-check earlier edits.

## Leaders

1. Look: `open FILE`, `styles FILE`, `list FILE [--contains REGEX] [--type mleader|leader|all]`, `show FILE HANDLE`.
2. See: `render FILE HANDLE...` writes a PNG crop to `renders/` (leader in blue) and reports which sheets show it. Look at it before and after moving notes.
3. Edit, dry run first (`--dry-run`):
   - `set FILE HANDLE [HANDLE ...] --to path=value ...` (`paths` lists every settable property).
   - `set FILE HANDLE --to "move=[dx,dy]"` moves a note's text and landing; the arrow tip stays.
   - `replace FILE find repl [--regex]` for find/replace in leader text.
   - `apply FILE edits.json` with `[{"handle": "..", "set": {path: value}}]`.
   - `normalize FILE --style STYLE --keep viewport` sets each leader's scale to the scale of the sheet viewport it shows in (or the `[leaders.scale_for_viewport]` entry), so all notes plot at the same height.
   - `mstyle FILE STYLE [--set char_height=2.5 ...]` shows or changes an MLEADERSTYLE.
4. Review with `changes FILE`, then `save FILE`.

Notes:
- Old-style LEADERs have no linked text. `list`, `dump` and `replace` default to `--type mleader`.
- Moving MULTILEADER text does not move its leader lines. Move both.
- After size changes, render the sheets. Notes can collide or clip at viewport edges.
- Leaders inside unused anonymous blocks (`A$C...`) are invisible leftovers. Ignore them.

## Sheets and schedule tables

- `new-sheet FILE NAME --template SHEET [--set TAG=VALUE ...]`, `rename-sheet FILE OLD NEW [--after SHEET]`, `delete FILE HANDLE...`.
- `attrib FILE find repl [--block NAME]` does find/replace in block attribute values (e.g. tag labels `DR-01` -> `DR01`).
- Schedules are CSVs plus a JSON config (path in `rules.toml` `[schedules] config`; format in `TOOL/README.md`).
- `table list | show ID [KEY] | set ID KEY COL=VAL... | add ID KEY COL=VAL... [--after KEY] | remove ID KEY | rename ID KEY NEW` edits the CSVs by row key (first column).
- `schedules FILE [--only ID...]` redraws the tables onto their sheets (a block per schedule, lines plus one MTEXT per cell). Missing sheets are created from the template. Placed tables keep their position. Then `save`.
- ezdxf cannot create native AutoCAD tables, so tables are drawn.
