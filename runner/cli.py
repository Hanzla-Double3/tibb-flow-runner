import argparse
import sys
from pathlib import Path

from core import flow_store, workspace_store
from core.client.factory import build_api_client
from core.graph_builder import make_flow_fn
from runner.discovery import discover_flows
from runner.executor import run_flow
from runner.reporter import print_report, to_json
from runner.workspace import run_workspace_flows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="runner")
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="Run flows found at a file or directory")
    run_p.add_argument("path", type=Path)
    run_p.add_argument("--json", action="store_true", help="Print a JSON report instead of text")

    run_ws_p = sub.add_parser("run-workspace", help="Run every flow in a workspace, in order")
    run_ws_p.add_argument("workspace_id")
    run_ws_p.add_argument("--json", action="store_true", help="Print a JSON report instead of text")

    args = parser.parse_args(argv)

    if args.command == "run":
        api = build_api_client()

        flows = discover_flows(args.path)
        if not flows:
            print(f"No flows found under {args.path}", file=sys.stderr)
            return 1

        reports = [run_flow(fn, api) for fn in flows]

        if args.json:
            print(to_json(reports))
        else:
            for report in reports:
                print_report(report)
                print()

        return 0 if all(r.status == "pass" for r in reports) else 1

    if args.command == "run-workspace":
        workspace = workspace_store.load_workspace(args.workspace_id)
        reports = run_workspace_flows(args.workspace_id)

        if args.json:
            print(to_json(reports))
        else:
            print(f"workspace: {workspace['name']}")
            for report in reports:
                print_report(report)
                print()

        return 0 if all(r.status == "pass" for r in reports) else 1

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
