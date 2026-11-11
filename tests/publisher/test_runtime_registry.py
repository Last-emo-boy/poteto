import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from publisher.runtime import RuntimeRegistry


class RuntimeRegistryTests(unittest.TestCase):
    def test_managed_runtime_bootstrap_is_retryable_without_general_installer(self):
        account = 'a' * 24
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'executor.json').write_text('{"url":"http://10.201.0.2:21999"}')
            (root / 'runtime-deploy.pub').write_text('fixture-public-key')
            (root / 'runtime-registry.json').write_text(json.dumps({'ssh_key_id': 'key', 'tenants': {}}))
            backend = Mock()
            backend.api.side_effect = lambda name, *args, **kwargs: [] if name == 'server.all' else {'serverId': 'server'}
            registry = RuntimeRegistry(backend, root)
            registry.executor = Mock()
            registry.executor.ensure_runtime.return_value = {'account_id': account, 'ssh_port': 22100, 'http_port': 22200}
            registry.executor.bootstrap.side_effect = [RuntimeError('interrupted'), {'prepared': True}, {'prepared': True}]
            with self.assertRaisesRegex(RuntimeError, 'interrupted'):
                registry.ensure(account)
            saved = lambda: json.loads((root / 'runtime-registry.json').read_text())['tenants'][account]
            self.assertFalse(saved()['prepared'])
            self.assertTrue(registry.ensure(account)['prepared'])
            self.assertTrue(registry.ensure(account)['prepared'])
            self.assertEqual(3, registry.executor.bootstrap.call_count)
            self.assertEqual(['server.all', 'server.create'], [call.args[0] for call in backend.api.call_args_list])
            self.assertTrue(saved()['prepared'])


if __name__ == '__main__':
    unittest.main()
