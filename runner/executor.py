import time
from typing import Any, Callable

from flows.sdk import FlowContext, FlowReport, StepFailed


def run_flow(flow_fn: Callable, api: Any, data: dict | None = None,
             on_step: Callable[[Any], None] | None = None) -> FlowReport:
    ctx = FlowContext(api, data=data, on_step=on_step)
    start = time.monotonic()
    error = None
    try:
        flow_fn(ctx)
        status = "pass" if all(s.status == "pass" for s in ctx.steps) else "fail"
    except StepFailed as sf:
        status = "fail"
        error = sf.report.error or f"Step '{sf.report.name}' failed"
    except Exception as exc:
        status = "fail"
        error = f"unhandled error: {exc!r}"
    duration = time.monotonic() - start
    return FlowReport(
        name=getattr(flow_fn, "__flow_name__", flow_fn.__name__),
        status=status,
        steps=ctx.steps,
        duration_s=duration,
        error=error,
    )
