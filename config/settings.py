import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    base_url: str
    testing_api_key: str | None


def load_settings() -> Settings:
    return Settings(
        base_url=os.environ.get("TIBB_BASE_URL", "http://localhost:8000"),
        testing_api_key=os.environ.get("TIBB_TESTING_API_KEY"),
    )


# Which OpenAPI spec files to load. Normally unset -- workspace specs (openapi.json
# and custom_apis.json in data/workspaces/<workspace_id>/) are the source of truth.
# Override only for scripts/tests that want a fixed file set without touching workspaces:
# TIBB_SPEC_FILES=stub_api/demo_main_api.yaml,stub_api/demo_tester_api.yaml
SPEC_FILES_OVERRIDE: list[str] | None = (
    [p.strip() for p in os.environ["TIBB_SPEC_FILES"].split(",") if p.strip()]
    if os.environ.get("TIBB_SPEC_FILES")
    else None
)

# Optional: restrict which operations become tools/client functions, e.g.
# TIBB_SPEC_TAGS=auth,lab-orders,patients -- unset means "all operations".
SPEC_TAGS: set[str] | None = (
    {t.strip() for t in os.environ["TIBB_SPEC_TAGS"].split(",") if t.strip()}
    if os.environ.get("TIBB_SPEC_TAGS")
    else None
)
