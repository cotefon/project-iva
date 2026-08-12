"""Repo-relative locations, resolved from this file rather than the cwd.

The service lives in `apps/api/` but its inputs and configuration live at the
repository root — `.env` beside the README, the source PDFs in `docs/`, the CLI's
generated Markdown in `outputs/`. Turborepo, uvicorn and a hand-run
`python extract_codes.py` each start with a different working directory, so
anything anchored on `os.getcwd()` resolves somewhere different depending on how
it was launched. Anchoring on `__file__` makes every path the same no matter who
starts the process.
"""

from __future__ import annotations

from pathlib import Path

# apps/api/paths.py -> apps/api -> apps -> repo root
API_DIR = Path(__file__).resolve().parent
REPO_ROOT = API_DIR.parents[1]

ENV_FILE = REPO_ROOT / ".env"
DOCS_DIR = REPO_ROOT / "docs"
OUTPUTS_DIR = REPO_ROOT / "outputs"


def from_root(path) -> Path:
    """Resolve `path` against the repo root when it is relative.

    Lets `.env` keep writing `PDF_FILE=docs/CARPETA TRIBUTARIA FRUTAM.pdf` — a
    path that reads naturally and stays correct — instead of a
    `../../docs/...` chain that only works from one directory. An absolute path
    is returned unchanged.
    """
    p = Path(path)
    return p if p.is_absolute() else REPO_ROOT / p
