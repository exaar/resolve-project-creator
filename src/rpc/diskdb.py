"""Read DaVinci Resolve database info straight from disk (no Resolve needed).

Only *disk* databases (classic "Resolve Disk Database" and the newer
"Project Library") can be read this way. PostgreSQL and Blackmagic Cloud
databases live on a server, so for them the app falls back to the API.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

INVALID_NAME_CHARS = '<>:"/\\|?*'
RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def resolve_prefs_dir() -> Path:
    appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(appdata) / "Blackmagic Design" / "DaVinci Resolve" / "Preferences"


@dataclass
class DbEntry:
    key: str          # first field of dblist.conf line, referenced by activedb.conf
    name: str         # human readable name
    kind: str         # "disk" | "network" | "cloud"
    path: Path | None = None   # disk databases only


@dataclass
class DiskSnapshot:
    db: DbEntry
    user_dir: Path
    presets: list[str] = field(default_factory=list)
    # folder path ("" = root, "A/B" = nested) -> set of lower-cased project names
    projects: dict[str, set[str]] = field(default_factory=dict)

    @property
    def folders(self) -> list[str]:
        return sorted(self.projects, key=lambda f: (f != "", f.lower()))


def _decode_path(raw: str) -> Path:
    """dblist.conf stores 'C:\\x' as 'C\\x' (colon is the field separator)."""
    raw = raw.strip()
    if len(raw) >= 2 and raw[0].isalpha() and raw[1] == "\\":
        raw = raw[0] + ":" + raw[1:]
    return Path(raw)


def parse_dblist(text: str) -> list[DbEntry]:
    out: list[DbEntry] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(":")
        kind_raw = parts[-1].strip().upper()
        if kind_raw == "DISK" and len(parts) >= 2:
            out.append(DbEntry(key=parts[0], name=parts[0], kind="disk", path=_decode_path(parts[1])))
        elif kind_raw in ("QPSQL", "PSQL", "POSTGRES") and len(parts) >= 5:
            # key:host:user:password:dbname:QPSQL  -> never keep credentials around
            out.append(DbEntry(key=parts[0], name=parts[4] or parts[0], kind="network"))
        else:
            out.append(DbEntry(key=parts[0], name=parts[0], kind="network"))
    return out


def parse_activedb(text: str) -> tuple[str, str] | None:
    """Returns (kind, key) of the active database, e.g. ('disk', 'MyDb')."""
    for line in text.splitlines():
        line = line.strip()
        if ":" not in line:
            continue
        kind, _, value = line.partition(":")
        if kind.endswith("*"):
            return kind.rstrip("*").lower(), value.strip()
    return None


def active_db(prefs: Path | None = None) -> DbEntry | None:
    prefs = prefs or resolve_prefs_dir()
    try:
        act = parse_activedb((prefs / "activedb.conf").read_text(encoding="utf-8", errors="ignore"))
    except OSError:
        return None
    if not act:
        return None
    kind, key = act
    if kind == "cloud":
        return DbEntry(key=key, name="Blackmagic Cloud", kind="cloud")
    try:
        entries = parse_dblist((prefs / "dblist.conf").read_text(encoding="utf-8", errors="ignore"))
    except OSError:
        entries = []
    for e in entries:
        if e.key == key:
            return e
    return DbEntry(key=key, name=key, kind="network" if kind == "network" else "disk")


def find_user_dir(db_path: Path) -> Path | None:
    candidates = [db_path / "Resolve Projects", db_path]
    for root in candidates:
        users = root / "Users"
        if not users.is_dir():
            continue
        guest = users / "guest"
        if (guest / "Projects").is_dir() or (guest / "Configs").is_dir():
            return guest
        for u in sorted(users.iterdir()):
            if (u / "Projects").is_dir():
                return u
    return None


def read_presets(user_dir: Path) -> list[str]:
    """Names exactly as Project.SetPreset() expects them (file stems).

    'User Default Config' is the per-user default; the API calls it
    '<user> default config', so we expose it under that name.
    """
    names: list[str] = ["System Config"]
    cfg = user_dir / "Configs"
    user_default = f"{user_dir.name} default config"
    seen_default = False
    if cfg.is_dir():
        for f in sorted(cfg.glob("*.xml"), key=lambda p: p.stem.lower()):
            if f.stem.lower() == "user default config":
                seen_default = True
                continue
            names.append(f.stem)
    if seen_default:
        names.insert(1, user_default)
    return names


def read_projects(user_dir: Path) -> dict[str, set[str]]:
    root = user_dir / "Projects"
    result: dict[str, set[str]] = {"": set()}
    if not root.is_dir():
        return result

    def walk(d: Path, rel: str) -> None:
        try:
            children = list(d.iterdir())
        except OSError:
            return
        for c in children:
            if not c.is_dir():
                continue
            if (c / "Project.db").is_file():
                result.setdefault(rel, set()).add(c.name.lower())
            else:
                sub = f"{rel}/{c.name}" if rel else c.name
                result.setdefault(sub, set())
                walk(c, sub)

    walk(root, "")
    return result


def snapshot(db: DbEntry) -> DiskSnapshot | None:
    if db.kind != "disk" or db.path is None:
        return None
    user_dir = find_user_dir(db.path)
    if user_dir is None:
        return None
    return DiskSnapshot(db=db, user_dir=user_dir, presets=read_presets(user_dir), projects=read_projects(user_dir))


def name_problem(name: str) -> str | None:
    """Return an error message for names Resolve can't store safely."""
    if not name.strip():
        return "Enter a project name."
    if name != name.strip():
        return "Name can't start or end with a space."
    bad = sorted({c for c in name if c in INVALID_NAME_CHARS or ord(c) < 32})
    if bad:
        shown = " ".join(c if ord(c) >= 32 else "?" for c in bad)
        return f"Name can't contain: {shown}"
    if name.endswith("."):
        return "Name can't end with a dot."
    if name.split(".")[0].upper() in RESERVED:
        return "This name is reserved by Windows."
    if len(name) > 200:
        return "Name is too long."
    return None
