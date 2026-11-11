"""Isolation, credentials and SSH lifecycle regression checks."""
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import urllib.error

from workbench import core, remote_agent


class ProfileTests(unittest.TestCase):
    def test_vscode_reuses_existing_remote_ssh_without_install_network(self):
        with tempfile.TemporaryDirectory() as directory:
            board = MagicMock()
            board.profile = core.Profile()
            board.scope = 'a' * 32
            board.ssh.return_value = ['ssh.exe']
            board.call.return_value = dict(flock=True)
            calls = []
            def run(args, **kwargs):
                calls.append(args)
                if '--new-window' in args:
                    log = Path(directory) / 'logs/new/window1/exthost/output_logging_new/Remote SSH.log'
                    log.parent.mkdir(parents=True)
                    log.write_text('Resolved "ssh-remote+edge-' + board.scope + '" to port')
                return subprocess.CompletedProcess(args, 0, b'ms-vscode-remote.remote-ssh\n', b'')
            with patch('workbench.vscode_server.prepare_server', return_value=dict(path='/private/server', ready=True)), patch('workbench.editor_runtime.editor_for', return_value=('Code.exe', 'cli.js', {}, 'a' * 40)), \
                    patch.object(core.subprocess, 'run', side_effect=run):
                result = core.open_vscode(board, directory)
            self.assertFalse(any('--install-extension' in args for args in calls))
            self.assertTrue(any('--reuse-window' in args for args in calls))
            self.assertFalse(any('--new-window' in args for args in calls))
            self.assertNotIn('已确认 VS Code Remote SSH 隧道', result)
            self.assertIn('SSH', result)

    def test_vscode_installs_remote_extension_and_sets_private_launcher(self):
        with tempfile.TemporaryDirectory() as directory:
            board = MagicMock()
            board.profile = core.Profile(protocol='responses', model='gpt-6.1-sol')
            board.scope = 'a' * 32
            board.ssh.return_value = ['ssh.exe']
            board.call.side_effect = [dict(flock=True), dict(ready=True, message='扩展已验证'), dict(cli='/home/user/private/run-ide-codex')]
            cli_args = []
            def run(args, **kwargs):
                cli_args.append(args)
                if '--new-window' in args:
                    log = Path(directory) / 'logs/new/window1/exthost/output_logging_new/Remote - SSH.log'
                    log.parent.mkdir(parents=True)
                    log.write_text('Resolved "ssh-remote+edge-' + board.scope + '" to port')
                return subprocess.CompletedProcess(args, 0, b'', b'')
            with patch('workbench.vscode_server.prepare_server', return_value=dict(path='/private/server', ready=True)), patch('workbench.editor_runtime.editor_for', return_value=('Code.exe', 'cli.js', {}, 'a' * 40)), \
                    patch.object(core.subprocess, 'run', side_effect=run):
                result = core.open_vscode(board, directory, api=dict(key='test-private-key', model='gpt-6.1-sol'))
            settings = json.loads((Path(directory) / 'User/settings.json').read_text())
            self.assertEqual(settings['chatgpt.cliExecutable'], '/home/user/private/run-ide-codex')
            self.assertIn('prepare_ide', [call.args[0] for call in board.call.call_args_list])
            self.assertFalse(any('openai.chatgpt' in args for args in cli_args))
            self.assertNotIn('test-private-key', str(cli_args))
            self.assertNotIn('test-private-key', str(settings))
            self.assertIn('API', result)

    def test_model_display_name_is_not_accepted_as_api_id(self):
        with self.assertRaisesRegex(ValueError, '不在当前供应商'):
            core.validate_model_selection('GPT6.1', ['gpt-6.1-sol', 'gpt-6.1-terra'])
        core.validate_model_selection('gpt-6.1-sol', ['gpt-6.1-sol'])
        core.validate_model_selection('custom-model', None)

    def test_save_retries_transient_windows_file_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            store = core.Store(directory)
            replace = os.replace
            calls = []
            def locked_once(source, dest):
                calls.append(True)
                if len(calls) == 1:
                    raise PermissionError('temporary scanner lock')
                return replace(source, dest)
            with patch.object(core.os, 'replace', side_effect=locked_once), patch.object(core.time, 'sleep'):
                store.save()
            self.assertEqual(len(calls), 2)
            self.assertTrue(store.path.exists())

    def test_rejects_ssh_option_and_command_injection(self):
        for field, value in [('host', '-oProxyCommand=bad'), ('host', 'board;id'),
                             ('user', 'user$(id)'), ('project', '/tmp\nexit'),
                             ('base_url', 'https://key@provider.test/v1'),
                             ('base_url', 'http://provider.test/v1')]:
            p = core.Profile()
            setattr(p, field, value)
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                p.validate()

    def test_scope_is_stable_per_client_and_unique_between_clients(self):
        with tempfile.TemporaryDirectory() as directory:
            a = core.Store(Path(directory) / 'a')
            b = core.Store(Path(directory) / 'b')
            p = a.profiles[0]
            self.assertNotEqual(a.scope(p), b.scope(p))
            a.save()
            self.assertEqual(a.scope(p), core.Store(Path(directory) / 'a').scope(p))

    @unittest.skipUnless(os.name == 'nt', 'Windows DPAPI')
    def test_credential_roundtrip_has_no_plaintext_in_store(self):
        with tempfile.TemporaryDirectory() as directory:
            store = core.Store(directory)
            key = 'test-secret-中文-987654'
            p = store.profiles[0]
            store.secrets[p.id] = core.protect(key)
            store.save()
            self.assertNotIn(key, store.path.read_text(encoding='utf-8'))
            self.assertEqual(core.Store(directory).key(p), key)

    def test_remote_credentials_only_travel_on_stdin(self):
        board = core.Board(core.Profile(), 'a' * 32)
        result = subprocess.CompletedProcess([], 0, b'{"ok": true}', b'')
        with patch('workbench.core.subprocess.run', return_value=result) as run:
            board.call('api_test', key='secret-marker', base_url='https://example.com')
        args, kwargs = run.call_args
        self.assertNotIn('secret-marker', str(args))
        self.assertIn(b'secret-marker', kwargs['input'])
        self.assertIn('StrictHostKeyChecking=yes', args[0])

    def test_only_interactive_ssh_can_ask_about_new_host(self):
        board = core.Board(core.Profile(), 'a' * 32)
        self.assertIn('StrictHostKeyChecking=yes', board.ssh())
        self.assertIn('StrictHostKeyChecking=ask', board.ssh(batch=False))


