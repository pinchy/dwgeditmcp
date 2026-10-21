"""Load the drawing rules: $DWG_RULES, else rules.toml, else rules.template.toml."""
from __future__ import annotations

import os
import sys
import tomllib
from pathlib import Path

HERE = Path(__file__).resolve().parent


def load_rules() -> tuple[Path, dict]:
    env = os.environ.get("DWG_RULES")
    paths = [Path(env).expanduser()] if env else [HERE / "rules.toml", HERE / "rules.template.toml"]
    for path in paths:
        if path.is_file():
            with open(path, "rb") as f:
                return path, tomllib.load(f)
    sys.exit(f"ERROR: no rules file at {paths[0]}")


RULES_PATH, RULES = load_rules()
LEADERS = RULES.get("leaders", {})
TAGS = RULES.get("tags", {})
SCHEDULES = RULES.get("schedules", {})
CLAUDE_NOTES = RULES.get("claude", {}).get("notes", "").strip()
