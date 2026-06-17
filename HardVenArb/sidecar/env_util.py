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

import os
from pathlib import Path


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
