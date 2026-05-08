"""Cross-invocation rate limiting via a SQLite file.

Same pattern as fast-flights / cd-trains: state persists across CLI
invocations and parallel processes via SQLite + a file lock. Default rates
are arbitrary — idos.cz's threshold isn't published.

Disable with `--no-rate-limit`, `IDOS_NO_RATE_LIMIT=1`, or `rate_limit=False`.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

from pyrate_limiter import Duration, Limiter, Rate
from pyrate_limiter.buckets import SQLiteBucket


DEFAULT_RATES = [
    Rate(5, Duration.SECOND * 5),
    Rate(30, Duration.MINUTE),
    Rate(300, Duration.HOUR),
]

ENV_DISABLE = "IDOS_NO_RATE_LIMIT"
BUCKET_NAME = "idos"


def _default_db_path() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Caches"
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"))
    return base / "idos" / "ratelimit.sqlite"


def make_limiter(rates: list[Rate] = DEFAULT_RATES, db_path: Optional[Path] = None) -> Limiter:
    path = Path(db_path) if db_path else _default_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    bucket = SQLiteBucket.init_from_file(rates, db_path=str(path), use_file_lock=True)
    return Limiter(bucket)


_GLOBAL: Optional[Limiter] = None


def shared() -> Optional[Limiter]:
    """Process-wide singleton, or None if disabled by env var."""
    global _GLOBAL
    if os.environ.get(ENV_DISABLE, "").lower() in ("1", "true", "yes", "on"):
        return None
    if _GLOBAL is None:
        _GLOBAL = make_limiter()
    return _GLOBAL
