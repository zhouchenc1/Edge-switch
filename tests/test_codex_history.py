"""History survives managed Codex account/provider switches on Linux boards."""
import json
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from workbench import remote_agent


def session(path, provider, ident, body=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    header = dict(type='session_meta', payload=dict(id=ident, model_provider=provider))
    body = body or b'{"type":"event_msg","payload":{"text":"openai must stay in the message"}}\n'
    content = json.dumps(header).encode() + b'\n' + body
    path.write_bytes(content)
    return content


def database(path, ident, provider, with_tools=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute('CREATE TABLE threads (id TEXT PRIMARY KEY, model_provider TEXT NOT NULL, title TEXT)')
    connection.execute('INSERT INTO threads VALUES (?, ?, ?)', (ident, provider, ident + ' title'))
    if with_tools:
        connection.execute('CREATE TABLE thread_dynamic_tools '
                           '(thread_id TEXT REFERENCES threads(id), name TEXT, PRIMARY KEY(thread_id, name))')
        connection.execute('INSERT INTO thread_dynamic_tools VALUES (?, ?)', (ident, 'test_tool'))
    connection.commit()
    return connection


class CodexHistoryConfigTests(unittest.TestCase):
    def test_failed_migration_in_only_source_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'source.sqlite'
            with closing(sqlite3.connect(source)) as db, db:
                db.execute('CREATE TABLE _sqlx_migrations (version INTEGER PRIMARY KEY, checksum BLOB, success BOOLEAN)')
                db.execute('INSERT INTO _sqlx_migrations VALUES (62, ?, 0)', (b'incomplete',))
            original = source.read_bytes()
            with self.assertRaisesRegex(ValueError, '未成功完成'):
                remote_agent._merge_codex_history_db([source], root / 'merged.sqlite')
            self.assertEqual(source.read_bytes(), original)

    def test_older_migration_prefix_imports_into_newer_schema_in_either_order(self):
        for reverse in (False, True):
            with self.subTest(reverse=reverse), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                paths = [root / 'old.sqlite', root / 'new.sqlite']
                for index, path in enumerate(paths):
                    with closing(sqlite3.connect(path)) as db, db:
                        db.execute('CREATE TABLE threads (id TEXT PRIMARY KEY, model_provider TEXT NOT NULL, title TEXT)')
                        db.execute('CREATE TABLE _sqlx_migrations (version INTEGER PRIMARY KEY, checksum BLOB, success BOOLEAN)')
                        db.execute('INSERT INTO _sqlx_migrations VALUES (59, ?, 1)', (b'common',))
                        if index:
                            db.execute('ALTER TABLE threads ADD COLUMN attached_thread_parent_id TEXT')
                            db.execute("ALTER TABLE threads ADD COLUMN is_pinned INTEGER NOT NULL DEFAULT 0")
                            db.execute('CREATE TABLE thread_read_receipts (thread_id TEXT PRIMARY KEY REFERENCES threads(id), read_at INTEGER)')
                            db.execute('INSERT INTO _sqlx_migrations VALUES (62, ?, 1)', (b'newer',))
                        db.execute('INSERT INTO threads (id,model_provider,title) VALUES (?, ?, ?)',
                                   ('new' if index else 'old', 'openai' if index else 'edge', 'fixture'))
                        if index:
                            db.execute('INSERT INTO thread_read_receipts VALUES (?, ?)', ('new', 123))
                dest = root / 'merged.sqlite'
                remote_agent._merge_codex_history_db(list(reversed(paths)) if reverse else paths, dest)
                with closing(sqlite3.connect(dest)) as db:
                    self.assertEqual(db.execute('SELECT id,model_provider,is_pinned,attached_thread_parent_id FROM threads ORDER BY id').fetchall(),
                                     [('new', 'edge', 0, None), ('old', 'edge', 0, None)])
                    self.assertEqual(db.execute('SELECT * FROM thread_read_receipts').fetchall(), [('new', 123)])
                    self.assertEqual(db.execute('SELECT MAX(version) FROM _sqlx_migrations').fetchone()[0], 62)

    def test_incompatible_checksums_or_required_columns_keep_source_databases(self):
        for invalid in ('checksum', 'required_column', 'removed_column'):
            with self.subTest(invalid=invalid), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                paths = [root / 'old.sqlite', root / 'new.sqlite']
                for index, path in enumerate(paths):
                    with closing(sqlite3.connect(path)) as db, db:
                        db.execute('CREATE TABLE threads (id TEXT PRIMARY KEY, model_provider TEXT)')
                        db.execute('CREATE TABLE _sqlx_migrations (version INTEGER PRIMARY KEY, checksum BLOB, success BOOLEAN)')
                        db.execute('INSERT INTO _sqlx_migrations VALUES (59, ?, 1)',
                                   (b'different' if index and invalid == 'checksum' else b'common',))
                        if index:
                            db.execute('INSERT INTO _sqlx_migrations VALUES (62, ?, 1)', (b'newer',))
                        if (index and invalid == 'required_column') or (not index and invalid == 'removed_column'):
                            db.execute('ALTER TABLE threads ADD COLUMN extra TEXT NOT NULL')
                before = [path.read_bytes() for path in paths]
                with self.assertRaises(ValueError):
                    remote_agent._merge_codex_history_db(paths, root / 'merged.sqlite')
                self.assertEqual([path.read_bytes() for path in paths], before)

    def test_api_and_official_use_one_history_bucket_and_database_without_sharing_auth(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            current = root / 'tools/codex/current.json'
            current.parent.mkdir(parents=True)
            current.write_text(json.dumps(dict(binary='test-codex')))
            official_auth = root / 'codex-chatgpt-home/auth.json'
            official_auth.parent.mkdir()
            official_auth.write_text('{"tokens":{"access_token":"official-fixture"}}')
            payload = dict(project=folder, model='test-model', key='api-fixture',
                           base_url='https://api.example.test/v1', proxy='')
            with patch.object(remote_agent, 'scope', return_value=root), \
                 patch.object(remote_agent, 'command', return_value='features'), \
                 patch.object(remote_agent, 'prepare_codex_history') as prepare:
                remote_agent.configure(payload)
                api_config = (root / 'codex-home/config.toml').read_text()
                remote_agent.configure(dict(payload, auth_mode='chatgpt'))
                official_config = (root / 'codex-chatgpt-home/config.toml').read_text()
            for config in (api_config, official_config):
                self.assertIn('model_provider = "edge"', config)
                self.assertIn('sqlite_home = ' + json.dumps(str(root / 'codex-history')), config)
            self.assertIn('name = "OpenAI"', official_config)
            self.assertIn('requires_openai_auth = true', official_config)
            self.assertNotIn('env_key', official_config)
            self.assertNotIn('base_url', official_config)
            self.assertEqual(json.loads(official_auth.read_text())['tokens']['access_token'], 'official-fixture')
            self.assertEqual(json.loads((root / 'codex-home/auth.json').read_text())['OPENAI_API_KEY'], 'api-fixture')
            self.assertEqual(prepare.call_count, 2)


class CodexRolloutMetadataTests(unittest.TestCase):
    def test_replayed_metadata_cannot_restore_the_old_provider(self):
        records = [
            dict(type='session_meta', payload=dict(id='current', model_provider='openai')),
            dict(type='session_meta', payload=dict(id='ancestor', model_provider='openai')),
            dict(type='session_meta', payload=dict(id='current', model_provider='openai')),
            dict(type='event_msg', payload=dict(type='thread_settings_applied', thread_id='ancestor',
                 thread_settings=dict(model_provider_id='openai', model='preserved-model'))),
            dict(type='event_msg', payload=dict(type='thread_settings_applied',
                 thread_settings=dict(model_provider_id='other-provider'))),
            dict(type='event_msg', payload=dict(type='thread_settings_applied',
                 thread_settings=dict(model_provider_id='openai'))),
        ]
        source = b'\n'.join(json.dumps(record).encode() for record in records)
        normalized = remote_agent._normalize_codex_rollout(source)
        actual = [json.loads(line) for line in normalized.splitlines()]
        self.assertEqual([item['payload']['model_provider'] for item in actual[:3]],
                         ['edge', 'openai', 'edge'])
        self.assertEqual([item['payload']['thread_settings']['model_provider_id'] for item in actual[3:]],
                         ['edge', 'other-provider', 'edge'])
        self.assertEqual(actual[3]['payload']['thread_settings']['model'], 'preserved-model')
        self.assertFalse(normalized.endswith(b'\n'))
        self.assertEqual(remote_agent._normalize_codex_rollout(normalized), normalized)

    def test_message_bytes_and_embedded_metadata_lookalikes_are_untouched(self):
        header = b'{ "type" : "session_meta", "payload" : {"id":"current","model_provider":"edge"}}\r\n'
        body = (
            b'{ "type":"response_item", "payload":{"type":"message","model_provider":"openai",'
            b'"nested":{"type":"session_meta","payload":{"id":"current","model_provider":"openai"}}}}\r\n'
            b'{"type":"turn_context","payload":{"model_provider":"openai"}}\n'
            b'{"type":"event_msg","payload":{"type":"user_message","thread_settings":{"model_provider_id":"openai"}}}\r\n'
            b'{"type":"compacted","payload":{"message":"openai"}}\n'
            b'{"type":"response_item","payload":{"text":"truncated'
        )
        self.assertEqual(remote_agent._normalize_codex_rollout(header + body), header + body)
        changed_header = header.replace(b'"edge"', b'"openai"')
        normalized = remote_agent._normalize_codex_rollout(changed_header + body)
        first, separator, remaining = normalized.partition(b'\r\n')
        self.assertEqual(separator, b'\r\n')
        self.assertEqual(json.loads(first)['payload']['model_provider'], 'edge')
        self.assertEqual(remaining, body)

    def test_invalid_header_is_rejected_before_migration(self):
        for content in (b'', b'not json\n', b'[]\n', b'{"type":"session_meta","payload":null}\n'):
            with self.subTest(content=content), self.assertRaises(ValueError):
                remote_agent._normalize_codex_rollout(content)


@unittest.skipUnless(os.name == 'posix', 'Remote migration requires Linux fcntl and symlinks')
class CodexHistoryMigrationTests(unittest.TestCase):
    def make_v1_history(self, root):
        body = (b'{"type":"event_msg","payload":{"type":"thread_settings_applied",'
                b'"thread_settings":{"model_provider_id":"openai"}}}\n'
                b'{"type":"session_meta","payload":{"id":"official","model_provider":"openai"}}\n'
                b'{ "type":"response_item", "payload":{"text":"keep openai exactly"}}\r\n')
        original = session(root / 'codex-chatgpt-home/sessions/official.jsonl', 'edge', 'official', body)
        archived = session(root / 'codex-chatgpt-home/archived_sessions/archived.jsonl', 'edge', 'archived', body)
        with closing(database(root / 'codex-chatgpt-home/state_5.sqlite', 'official', 'openai')):
            pass
        with patch.object(remote_agent, 'codex_history_busy', return_value=False):
            remote_agent.prepare_codex_history(root)
        shared = root / 'codex-history'
        (shared / 'sessions/official.jsonl').write_bytes(original)
        (shared / 'archived_sessions/archived.jsonl').write_bytes(archived)
        with closing(sqlite3.connect(shared / 'state_5.sqlite')) as db, db:
            db.execute("UPDATE threads SET model_provider='openai'")
        marker = shared / 'ready-v1.json'
        manifest = json.loads(marker.read_text())
        manifest.pop('metadata_version', None)
        marker.write_text(json.dumps(manifest))
        return shared, original, archived

    def test_v1_metadata_upgrade_repairs_linked_history_and_keeps_complete_backups(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            shared, original, archived = self.make_v1_history(root)
            with patch.object(remote_agent, 'codex_history_busy', return_value=False):
                remote_agent.prepare_codex_history(root)
            self.assertEqual((shared / 'sessions/official.jsonl').read_bytes(),
                             remote_agent._normalize_codex_rollout(original))
            self.assertEqual((shared / 'archived_sessions/archived.jsonl').read_bytes(),
                             remote_agent._normalize_codex_rollout(archived))
            self.assertEqual(json.loads((shared / 'ready-v1.json').read_text())['metadata_version'], 2)
            with closing(sqlite3.connect(shared / 'state_5.sqlite')) as db:
                self.assertEqual(db.execute('SELECT id,model_provider FROM threads').fetchall(), [('official', 'edge')])
                self.assertEqual(db.execute('PRAGMA quick_check').fetchone(), ('ok',))
            backups = list(root.glob('codex-history-backup-v2-*'))
            self.assertTrue(backups)
            self.assertTrue(any(path.read_bytes() == original for backup in backups for path in backup.rglob('official.jsonl')))
            self.assertTrue(any(path.read_bytes() == archived for backup in backups for path in backup.rglob('archived.jsonl')))
            db_backups = [path for backup in backups for path in backup.rglob('state_5.sqlite')]
            self.assertTrue(db_backups)
            with closing(sqlite3.connect(db_backups[0])) as db:
                self.assertEqual(db.execute('SELECT id,model_provider FROM threads').fetchall(), [('official', 'openai')])
            before = (shared / 'sessions/official.jsonl').read_bytes()
            with patch.object(remote_agent, 'codex_history_busy', return_value=True):
                remote_agent.prepare_codex_history(root)
            self.assertEqual((shared / 'sessions/official.jsonl').read_bytes(), before)
            self.assertEqual(list(root.glob('codex-history-backup-v2-*')), backups)

    def test_v1_metadata_upgrade_refuses_active_process_without_changing_history(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            shared, original, archived = self.make_v1_history(root)
            before = {path: path.read_bytes() for path in shared.rglob('*') if path.is_file()}
            with patch.object(remote_agent, 'codex_history_busy', return_value=True):
                with self.assertRaises(ValueError):
                    remote_agent.prepare_codex_history(root)
            self.assertEqual({path: path.read_bytes() for path in shared.rglob('*') if path.is_file()}, before)
            self.assertFalse(list(root.glob('codex-history-backup-v2-*')))

    def test_interrupted_v1_metadata_upgrade_keeps_backups_and_can_resume(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            shared, original, archived = self.make_v1_history(root)
            replace = Path.replace
            interrupted = []

            def fail_after_first_rollout(path, target):
                result = replace(path, target)
                if Path(target).suffix == '.jsonl' and shared in Path(target).parents and not interrupted:
                    interrupted.append(target)
                    raise OSError('simulated interruption after one rollout publication')
                return result

            with patch.object(remote_agent, 'codex_history_busy', return_value=False):
                with patch.object(Path, 'replace', fail_after_first_rollout):
                    with self.assertRaises(OSError):
                        remote_agent.prepare_codex_history(root)
                self.assertTrue(interrupted)
                self.assertNotEqual(json.loads((shared / 'ready-v1.json').read_text()).get('metadata_version'), 2)
                backups = list(root.glob('codex-history-backup-v2-*'))
                self.assertTrue(any(path.read_bytes() == original for backup in backups for path in backup.rglob('official.jsonl')))
                self.assertTrue(any(path.read_bytes() == archived for backup in backups for path in backup.rglob('archived.jsonl')))
                remote_agent.prepare_codex_history(root)
            self.assertEqual(json.loads((shared / 'ready-v1.json').read_text())['metadata_version'], 2)
            self.assertEqual((shared / 'sessions/official.jsonl').read_bytes(), remote_agent._normalize_codex_rollout(original))
            self.assertEqual((shared / 'archived_sessions/archived.jsonl').read_bytes(), remote_agent._normalize_codex_rollout(archived))
            with closing(sqlite3.connect(shared / 'state_5.sqlite')) as db:
                self.assertEqual(db.execute('SELECT id,model_provider FROM threads').fetchall(), [('official', 'edge')])

    def test_v1_metadata_upgrade_retains_wal_only_rows_and_their_backup(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            shared, _, _ = self.make_v1_history(root)
            path = shared / 'state_5.sqlite'
            with closing(sqlite3.connect(path)) as writer:
                writer.execute('PRAGMA journal_mode=WAL')
                writer.execute('PRAGMA wal_autocheckpoint=0')
                main_before = path.read_bytes()
                writer.execute('INSERT INTO threads VALUES (?, ?, ?)', ('wal-only', 'openai', 'WAL title'))
                writer.commit()
                self.assertEqual(path.read_bytes(), main_before)
                self.assertGreater(path.with_name(path.name + '-wal').stat().st_size, 32)
                with patch.object(remote_agent, 'codex_history_busy', return_value=False):
                    remote_agent.prepare_codex_history(root)
                self.assertEqual(writer.execute('SELECT id,model_provider FROM threads ORDER BY id').fetchall(),
                                 [('official', 'edge'), ('wal-only', 'edge')])
                backups = [path for backup in root.glob('codex-history-backup-v2-*')
                           for path in backup.rglob('state_5.sqlite')]
                self.assertTrue(backups)
                with closing(sqlite3.connect(backups[0])) as db:
                    self.assertEqual(db.execute('SELECT id,model_provider FROM threads ORDER BY id').fetchall(),
                                     [('official', 'openai'), ('wal-only', 'openai')])
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute('PRAGMA quick_check').fetchone(), ('ok',))
                self.assertEqual(db.execute('SELECT COUNT(*) FROM threads').fetchone()[0], 2)

    def test_v1_database_transaction_failure_rolls_back_and_retry_preserves_wal_rows(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            shared, original, archived = self.make_v1_history(root)
            path = shared / 'state_5.sqlite'
            normalize = remote_agent._normalize_codex_history_index
            failures = []

            def fail_during_live_transaction(db):
                normalize(db)
                main = Path(db.execute('PRAGMA database_list').fetchone()[2])
                if main == path:
                    failures.append(main)
                    raise OSError('simulated interruption before database commit')

            with closing(sqlite3.connect(path)) as writer:
                writer.execute('PRAGMA journal_mode=WAL')
                writer.execute('PRAGMA wal_autocheckpoint=0')
                writer.execute('INSERT INTO threads VALUES (?, ?, ?)', ('wal-only', 'openai', 'WAL title'))
                writer.commit()
                with patch.object(remote_agent, 'codex_history_busy', return_value=False):
                    with patch.object(remote_agent, '_normalize_codex_history_index', fail_during_live_transaction):
                        with self.assertRaises(OSError):
                            remote_agent.prepare_codex_history(root)
                    self.assertEqual(failures, [path])
                    self.assertEqual(writer.execute('SELECT id,model_provider FROM threads ORDER BY id').fetchall(),
                                     [('official', 'openai'), ('wal-only', 'openai')])
                    self.assertNotEqual(json.loads((shared / 'ready-v1.json').read_text()).get('metadata_version'), 2)
                    remote_agent.prepare_codex_history(root)
                self.assertEqual(writer.execute('SELECT id,model_provider FROM threads ORDER BY id').fetchall(),
                                 [('official', 'edge'), ('wal-only', 'edge')])
            self.assertEqual(json.loads((shared / 'ready-v1.json').read_text())['metadata_version'], 2)
            self.assertEqual((shared / 'sessions/official.jsonl').read_bytes(), remote_agent._normalize_codex_rollout(original))
            self.assertEqual((shared / 'archived_sessions/archived.jsonl').read_bytes(), remote_agent._normalize_codex_rollout(archived))

    def test_unifies_live_archived_and_text_history_preserving_bodies_auth_and_originals(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            api, official = root / 'codex-home', root / 'codex-chatgpt-home'
            api_original = session(api / 'sessions/2026/api.jsonl', 'edge', 'api')
            body = b'{"type":"turn_context","payload":{"model_provider":"openai"}}\n' \
                   b'{"type":"event_msg","payload":{"text":"openai"}}\n'
            official_original = session(official / 'sessions/2026/official.jsonl', 'openai', 'official', body)
            session(official / 'archived_sessions/archived.jsonl', 'openai', 'archived')
            api.joinpath('history.jsonl').write_bytes(b'{"session_id":"api"}\n')
            official.joinpath('history.jsonl').write_bytes(b'{"session_id":"official"}\n')
            api.joinpath('session_index.jsonl').write_bytes(b'{"id":"api"}\n')
            official.joinpath('session_index.jsonl').write_bytes(b'{"id":"official"}\n')
            auth_files = {api / 'auth.json': b'api-private-fixture',
                          official / 'auth.json': b'official-private-fixture'}
            for path, content in auth_files.items():
                path.write_bytes(content)
            with patch.object(remote_agent, 'codex_history_busy', return_value=False):
                remote_agent.prepare_codex_history(root)
            shared = root / 'codex-history'
            for home in (api, official):
                for name in ('sessions', 'archived_sessions', 'history.jsonl', 'session_index.jsonl'):
                    self.assertTrue((home / name).is_symlink())
                    self.assertEqual((home / name).resolve(), shared / name)
                self.assertTrue((home / 'sessions/2026/api.jsonl').exists())
                self.assertTrue((home / 'sessions/2026/official.jsonl').exists())
            merged = (shared / 'sessions/2026/official.jsonl').read_bytes()
            header, _, actual_body = merged.partition(b'\n')
            self.assertEqual(json.loads(header)['payload']['model_provider'], 'edge')
            self.assertEqual(actual_body, body)
            self.assertEqual((shared / 'sessions/2026/api.jsonl').read_bytes(), api_original)
            archived = json.loads((shared / 'archived_sessions/archived.jsonl').read_bytes().splitlines()[0])
            self.assertEqual(archived['payload']['model_provider'], 'edge')
            self.assertEqual(set((shared / 'history.jsonl').read_bytes().splitlines()),
                             {b'{"session_id":"api"}', b'{"session_id":"official"}'})
            self.assertEqual(set((shared / 'session_index.jsonl').read_bytes().splitlines()),
                             {b'{"id":"api"}', b'{"id":"official"}'})
            backups = list((root / 'codex-history-backup-v1').rglob('official.jsonl'))
            self.assertTrue(backups)
            self.assertTrue(all(path.read_bytes() == official_original for path in backups))
            for path, content in auth_files.items():
                self.assertFalse(path.is_symlink())
                self.assertEqual(path.read_bytes(), content)
            self.assertFalse(any(path.name == 'auth.json' for path in shared.rglob('*')))
            self.assertFalse(any(path.name == 'auth.json' for path in (root / 'codex-history-backup-v1').rglob('*')))
            session(api / 'sessions/new.jsonl', 'edge', 'new')
            with patch.object(remote_agent, 'codex_history_busy', return_value=True):
                remote_agent.prepare_codex_history(root)
            self.assertEqual((official / 'sessions/new.jsonl').read_bytes(), (api / 'sessions/new.jsonl').read_bytes())

    def test_merges_sqlite_indexes_and_secondary_thread_metadata(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            api, official = root / 'codex-home', root / 'codex-chatgpt-home'
            with database(api / 'state_5.sqlite', 'api', 'edge', with_tools=True):
                pass
            with database(official / 'state_5.sqlite', 'official', 'openai', with_tools=True):
                pass
            with patch.object(remote_agent, 'codex_history_busy', return_value=False):
                remote_agent.prepare_codex_history(root)
            with sqlite3.connect(root / 'codex-history/state_5.sqlite') as db:
                self.assertEqual(db.execute('SELECT id, model_provider FROM threads ORDER BY id').fetchall(),
                                 [('api', 'edge'), ('official', 'edge')])
                self.assertEqual(db.execute('SELECT thread_id FROM thread_dynamic_tools ORDER BY thread_id').fetchall(),
                                 [('api',), ('official',)])
            for home, expected in ((api, 'edge'), (official, 'openai')):
                backups = list((root / 'codex-history-backup-v1').rglob(home.name + '/state_5.sqlite'))
                self.assertTrue(backups)
                with sqlite3.connect(backups[0]) as db:
                    self.assertEqual(db.execute('SELECT model_provider FROM threads').fetchone()[0], expected)

    def test_sqlite_wal_rows_are_included_in_published_history_and_backup(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            home = root / 'codex-chatgpt-home'
            db = database(home / 'state_5.sqlite', 'original', 'openai')
            try:
                db.execute('PRAGMA journal_mode=WAL')
                db.execute('INSERT INTO threads VALUES (?, ?, ?)', ('wal-thread', 'openai', 'WAL title'))
                db.commit()
                self.assertTrue((home / 'state_5.sqlite-wal').exists())
                with patch.object(remote_agent, 'codex_history_busy', return_value=False):
                    remote_agent.prepare_codex_history(root)
                with sqlite3.connect(root / 'codex-history/state_5.sqlite') as merged:
                    self.assertEqual(merged.execute('SELECT id FROM threads ORDER BY id').fetchall(),
                                     [('original',), ('wal-thread',)])
                copies = list((root / 'codex-history-backup-v1').rglob('state_5.sqlite'))
                self.assertTrue(copies)
                with sqlite3.connect(copies[0]) as backup:
                    self.assertEqual(backup.execute('SELECT COUNT(*) FROM threads').fetchone()[0], 2)
            finally:
                db.close()

    def test_empty_primary_database_does_not_hide_official_threads(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            first = root / 'codex-home/state_5.sqlite'
            first.parent.mkdir(parents=True)
            first.touch()
            db = database(root / 'codex-chatgpt-home/state_5.sqlite', 'official', 'openai')
            db.close()
            with patch.object(remote_agent, 'codex_history_busy', return_value=False):
                remote_agent.prepare_codex_history(root)
            with sqlite3.connect(root / 'codex-history/state_5.sqlite') as merged:
                self.assertEqual(merged.execute('SELECT id, model_provider FROM threads').fetchall(),
                                 [('official', 'edge')])

    def test_readonly_creation_of_empty_wal_is_not_source_drift(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / 'codex-home/state_5.sqlite'
            db = database(path, 'api', 'edge')
            try:
                db.execute('PRAGMA journal_mode=WAL')
                # No transaction writes a WAL frame. Read-only snapshotting may
                # create an empty WAL file, which does not mean new chat data.
                with patch.object(remote_agent, 'codex_history_busy', return_value=False):
                    remote_agent.prepare_codex_history(root)
                with sqlite3.connect(root / 'codex-history/state_5.sqlite') as merged:
                    self.assertEqual(merged.execute('SELECT id FROM threads').fetchall(), [('api',)])
            finally:
                db.close()

    def test_sqlx_install_timestamps_are_not_treated_as_chat_conflicts(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for home, ident, provider, installed, elapsed in (
                    ('codex-home', 'api', 'edge', '2026-11-01', 100),
                    ('codex-chatgpt-home', 'official', 'openai', '2026-11-02', 200)):
                db = database(root / home / 'state_5.sqlite', ident, provider)
                try:
                    db.execute('CREATE TABLE _sqlx_migrations (version BIGINT PRIMARY KEY, '
                               'description TEXT NOT NULL, installed_on TEXT NOT NULL, '
                               'success BOOLEAN NOT NULL, checksum BLOB NOT NULL, execution_time BIGINT NOT NULL)')
                    db.execute('INSERT INTO _sqlx_migrations VALUES (?, ?, ?, ?, ?, ?)',
                               (1, 'create threads', installed, True, b'same-migration-checksum', elapsed))
                    db.commit()
                finally:
                    db.close()
            with patch.object(remote_agent, 'codex_history_busy', return_value=False):
                remote_agent.prepare_codex_history(root)
            with sqlite3.connect(root / 'codex-history/state_5.sqlite') as merged:
                self.assertEqual(merged.execute('SELECT COUNT(*) FROM threads').fetchone()[0], 2)
                self.assertEqual(merged.execute('SELECT installed_on, execution_time FROM _sqlx_migrations').fetchall(),
                                 [('2026-11-01', 100)])

    def test_database_instance_metadata_does_not_block_history_and_backfill_restarts(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for home, ident, provider, timestamp in (
                    ('codex-home', 'api', 'edge', 100),
                    ('codex-chatgpt-home', 'official', 'openai', 200)):
                db = database(root / home / 'state_5.sqlite', ident, provider)
                try:
                    db.execute('CREATE TABLE backfill_state (id INTEGER PRIMARY KEY CHECK (id = 1), '
                               'status TEXT NOT NULL, last_watermark TEXT, last_success_at INTEGER, '
                               'updated_at INTEGER NOT NULL)')
                    db.execute('INSERT INTO backfill_state VALUES (1, ?, ?, ?, ?)',
                               ('complete', ident + '-watermark', timestamp, timestamp))
                    db.execute('CREATE TABLE external_agent_config_imports '
                               '(id INTEGER PRIMARY KEY, source_path TEXT NOT NULL, imported_at INTEGER NOT NULL)')
                    db.execute('INSERT INTO external_agent_config_imports VALUES (1, ?, ?)',
                               ('/' + ident + '/settings.json', timestamp))
                    db.commit()
                finally:
                    db.close()
            with patch.object(remote_agent, 'codex_history_busy', return_value=False):
                remote_agent.prepare_codex_history(root)
            with sqlite3.connect(root / 'codex-history/state_5.sqlite') as merged:
                self.assertEqual(merged.execute('SELECT COUNT(*) FROM threads').fetchone()[0], 2)
                self.assertEqual(merged.execute('SELECT status,last_watermark,last_success_at FROM backfill_state').fetchall(),
                                 [('pending', None, None)])
                self.assertEqual(merged.execute('SELECT source_path,imported_at FROM external_agent_config_imports').fetchall(),
                                 [('/api/settings.json', 100)])

    def test_unknown_nonempty_secondary_table_refuses_publication_without_changing_sources(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            paths = [root / home / 'state_5.sqlite' for home in ('codex-home', 'codex-chatgpt-home')]
            for index, path in enumerate(paths):
                db = database(path, 'thread-' + str(index), 'openai' if index else 'edge')
                try:
                    db.execute('CREATE TABLE future_conversation_metadata (id TEXT PRIMARY KEY, value TEXT)')
                    if index:
                        db.execute('INSERT INTO future_conversation_metadata VALUES (?, ?)',
                                   ('thread-1', 'preserve this unknown data'))
                    db.commit()
                finally:
                    db.close()
            originals = [path.read_bytes() for path in paths]
            with patch.object(remote_agent, 'codex_history_busy', return_value=False):
                with self.assertRaises(ValueError):
                    remote_agent.prepare_codex_history(root)
            self.assertFalse((root / 'codex-history').exists())
            for path, original in zip(paths, originals):
                self.assertFalse(path.is_symlink())
                self.assertEqual(path.read_bytes(), original)

    def test_wal_only_source_drift_after_interrupted_link_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / 'codex-home/state_5.sqlite'
            db = database(path, 'old', 'edge')
            link = Path.symlink_to

            def stop_before_database_link(path, target, **kwargs):
                if path.name == 'sessions' and path.parent.name == 'codex-home':
                    raise OSError('simulated interruption before database move')
                return link(path, target, **kwargs)

            try:
                db.execute('PRAGMA journal_mode=WAL')
                db.execute('PRAGMA wal_autocheckpoint=0')
                db.execute('UPDATE threads SET title=? WHERE id=?', ('initial WAL title', 'old'))
                db.commit()
                with patch.object(remote_agent, 'codex_history_busy', return_value=False):
                    with patch.object(Path, 'symlink_to', stop_before_database_link):
                        with self.assertRaises(OSError):
                            remote_agent.prepare_codex_history(root)
                    before = path.read_bytes()
                    db.execute('INSERT INTO threads VALUES (?, ?, ?)', ('new-in-wal', 'edge', 'new title'))
                    db.commit()
                    self.assertEqual(path.read_bytes(), before)
                    self.assertTrue(path.with_name(path.name + '-wal').exists())
                    with self.assertRaises(ValueError):
                        remote_agent.prepare_codex_history(root)
                self.assertFalse(path.is_symlink())
                self.assertEqual(db.execute('SELECT id FROM threads ORDER BY id').fetchall(),
                                 [('new-in-wal',), ('old',)])
                with sqlite3.connect(root / 'codex-history/state_5.sqlite') as staged:
                    self.assertEqual(staged.execute('SELECT id FROM threads').fetchall(), [('old',)])
            finally:
                db.close()

    def test_interrupted_linking_can_resume_without_losing_originals(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / 'codex-home/sessions/thread.jsonl'
            original = session(path, 'edge', 'thread')
            link = Path.symlink_to

            def fail_after_original_move(path, target, **kwargs):
                if path.name == 'sessions' and path.parent.name == 'codex-home':
                    raise OSError('simulated interruption')
                return link(path, target, **kwargs)

            with patch.object(remote_agent, 'codex_history_busy', return_value=False):
                with patch.object(Path, 'symlink_to', fail_after_original_move):
                    with self.assertRaises(OSError):
                        remote_agent.prepare_codex_history(root)
                self.assertFalse(path.exists())
                remote_agent.prepare_codex_history(root)
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual((root / 'codex-chatgpt-home/sessions/thread.jsonl').read_bytes(), original)
            backups = list((root / 'codex-history-backup-v1').rglob('thread.jsonl'))
            self.assertTrue(backups)
            self.assertTrue(all(path.read_bytes() == original for path in backups))

    def test_retry_refuses_new_records_in_an_unlinked_home(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            old = root / 'codex-home/sessions/old.jsonl'
            original = session(old, 'edge', 'old')
            link = Path.symlink_to

            def fail_after_original_move(path, target, **kwargs):
                if path.name == 'sessions' and path.parent.name == 'codex-home':
                    raise OSError('simulated interruption')
                return link(path, target, **kwargs)

            with patch.object(remote_agent, 'codex_history_busy', return_value=False):
                with patch.object(Path, 'symlink_to', fail_after_original_move):
                    with self.assertRaises(OSError):
                        remote_agent.prepare_codex_history(root)
                new = root / 'codex-home/sessions/new.jsonl'
                new_content = session(new, 'edge', 'new')
                with self.assertRaises(ValueError):
                    remote_agent.prepare_codex_history(root)
            self.assertFalse(new.parent.is_symlink())
            self.assertEqual(new.read_bytes(), new_content)
            self.assertEqual((root / 'codex-history/sessions/old.jsonl').read_bytes(), original)
            self.assertEqual((root / 'codex-history-backup-v1/originals/codex-home/sessions/old.jsonl').read_bytes(), original)

    def test_busy_process_does_not_publish_or_change_originals(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / 'codex-chatgpt-home/sessions/official.jsonl'
            original = session(path, 'openai', 'official')
            with patch.object(remote_agent, 'codex_history_busy', return_value=True):
                with self.assertRaises(ValueError):
                    remote_agent.prepare_codex_history(root)
            self.assertEqual(path.read_bytes(), original)
            self.assertFalse(path.parent.is_symlink())
            self.assertFalse((root / 'codex-history').exists())

    def test_conflicting_session_files_are_not_overwritten(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            paths = [root / home / 'sessions/same.jsonl' for home in ('codex-home', 'codex-chatgpt-home')]
            originals = [session(paths[0], 'edge', 'one'), session(paths[1], 'openai', 'two')]
            with patch.object(remote_agent, 'codex_history_busy', return_value=False):
                with self.assertRaises(ValueError):
                    remote_agent.prepare_codex_history(root)
            for path, content in zip(paths, originals):
                self.assertEqual(path.read_bytes(), content)
                self.assertFalse(path.parent.is_symlink())
            self.assertFalse((root / 'codex-history').exists())

    def test_schema_mismatch_leaves_both_original_databases_untouched(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            first = root / 'codex-home/state_5.sqlite'
            second = root / 'codex-chatgpt-home/state_5.sqlite'
            with database(first, 'api', 'edge'):
                pass
            with database(second, 'official', 'openai') as db:
                db.execute('ALTER TABLE threads ADD COLUMN future_column TEXT')
            originals = first.read_bytes(), second.read_bytes()
            with patch.object(remote_agent, 'codex_history_busy', return_value=False):
                with self.assertRaises(ValueError):
                    remote_agent.prepare_codex_history(root)
            self.assertEqual((first.read_bytes(), second.read_bytes()), originals)
            self.assertFalse((root / 'codex-history').exists())


if __name__ == '__main__':
    unittest.main()
