# Contributing

Thanks for helping improve Tibb Flow Runner.

## Local setup

Use Python 3.11 or newer and install the project in an isolated environment:

```bash
python -m venv .venv
.venv/bin/pip install -e ".[stub,dev]"
cp .env.example .env
```

Never commit `.env`, a populated `data/workspaces/` directory, production credentials, or real user data.

## Making changes

1. Keep a change focused and add or update tests when behavior changes.
2. Preserve flow cleanup: resources created by a flow should be removed in `finally_steps` or another reliable teardown path.
3. Prefer context references over hard-coded IDs so flows can be rerun safely.
4. Validate graph changes before running them against an API.
5. Describe the behavior and verification in your pull request.

## Tests

Run the targeted regression suite before opening a pull request:

```bash
.venv/bin/pytest -q \
  tests/test_runner_primitives.py \
  tests/test_conditionals.py \
  tests/test_workspace_runner.py \
  tests/test_mcp_handshake.py
```

If you change dashboard or OpenAPI behavior, also exercise the affected workflow manually using a disposable workspace and test API.

## Style

- Keep Python code typed where practical and favor small, composable helpers.
- Keep JSON flows readable and give nodes stable, descriptive IDs.
- Do not weaken an assertion merely to make a flow pass; correct the expected behavior or make the failure explicit.
