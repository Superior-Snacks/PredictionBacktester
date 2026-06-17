"""Shared: load the repo-root .env into os.environ (dependency-free).

Used by both app.py and the bookmaker_stomp.py smoke test so `BOOKMAKER_*`, `HARDVEN_*`, etc. can live
in the repo's .env (the same file the C# bot reads). Walks up from this file to find the nearest .env,
strips an optional `export ` prefix, splits on the FIRST `=` (so values containing `=`, like the WSS
URL's `?f=ws`, survive), and does NOT overwrite vars already set in the environment.
"""
from __future__ import annotations

import os
from pathlib import Path


def load_dotenv_upwards() -> None:
    here = Path(__file__).resolve()
    for d in (here.parent, *here.parents):
        env = d / ".env"
        if env.exists():
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
