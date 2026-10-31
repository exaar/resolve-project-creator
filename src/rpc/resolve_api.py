"""Thin wrapper around DaVinci Resolve's external scripting API (Windows)."""
from __future__ import annotations

import ctypes
import importlib.machinery
import importlib.util
import os
import subprocess
import sys
import time
import winreg
from ctypes import wintypes
from pathlib import Path

DEFAULT_INSTALL = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Blackmagic Design" / "DaVinci Resolve"
UNTITLED = "Untitled Project"


class ResolveError(Exception):
    """User-facing error (message is shown as-is)."""


SCRIPTING_HELP = (
    "Can't talk to DaVinci Resolve.\n\n"
    "In Resolve open Preferences → System → General and set\n"
    "“External scripting using” to “Local”, then restart Resolve.\n\n"
    "External scripting only works in DaVinci Resolve Studio."
)


# ---------------------------------------------------------------- discovery

def installed_version() -> tuple[int, ...] | None:
    for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Blackmagic Design\DaVinci Resolve",
                                0, winreg.KEY_READ | view) as k:
                raw, _ = winreg.QueryValueEx(k, "Version")  # e.g. "20.0.00049"
                return tuple(int(p) for p in str(raw).split(".") if p.isdigit())
        except OSError:
            continue
    return None


def find_install(custom: str | None = None) -> Path | None:
    cands = []
    if custom:
        p = Path(custom)
        cands.append(p.parent if p.suffix.lower() == ".exe" else p)
    cands.append(DEFAULT_INSTALL)
    for c in cands:
        if (c / "Resolve.exe").is_file() and (c / "fusionscript.dll").is_file():
            return c
    return None


# ---------------------------------------------------------------- process

def _resolve_pids() -> set[int]:
    """PIDs of Resolve.exe, via a toolhelp snapshot (no console flash)."""
    TH32CS_SNAPPROCESS = 0x2

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_void_p),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]

    k32 = ctypes.windll.kernel32
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    pids: set[int] = set()
    if not snap or snap == wintypes.HANDLE(-1).value:
        return pids
    try:
        e = PROCESSENTRY32W()
        e.dwSize = ctypes.sizeof(e)
        ok = k32.Process32FirstW(snap, ctypes.byref(e))
        while ok:
            if e.szExeFile.lower() == "resolve.exe":
                pids.add(e.th32ProcessID)
            ok = k32.Process32NextW(snap, ctypes.byref(e))
    finally:
        k32.CloseHandle(snap)
    return pids


def is_running() -> bool:
    return bool(_resolve_pids())


def launch(install: Path) -> None:
    subprocess.Popen([str(install / "Resolve.exe")], cwd=str(install),
                     creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))


def _resolve_windows() -> list[int]:
    """Visible top-level windows owned by Resolve.exe (never touches our own windows)."""
    pids = _resolve_pids()
    user32 = ctypes.windll.user32
    found: list[int] = []
    if not pids:
        return found

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in pids and user32.IsWindowVisible(hwnd) and user32.GetWindowTextLengthW(hwnd) > 0:
            found.append(hwnd)
        return True

    user32.EnumWindows(cb, 0)
    return found


def bring_to_front() -> None:
    wins = _resolve_windows()
    if wins:
        user32 = ctypes.windll.user32
        user32.ShowWindow(wins[0], 9)  # SW_RESTORE
        user32.SetForegroundWindow(wins[0])


# ---------------------------------------------------------------- api

_module = None


def _load_module(install: Path):
    global _module
    if _module is None:
        dll = install / "fusionscript.dll"
        loader = importlib.machinery.ExtensionFileLoader("fusionscript", str(dll))
        spec = importlib.util.spec_from_file_location("fusionscript", str(dll), loader=loader)
        mod = importlib.util.module_from_spec(spec)
        loader.exec_module(mod)
        _module = mod
    return _module


def connect(install: Path):
    """Return the Resolve object or None. Only call when Resolve.exe is running
    (scriptapp() blocks for seconds when nothing is listening)."""
    mod = _load_module(install)
    return mod.scriptapp("Resolve")


_conn = None


def _alive(r) -> bool:
    try:
        return bool(r.GetVersion())
    except Exception:  # noqa: BLE001
        return False


