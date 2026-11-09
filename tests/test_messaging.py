import concurrent.futures
import base64
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import pymysql

from agent_messaging.client import Client
from agent_messaging.delivery import deliver_codex
from agent_messaging.server import Server
from agent_messaging.store import digest


class MessagingTests(unittest.TestCase):
    """Every query targets a MariaDB process and database created by this suite."""
    @classmethod
    def setUpClass(cls):
        if not shutil.which('mariadbd') or not shutil.which('mariadb-install-db'):
            raise unittest.SkipTest('Disposable local MariaDB binaries are required')
        cls.temporary = tempfile.TemporaryDirectory(prefix='agent-mail-tests-')
        cls.root = Path(cls.temporary.name)
        cls.socket = str(cls.root / 'mysql.sock')
        subprocess.run(['mariadb-install-db', '--datadir=' + str(cls.root / 'data'),
                        '--auth-root-authentication-method=normal', '--skip-test-db'],
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=True)
        cls.process = subprocess.Popen(['mariadbd', '--datadir=' + str(cls.root / 'data'), '--socket=' + cls.socket,
                                        '--pid-file=' + str(cls.root / 'mysql.pid'), '--skip-networking', '--skip-log-bin',
                                        '--innodb-buffer-pool-size=32M', '--max-connections=30',
                                        '--log-error=' + str(cls.root / 'mysql.log')], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            try:
                cls.admin = pymysql.connect(unix_socket=cls.socket, user='root', autocommit=True)
                break
            except pymysql.OperationalError:
                if cls.process.poll() is not None: raise RuntimeError((cls.root / 'mysql.log').read_text())
                time.sleep(.1)
        else: raise RuntimeError('Disposable MariaDB did not start')

    @classmethod
    def tearDownClass(cls):
        cls.admin.close()
        cls.process.terminate()
        cls.process.wait(timeout=20)
        cls.temporary.cleanup()

    def setUp(self):
        self.name = 'test_' + self.id().split('.')[-1][:45]
        with self.admin.cursor() as cursor:
            cursor.execute('CREATE DATABASE `' + self.name + '` CHARACTER SET utf8mb4')
        self.config = {'database': {'unix_socket': self.socket, 'user': 'root', 'database': self.name},
                       'adminTokenHash': digest('test-admin'), 'port': 0}
        db = pymysql.connect(**self.config['database'], autocommit=True)
        try:
            with db.cursor() as cursor:
                schema = Path(__file__).parents[1] / 'agent_messaging/schema.sql'
                for statement in schema.read_text().split(';'):
                    if statement.strip(): cursor.execute(statement)
        finally:
            db.close()
        self.server = Server(self.config)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.client_file = self.root / (self.name + '.json')
        self.write_config(self.client_file, 'test-admin', 'alpha')
        self.client = Client(self.client_file)
        self.client.call('project', name='Alpha')
        self.client.call('project', project='beta', name='Beta')
        for agent in ('sender', 'first', 'second'):
            self.client.call('register', agent=agent, name=agent, session='session-' + agent)
        self.client.call('alias', alias='principal', agent='first')

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=5)
        with self.admin.cursor() as cursor: cursor.execute('DROP DATABASE `' + self.name + '`')
        self.client_file.unlink()

    def test_retirement_preserves_history_blocks_new_mail_and_can_be_restored(self):
        message = self.client.call('send', agent='sender', to='second', subject='Original', body='Preserved evidence')
        self.client.call('retire', agent='second', reason='Historical worker')
        self.assertEqual(self.client.call('read', message=message['id'], agent='second')['body'], 'Preserved evidence')
        self.assertNotIn('second', [row['agentId'] for row in self.client.call('agents')['agents']])
        self.assertIn('second', [row['agentId'] for row in self.client.call('agents', includeRetired=True)['agents']])
        self.assertNotIn('second', [row['agentId'] for row in self.client.call('directory')['agents']])
        for action, params in [
            ('send', dict(agent='sender', to='second', subject='New', body='Blocked')),
            ('send', dict(agent='second', to='sender', subject='New', body='Blocked')),
            ('status', dict(agent='second', state='active')),
            ('register', dict(agent='second', name='Second', session='session-second'))]:
            with self.assertRaisesRegex(RuntimeError, 'retired'):
                self.client.call(action, **params)
        self.client.call('restore-agent', agent='second')
        self.assertTrue(self.client.call('send', agent='sender', to='second', subject='Restored', body='Works')['saved'])

    def test_retirement_requires_admin_and_protects_alias_owner(self):
        with self.assertRaisesRegex(RuntimeError, 'aliases'):
            self.client.call('retire', agent='first', reason='Historical')
        token = self.client.call('credential', agent='second', role='agent')['token']
        private = self.root / 'retirement-agent.json'
        self.write_config(private, token, 'alpha')
        try:
            with self.assertRaises(RuntimeError):
                Client(private).call('retire', agent='second', reason='Self retirement')
        finally:
            private.unlink()

    def test_owner_deletes_cross_project_item_with_version_and_history_preserved(self):
        self.client.call('project', project='global', name='Global')
        self.client.call('register', project='global', agent='user', name='Owner', session='owner')
        item = self.client.call('action-put', agent='sender', key='remove-one', title='Remove me', description='Fixture', source='fixture')
        path = self.root / 'delete-owner.json'
        token = self.client.call('credential', project='global', agent='user', role='agent')['token']
        self.write_config(path, token, 'global')
        self.addCleanup(path.unlink)
        owner = Client(path)
        with self.assertRaisesRegex(RuntimeError, 'changed'):
            owner.call('action-delete', itemId=item['id'], expectedVersion=2)
        self.assertTrue(owner.call('action-delete', itemId=item['id'], expectedVersion=1)['saved'])
        self.assertTrue(owner.call('action-delete', itemId=item['id'], expectedVersion=1)['saved'])
        self.assertEqual(owner.call('actions', includeResolved=True)['items'], [])
        with self.admin.cursor() as cursor:
            cursor.execute('SELECT status,description FROM `' + self.name + '`.actionItems WHERE id=%s', (item['id'],))
            self.assertEqual(cursor.fetchone(), ('deleted', 'Fixture'))
        with self.assertRaisesRegex(RuntimeError, 'deleted'):
            self.client.call('action-put', agent='sender', key='remove-one', title='Revive', description='Fixture', source='fixture', expectedVersion=2)

    def test_agent_deletes_only_own_completed_items_with_recorded_outcome(self):
        token = self.client.call('credential', agent='sender', role='agent')['token']
        path = self.root / 'delete-agent.json'
        self.write_config(path, token, 'alpha'); self.addCleanup(path.unlink)
        item = self.client.call('action-put', agent='sender', key='protected', title='Protected', description='Fixture', source='fixture')
        agent = Client(path)
        with self.assertRaisesRegex(RuntimeError, 'completed outcome'):
            agent.call('action-delete', itemId=item['id'], expectedVersion=1)
        other = self.client.call('action-put', agent='second', key='other', title='Other', description='Fixture', source='fixture', status='resolved', resolution='Completed')
        with self.assertRaises(RuntimeError):
            agent.call('action-delete', itemId=other['id'], expectedVersion=1)
        with self.assertRaises(RuntimeError):
            agent.call('action-delete', project='global', itemId=item['id'], expectedVersion=1)
        self.assertEqual(self.client.call('actions')['items'][0]['status'], 'needs-input')
        current = agent.call('action-put', agent='sender', key='protected', title='Protected', description='Fixture', source='fixture', status='resolved', resolution='Implemented and verified', expectedVersion=1)
        with self.assertRaisesRegex(RuntimeError, 'changed'):
            agent.call('action-delete', itemId=item['id'], expectedVersion=1)
        self.assertTrue(agent.call('action-delete', itemId=item['id'], expectedVersion=current['version'])['saved'])
        self.assertTrue(agent.call('action-delete', itemId=item['id'], expectedVersion=current['version'])['saved'])
        self.assertNotIn(item['id'], [x['id'] for x in agent.call('actions', includeResolved=True)['items']])

    def test_action_reply_is_atomic_referenced_mail_with_retry_protection(self):
        self.client.call('project', project='global', name='Global')
        for agent in ('user', 'global-coordinator'):
            self.client.call('register', project='global', agent=agent, name=agent, session=agent)
        item = self.client.call('action-put', agent='sender', key='audit-one', title='Choose a policy',
                                description='A bounded question', source='audit.md#finding-2')
        reply = dict(project='global', itemId=item['id'], body='Use option A.\n<literal>', idempotencyKey='response-one', expectedVersion=1)
        first = self.client.call('action-reply', **reply)
        second = self.client.call('action-reply', **reply)
        self.assertEqual(first['id'], second['id'])
        message = self.client.call('read', project='global', agent='global-coordinator', message=first['id'])
        self.assertEqual(message['senderId'], 'user')
        self.assertEqual(message['recipient'], 'global-coordinator')
        self.assertIn('Reference: alpha/audit-one', message['body'])
        self.assertIn('Source: audit.md#finding-2', message['body'])
        self.assertIn('Use option A.\n<literal>', message['body'])
        current = self.client.call('actions')['items'][0]
        self.assertEqual(current['status'], 'answered')
        self.assertEqual(current['responseMessageId'], first['id'])
        with self.assertRaisesRegex(RuntimeError, 'different content'):
            self.client.call('action-reply', **dict(reply, body='Changed answer'))

    def test_action_revision_prevents_lost_updates_and_scopes_authors(self):
        item = dict(agent='sender', key='audit-one', title='Choose', description='Question', source='audit.md')
        self.client.call('action-put', **item)
        with self.assertRaisesRegex(RuntimeError, 'current version'):
            self.client.call('action-put', **dict(item, title='Stale update'))
        self.client.call('action-put', **dict(item, status='resolved', resolution='User chose A', expectedVersion=1))
        self.assertEqual(self.client.call('actions')['items'], [])
        self.assertEqual(self.client.call('actions', includeResolved=True)['items'][0]['resolution'], 'User chose A')
        token = self.client.call('credential', agent='second', role='agent')['token']
        private = self.root / 'action-agent.json'
        self.write_config(private, token, 'alpha')
        try:
            with self.assertRaisesRegex(RuntimeError, 'author or coordinator'):
                Client(private).call('action-put', **dict(item, agent='second', expectedVersion=2))
            with self.assertRaises(RuntimeError):
                Client(private).call('action-reply', project='global', itemId=1, body='Unauthorized', idempotencyKey='bad', expectedVersion=2)
        finally:
            private.unlink()

    def write_config(self, path, token, project):
        path.write_text(json.dumps({'url': 'http://127.0.0.1:' + str(self.server.server_port), 'token': token, 'project': project}))
        path.chmod(0o600)

    def send(self, **params):
        defaults = {'agent': 'sender', 'to': 'principal', 'subject': 'A finding', 'body': 'Exact $HOME and `code`\n\\frac{a}{b}'}
        defaults.update(params)
        return self.client.call('send', **defaults)

    def test_only_global_coordinator_or_admin_deletes_any_message(self):
        self.client.call('project', project='global', name='Global')
        self.client.call('register', project='global', agent='global-coordinator', name='Coordinator', session='coord')
        self.client.call('register', project='global', agent='user', name='Owner', session='owner')
        sent = self.send()
        self.client.call('ack', agent='first', message=sent['id'], note='Read')

        def client_for(project, agent):
            path = self.root / ('delete-message-' + project + '-' + agent + '.json')
            token = self.client.call('credential', project=project, agent=agent, role='agent')['token']
            self.write_config(path, token, project)
            self.addCleanup(path.unlink)
            return Client(path)

        for who in (client_for('global', 'user'), client_for('test', 'sender')):
            with self.assertRaisesRegex(RuntimeError, 'coordinator'):
                who.call('delete-message', messageId=sent['id'], reason='Not allowed')
        coordinator = client_for('global', 'global-coordinator')
        with self.assertRaisesRegex(RuntimeError, 'reason'):
            coordinator.call('delete-message', messageId=sent['id'], reason='   ')
        with self.assertRaisesRegex(RuntimeError, 'not found'):
            coordinator.call('delete-message', messageId='missing', reason='Nope')
        result = coordinator.call('delete-message', messageId=sent['id'], reason='Obsolete instruction')
        self.assertTrue(result['deleted'])
        with self.assertRaisesRegex(RuntimeError, 'not found'):
            self.client.call('read', message=sent['id'])
        self.assertFalse(self.client.call('inbox', agent='first', includeAcknowledged=True)['messages'])
        with self.admin.cursor() as cursor:
            for table in ('receipts', 'deliveries'):
                cursor.execute('SELECT COUNT(*) FROM `' + self.name + '`.' + table + ' WHERE messageId=%s', (sent['id'],))
                self.assertEqual(cursor.fetchone(), (0,))
            cursor.execute('SELECT reference FROM `' + self.name + '`.events WHERE kind=%s', ('message-deleted',))
            reference = cursor.fetchone()[0]
        self.assertIn(sent['id'], reference)
        self.assertIn('Obsolete instruction', reference)
        self.assertNotIn('Exact $HOME', reference)
        again = self.send(agent='sender', to='first')
        self.assertTrue(self.client.call('delete-message', messageId=again['id'], reason='Admin removal')['deleted'])

    def test_round_trip_reply_and_explicit_receipt(self):
        sent = self.send()
        inbox = self.client.call('inbox', agent='first')
        self.assertEqual(inbox['messages'][0]['id'], sent['id'])
        read = self.client.call('read', message=sent['id'])
        self.assertIn('Exact $HOME and `code`', read['rawText'])
        self.assertFalse(self.client.call('receipts', message=sent['id'])['receipts'])
        reply = self.send(agent='first', to='sender', replyTo=sent['id'])
        self.assertEqual(self.client.call('read', message=reply['id'])['replyTo'], sent['id'])
        one = self.client.call('ack', agent='first', message=sent['id'], note='Read')
        two = self.client.call('ack', agent='first', message=sent['id'], note='Different')
        self.assertEqual(one, two)
        self.assertFalse(self.client.call('inbox', agent='first')['messages'])

    def test_concurrent_retry_key_creates_one_message(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda _: self.send(idempotencyKey='retry-one'), range(12)))
        self.assertEqual(len({r['id'] for r in results}), 1)
        self.assertEqual(len(self.client.call('inbox', agent='first')['messages']), 1)
        with self.assertRaisesRegex(RuntimeError, 'different content'):
            self.send(idempotencyKey='retry-one', body='Changed')

    def test_watch_exits_with_mail_without_acknowledging(self):
        command = [os.sys.executable, '-m', 'agent_messaging.cli', '--config', str(self.client_file),
                   'watch', '--agent', 'first', '--timeout', '8']
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        time.sleep(.3)
        sent = self.send()
        output, errors = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 0, errors)
        self.assertEqual(json.loads(output)['messages'][0]['id'], sent['id'])
        self.assertFalse(self.client.call('receipts', message=sent['id'])['receipts'])

    def test_watch_timeout_has_distinct_exit_status(self):
        result = subprocess.run([os.sys.executable, '-m', 'agent_messaging.cli', '--config', str(self.client_file),
                                 'watch', '--agent', 'first', '--timeout', '.1'], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 3)
        self.assertEqual(json.loads(result.stdout)['messages'], [])

    def test_alias_handoff_preserves_receipt_and_pending_mail(self):
        done = self.send(); pending = self.send()
        self.client.call('ack', agent='first', message=done['id'])
        with self.assertRaisesRegex(RuntimeError, 'handoff'):
            self.client.call('alias', alias='principal', agent='second')
        self.client.call('alias', alias='principal', agent='second', replace=True)
        self.assertEqual([r['id'] for r in self.client.call('inbox', agent='second')['messages']], [pending['id']])
        self.assertFalse(self.client.call('inbox', agent='first')['messages'])

    def test_broadcast_receipts_are_per_recipient(self):
        sent = self.send(to='all')
        self.client.call('ack', agent='first', message=sent['id'])
        self.assertFalse(self.client.call('inbox', agent='first')['messages'])
        self.assertEqual(self.client.call('inbox', agent='second')['messages'][0]['id'], sent['id'])

    def test_project_and_agent_credentials_enforce_scope(self):
        token = self.client.call('credential', agent='first', role='agent')['token']
        path = self.root / 'scoped.json'; self.write_config(path, token, 'alpha')
        client = Client(path)
        with self.assertRaises(RuntimeError): client.call('inbox', agent='second')
        with self.assertRaises(RuntimeError): client.call('project', name='Forbidden')
        with self.assertRaises(RuntimeError): client.call('info', project='beta')
        with self.assertRaises(RuntimeError): client.call('alias', alias='principal', agent='first', replace=True)
        private = self.send(to='second')
        with self.assertRaises(RuntimeError): client.call('read', agent='first', message=private['id'])
        with self.assertRaises(RuntimeError): client.call('ack', agent='first', message=private['id'])
        self.assertTrue(client.call('info')['aliases'])
        path.unlink()

    def test_pagination_and_cross_project_reply(self):
        self.client.call('register', project='beta', agent='other', name='Other', session='other')
        sent = self.send(to='beta/other')
        received = self.client.call('inbox', project='beta', agent='other')['messages'][0]
        self.assertEqual(received['senderProject'], 'alpha')
        self.client.call('send', project='beta', agent='other', to='alpha/sender', subject='Reply', body='Received', replyTo=sent['id'])
        for _ in range(3): self.send()
        first = self.client.call('inbox', agent='first', limit=2)
        second = self.client.call('inbox', agent='first', limit=2, after=first['nextCursor'])
        self.assertTrue(first['hasMore']); self.assertEqual(len(second['messages']), 1)
        self.assertFalse({r['id'] for r in first['messages']} & {r['id'] for r in second['messages']})

    def test_newest_first_pages_do_not_hide_new_mail_or_repeat_messages(self):
        sent = [self.send(subject=str(i)) for i in range(5)]
        page = self.client.call('inbox', agent='first', newestFirst=True, limit=2)
        self.assertEqual([m['id'] for m in page['messages']], [sent[4]['id'], sent[3]['id']])
        older = self.client.call('inbox', agent='first', newestFirst=True, before=page['nextCursor'], limit=2)
        self.assertEqual([m['id'] for m in older['messages']], [sent[2]['id'], sent[1]['id']])
        self.send(subject='Newer mail')
        rest = self.client.call('inbox', agent='first', newestFirst=True, before=older['nextCursor'], limit=2)
        self.assertEqual([m['id'] for m in rest['messages']], [sent[0]['id']])

    def test_live_delivery_claim_is_host_bound_and_single(self):
        self.client.call('register', agent='first', name='First', session='session-first', host='machine', runtime='codex')
        sent = self.send()
        token = self.client.call('credential', role='host', host='machine')['token']
        path = self.root / 'host.json'; self.write_config(path, token, 'alpha')
        host = Client(path)
        claimed = host.call('claim')['delivery']
        self.assertEqual(claimed['messageId'], sent['id'])
        self.assertIsNone(host.call('claim')['delivery'])
        host.call('delivered', deliveryId=claimed['id'], attemptId=claimed['attemptId'], state='accepted', turnId='turn-one')
        self.assertEqual(self.client.call('receipts', message=sent['id'])['deliveries'][0]['state'], 'accepted')
        self.assertFalse(self.client.call('receipts', message=sent['id'])['receipts'])
        path.unlink()

    def test_no_notification_on_explicit_mail_only_send(self):
        self.client.call('register', agent='first', name='First', session='session-first', host='machine', runtime='codex')
        sent = self.send(notify=False)
        self.assertEqual(self.client.call('receipts', message=sent['id'])['deliveries'], [])

    def test_changed_session_cannot_take_over_identity(self):
        with self.assertRaisesRegex(RuntimeError, 'different session'):
            self.client.call('register', agent='first', name='Changed', session='other-session')

    def test_owner_directory_is_paginated_and_excludes_private_session_data(self):
        self.client.call('project', project='global', name='Global')
        self.client.call('register', project='global', agent='user', name='Owner', session='human')
        token = self.client.call('credential', project='global', agent='user', role='agent')['token']
        path = self.root / 'owner.json'; self.write_config(path, token, 'global')
        owner = Client(path)
        first = owner.call('directory', limit=2)
        self.assertTrue(first['hasMore'])
        second = owner.call('directory', after=first['nextCursor'], limit=2)
        self.assertFalse({r['agentId'] for r in first['agents']} & {r['agentId'] for r in second['agents']})
        self.assertTrue(all('sessionId' not in row and 'runtimeSessionId' not in row for row in first['agents']))
        other_token = self.client.call('credential', agent='first', role='agent')['token']
        self.write_config(path, other_token, 'alpha')
        with self.assertRaises(RuntimeError): Client(path).call('directory')
        path.unlink()

    def test_live_binding_preserves_imported_session_label(self):
        record = self.client.call('register', agent='first', name='First', session='session-first',
                                  runtimeSession='actual-runtime-uuid', host='machine', runtime='codex')
        self.assertEqual(record['sessionId'], 'session-first')
        self.assertEqual(record['runtimeSessionId'], 'actual-runtime-uuid')

    def test_guidance_versions_preserve_exact_bytes_and_shared_fallback(self):
        self.client.call('project', project='global', name='Global')
        original = b'Exact guidance\r\nwith $HOME and `code`.\r\n'
        result = self.client.call('save-guide', project='global', path='communication-guide.md',
                                  contentBase64=base64.b64encode(original).decode())
        token = self.client.call('credential', agent='first', role='agent')['token']
        path = self.root / 'guide-reader.json'; self.write_config(path, token, 'alpha')
        client = Client(path)
        read = client.call('guide', path='communication-guide.md')
        self.assertEqual(base64.b64decode(read['contentBase64']), original)
        self.assertEqual(read['sha256'], result['sha256'])
        with self.assertRaises(RuntimeError):
            client.call('save-guide', path='communication-guide.md', contentBase64='')
        path.unlink()

    def test_compatibility_adapter_keeps_workflow_and_cli_semantics(self):
        from agent_messaging.contrib.idwt.compat import CentralMailbox
        box = CentralMailbox(self.root, self.client_file)
        sent = box.send('sender', 'principal', 'Compatibility', 'Exact body\n')
        self.assertEqual(box.coordinator()['agent'], 'first')
        result = box.list_inbox('first', limit=0)
        self.assertEqual(result['messages'][0]['id'], sent['id'])
        self.assertTrue(box.read(sent['id']).endswith('Exact body\n'))
        before = box.inbox_signature()
        box.ack('first', sent['id'], 'Read')
        self.assertNotEqual(before, box.inbox_signature())
        self.assertFalse(box.list_inbox('first')['messages'])

    def test_legacy_import_preserves_bytes_duplicates_receipts_and_is_repeatable(self):
        from agent_messaging.contrib.idwt.importer import import_mail
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'scripts').mkdir()
            state = root / '.site-build/agent-team/communication'
            (state / 'acks/first').mkdir(parents=True)
            (root / '.site-build/agent-team/inbox').mkdir()
            fixture = ('import json\nfrom pathlib import Path\nclass Mailbox:\n'
                       ' def __init__(self,root): self.root=Path(root); self.state=self.root/".site-build/agent-team/communication"\n'
                       ' def messages(self): return json.loads((self.root/"messages.json").read_text())\n'
                       ' def agents(self): return []\n'
                       ' def coordinator(self): return None\n')
            (root / 'scripts/agent-mail.py').write_text(fixture)
            messages = []
            for name in ('one.md', 'two.md'):
                path = '.site-build/agent-team/inbox/' + name
                (root / path).write_bytes(('Subject: Original\r\n\r\nLiteral \\frac $HOME ' + name + '\r\n').encode())
                messages.append(dict(id='duplicate', path=path, sender='sender', to='principal', subject='Original',
                                     kind='legacy-' * 11, task='', in_reply_to=None, created_at='2026-01-01T00:00:00+00:00', metadata_warning=None))
            (root / 'messages.json').write_text(json.dumps(messages))
            receipt = dict(path=messages[0]['path'], agent='first', addressed_as='principal',
                           acknowledged_at='2026-01-01T01:00:00Z', note='Historical receipt')
            (state / 'acks/first/receipt.json').write_text(json.dumps(receipt))
            result = import_mail(root, self.config, 'alpha')
            self.assertEqual(result['messagesImported'], 2)
            self.assertEqual(result['receiptsImported'], 1)
            self.assertEqual(import_mail(root, self.config, 'alpha')['messagesImported'], 0)
            with self.assertRaisesRegex(RuntimeError, 'ambiguous'):
                self.client.call('read', message='duplicate')
            stored = self.client.call('read', message='one.md')
            self.assertEqual(stored['rawText'].encode(), (root / messages[0]['path']).read_bytes())
            self.assertEqual(self.client.call('receipts', message=stored['id'])['receipts'][0]['note'], 'Historical receipt')
            (root / messages[0]['path']).write_text('Changed')
            with self.assertRaisesRegex(ValueError, 'changed'):
                import_mail(root, self.config, 'alpha')


