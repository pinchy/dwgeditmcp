# dwg edit skill

Query and edit MULTILEADERs, sheets and schedule tables in AutoCAD DWG files from the command line, or from Claude Desktop through an MCP server.

- `dwg_edit.py` is the CLI.
- `dwg_mcp.py` is a stdio MCP server that wraps the CLI for Claude Desktop.

Edits go to a working DXF copy in `.dwg_work/`. The DWG is only written by `save`.

## Install on macOS

Needs [Homebrew](https://brew.sh).

1. Install Python 3.12 or newer (tested on 3.14):

   ```bash
   brew install python
   ```

2. Install ODA File Converter. It converts DWG to DXF and back. Free download:
   https://www.opendesign.com/guestfiles/oda_file_converter

   Install the macOS build so the app is at `/Applications/ODAFileConverter.app` or `~/Applications/ODAFileConverter.app`. Open it once from Finder so macOS lets it run.

3. Install rsvg-convert. Only `render` (PNG previews) needs it:

   ```bash
   brew install librsvg
   ```

4. Get the tool and set up its Python environment:

   ```bash
   git clone https://github.com/pinchy/dwgeditmcp.git
   ```

   ```bash
   cd dwgeditmcp
   ```

   ```bash
   python3 -m venv .venv
   ```

   ```bash
   .venv/bin/pip install -r requirements.txt
   ```

5. Check it works:

   ```bash
   .venv/bin/python dwg_edit.py -h
   ```

## Install on Windows

Use PowerShell.

1. Install Python 3.12 or newer from https://www.python.org/downloads/windows/. In the installer, tick "Add python.exe to PATH".

2. Install Git from https://git-scm.com/download/win.

3. Install ODA File Converter. It converts DWG to DXF and back. Free download:
   https://www.opendesign.com/guestfiles/oda_file_converter

   Use the Windows installer and keep the default folder (`C:\Program Files\ODA\...`). The tool finds it there.

4. Install rsvg-convert. Only `render` (PNG previews) needs it. Download `rsvg-convert.exe` from https://github.com/miyako/console-rsvg-convert/releases and put its folder on your PATH. Or, with [Chocolatey](https://chocolatey.org):

   ```powershell
   choco install rsvg-convert
   ```

5. Get the tool and set up its Python environment:

   ```powershell
   git clone https://github.com/pinchy/dwgeditmcp.git
   ```

   ```powershell
   cd dwgeditmcp
   ```

   ```powershell
   py -m venv .venv
   ```

   ```powershell
   .venv\Scripts\pip install -r requirements.txt
   ```

6. Check it works:

   ```powershell
   .venv\Scripts\python dwg_edit.py -h
   ```

On Windows, use `.venv\Scripts\python` wherever this README says `.venv/bin/python`.

## Drawing rules

Your drawing rules live in `rules.toml`: leader style, leader scale per viewport scale, tag block, schedule table layout, and house rules for Claude. Start from the template:

```bash
cp rules.template.toml rules.toml
```

On Windows: `copy rules.template.toml rules.toml`.

Then edit `rules.toml`. It is git-ignored. Without it, the tool uses `rules.template.toml`. To use a file somewhere else, set `DWG_RULES=/path/to/rules.toml`.

## Use

### With an AI client

Just point your AI client (Claude Code, Claude Desktop, Cursor, ...) to this folder and it will work out how to invoke the tool. Alternatively, you can use it as a skill: [SKILL.md](SKILL.md) tells the AI how to run it. For Claude Code, link this folder into your skills folder (run it from this folder):

```bash
ln -s "$PWD" ~/.claude/skills/dwg-edit
```

On Windows, clone the repo straight into `%USERPROFILE%\.claude\skills\dwg-edit` instead, then do the install steps there.

### From the command line

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

Add this to Claude Desktop's config file, with your own paths:

- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "dwg": {
      "command": "/path/to/dwgeditmcp/.venv/bin/python",
      "args": ["/path/to/dwgeditmcp/dwg_mcp.py"],
      "env": {"DWG_HOME": "/path/to/your/drawings"}
    }
  }
}
```

On Windows, `command` is `C:\\path\\to\\dwgeditmcp\\.venv\\Scripts\\python.exe` (double backslashes in JSON).

Restart Claude Desktop. `DWG_HOME` is the folder relative DWG paths resolve against.
