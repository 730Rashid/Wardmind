# PyInstaller spec for the Windows desktop build. Run it through packaging\build.ps1.
#
# One folder, two programs that share it:
#   Wardmind.exe      the dashboard in its own window (no console)
#   wardmind-cli.exe  the command line, for `wardmind-cli mcp` (Claude) and `score`
# A folder build rather than a single file: it starts faster and unpacks nothing to %TEMP%.
# UPX is off, because compressed programs are flagged by antivirus far more often.

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

HERE = Path(SPECPATH)  # noqa: F821 (set by PyInstaller)
ICON = str(HERE / "wardmind.ico")

datas = collect_data_files("wardmind")  # templates, static files, demo data
for package in ("mcp", "fastapi", "pydantic", "starlette", "uvicorn", "httpx", "typer"):
    datas += copy_metadata(package)

hiddenimports = (
    collect_submodules("wardmind")  # several modules are imported inside functions
    + collect_submodules("uvicorn")  # its loops and protocols are chosen by name at run time
    + ["webview.platforms.winforms", "webview.platforms.edgechromium"]
)

excludes = ["tkinter", "pytest", "ruff", "PyInstaller"]


def analysis(script):
    return Analysis(  # noqa: F821
        [str(HERE / script)],
        pathex=[str(HERE.parent / "src")],
        datas=datas,
        hiddenimports=hiddenimports,
        excludes=excludes,
        noarchive=False,
    )


def program(found, name, console):
    return EXE(  # noqa: F821
        PYZ(found.pure),  # noqa: F821
        found.scripts,
        [],
        exclude_binaries=True,
        name=name,
        icon=ICON,
        console=console,
        upx=False,
        version=str(HERE / "version_info.txt"),
    )


desktop = analysis("wardmind_desktop.py")
cli = analysis("wardmind_cli.py")

COLLECT(  # noqa: F821
    program(desktop, "Wardmind", console=False),
    desktop.binaries,
    desktop.datas,
    program(cli, "wardmind-cli", console=True),
    cli.binaries,
    cli.datas,
    name="Wardmind",
    upx=False,
)
