"""Pull a live OpenAPI spec and write it directly to a workspace or file path.

Usage:
  python scripts/pull_openapi_spec.py http://localhost:8000/openapi.json [workspace_id or file_path]
"""

import sys
from pathlib import Path
import requests

from core import workspace_store


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    url = sys.argv[1]
    target = sys.argv[2] if len(sys.argv) > 2 else workspace_store.get_active_workspace_id()

    response = requests.get(url, timeout=10)
    response.raise_for_status()

    if "/" not in target and "\\" not in target and not target.endswith(".json") and not target.endswith(".yaml"):
        out_path = workspace_store.get_workspace_spec_path(target)
    else:
        out_path = Path(target)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(response.text)
    print(f"Wrote OpenAPI spec to {out_path} ({len(response.content)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
