import json
from dataclasses import asdict

from flows.sdk import FlowReport, StepReport

MARK = {"pass": "PASS", "fail": "FAIL"}


def print_report(report: FlowReport) -> None:
    print(f"[{MARK[report.status]}] flow: {report.name} ({report.duration_s:.3f}s)")
    for step in report.steps:
        _print_step(step, indent=1)
    if report.error:
        print(f"  error: {report.error}")


def _print_step(step: StepReport, indent: int) -> None:
    pad = "  " * indent
    print(f"{pad}[{MARK[step.status]}] {step.kind}: {step.name}")
    if step.error:
        for line in step.error.rstrip().splitlines():
            print(f"{pad}    {line}")
    for child in step.children:
        _print_step(child, indent + 1)


def to_json(reports: list[FlowReport]) -> str:
    return json.dumps([asdict(r) for r in reports], indent=2, default=str)
