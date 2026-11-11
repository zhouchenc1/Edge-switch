"""Per-tool routing, migration and owned connection lifecycle regression checks."""
import subprocess
import shlex
import sys
import threading
import unittest
from unittest.mock import MagicMock, patch

from workbench import core
from workbench.app import App


class LifecycleTests(unittest.TestCase):
    def test_legacy_proxy_migrates_both_tools(self):
        profile = core.Profile(proxy=True)
        self.assertTrue(profile.uses_proxy('codex'))
        self.assertTrue(profile.uses_proxy('harness'))
        profile = core.Profile(proxy=True, codex_proxy=False)
        self.assertFalse(profile.uses_proxy('codex'))
        self.assertTrue(profile.uses_proxy('harness'))

    def test_direct_harness_does_not_require_codex_proxy(self):
        app = App.__new__(App)
        app.tunnels = {}
        board = MagicMock()
        board.profile = core.Profile(codex_proxy=True, harness_proxy=False)
        with patch('workbench.app.connect_proxy') as connect:
            self.assertEqual(app.proxy_url(board, 'harness'), '')
            connect.assert_not_called()

    def test_background_close_detaches_without_stopping_service(self):
        session = core.PersistentHarness.__new__(core.PersistentHarness)
        session.detach = MagicMock()
        session.board = MagicMock()
        session.background = True
        session.close()
        session.detach.assert_called_once()
        session.board.call.assert_not_called()
        session.background = False
        session.close()
        session.board.call.assert_called_once_with('harness_stop', timeout=25)

    def test_closed_connection_does_not_retry(self):
        board = MagicMock(profile=core.Profile())
        connection = MagicMock(port=39001, alive=False)
        factory = MagicMock(return_value=connection)
        tunnel = core.ManagedTunnel(board, 7890, factory=factory)
        tunnel.close()
        self.assertFalse(tunnel.alive)
        self.assertFalse(tunnel.thread.is_alive())
        factory.assert_called_once()

    def test_agent_transport_fits_windows_command_limit_and_hides_key(self):
        board = core.Board(core.Profile(), 'a' * 32)
        with patch.object(core.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, b'{}', b'')) as run:
            board.call('api_test', key='private-test-marker')
        args = run.call_args.args[0]
        self.assertLess(len(subprocess.list2cmdline(args)), 30000)
        self.assertNotIn('private-test-marker', str(args))
        self.assertIn(b'private-test-marker', run.call_args.kwargs['input'])

    def test_stdin_agent_bootstrap_executes_without_consuming_json_payload(self):
        board = core.Board(core.Profile(), 'invalid-scope')
        execute = subprocess.run

        def local_remote(args, **kwargs):
            # Execute the exact remote bootstrap against an invalid scope. It
            # must read the following JSON and reject it before writing files.
            command = shlex.split(args[-1])
            return execute([sys.executable, *command[1:]], **kwargs)

        with patch.object(core.subprocess, 'run', side_effect=local_remote):
            with self.assertRaisesRegex(RuntimeError, 'Invalid workspace ID'):
                board.call('prepare')


if __name__ == '__main__':
    unittest.main()
