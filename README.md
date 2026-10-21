# dwg

Query and edit MULTILEADERs, sheets and schedule tables in AutoCAD DWG files from the command line, or from Claude Desktop through an MCP server.

- `dwg_edit.py` is the CLI.
- `dwg_mcp.py` is a stdio MCP server that wraps the CLI for Claude Desktop.

Edits go to a working DXF copy in `.dwg_work/`. The DWG is only written by `save`.

## Requirements

macOS with [Homebrew](https://brew.sh).

### 1. Python

Python 3.12 or newer (tested on 3.14).

```bash
brew install python
```

### 2. ODA File Converter

Converts DWG to DXF and back. Free download (needs a free account):
https://www.opendesign.com/guestfiles/oda_file_converter

Install the macOS build so the app is at one of:

- `/Applications/ODAFileConverter.app`
- `~/Applications/ODAFileConverter.app`

Open it once from Finder so macOS lets it run.

### 3. rsvg-convert

Only needed for `render` (PNG previews).

```bash
brew install librsvg
```

## Install

```bash
git clone <this repo> dwg
```

```bash
cd dwg
```

```bash
python3 -m venv .venv
```

```bash
.venv/bin/pip install -r requirements.txt
```

Check it works:

```bash
.venv/bin/python dwg_edit.py -h
```

## Drawing rules

Your drawing rules live in `rules.toml`: leader style, leader scale per viewport scale, tag block, schedule table layout, and house rules for Claude. Start from the template:

```bash
cp rules.template.toml rules.toml
```

Then edit `rules.toml`. It is git-ignored. Without it, the tool uses `rules.template.toml`. To use a file somewhere else, set `DWG_RULES=/path/to/rules.toml`.

## Use

```bash
.venv/bin/python dwg_edit.py open "My Drawing.dwg"
```

```bash
.venv/bin/python dwg_edit.py list "My Drawing.dwg"
```

Typical flow:

1. `open`, `list`, `show HANDLE` to look around.
2. `set`, `replace` or `apply` to edit (most edits support a dry run).
3. `changes` to review.
4. `save` writes `<name>-edited.dwg`. `save --overwrite` replaces the original and keeps a backup in `.dwg_work/`.

Run `.venv/bin/python dwg_edit.py <command> -h` for each command's options.

Close the drawing in AutoCAD (without saving) before you save over it. `save` refuses while AutoCAD's lock file (`.dwl`) exists.

### Schedules

`schedules` and `table` read the schedule config (`[schedules] config` in `rules.toml`, default `schedules/schedules.json`) and one CSV per schedule. Example config:

```json
{
  "sheet_template": "A7.01",
  "sheets_after": "A5.11",
  "defaults": {"text_height": 2.5, "layer": "text", "text_style": "Standard"},
  "schedules": [
    {"id": "doors", "title": "DOOR SCHEDULE", "csv": "doors.csv", "block": "DOOR_SCHEDULE", "sheet": "A6.01"}
  ]
}
```

The first CSV column is the row key (e.g. `MARK`).

## Claude Desktop (optional)

Add this to `~/Library/Application Support/Claude/claude_desktop_config.json`, with your own path:

```json
{
  "mcpServers": {
    "dwg": {
      "command": "/path/to/dwg/.venv/bin/python",
      "args": ["/path/to/dwg/dwg_mcp.py"],
      "env": {"DWG_HOME": "/path/to/your/drawings"}
    }
  }
}
```

Restart Claude Desktop. `DWG_HOME` is the folder relative DWG paths resolve against.
