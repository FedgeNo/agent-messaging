import unittest

from agent_messaging import delivery
from agent_messaging.store import PASSIVE_RUNTIMES


class FakeClient:
    def __init__(self, runtime):
        self.item = {'runtime': runtime, 'projectKey': 'p', 'id': 1, 'attemptId': 2}
        self.reported = None

    def call(self, name, **fields):
        if name == 'claim': return {'delivery': self.item}
        self.reported = fields
        return {}


class ConnectorTests(unittest.TestCase):
    def test_registered_connector_receives_delivery(self):
        seen = []
        delivery.CONNECTORS['custom'] = lambda item, config: seen.append(item) or {'state': 'accepted', 'detail': 'ok'}
        try:
            client = FakeClient('custom')
            self.assertTrue(delivery.run_once(client, {}))
        finally:
            del delivery.CONNECTORS['custom']
        self.assertEqual(len(seen), 1)
        self.assertEqual(client.reported['state'], 'accepted')

    def test_runtime_without_connector_is_unavailable(self):
        client = FakeClient('unknown')
        delivery.run_once(client, {})
        self.assertEqual(client.reported['state'], 'unavailable')

    def test_builtin_connectors_and_passive_runtimes(self):
        self.assertEqual(set(delivery.CONNECTORS), {'codex', 'slipagent'})
        self.assertEqual(set(PASSIVE_RUNTIMES), {'mailbox', 'scheduled'})


if __name__ == '__main__':
    unittest.main()
