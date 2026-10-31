"""Resolve Project Creator — create a DaVinci Resolve project with a settings preset."""
from __future__ import annotations

import json
import os
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import diskdb, resolve_api
from .resolve_api import ResolveError

APP_NAME = "Resolve Project Creator"
DEBOUNCE_MS = 400


def settings_path() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home())
    return Path(base) / "ResolveProjectCreator" / "settings.json"


def load_settings() -> dict:
    try:
        return json.loads(settings_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_settings(data: dict) -> None:
    p = settings_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError:
        pass


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.settings = load_settings()
        self.events: queue.Queue = queue.Queue()
        self.db = diskdb.active_db()
        self.projects: dict[str, set[str]] = {}
        self.busy = False
        self._debounce = None
        self.install = resolve_api.find_install(self.settings.get("resolve_path"))
        ver = resolve_api.installed_version()
        self.media_supported = ver is None or ver[0] >= 20

        root.title(APP_NAME)
        root.resizable(False, False)
        self.jobs: queue.Queue = queue.Queue()
        threading.Thread(target=self._api_thread, daemon=True).start()
        self._fonts()
        self._build()
        self._center()
        self._load_lists()
        if self.install is not None and resolve_api.is_running():
            self.jobs.put(("warm",))  # connect in the background so Create is instant
        root.after(50, self._pump)

    def _api_thread(self) -> None:
        """All Resolve API calls happen on this one thread."""
        while True:
            job = self.jobs.get()
            kind = job[0]
            if kind == "warm":
                try:
                    resolve_api.wait_ready(self.install, timeout=30)
                except Exception:  # noqa: BLE001
                    pass
            elif kind == "lists":
                self._api_lists_worker()
            elif kind == "create":
                self._create_worker(*job[1:])

    def _fonts(self) -> None:
        import tkinter.font as tkfont
        for n in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
            try:
                tkfont.nametofont(n).configure(size=10)
            except tk.TclError:
                pass
        self.root.option_add("*TCombobox*Listbox.font", tkfont.nametofont("TkDefaultFont"))

    def _center(self) -> None:
        self.root.update_idletasks()
        w, h = self.root.winfo_reqwidth(), self.root.winfo_reqheight()
        x = (self.root.winfo_screenwidth() - w) // 2
        y = (self.root.winfo_screenheight() - h) // 3
        self.root.geometry(f"+{x}+{y}")

    # ------------------------------------------------------------ ui

    def _build(self) -> None:
        f = ttk.Frame(self.root, padding=(16, 14, 16, 12))
        f.grid(sticky="nsew")
        f.columnconfigure(1, weight=1)

        ttk.Label(f, text="Name").grid(row=0, column=0, sticky="w", padx=(0, 10))
        self.name_var = tk.StringVar()
        self.name_entry = ttk.Entry(f, textvariable=self.name_var, width=44)
        self.name_entry.grid(row=0, column=1, columnspan=2, sticky="ew")
        self.name_var.trace_add("write", lambda *_: self._schedule_check())
        self.name_entry.bind("<FocusOut>", lambda _e: self._check_name())
        self.err_var = tk.StringVar()
        ttk.Label(f, textvariable=self.err_var, foreground="#d03030").grid(row=1, column=1, columnspan=2, sticky="w")

        ttk.Label(f, text="Folder").grid(row=2, column=0, sticky="w", padx=(0, 10), pady=4)
        self.folder_var = tk.StringVar()
        self.folder_cb = ttk.Combobox(f, textvariable=self.folder_var, state="readonly")
        self.folder_cb.grid(row=2, column=1, columnspan=2, sticky="ew", pady=4)
        self.folder_cb.bind("<<ComboboxSelected>>", lambda _e: self._check_name())

        ttk.Label(f, text="Preset").grid(row=3, column=0, sticky="w", padx=(0, 10), pady=4)
        self.preset_var = tk.StringVar()
        self.preset_cb = ttk.Combobox(f, textvariable=self.preset_var, state="readonly")
        self.preset_cb.grid(row=3, column=1, columnspan=2, sticky="ew", pady=4)

        ttk.Label(f, text="Media Location").grid(row=4, column=0, sticky="w", padx=(0, 10), pady=4)
        self.media_var = tk.StringVar(value=self.settings.get("media", ""))
        self.media_entry = ttk.Entry(f, textvariable=self.media_var)
        self.media_entry.grid(row=4, column=1, sticky="ew", pady=4)
        self.media_btn = ttk.Button(f, text="Change…", command=self._pick_media)
        self.media_btn.grid(row=4, column=2, sticky="e", padx=(6, 0), pady=4)
        hint = "Empty = location from the preset" if self.media_supported else "Needs Resolve 20+"
        ttk.Label(f, text=hint, foreground="#808080").grid(row=5, column=1, columnspan=2, sticky="w")
        if not self.media_supported:
            self.media_entry.state(["disabled"])
            self.media_btn.state(["disabled"])

        bottom = ttk.Frame(f)
        bottom.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(12, 0))
        bottom.columnconfigure(0, weight=1)
        self.status_var = tk.StringVar()
        ttk.Label(bottom, textvariable=self.status_var, foreground="#606060").grid(row=0, column=0, sticky="w")
        ttk.Button(bottom, text="Cancel", command=self.root.destroy).grid(row=0, column=1, padx=(0, 6))
        self.create_btn = ttk.Button(bottom, text="Create", command=self._create)
        self.create_btn.grid(row=0, column=2)

        self.root.bind("<Return>", lambda _e: self._create())
        self.root.bind("<Escape>", lambda _e: self.root.destroy())
        self.name_entry.focus_set()

    def _pick_media(self) -> None:
        d = filedialog.askdirectory(parent=self.root, initialdir=self.media_var.get() or None,
                                    title="Media Location")
        if d:
            self.media_var.set(str(Path(d)))

    # ------------------------------------------------------------ data

    def _db_key(self) -> str:
        return self.db.key if self.db else "?"

    def _load_lists(self) -> None:
        snap = diskdb.snapshot(self.db) if self.db else None
        if snap is not None:
            self._apply_lists(snap.presets, snap.projects)
            return
        # Network / cloud database or nothing readable on disk -> ask Resolve.
        if self.install is None:
            self._ask_install()
            return
        self._set_busy(True, "Connecting to DaVinci Resolve…")
        self.jobs.put(("lists",))

    def _api_lists_worker(self) -> None:
        try:
            _r, pm = resolve_api.wait_ready(self.install, on_status=lambda s: self.events.put(("status", s)))
            presets, tree = resolve_api.api_listing(pm)
            db = pm.GetCurrentDatabase() or {}
            self.events.put(("lists", presets, tree, db.get("DbName")))
        except ResolveError as e:
            self.events.put(("fatal", str(e)))
        except Exception as e:  # noqa: BLE001
            self.events.put(("fatal", f"Unexpected error: {e}"))

    def _apply_lists(self, presets: list[str], projects: dict[str, set[str]]) -> None:
        self.projects = projects
        folders = sorted(projects, key=lambda x: (x != "", x.lower()))
        self.folder_cb["values"] = ["(root)" if x == "" else x for x in folders]
        last_folder = self.settings.get("folders", {}).get(self._db_key(), "")
        self.folder_var.set("(root)" if last_folder not in projects or last_folder == "" else last_folder)
        self.preset_cb["values"] = presets
        last = self.settings.get("preset")
        self.preset_var.set(last if last in presets else (presets[0] if presets else ""))
        self._set_busy(False, f"Database: {self.db.name}" if self.db else "")
        self._check_name()

    def _folder(self) -> str:
        v = self.folder_var.get()
        return "" if v == "(root)" else v

    # ------------------------------------------------------------ name check

    def _schedule_check(self) -> None:
        if self._debounce is not None:
            self.root.after_cancel(self._debounce)
        self._debounce = self.root.after(DEBOUNCE_MS, self._check_name)

    def _name_error(self) -> str | None:
        name = self.name_var.get()
        if not name:
            return None
        problem = diskdb.name_problem(name)
        if problem:
            return problem
        if name.lower() in self.projects.get(self._folder(), set()):
            return "A project with this name already exists in this folder."
        return None

    def _check_name(self) -> None:
        self._debounce = None
        self.err_var.set(self._name_error() or "")

    # ------------------------------------------------------------ create

    def _ask_install(self) -> None:
        messagebox.showinfo(APP_NAME, "DaVinci Resolve wasn't found. Point me to Resolve.exe.", parent=self.root)
        p = filedialog.askopenfilename(parent=self.root, title="Resolve.exe",
                                       filetypes=[("Resolve.exe", "Resolve.exe")])
        inst = resolve_api.find_install(p) if p else None
        if inst is None:
            self.root.destroy()
            return
        self.install = inst
        self.settings["resolve_path"] = str(inst)
        save_settings(self.settings)
        self._load_lists()

    def _create(self) -> None:
        if self.busy:
            return
        name = self.name_var.get()
        err = diskdb.name_problem(name) or self._name_error()
        if err:
            self.err_var.set(err)
            self.name_entry.focus_set()
            return
        if not self.preset_var.get():
            self.status_var.set("Pick a preset.")
            return
        if self.install is None:
            self._ask_install()
            if self.install is None:
                return
        media = self.media_var.get().strip() if self.media_supported else ""
        args = (name, self._folder(), self.preset_var.get(), media or None)
        self._set_busy(True, "Connecting to DaVinci Resolve…")
        self.jobs.put(("create", *args))

    def _create_worker(self, name, folder, preset, media) -> None:
        try:
            warnings = resolve_api.create_project(
                self.install, name, folder, preset, media,
                expected_db=self.db.name if self.db and self.db.kind == "disk" else None,
                on_status=lambda s: self.events.put(("status", s)))
            self.events.put(("done", warnings, folder, preset, media))
        except ResolveError as e:
            self.events.put(("error", str(e)))
        except Exception as e:  # noqa: BLE001
            self.events.put(("error", f"Unexpected error: {e}"))

    def _set_busy(self, busy: bool, status: str = "") -> None:
        self.busy = busy
        self.status_var.set(status)
        for w in (self.create_btn, self.folder_cb, self.preset_cb):
            w.state(["disabled"] if busy else ["!disabled"])
        if not busy:
            self.folder_cb.state(["readonly"])
            self.preset_cb.state(["readonly"])

    def _pump(self) -> None:
        try:
            while True:
                ev = self.events.get_nowait()
                kind = ev[0]
                if kind == "status":
                    self.status_var.set(ev[1])
                elif kind == "lists":
                    _, presets, tree, dbname = ev
                    if dbname and self.db:
                        self.db.name = dbname
                    self._apply_lists(presets, tree)
                elif kind == "fatal":
                    self._set_busy(False, "")
                    messagebox.showerror(APP_NAME, ev[1], parent=self.root)
                elif kind == "error":
                    self._set_busy(False, "")
                    messagebox.showerror(APP_NAME, ev[1], parent=self.root)
                elif kind == "done":
                    _, warnings, folder, preset, media = ev
                    self.settings["preset"] = preset
                    self.settings.setdefault("folders", {})[self._db_key()] = folder
                    self.settings["media"] = media or ""
                    save_settings(self.settings)
                    if warnings:
                        messagebox.showwarning(APP_NAME, "Project created, but:\n\n" + "\n".join(warnings),
                                               parent=self.root)
                    self.root.destroy()
                    return
        except queue.Empty:
            pass
        self.root.after(50, self._pump)


def main() -> None:
    try:
        ctypes_dpi()
    except Exception:  # noqa: BLE001
        pass
    root = tk.Tk()
    App(root)
    root.mainloop()


def ctypes_dpi() -> None:
    import ctypes
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
