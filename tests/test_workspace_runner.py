from core import flow_store, workspace_store
from runner.workspace import run_workspace_flows


def test_workspace_runner_runs_all_saved_flows_sequentially_and_emits_statuses():
    workspace_id = "test_workspace_runner"
    if workspace_store.get_workspace_dir(workspace_id).exists():
        workspace_store.delete_workspace(workspace_id)
    workspace_store.create_workspace(workspace_id, name="Workspace Runner Test")
    events = []
    try:
        # Flow names are intentionally out of order: store/listing order is the
        # deterministic suite order used by the workspace runner.
        for name in ("z_second", "a_first"):
            flow_store.save_flow(name, {"name": name, "steps": []}, workspace_id=workspace_id)

        reports = run_workspace_flows(workspace_id, on_event=events.append)

        assert [report.name for report in reports] == ["a_first", "z_second"]
        assert all(report.status == "pass" for report in reports)
        assert [(event["type"], event["flow"]) for event in events] == [
            ("flow_start", "a_first"), ("flow_done", "a_first"),
            ("flow_start", "z_second"), ("flow_done", "z_second"),
        ]
        assert events[0]["index"] == 1 and events[0]["total"] == 2
        assert events[2]["index"] == 2 and events[2]["total"] == 2
    finally:
        workspace_store.delete_workspace(workspace_id)