class RemoteTests(unittest.TestCase):
    def test_ide_wrapper_uses_bundled_cli_and_private_auth(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(Path, 'home', return_value=Path(directory)):
            home = Path(directory)
            p = dict(scope='f' * 32, project=directory, model='gpt-6.1-sol', base_url='https://example.test/v1', key='test-key')
            root = remote_agent.scope(p)
            current = root / 'tools/codex/current.json'
            current.parent.mkdir(parents=True)
            current.write_text('{"binary":"managed-codex"}')
            bundled = home / '.vscode-server/extensions/openai.chatgpt-test/bin/linux-aarch64/codex'
            bundled.parent.mkdir(parents=True)
            bundled.touch()
            bundled.chmod(0o700)
            (bundled.parent.parent.parent / 'package.json').write_text(json.dumps(dict(publisher='openai', name='chatgpt', version='1.0.0')))
            with patch.object(remote_agent.platform, 'machine', return_value='aarch64'), \
                    patch.object(remote_agent, 'prepare_codex_history'), \
                    patch.object(remote_agent, 'command', side_effect=['features', 'Logged in using an API key']):
                result = remote_agent.configure_ide(p)
            self.assertEqual(result['auth'], 'API Key')
            self.assertEqual(json.loads((root / 'ide-runtime.json').read_text())['binary'], str(bundled))
            self.assertEqual(json.loads((root / 'codex-home/auth.json').read_text())['OPENAI_API_KEY'], 'test-key')
            self.assertNotIn('test-key', (root / 'run-ide-codex').read_text())
            self.assertIn('sys.argv[1:]', (root / 'run-ide-codex').read_text())
            self.assertFalse((home / '.codex').exists())
            self.assertFalse((home / '.vscode-server/server-env-setup').exists())

    def test_ide_missing_extension_does_not_replace_active_config(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(Path, 'home', return_value=Path(directory)):
            p = dict(scope='f' * 32)
            with patch.object(remote_agent, 'configure') as configure:
                with self.assertRaisesRegex(RuntimeError, '扩展'):
                    remote_agent.configure_ide(p)
            configure.assert_not_called()

    def test_reusing_ide_key_never_sends_it_to_a_changed_provider(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(Path, 'home', return_value=Path(directory)):
            p = dict(scope='f' * 32, reuse=True, base_url='https://different.test/v1')
            root = remote_agent.scope(p)
            (root / 'codex-home').mkdir(parents=True)
            (root / 'launch.json').write_text('{"key":"private-key"}')
            (root / 'codex-home/config.toml').write_text('base_url = "https://original.test/v1"\nmodel = "model"\n')
            with patch.object(remote_agent, 'configure') as configure:
                with self.assertRaisesRegex(ValueError, 'API 地址已改变'):
                    remote_agent.configure_ide(p)
            configure.assert_not_called()

    def test_command_timeout_stops_own_process_group(self):
        child = MagicMock()
        child.pid = 12345
        child.communicate.side_effect = [subprocess.TimeoutExpired(['npm'], 10), ('', None)]
        with patch.object(remote_agent.subprocess, 'Popen', return_value=child) as popen, \
                patch.object(remote_agent.os, 'killpg', create=True) as killpg:
            with self.assertRaisesRegex(RuntimeError, '操作超时'):
                remote_agent.command(['npm'], timeout=10)
        self.assertTrue(popen.call_args.kwargs['start_new_session'])
        self.assertEqual(killpg.call_args.args[0], 12345)

    def test_failed_install_preserves_current_version(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(Path, 'home', return_value=Path(directory)):
            p = dict(scope='d' * 32, tool='codex', version='1.2.3')
            root = remote_agent.scope(p)
            node = root / 'runtime/bin/node'
            node.parent.mkdir(parents=True)
            node.touch()
            current = root / 'tools/codex/current.json'
            current.parent.mkdir(parents=True)
            current.write_text('{"version":"old"}')
            with patch.object(remote_agent, 'command', side_effect=RuntimeError('network failed')):
                with self.assertRaises(RuntimeError):
                    remote_agent.install_tool(p)
            self.assertEqual(json.loads(current.read_text())['version'], 'old')
            self.assertFalse((current.parent / '1.2.3').exists())

    def test_install_env_does_not_inherit_api_credentials(self):
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'secret', 'DEEPSEEK_API_KEY': 'secret', 'GITHUB_TOKEN': 'secret'}):
            env = remote_agent.tool_env({}, Path('/tmp/example'))
        for key in ('OPENAI_API_KEY', 'DEEPSEEK_API_KEY', 'GITHUB_TOKEN'):
            self.assertNotIn(key, env)

    def test_proxy_does_not_inherit_shared_shell_settings(self):
        with patch.dict(os.environ, {'HTTP_PROXY': 'http://bad:1', 'all_proxy': 'socks5://bad:2'}):
            direct = remote_agent.clean_env()
            self.assertNotIn('HTTP_PROXY', direct)
            self.assertNotIn('all_proxy', direct)
            proxied = remote_agent.clean_env('http://127.0.0.1:23456')
            self.assertEqual(proxied['HTTPS_PROXY'], 'http://127.0.0.1:23456')
            self.assertNotIn('all_proxy', proxied)

    def test_codex_config_never_writes_shared_home(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(Path, 'home', return_value=Path(directory)):
            home = Path(directory)
            shared = home / '.codex/config.toml'
            shared.parent.mkdir()
            shared.write_text('original-shared-config')
            p = dict(scope='b' * 32, project=directory, model='model-id',
                     base_url='https://example.test/v1', key='test-key', proxy='')
            root = remote_agent.scope(p)
            current = root / 'tools/codex/current.json'
            current.parent.mkdir(parents=True)
            current.write_text('{"binary":"test-codex"}')
            with patch.object(remote_agent, 'prepare_codex_history'), \
                    patch.object(remote_agent, 'command', return_value='features'):
                remote_agent.configure(p)
            self.assertEqual(shared.read_text(), 'original-shared-config')
            self.assertFalse((home / '.bashrc').exists())
            self.assertFalse((home / '.vscode-server').exists())
            launch = root / 'launch.json'
            self.assertEqual(json.loads(launch.read_text())['key'], 'test-key')
            if os.name != 'nt':
                self.assertEqual(launch.stat().st_mode & 0o777, 0o600)

    def test_rejected_codex_config_does_not_replace_previous_profile(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(Path, 'home', return_value=Path(directory)):
            p = dict(scope='e' * 32, project=directory, model='model', base_url='https://example.test', key='key')
            root = remote_agent.scope(p)
            current = root / 'tools/codex/current.json'
            current.parent.mkdir(parents=True)
            current.write_text('{"binary":"test-codex"}')
            profile = root / 'codex-home/config.toml'
            profile.parent.mkdir()
            profile.write_text('previous-profile')
            with patch.object(remote_agent, 'command', side_effect=RuntimeError('unsupported config')):
                with self.assertRaises(RuntimeError):
                    remote_agent.configure(p)
            self.assertEqual(profile.read_text(), 'previous-profile')
            self.assertFalse((root / 'launch.json').exists())

    def test_http_401_and_redirect_are_failures_without_body_leaks(self):
        for status in (401, 302, 429):
            opener = MagicMock()
            opener.open.side_effect = urllib.error.HTTPError('https://test', status, 'bad', {}, io.BytesIO(b'secret'))
            with patch.object(remote_agent.urllib.request, 'build_opener', return_value=opener):
                result = remote_agent.api_test(dict(base_url='https://test/v1', key='secret', protocol='responses'))
            self.assertFalse(result['ok'])
            self.assertNotIn('secret', str(result))

    def test_api_uses_explicit_client_headers(self):
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value.read.return_value = b'{"data":[]}'
        with patch.object(remote_agent.urllib.request, 'build_opener', return_value=opener):
            remote_agent.api_test(dict(base_url='https://test', key='key', protocol='responses'))
        request = opener.open.call_args.args[0]
        self.assertEqual(request.get_header('User-agent'), 'EdgeSwitch/1.1.3')
        self.assertEqual(request.get_header('Accept'), 'application/json')

    def test_gateway_403_is_distinguished_from_authentication(self):
        opener = MagicMock()
        opener.open.side_effect = urllib.error.HTTPError('https://test', 403, 'Forbidden',
            {'Content-Type': 'text/html'}, io.BytesIO(b'<html>secret-in-page</html>'))
        with patch.object(remote_agent.urllib.request, 'build_opener', return_value=opener):
            result = remote_agent.api_test(dict(base_url='https://test', key='key', protocol='responses'))
        self.assertEqual(result['category'], 'gateway')
        self.assertNotIn('secret-in-page', str(result))

    def test_no_channel_error_explains_model_and_provider_routing(self):
        opener = MagicMock()
        body = json.dumps({'error': {'message': '分组 codex 下模型 GPT6.1 无可用渠道（distributor）'}}).encode()
        opener.open.side_effect = urllib.error.HTTPError('https://test', 503, 'Unavailable',
            {'Content-Type': 'application/json'}, io.BytesIO(body))
        with patch.object(remote_agent.urllib.request, 'build_opener', return_value=opener):
            result = remote_agent.api_test(dict(base_url='https://test', key='private-key', protocol='responses'))
        self.assertEqual(result['category'], 'provider_unavailable')
        self.assertIn('模型 ID', result['message'])
        self.assertIn('分组权限', result['message'])

    def test_api_error_message_is_redacted(self):
        opener = MagicMock()
        body = json.dumps({'error': {'message': 'Invalid key private-key sk-abcdef Bearer other-token'}}).encode()
        opener.open.side_effect = urllib.error.HTTPError('https://test', 401, 'Unauthorized',
            {'Content-Type': 'application/json'}, io.BytesIO(body))
        with patch.object(remote_agent.urllib.request, 'build_opener', return_value=opener):
            result = remote_agent.api_test(dict(base_url='https://test', key='private-key', protocol='responses'))
        self.assertEqual(result['category'], 'api')
        for secret in ('private-key', 'sk-abcdef', 'other-token'):
            self.assertNotIn(secret, str(result))

    def test_html_success_and_empty_inference_are_not_success(self):
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value.read.return_value = b'{"output": [], "status": "completed"}'
        with patch.object(remote_agent.urllib.request, 'build_opener', return_value=opener):
            result = remote_agent.api_test(dict(base_url='https://test', key='key', model='x', protocol='responses', infer=True))
        self.assertFalse(result['ok'])

    def test_model_list_does_not_claim_inference(self):
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value.read.return_value = b'{"data": [{"id":"model"}]}'
        with patch.object(remote_agent.urllib.request, 'build_opener', return_value=opener):
            result = remote_agent.api_test(dict(base_url='https://test', key='key', protocol='responses'))
        self.assertTrue(result['ok'])
        self.assertEqual(result['models'], ['model'])
        self.assertIn('尚未验证推理', result['message'])

    def test_reasoning_only_is_not_a_successful_answer(self):
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value.read.return_value = b'{"output":[{"type":"reasoning"}],"status":"incomplete"}'
        with patch.object(remote_agent.urllib.request, 'build_opener', return_value=opener):
            result = remote_agent.api_test(dict(base_url='https://test', key='key', model='x', protocol='responses', infer=True))
        self.assertFalse(result['ok'])

    def test_completed_text_is_a_successful_answer(self):
        opener = MagicMock()
        value = dict(output=[dict(type='message', content=[dict(type='output_text', text='OK')])], status='completed')
        opener.open.return_value.__enter__.return_value.read.return_value = json.dumps(value).encode()
        with patch.object(remote_agent.urllib.request, 'build_opener', return_value=opener):
            result = remote_agent.api_test(dict(base_url='https://test', key='key', model='x', protocol='responses', infer=True))
        self.assertTrue(result['ok'])


class TunnelTests(unittest.TestCase):
    def test_harness_access_token_is_kept_out_of_diagnostics(self):
        session = core.HarnessSession.__new__(core.HarnessSession)
        session.process = MagicMock()
        session.process.stdout = io.BytesIO(b'dsh web: http://127.0.0.1:3080/?token=private-session-token\n')
        session.lines = []
        session.token = ''
        session._read()
        self.assertEqual(session.token, 'private-session-token')
        self.assertNotIn('private-session-token', str(session.lines))

    def test_port_is_allocated_by_ssh_and_only_owned_process_is_stopped(self):
        child = MagicMock()
        child.stderr = io.BytesIO(b'Allocated port 34567 for remote forward to 127.0.0.1:7890\n')
        child.poll.return_value = None
        with patch('workbench.core.socket.create_connection'), patch('workbench.core.subprocess.Popen', return_value=child) as popen:
            tunnel = core.Tunnel(core.Board(core.Profile(), 'c' * 32), 7890)
            self.assertEqual(tunnel.port, 34567)
            self.assertIn('127.0.0.1:0:127.0.0.1:7890', popen.call_args.args[0])
            tunnel.close()
        child.terminate.assert_called_once()
        child.kill.assert_not_called()


if __name__ == '__main__':
    unittest.main()