def wait_ready(install: Path, timeout: float = 600, on_status=None):
    """Connect, launching Resolve first if needed. Returns (resolve, pm).

    Not thread-safe: call from one dedicated worker thread only.
    """
    global _conn
    if _conn is not None and is_running() and _alive(_conn):
        pm = _conn.GetProjectManager()
        if pm is not None and pm.GetCurrentDatabase():
            return _conn, pm
    _conn = None
    if not is_running():
        if on_status:
            on_status("Starting DaVinci Resolve…")
        launch(install)
    deadline = time.time() + timeout
    r = None
    tried_while_running = 0.0
    while time.time() < deadline:
        if is_running():
            r = connect(install)
            if r is not None:
                break
            if tried_while_running == 0.0:
                tried_while_running = time.time()
            # Resolve has been up for a while but API never answers -> scripting off / free
            if time.time() - tried_while_running > 90 and _main_window_up():
                raise ResolveError(SCRIPTING_HELP)
        time.sleep(1)
    if r is None:
        raise ResolveError("DaVinci Resolve didn't start in time.")
    pm = None
    while time.time() < deadline:
        pm = r.GetProjectManager()
        if pm is not None and pm.GetCurrentDatabase():
            _conn = r
            return r, pm
        time.sleep(0.5)
    raise ResolveError("DaVinci Resolve is running but its project database isn't ready.")


def _main_window_up() -> bool:
    return bool(_resolve_windows())


def goto_folder(pm, folder: str) -> None:
    if not pm.GotoRootFolder():
        raise ResolveError("Couldn't open the root folder in Project Manager.")
    for part in [p for p in folder.split("/") if p]:
        if not pm.OpenFolder(part):
            raise ResolveError(f"Folder “{folder}” no longer exists in Project Manager.")


def api_listing(pm) -> tuple[list[str], dict[str, set[str]]]:
    """Presets + folder tree through the API (network / cloud databases)."""
    proj = pm.GetCurrentProject()
    presets = []
    if proj is not None:
        presets = [p["Name"] for p in (proj.GetPresetList() or []) if p.get("Name") != "Current Project"]
    tree: dict[str, set[str]] = {}

    def walk(rel: str, depth: int) -> None:
        tree[rel] = {n.lower() for n in (pm.GetProjectListInCurrentFolder() or [])}
        if depth > 8:
            return
        for sub in pm.GetFolderListInCurrentFolder() or []:
            if pm.OpenFolder(sub):
                walk(f"{rel}/{sub}" if rel else sub, depth + 1)
                pm.GotoParentFolder()

    pm.GotoRootFolder()
    walk("", 0)
    pm.GotoRootFolder()
    return presets, tree


def create_project(install: Path, name: str, folder: str, preset: str, media: str | None,
                   expected_db: str | None = None, on_status=None) -> list[str]:
    """Full create flow. Returns a list of non-fatal warnings."""
    warnings: list[str] = []
    r, pm = wait_ready(install, on_status=on_status)
    db = pm.GetCurrentDatabase() or {}
    if expected_db and db.get("DbName") and db.get("DbName") != expected_db:
        raise ResolveError(
            f"Resolve is using database “{db.get('DbName')}”, not “{expected_db}”.\n"
            "Reopen this window to refresh the lists.")

    if on_status:
        on_status("Creating project…")
    cur = pm.GetCurrentProject()
    if cur is not None and cur.GetName() != UNTITLED:
        pm.SaveProject()

    goto_folder(pm, folder)
    existing = {n.lower() for n in (pm.GetProjectListInCurrentFolder() or [])}
    if name.lower() in existing:
        raise ResolveError("A project with this name already exists in this folder.")

    proj = pm.CreateProject(name)
    if not proj:
        raise ResolveError("Resolve refused to create the project (name taken or invalid).")

    if preset and not proj.SetPreset(preset):
        warnings.append(f"Couldn't apply preset “{preset}”.")

    if media:
        m = media.rstrip("\\/")
        ok = proj.SetSetting("projectMediaLocation", m)
        proj.SetSetting("perfCacheClipsLocation", m + "\\CacheClip")
        proj.SetSetting("colorGalleryStillsLocation", m + "\\.gallery")
        if not ok:
            warnings.append("This Resolve version doesn't accept Media Location via API; preset's location kept.")

    if not pm.SaveProject():
        warnings.append("Resolve didn't confirm saving the new project.")
    bring_to_front()
    return warnings
