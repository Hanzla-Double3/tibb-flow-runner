from types import SimpleNamespace

from flows.sdk import Call, FlowContext


class Result:
    status_code = 200

    def __init__(self, body):
        self.body = body

    def json(self):
        return self.body


def test_notification_refresh_does_not_lookup_notification_id_as_order():
    def tool(**kwargs):
        pass
    tool.operation = SimpleNamespace(method='POST', path='/api/v1/lab/orders/{report_id}/notifications/{notification_id}/refresh-status')
    ctx = FlowContext(SimpleNamespace(testing=SimpleNamespace()))
    assert ctx._persistence_checks(Call(tool=tool), Result({'id': 900, 'status': 'sent', 'channel': 'sms'}), {}) == []


def test_order_send_keeps_automatic_database_check():
    calls = []
    def report_state(**args):
        calls.append(args)
        return Result({'id': 12, 'fulfillment_status': 'sent'})
    def tool(**kwargs):
        pass
    tool.operation = SimpleNamespace(method='POST', path='/api/v1/lab/orders/{report_id}/send')
    ctx = FlowContext(SimpleNamespace(testing=SimpleNamespace(report_state=report_state)))
    checks = ctx._persistence_checks(Call(tool=tool), Result({'id': 12, 'fulfillment_status': 'sent'}), {})
    assert len(checks) == 1
    assert checks[0].fn(None, ctx) is True
    assert calls == [{'report_id': 12}]
