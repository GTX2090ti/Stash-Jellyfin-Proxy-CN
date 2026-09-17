"""Config file parser — flat KEY=value pairs plus [section.name] blocks.

Grammar:
    # comments and blank lines ignored
    KEY = value                # flat key (always global scope)
    KEY = "quoted value"
    [section.name]             # opens a section scope
    key = value                # scoped into the current section

Pure function, no module-level state. `config.bootstrap.run_bootstrap()`
calls this, holds the returned dicts, and coordinates the
local-override merge.
"""
import os
import sys


class CaseInsensitiveDict(dict):
    """Dict with case-insensitive string keys.

    Config keys legitimately appear in either case: the WebUI settings
    handler writes UPPER_CASE (POSTER_CROP_ANCHOR), hand edits and older
    releases wrote lower_case (poster_crop_anchor) — and bootstrap read
    sites are mixed too. With a plain dict a same-key-different-case pair
    silently shadows one of the two values; this view folds all lookups
    so the last-written line always wins, matching user expectation that
    a newer edit overrides an older one.

    Iteration (keys/items) yields casefolded keys.
    """

    def __init__(self, data=None, **kw):
        super().__init__()
        if data:
            for k, v in data.items():
                self[k] = v
        for k, v in kw.items():
            self[k] = v

    def __setitem__(self, key, value):
        super().__setitem__(str(key).casefold(), value)

    def __getitem__(self, key):
        return super().__getitem__(str(key).casefold())

    def __contains__(self, key):
        return super().__contains__(str(key).casefold())

    def get(self, key, default=None):
        return super().get(str(key).casefold(), default)

    def pop(self, key, *args):
        return super().pop(str(key).casefold(), *args)

    def setdefault(self, key, *args):
        return super().setdefault(str(key).casefold(), *args)

    def update(self, other=(), **kw):
        if hasattr(other, "items"):
            other = other.items()
        for k, v in other:
            self[k] = v
        for k, v in kw.items():
            self[k] = v


def load_config(filepath):
    """Load configuration from a shell-style config file with optional
    INI-style section blocks.

    Returns a 3-tuple:
        config (dict): flat KEY → value for keys in the global scope
        defined_keys (set): flat keys explicitly present in the file
        sections (dict): {section_name: {key: value}} for every [section]
                         block; empty dict if none.
    """
    config = {}
    defined_keys = set()
    sections = {}
    current_section = None  # None = global scope
    if os.path.isfile(filepath):
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    # Skip comments and empty lines
                    if not line or line.startswith('#'):
                        continue
                    # Section header: [name] opens a new scope for subsequent
                    # key/value lines. Empty or malformed headers reset to
                    # global scope rather than raise — we log to stderr but
                    # keep loading so a partial-bad file doesn't brick startup.
                    if line.startswith('[') and line.endswith(']'):
                        name = line[1:-1].strip()
                        if name:
                            current_section = name
                            sections.setdefault(current_section, {})
                        else:
                            current_section = None
                        continue
                    # KEY=value or KEY="value" — into section or global.
                    if '=' in line:
                        key, _, value = line.partition('=')
                        key = key.strip()
                        value = value.strip().strip('"').strip("'")
                        if current_section is None:
                            config[key] = value
                            defined_keys.add(key)
                        else:
                            sections[current_section][key] = value
        except Exception as e:
            print(f"Error loading config file {filepath}: {e}", file=sys.stderr)
    return config, defined_keys, sections
