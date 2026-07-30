"""Fail clearly when the compatibility patch no longer matches Atopile."""

from __future__ import annotations

import sys

from atopile_compat import require_supported_atopile_version


try:
    require_supported_atopile_version()
except RuntimeError as exc:
    print(f"pcbforge: {exc}", file=sys.stderr)
    raise SystemExit(2) from exc
