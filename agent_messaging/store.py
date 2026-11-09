from datetime import datetime, timezone
import hashlib
import base64
import hmac
import json
import re
import secrets
import time
import uuid

# Runtimes with no live session to notify; mail for them is only read from the inbox.
# Any other runtime name is delivered through the matching delivery connector.
PASSIVE_RUNTIMES = ('mailbox', 'scheduled')


def now():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-z0-9][a-z0-9._-]{0,127}', value):
        raise ValueError('Invalid project or agent ID')
    return value


def text(value, maximum, empty=True):
    if not isinstance(value, str) or len(value) > maximum or '\x00' in value or (not empty and not value.strip()):
        raise ValueError('Invalid or oversized text field')
    return value


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class Store:
    def __init__(self, db, admin_hash):
        self.db = db
        self.admin_hash = admin_hash

    def rows(self, sql, args=()):
        with self.db.cursor() as cursor:
            cursor.execute(sql, args)
            return list(cursor.fetchall())

    def execute(self, sql, args=()):
        with self.db.cursor() as cursor:
            cursor.execute(sql, args)
            return cursor.lastrowid

    def event(self, project, kind, reference):
        self.execute('INSERT INTO events(projectKey,kind,reference,createdAt) VALUES(%s,%s,%s,%s)',
                     (project, kind, reference, now()))

    def authenticate(self, token):
        hashed = digest(token)
        if hmac.compare_digest(hashed, self.admin_hash):
            return {'role': 'admin'}
        found = self.rows('SELECT projectKey,agentId,hostKey,role FROM credentials WHERE tokenHash=%s', (hashed,))
        if not found:
            raise PermissionError('Invalid credentials')
        return found[0]

    def authorize(self, auth, project, agent=None, admin=False):
        identifier(project)
        if auth['role'] == 'admin':
            return
        if admin or auth['role'] == 'host' or auth.get('projectKey') != project:
            raise PermissionError('Credential does not authorize this project or operation')
        if auth.get('agentId') and auth['agentId'] != agent:
            raise PermissionError('Credential belongs to another agent')

    def project(self, auth, project, name):
        self.authorize(auth, project, admin=True)
        self.execute('INSERT INTO projects(projectKey,name) VALUES(%s,%s) ON DUPLICATE KEY UPDATE name=VALUES(name)',
                     (project, text(name, 255, False)))
        return {'project': project}

    def credential(self, auth, project, agent=None, role='project', host=None):
        self.authorize(auth, project, admin=True)
        if role not in ('project', 'agent', 'host'):
            raise ValueError('Invalid credential role')
        if role == 'agent': identifier(agent)
        if role == 'host': identifier(host)
        token = secrets.token_urlsafe(36)
        self.execute('INSERT INTO credentials(tokenHash,projectKey,agentId,hostKey,role,createdAt) VALUES(%s,%s,%s,%s,%s,%s)',
                     (digest(token), project if role != 'host' else None, agent if role == 'agent' else None,
                      host if role == 'host' else None, role, now()))
        return {'token': token}

    def agent(self, project, agent):
        found = self.rows('SELECT * FROM agents WHERE projectKey=%s AND agentId=%s', (project, agent))
        if not found: raise ValueError('Agent is not registered')
        return found[0]

    def register(self, auth, project, agent, name, session, coordinator=False, replace=False, host='', runtime='mailbox', runtimeSession=None):
        identifier(agent)
        if agent in ('principal', 'all', 'coordinator'): raise ValueError('Choose an identity rather than a delivery alias')
        self.authorize(auth, project, agent)
        if not re.fullmatch(r'[a-z][a-z0-9-]{0,31}', runtime): raise ValueError('Invalid runtime')
        if host: identifier(host)
        if (runtime not in PASSIVE_RUNTIMES or runtimeSession is not None) and auth['role'] != 'admin':
            raise PermissionError('A coordinator must bind live session delivery')
        name, session = text(name, 255, False), text(session, 255, False)
        # Serialize registration and handoff within one project.
        self.rows('SELECT projectKey FROM projects WHERE projectKey=%s FOR UPDATE', (project,))
        existing = self.rows('SELECT * FROM agents WHERE projectKey=%s AND agentId=%s', (project, agent))
        if existing and existing[0]['sessionId'] != session:
            raise ValueError('Agent ID belongs to a different session')
        if existing and existing[0]['declaredState'] == 'retired':
            raise ValueError('Mailbox is retired; an administrator must restore it first')
        if existing and auth['role'] != 'admin':
            host, runtime = existing[0]['hostKey'], existing[0]['runtime']
            runtimeSession = existing[0]['runtimeSessionId']
        elif existing and runtimeSession is None:
            runtimeSession = existing[0]['runtimeSessionId']
        if runtimeSession is not None: text(runtimeSession, 255, False)
        stamp = now()
        self.execute('INSERT INTO agents(projectKey,agentId,name,sessionId,runtimeSessionId,hostKey,runtime,note,createdAt,updatedAt) '
                     'VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE name=VALUES(name),'
                     'updatedAt=VALUES(updatedAt),hostKey=VALUES(hostKey),runtime=VALUES(runtime),runtimeSessionId=VALUES(runtimeSessionId)',
                     (project, agent, name, session, runtimeSession, host, runtime, '', stamp, stamp))
        if coordinator:
            self.set_alias(auth, project, 'principal', agent, replace)
        self.event(project, 'register', agent)
        return self.agent(project, agent)

    def set_alias(self, auth, project, alias, agent, replace=False):
        self.authorize(auth, project, admin=True)
        identifier(alias); self.agent(project, identifier(agent))
        if self.agent(project, agent)['declaredState'] == 'retired':
            raise ValueError('Cannot assign an alias to a retired mailbox')
        self.rows('SELECT projectKey FROM projects WHERE projectKey=%s FOR UPDATE', (project,))
        existing = self.rows('SELECT agentId FROM aliases WHERE projectKey=%s AND alias=%s', (project, alias))
        if existing and existing[0]['agentId'] != agent and not replace:
            raise ValueError('Alias already has an owner; an explicit handoff is required')
        self.execute('INSERT INTO aliases(projectKey,alias,agentId,updatedAt) VALUES(%s,%s,%s,%s) '
                     'ON DUPLICATE KEY UPDATE agentId=VALUES(agentId),updatedAt=VALUES(updatedAt)',
                     (project, alias, agent, now()))
        self.event(project, 'alias', alias)
        self.execute("UPDATE deliveries d JOIN messages m ON m.id=d.messageId SET d.state='superseded',"
                     "d.detail='Recipient alias was handed off' WHERE m.projectKey=%s AND m.recipient=%s "
                     "AND d.agentId<>%s AND d.state='queued'", (project, alias, agent))
        target = self.agent(project, agent)
        if target['runtime'] not in PASSIVE_RUNTIMES:
            self.execute('INSERT IGNORE INTO deliveries(messageId,projectKey,agentId) '
                         'SELECT m.id,m.projectKey,%s FROM messages m WHERE m.projectKey=%s AND m.recipient=%s '
                         'AND NOT EXISTS(SELECT 1 FROM receipts r WHERE r.messageId=m.id AND r.addressedAs=%s)',
                         (agent, project, alias, alias))
        return {'alias': alias, 'agent': agent}

    def status(self, auth, project, agent, state, task=None, note=None):
        self.authorize(auth, project, agent); self.agent(project, agent)
        if self.agent(project, agent)['declaredState'] == 'retired':
            raise ValueError('Mailbox is retired')
        if state not in ('active', 'idle', 'done', 'blocked'): raise ValueError('Invalid state')
        assignments, values = ['declaredState=%s', 'updatedAt=%s'], [state, now()]
        if task is not None: assignments.append('task=%s'); values.append(text(task, 255))
        if note is not None: assignments.append('note=%s'); values.append(text(note, 10000))
        self.execute('UPDATE agents SET ' + ','.join(assignments) + ' WHERE projectKey=%s AND agentId=%s', (*values, project, agent))
        self.event(project, 'status', agent)
        return self.agent(project, agent)

    def info(self, auth, project):
        self.authorize(auth, project, auth.get('agentId'))
        return {'project': project, 'aliases': self.rows('SELECT alias,agentId,updatedAt FROM aliases WHERE projectKey=%s', (project,)),
                'transport': 'central MariaDB service', 'statusIs': 'declared state and observed runtime state are separate'}

    def retire(self, auth, project, agent, reason):
        self.authorize(auth, project, admin=True)
        identifier(agent); text(reason, 1000, False)
        self.rows('SELECT projectKey FROM projects WHERE projectKey=%s FOR UPDATE', (project,))
        target = self.agent(project, agent)
        if self.rows('SELECT alias FROM aliases WHERE projectKey=%s AND agentId=%s', (project, agent)):
            raise ValueError('Hand off active aliases before retiring their owner')
        if target['declaredState'] == 'retired':
            return {'agent': agent, 'retired': True, 'alreadyRetired': True}
        stamp = now()
        self.execute("UPDATE agents SET declaredState='retired',updatedAt=%s,note=%s WHERE projectKey=%s AND agentId=%s",
                     (stamp, target['note'] + '\nRetired ' + stamp + ': ' + reason, project, agent))
        self.execute("UPDATE deliveries SET state='superseded',detail='Recipient mailbox retired' "
                     "WHERE projectKey=%s AND agentId=%s AND state IN ('queued','dispatching','unavailable','uncertain')",
                     (project, agent))
        self.event(project, 'retire', agent)
        return {'agent': agent, 'retired': True, 'previousState': target['declaredState']}

    def restore_agent(self, auth, project, agent):
        self.authorize(auth, project, admin=True)
        self.agent(project, identifier(agent))
        self.execute("UPDATE agents SET declaredState='idle',updatedAt=%s WHERE projectKey=%s AND agentId=%s AND declaredState='retired'",
                     (now(), project, agent))
        self.event(project, 'restore-agent', agent)
        return self.agent(project, agent)

    def agents(self, auth, project, includeRetired=False):
        self.authorize(auth, project, auth.get('agentId'))
        clause = '' if includeRetired else " AND declaredState<>'retired'"
        return {'agents': self.rows('SELECT * FROM agents WHERE projectKey=%s' + clause + ' ORDER BY agentId', (project,))}

    def directory(self, auth, project, after='', limit=1000, includeRetired=False):
        self.authorize(auth, project, auth.get('agentId'))
        if auth['role'] != 'admin' and (project != 'global' or auth.get('agentId') != 'user'):
            raise PermissionError('The cross-project directory is available to the mailbox owner')
        if not isinstance(limit, int) or not 1 <= limit <= 1000: raise ValueError('Invalid directory limit')
        previous_project, previous_agent = ('', '')
        if after:
            previous_project, previous_agent = after.split('/', 1)
            identifier(previous_project); identifier(previous_agent)
        found = self.rows('SELECT projectKey,agentId,name,hostKey,runtime,declaredState,updatedAt FROM agents '
                          'WHERE (projectKey>%s OR (projectKey=%s AND agentId>%s))' +
                          ('' if includeRetired else " AND declaredState<>'retired'") + ' ORDER BY projectKey,agentId LIMIT %s',
                          (previous_project, previous_project, previous_agent, limit + 1))
        more = len(found) > limit; found = found[:limit]
        return {'agents': found, 'hasMore': more,
                'nextCursor': found[-1]['projectKey'] + '/' + found[-1]['agentId'] if found else after}

    def resolve_recipients(self, project, recipient):
        if recipient == 'all':
            return self.rows("SELECT * FROM agents WHERE projectKey=%s AND declaredState<>'retired'", (project,))
        return self.rows('SELECT a.* FROM agents a WHERE a.projectKey=%s AND (a.agentId=%s OR a.agentId IN '
                         '(SELECT agentId FROM aliases WHERE projectKey=%s AND alias=%s))',
                         (project, recipient, project, recipient))

    def send(self, auth, project, agent, to, subject, body, kind='message', task='', replyTo=None, idempotencyKey=None, notify=True):
        self.authorize(auth, project, agent); self.agent(project, agent)
        if self.agent(project, agent)['declaredState'] == 'retired':
            raise ValueError('Sender mailbox is retired')
        if not isinstance(to, str): raise ValueError('Invalid recipient')
        destination, recipient = to.split('/', 1) if '/' in to else (project, to)
        identifier(destination); identifier(recipient)
        text(subject, 1000, False); text(body, 262144, False); text(task, 255)
        if kind not in ('message', 'introduction', 'finding', 'question', 'blocked', 'complete', 'acknowledgement'):
            raise ValueError('Invalid message kind')
        if '\n' in subject or '\r' in subject: raise ValueError('Subject must be one line')
        if replyTo: self.read(auth, project, replyTo, agent)
        if idempotencyKey: identifier(idempotencyKey)
        request_hash = digest(json.dumps([destination, recipient, subject, body, kind, task, replyTo, bool(notify)], ensure_ascii=False))
        # Serialize a sender's retry-key check and insertion.
        self.rows('SELECT agentId FROM agents WHERE projectKey=%s AND agentId=%s FOR UPDATE', (project, agent))
        if idempotencyKey:
            existing = self.rows('SELECT id,requestSHA256 FROM messages WHERE senderProject=%s AND senderId=%s AND idempotencyKey=%s FOR UPDATE',
                                 (project, agent, idempotencyKey))
            if existing:
                if existing[0]['requestSHA256'] != request_hash: raise ValueError('Retry key was already used for different content')
                return {'id': existing[0]['id'], 'saved': True, 'duplicate': True}
        recipients = self.resolve_recipients(destination, recipient)
        if any(target['declaredState'] == 'retired' for target in recipients):
            raise ValueError('Recipient mailbox is retired')
        stamp, message_id = now(), uuid.uuid4().hex
        raw = '\n'.join([f'Message-ID: {message_id}', f'From: {project}/{agent}', f'To: {destination}/{recipient}',
                         f'Created-UTC: {stamp}', f'Subject: {subject}', f'Kind: {kind}', f'Task: {task}'])
        if replyTo: raw += '\nIn-Reply-To: ' + replyTo
        raw += '\n\n' + body
        if not raw.endswith('\n'): raw += '\n'
        self.execute('INSERT INTO messages(id,projectKey,senderProject,senderId,recipient,subject,kind,task,replyTo,body,rawText,'
                     'bodySHA256,createdAt,idempotencyKey,requestSHA256) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                     (message_id, destination, project, agent, recipient, subject, kind, task, replyTo, body, raw, digest(raw), stamp,
                      idempotencyKey, request_hash))
        if notify:
            for target in recipients:
                if target['runtime'] not in PASSIVE_RUNTIMES:
                    self.execute('INSERT INTO deliveries(messageId,projectKey,agentId) VALUES(%s,%s,%s)',
                                 (message_id, destination, target['agentId']))
        self.event(destination, 'message', message_id)
        return {'id': message_id, 'saved': True, 'recipientRegistered': bool(recipients),
                'delivery': 'saved; notification and acknowledgement are separate', 'path': 'message:' + message_id}

    def aliases_for(self, project, agent):
        self.agent(project, agent)
        return [r['alias'] for r in self.rows('SELECT alias FROM aliases WHERE projectKey=%s AND agentId=%s', (project, agent))]

    def find(self, message):
        text(message, 1000, False)
        found = self.rows('SELECT * FROM messages WHERE id=%s OR legacyId=%s', (message, message))
        if not found:
            # Legacy callers may supply a filename to disambiguate duplicate IDs.
            found = self.rows('SELECT * FROM messages WHERE legacyPath=%s OR legacyPath=%s',
                              (message, '.site-build/agent-team/inbox/' + message))
        if len(found) != 1: raise ValueError('Message not found or legacy ID is ambiguous')
        return found[0]

    def accessible(self, auth, project, message, agent=None):
        if auth['role'] == 'admin': return
        self.authorize(auth, project, agent)
        if message['senderProject'] == project and (not auth.get('agentId') or message['senderId'] == agent): return
        if message['projectKey'] == project:
            if not auth.get('agentId'): return
            if message['recipient'] in [agent, 'all', *self.aliases_for(project, agent)]: return
        raise PermissionError('Message is not addressed to this identity')

    def read(self, auth, project, message, agent=None):
        found = self.find(message); self.accessible(auth, project, found, agent)
        return found

    def inbox(self, auth, project, agent, includeAcknowledged=False, since=None, limit=50, after=0, newestFirst=False, before=None):
        self.authorize(auth, project, agent)
        aliases = [agent, 'all', *self.aliases_for(project, agent)]
        if not isinstance(limit, int) or not 0 <= limit <= 1000: raise ValueError('Limit must be 0–1000')
        if not isinstance(after, int) or after < 0: raise ValueError('Invalid cursor')
        clauses = ['m.projectKey=%s', 'm.recipient IN (' + ','.join(['%s'] * len(aliases)) + ')', 'm.sequence>%s']
        args = [project, *aliases, after]
        if before is not None:
            if not isinstance(before, int) or before <= 0: raise ValueError('Invalid cursor')
            clauses.append('m.sequence<%s'); args.append(before)
        if not isinstance(newestFirst, bool): raise ValueError('Invalid sort order')
        if since:
            parsed = datetime.fromisoformat(since.replace('Z', '+00:00'))
            if parsed.tzinfo is None: raise ValueError('Timestamp requires timezone')
            clauses.append('m.createdAt>=%s'); args.append(parsed.astimezone(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z'))
        acknowledged = ('EXISTS(SELECT 1 FROM receipts r WHERE r.messageId=m.id AND r.projectKey=m.projectKey '
                        'AND (r.agentId=%s OR (m.recipient NOT IN (%s,%s) AND r.addressedAs=m.recipient)))')
        if not includeAcknowledged: clauses.append('NOT ' + acknowledged); args.extend([agent, agent, 'all'])
        size = limit or 1000
        rows = self.rows('SELECT m.sequence,m.id,m.senderProject,m.senderId,m.projectKey,m.recipient,m.subject,m.kind,m.task,'
                         'm.replyTo,m.createdAt,m.legacyPath,m.metadataWarning,' + acknowledged + ' AS acknowledged FROM messages m WHERE '
                         + ' AND '.join(clauses) + (' ORDER BY m.sequence DESC' if newestFirst else ' ORDER BY m.sequence')
                         + ' LIMIT %s', [agent, agent, 'all', *args, size + 1])
        more = len(rows) > size
        rows = rows[:size]
        return {'agent': agent, 'messages': rows, 'hasMore': more, 'nextCursor': rows[-1]['sequence'] if rows else after,
                'totalMatching': len(rows), 'truncated': more}

    def ack(self, auth, project, agent, message, note=''):
        self.authorize(auth, project, agent)
        row = self.find(message)
        if row['projectKey'] != project or row['recipient'] not in [agent, 'all', *self.aliases_for(project, agent)]:
            raise PermissionError('Only a recipient may acknowledge a message')
        self.execute('INSERT IGNORE INTO receipts(messageId,projectKey,agentId,addressedAs,acknowledgedAt,note) VALUES(%s,%s,%s,%s,%s,%s)',
                     (row['id'], project, agent, row['recipient'], now(), text(note, 10000)))
        self.execute("UPDATE deliveries SET state='acknowledged',detail='' WHERE messageId=%s AND projectKey=%s AND agentId=%s",
                     (row['id'], project, agent))
        self.event(project, 'acknowledgement', row['id'])
        return self.rows('SELECT * FROM receipts WHERE messageId=%s AND projectKey=%s AND agentId=%s', (row['id'], project, agent))[0]

    def receipts(self, auth, project, message, agent=None):
        row = self.read(auth, project, message, agent)
        return {'receipts': self.rows('SELECT * FROM receipts WHERE messageId=%s ORDER BY acknowledgedAt', (row['id'],)),
                'deliveries': self.rows('SELECT * FROM deliveries WHERE messageId=%s ORDER BY id', (row['id'],))}

    def revision(self, auth, project):
        self.authorize(auth, project, auth.get('agentId'))
        found = self.rows('SELECT sequence FROM events WHERE projectKey=%s ORDER BY sequence DESC LIMIT 1', (project,))
        return {'revision': found[0]['sequence'] if found else 0}

    def history(self, auth, project, after=0, limit=1000):
        self.authorize(auth, project, admin=True)
        return {'messages': self.rows('SELECT * FROM messages WHERE projectKey=%s AND sequence>%s ORDER BY sequence LIMIT %s',
                                      (project, int(after), min(1000, max(1, int(limit))))) }

    def claim(self, auth, project=None, messageId=None):
        if auth['role'] not in ('host', 'admin'): raise PermissionError('Host credential required')
        host = auth.get('hostKey')
        if not host: raise ValueError('A host-bound credential is required')
        message_filter = 'AND d.messageId=%s ' if messageId else ''
        found = self.rows("SELECT d.*,m.senderProject,m.senderId,COALESCE(a.runtimeSessionId,a.sessionId) AS sessionId,a.runtime,a.hostKey "
                          "FROM deliveries d JOIN messages m ON m.id=d.messageId JOIN agents a ON a.projectKey=d.projectKey "
                          "AND a.agentId=d.agentId WHERE d.state='queued' AND a.declaredState<>'retired' AND d.nextAttemptAt<=%s AND a.hostKey=%s "
                          + message_filter + 'ORDER BY d.id LIMIT 1 FOR UPDATE SKIP LOCKED',
                          (time.time(), host, messageId) if messageId else (time.time(), host))
        if not found: return {'delivery': None}
        row = found[0]; attempt = uuid.uuid4().hex
        self.execute("UPDATE deliveries SET state='dispatching',attemptId=%s,attemptedAt=%s WHERE id=%s", (attempt, now(), row['id']))
        row['attemptId'] = attempt
        return {'delivery': row}

    def delivered(self, auth, project, deliveryId, attemptId, state, detail='', turnId=None, observedState='unknown'):
        if auth['role'] != 'host': raise PermissionError('Host credential required')
        if state not in ('queued', 'accepted', 'uncertain', 'unavailable'): raise ValueError('Invalid delivery state')
        found = self.rows('SELECT d.*,a.hostKey FROM deliveries d JOIN agents a ON a.projectKey=d.projectKey AND a.agentId=d.agentId '
                          'WHERE d.id=%s AND d.attemptId=%s FOR UPDATE', (deliveryId, attemptId))
        if not found or found[0]['hostKey'] != auth['hostKey']: raise PermissionError('Invalid delivery claim')
        row = found[0]
        if row['state'] not in ('acknowledged', 'superseded'):
            self.execute('UPDATE deliveries SET state=%s,detail=%s,turnId=%s,nextAttemptAt=%s WHERE id=%s',
                         (state, text(detail, 1000), turnId, time.time() + 60, deliveryId))
        self.execute('UPDATE agents SET observedState=%s,observedAt=%s WHERE projectKey=%s AND agentId=%s',
                     (text(observedState, 32), now(), row['projectKey'], row['agentId']))
        self.event(row['projectKey'], 'delivery', row['messageId'])
        return {'state': state}

    def recover(self):
        # A lost response might hide an accepted turn; never replay it automatically.
        self.execute("UPDATE deliveries SET state='uncertain',detail='Dispatcher ended before recording acceptance; reconcile before retrying' "
                     "WHERE state='dispatching' AND attemptedAt<%s", (datetime.fromtimestamp(time.time() - 180, timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z'),))

    def retry(self, auth, project, deliveryId):
        self.authorize(auth, project, admin=True)
        found = self.rows('SELECT state FROM deliveries WHERE id=%s AND projectKey=%s FOR UPDATE', (deliveryId, project))
        if not found or found[0]['state'] not in ('unavailable', 'uncertain'):
            raise ValueError('Only unavailable or reconciled uncertain deliveries can be retried explicitly')
        self.execute("UPDATE deliveries SET state='queued',nextAttemptAt=0 WHERE id=%s", (deliveryId,))
        return {'queued': True}

    def guide(self, auth, project, path=None):
        self.authorize(auth, project, auth.get('agentId'))
        if path is None:
            return {'documents': self.rows('SELECT id,sourcePath,sha256,createdAt,OCTET_LENGTH(content) AS byteCount FROM documents '
                                           'WHERE projectKey=%s AND sourcePath NOT LIKE %s ORDER BY id DESC LIMIT 100',
                                           (project, '.site-build/agent-team/%'))}
        found = self.rows('SELECT * FROM documents WHERE projectKey=%s AND sourcePath=%s ORDER BY id DESC LIMIT 1',
                          (project, text(path, 512, False)))
        if not found and path == 'communication-guide.md':
            found = self.rows('SELECT * FROM documents WHERE projectKey=%s AND sourcePath=%s ORDER BY id DESC LIMIT 1',
                              ('global', path))
        if not found: raise ValueError('Guidance document not found')
        row = found[0]; content = row.pop('content')
        row['contentBase64'] = base64.b64encode(content).decode()
        return row

    def save_guide(self, auth, project, path, contentBase64):
        self.authorize(auth, project, admin=True)
        content = base64.b64decode(contentBase64, validate=True)
        if len(content) > 1024 * 1024: raise ValueError('Guidance must be at most 1 MiB')
        path = text(path, 512, False)
        hashed = hashlib.sha256(content).hexdigest()
        self.execute('INSERT IGNORE INTO documents(projectKey,sourcePath,sha256,content,createdAt) VALUES(%s,%s,%s,%s,%s)',
                     (project, path, hashed, content, now()))
        row = self.rows('SELECT id,content FROM documents WHERE projectKey=%s AND sourcePath=%s AND sha256=%s',
                        (project, path, hashed))[0]
        if row['content'] != content: raise RuntimeError('Guidance byte verification failed')
        self.event(project, 'guidance', path)
        return {'id': row['id'], 'sha256': hashed, 'byteCount': len(content)}

    def action_owner(self, auth, project):
        return auth['role'] == 'admin' or (project == 'global' and auth.get('projectKey') == 'global'
                and auth.get('agentId') in ('user', 'global-coordinator'))

    def actions(self, auth, project, after=0, limit=50, includeResolved=False):
        self.authorize(auth, project, auth.get('agentId'))
        if not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError('Action page limit must be 1–100')
        clauses, values = ["id>%s", "status<>'deleted'"], [int(after)]
        if not self.action_owner(auth, project):
            clauses.append('projectKey=%s'); values.append(project)
        if not includeResolved:
            clauses.append("status<>'resolved'")
        rows = self.rows('SELECT * FROM actionItems WHERE ' + ' AND '.join(clauses) + ' ORDER BY id LIMIT %s', (*values, limit + 1))
        more = len(rows) > limit; rows = rows[:limit]
        return {'items': rows, 'hasMore': more, 'nextCursor': rows[-1]['id'] if rows else int(after)}

    def action_put(self, auth, project, agent, key, title, description, source, status='needs-input', resolution='', expectedVersion=None):
        self.authorize(auth, project, agent)
        if self.agent(project, agent)['declaredState'] == 'retired':
            raise ValueError('Author mailbox is retired')
        identifier(key); text(title, 255, False); text(description, 30000, False); text(source, 2000, False); text(resolution, 30000)
        if status not in ('needs-input', 'waiting', 'answered', 'resolved'):
            raise ValueError('Invalid action status')
        self.rows('SELECT projectKey FROM projects WHERE projectKey=%s FOR UPDATE', (project,))
        existing = self.rows('SELECT * FROM actionItems WHERE projectKey=%s AND actionKey=%s FOR UPDATE', (project, key))
        stamp = now()
        if existing:
            item = existing[0]
            if item['status'] == 'deleted': raise ValueError('Action item was deleted by its owner')
            if not self.action_owner(auth, project) and item['authorId'] != agent:
                raise PermissionError('Only the action author or coordinator may revise this item')
            if expectedVersion is None or int(expectedVersion) != item['version']:
                raise ValueError('Action changed; read it again and supply its current version')
            self.execute('UPDATE actionItems SET title=%s,description=%s,sourceReference=%s,status=%s,resolution=%s,version=version+1,updatedAt=%s WHERE id=%s',
                         (title, description, source, status, resolution, stamp, item['id']))
        else:
            self.execute('INSERT INTO actionItems(projectKey,actionKey,authorId,title,description,sourceReference,status,resolution,createdAt,updatedAt) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                         (project, key, agent, title, description, source, status, resolution, stamp, stamp))
        self.event('global', 'action', project + '/' + key)
        return self.rows('SELECT * FROM actionItems WHERE projectKey=%s AND actionKey=%s', (project, key))[0]

    def action_delete(self, auth, project, itemId, expectedVersion, agent=None):
        agent = agent or auth.get('agentId')
        self.authorize(auth, project, agent)
        rows = self.rows('SELECT * FROM actionItems WHERE id=%s FOR UPDATE', (int(itemId),))
        if not rows: raise ValueError('Action item not found')
        item = rows[0]
        if not self.action_owner(auth, project):
            if item['projectKey'] != project or item['authorId'] != agent:
                raise PermissionError('Agents may delete only their own completed items')
            if self.agent(project, agent)['declaredState'] == 'retired':
                raise PermissionError('Author mailbox is retired')
            if item['status'] not in ('resolved', 'deleted') or not item['resolution'].strip():
                raise ValueError('Record the completed outcome and mark this item resolved before deleting')
        if item['status'] == 'deleted': return {'saved': True, 'itemId': item['id']}
        if int(expectedVersion) != item['version']:
            raise ValueError('This item changed; refresh it before deleting')
        self.execute("UPDATE actionItems SET status='deleted',version=version+1,updatedAt=%s WHERE id=%s", (now(), item['id']))
        self.event('global', 'action-deleted', str(item['id']))
        return {'saved': True, 'itemId': item['id']}

    def action_reply(self, auth, project, itemId, body, idempotencyKey, expectedVersion):
        self.authorize(auth, 'global', 'user')
        if project != 'global': raise PermissionError('Reply using the owner mailbox')
        identifier(idempotencyKey); text(body, 200000, False)
        rows = self.rows('SELECT * FROM actionItems WHERE id=%s FOR UPDATE', (int(itemId),))
        if not rows: raise ValueError('Action item not found')
        item = rows[0]
        reference = item['projectKey'] + '/' + item['actionKey']
        subject = '[Action ' + str(item['id']) + '] ' + item['title']
        content = ('Action ID: ' + str(item['id']) + '\nReference: ' + reference + '\nTitle: ' + item['title']
                   + '\nSource: ' + item['sourceReference'] + '\nItem version: ' + str(expectedVersion) + '\n\nUser response:\n' + body)
        previous = self.rows('SELECT id,body FROM messages WHERE senderProject=%s AND senderId=%s AND idempotencyKey=%s',
                             ('global', 'user', idempotencyKey))
        if previous:
            if previous[0]['body'] != content: raise ValueError('Retry key was already used for different content')
            return {'saved': True, 'id': previous[0]['id'], 'itemId': item['id'], 'duplicate': True}
        if int(expectedVersion) != item['version']:
            raise ValueError('This item changed; refresh it before sending your response')
        if item['status'] == 'deleted': raise ValueError('Action item was deleted by its owner')
        result = self.send(auth, 'global', 'user', 'global/global-coordinator', subject, content,
                           task='action-' + str(item['id']), idempotencyKey=idempotencyKey)
        self.execute("UPDATE actionItems SET status='answered',responseMessageId=%s,version=version+1,updatedAt=%s WHERE id=%s",
                     (result['id'], now(), item['id']))
        self.event('global', 'action-response', str(item['id']))
        return dict(result, itemId=item['id'])

    def delete_message(self, auth, project, messageId, reason):
        """Permanently remove one message with its receipts and delivery records.

        Only the global coordinator (or an administrator) may do this. The event log
        keeps who deleted which message and why, but never the message text."""
        coordinator = auth['role'] == 'admin' or (auth.get('projectKey') == 'global'
                                                  and auth.get('agentId') == 'global-coordinator')
        if not coordinator: raise PermissionError('Only the global coordinator may delete messages')
        reason = text(reason, 500, True) if reason is not None else ''
        if not reason.strip(): raise ValueError('A reason is required to delete a message')
        rows = self.rows('SELECT id,projectKey,senderProject,senderId,recipient FROM messages WHERE id=%s FOR UPDATE', (str(messageId),))
        if not rows: raise ValueError('Message not found')
        message = rows[0]
        # An owner's action-item reply is a message; keep the item but drop its pointer.
        self.execute('UPDATE actionItems SET responseMessageId=NULL WHERE responseMessageId=%s', (message['id'],))
        self.execute('DELETE FROM receipts WHERE messageId=%s', (message['id'],))
        self.execute('DELETE FROM deliveries WHERE messageId=%s', (message['id'],))
        self.execute('DELETE FROM messages WHERE id=%s', (message['id'],))
        actor = 'admin' if auth['role'] == 'admin' else auth.get('agentId')
        self.event(message['projectKey'], 'message-deleted', (message['id'] + ' by ' + str(actor) + ': ' + reason)[:255])
        return {'deleted': True, 'messageId': message['id'], 'projectKey': message['projectKey']}

    def dispatch(self, auth, action, params):
        methods = {'project': self.project, 'credential': self.credential, 'register': self.register, 'alias': self.set_alias,
                   'retire': self.retire, 'restore-agent': self.restore_agent,
                   'actions': self.actions, 'action-put': self.action_put, 'action-reply': self.action_reply, 'action-delete': self.action_delete, 'delete-message': self.delete_message,
                   'status': self.status, 'info': self.info, 'agents': self.agents, 'directory': self.directory, 'send': self.send, 'read': self.read,
                   'inbox': self.inbox, 'ack': self.ack, 'receipts': self.receipts, 'revision': self.revision,
                   'claim': self.claim, 'delivered': self.delivered, 'retry': self.retry, 'history': self.history,
                   'guide': self.guide, 'save-guide': self.save_guide}
        if action not in methods: raise ValueError('Unknown action')
        return methods[action](auth, **params)