class DeliveryTests(unittest.TestCase):
    def test_owner_mail_is_identified_separately_from_agent_coordination(self):
        from agent_messaging.delivery import notification
        delivery = {'senderProject': 'global', 'senderId': 'user', 'projectKey': 'alpha', 'agentId': 'first', 'messageId': 'one'}
        prompt = notification(delivery, {'projectConfigs': {'alpha': '/private/client.json'}})
        self.assertIn('owner\u2019s personal mailbox', prompt)
        self.assertNotIn('Agent-authored coordination notification', prompt)

    def test_busy_session_defers_without_starting_turn(self):
        from unittest.mock import MagicMock
        connection = MagicMock()
        connection.rpc.return_value = {'thread': {'status': {'type': 'active'}}}
        with patch('agent_messaging.delivery.find_codex', return_value=connection):
            result = deliver_codex({'sessionId': 'one'}, {'codexSocketDirectory': '/unused'})
        self.assertEqual(result['state'], 'queued')
        self.assertEqual(connection.rpc.call_count, 1)

    def test_lost_turn_acceptance_is_not_automatically_retried(self):
        from unittest.mock import MagicMock
        connection = MagicMock()
        connection.rpc.side_effect = [{'thread': {'status': {'type': 'idle'}}}, TimeoutError()]
        delivery = {'sessionId': 'one', 'projectKey': 'alpha', 'agentId': 'first', 'messageId': 'message'}
        with patch('agent_messaging.delivery.find_codex', return_value=connection):
            result = deliver_codex(delivery, {'codexSocketDirectory': '/unused', 'projectConfigs': {'alpha': '/private/client.json'}})
        self.assertEqual(result['state'], 'uncertain')


if __name__ == '__main__': unittest.main()
