"""Every data and secret file goes through core/vault.py: nothing reads or writes one directly.

An AST scan of the package: a call to .read_text, .read_bytes, .write_text, .write_bytes, open(),
config.write_atomic, os.replace or json.load in a module that isn't on the allow-list fails the
test. The allow-list holds the modules whose files are not customer data (plain YAML settings,
the packaged demo data, a user-chosen spreadsheet, the session link) and the vault itself.
"""

from __future__ import annotations

import ast
from pathlib import Path

import wardmind

PACKAGE = Path(wardmind.__file__).parent

# Module (relative to the package) -> why it may touch files directly.
ALLOWED = {
    "core/vault.py": "the vault itself",
    "core/config.py": "scoring.yaml and write_atomic for plain settings files",
    "core/settings.py": "settings.yaml: appearance, read before unlock",
    "core/connectors/demo.py": "the packaged, invented demo data",
    "core/connectors/hubspot.py": "hubspot.yaml: the property mapping, no customer data",
    "core/feeds/providers/files.py": "a spreadsheet the user picked, wherever it is",
    "web/session.py": "session.json: this run's sign-in link, deleted when Wardmind stops",
    "desktop.py": "the null device for a windowed app's output",
}
FILE_METHODS = {"read_text", "read_bytes", "write_text", "write_bytes"}
FILE_FUNCTIONS = {"open", "write_atomic"}
# Same-named functions that aren't file access: (owner, name).
NOT_FILES = {("contact_pages", "read_text")}  # formats when the contacts were last read


def _offences(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute):
            owner = func.value.id if isinstance(func.value, ast.Name) else ""
            if owner == "vault" or (owner, func.attr) in NOT_FILES:
                continue
            if func.attr in FILE_METHODS:
                found.append(f"line {node.lineno}: .{func.attr}()")
            elif owner == "os" and func.attr == "replace":
                found.append(f"line {node.lineno}: os.replace()")
            elif owner == "json" and func.attr == "load":
                found.append(f"line {node.lineno}: json.load()")
            elif owner == "config" and func.attr == "write_atomic":
                found.append(f"line {node.lineno}: config.write_atomic()")
        elif isinstance(func, ast.Name) and func.id in FILE_FUNCTIONS:
            found.append(f"line {node.lineno}: {func.id}()")
    return found


def test_no_module_reads_or_writes_a_data_file_without_the_vault() -> None:
    problems: dict[str, list[str]] = {}
    for path in sorted(PACKAGE.rglob("*.py")):
        name = path.relative_to(PACKAGE).as_posix()
        if name in ALLOWED:
            continue
        offences = _offences(path)
        if offences:
            problems[name] = offences
    assert problems == {}, (
        "These read or write files directly. Use core/vault.py (read_text, write_text, "
        f"read_bytes, write_bytes, move), or add the module to ALLOWED with a reason: {problems}"
    )


def test_the_allow_list_names_real_modules() -> None:
    for name in ALLOWED:
        assert (PACKAGE / name).is_file(), name
