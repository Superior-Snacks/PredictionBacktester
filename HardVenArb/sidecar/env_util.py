"""Shared: load the repo/server .env into os.environ (dependency-free).

Used by both app.py and the bookmaker_stomp.py smoke test so `BOOKMAKER_*`, `HARDVEN_*`, etc. can live
in the .env the rest of the bots read. Search order mirrors the C# bots' loader
(KalshiApiConfig.LoadDotEnv → exe dir → parent → user home → CWD), so on the Linux server the sidecar
finds the SAME .env wherever it lives:

    1. $HARDVEN_ENV_FILE (explicit override, if set)
    2. this file's dir, then each parent up the tree   (covers the repo .env at any depth)
    3. the user home dir  (~/.env)   ← where server deploys often keep it
    4. the current working dir

First .env found wins. Parses `export KEY=VALUE` and bare `KEY=VALUE`, strips surrounding quotes,
splits on the FIRST `=` (so values with `=`, like the WSS URL's `?f=ws`, survive), and does NOT
overwrite vars already set in the environment.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path


def atomic_write_json(path, obj, indent: int = 2, retries: int = 25) -> None:
    """Write `obj` as JSON to `path` ATOMICALLY: serialise to a temp file in the SAME directory, fsync, then
    os.replace it onto `path` (an atomic rename). A concurrent reader — the C# bot's ~15-min hot-reload — then
    always sees the OLD or the NEW complete file, never a half-written one. Fixes the '[HOT-RELOAD] Object
    reference not set to an instance of an object' that fired right after a re-pair overwrote the file in place
    (or, for the old replace→write dance, while the file was momentarily ABSENT). On Windows os.replace can hit a
    brief sharing-violation while a reader holds the target open, so it's retried."""
    p = Path(path)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=p.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=indent)
            f.flush()
            os.fsync(f.fileno())
        for attempt in range(retries):
            try:
                os.replace(tmp, str(p))
                return
            except PermissionError:                      # Windows: reader briefly holds the target open
                if attempt == retries - 1:
                    raise
                time.sleep(0.1)
    finally:
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)                           # only reached if the replace never succeeded
        except OSError:
            pass


def _candidate_paths() -> list[Path]:
    here = Path(__file__).resolve()
    paths: list[Path] = []
    override = os.environ.get("HARDVEN_ENV_FILE")
    if override:
        paths.append(Path(override).expanduser())
    paths += [d / ".env" for d in (here.parent, *here.parents)]   # script dir + parents (repo)
    paths.append(Path.home() / ".env")                            # user home (server deploys)
    paths.append(Path.cwd() / ".env")                             # CWD
    return paths


def load_dotenv_upwards() -> None:
    for env in _candidate_paths():
        try:
            if not env.is_file():
                continue
        except OSError:
            continue
        for raw in env.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line[:7].lower() == "export ":
                line = line[7:]
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v
        print(f"[SIDECAR] loaded env from {env}")
        return
    print("[SIDECAR] no .env found — using the process environment only")
