"""IDWT's existing Mailbox interface backed by central durable mail."""
from pathlib import Path
import time

from ...client import Client


def agent_record(row):
    return dict(agent=row['agentId'], name=row['name'], session=row['sessionId'], state=row['declaredState'],
                task=row['task'], note=row['note'], created_at=row['createdAt'], updated_at=row['updatedAt'])


def message_record(row):
    return dict(id=row['id'], sender=row['senderId'] if row['senderProject'] == row['projectKey'] else row['senderProject'] + '/' + row['senderId'],
                to=row['recipient'], subject=row['subject'], kind=row['kind'], task=row['task'],
                in_reply_to=row['replyTo'], created_at=row['createdAt'], path='message:' + row['id'],
                metadata_warning=row.get('metadataWarning'), acknowledged=bool(row.get('acknowledged')))


def receipt_record(row):
    return dict(agent=row['agentId'], message_id=row['messageId'], path='message:' + row['messageId'],
                addressed_as=row['addressedAs'], acknowledged_at=row['acknowledgedAt'], note=row['note'])


class CentralMailbox:
    transport = 'central MariaDB service; file transport superseded'

    def __init__(self, root, config):
        self.root = Path(root).resolve()
        self.state = self.root / '.site-build/agent-team/communication'
        self.client = Client(config)
        self.inbox = 'central://' + self.client.config['project']

    def coordinator(self):
        info = self.client.call('info')
        alias = next((r for r in info['aliases'] if r['alias'] == 'principal'), None)
        return {'agent': alias['agentId'], 'updated_at': alias['updatedAt']} if alias else None

    def register(self, agent, name, session, coordinator=False, replace=False):
        # Existing bindings are preserved for non-admin project credentials.
        return agent_record(self.client.call('register', agent=agent, name=name, session=session,
                                             coordinator=coordinator, replace=replace))

    def agents(self):
        return [agent_record(r) for r in self.client.call('agents')['agents']]

    def status(self, agent, state, task=None, note=None):
        return agent_record(self.client.call('status', agent=agent, state=state, task=task, note=note))

    def send(self, agent, recipient, subject, body, kind='message', task='', reply_to=None):
        result = self.client.call('send', agent=agent, to=recipient, subject=subject, body=body, kind=kind, task=task, replyTo=reply_to)
        result['recipient_registered'] = result.pop('recipientRegistered')
        result['to'] = recipient
        return result

    def list_inbox(self, agent, include_acknowledged=False, since=None, limit=50):
        output, cursor = [], 0
        while True:
            result = self.client.call('inbox', agent=agent, includeAcknowledged=include_acknowledged,
                                      since=since, limit=min(1000, limit) if limit else 1000, after=cursor)
            output.extend(message_record(row) for row in result['messages'])
            cursor = result['nextCursor']
            if limit or not result['hasMore']: break
        return {'agent': agent, 'messages': output, 'total_matching': len(output), 'truncated': result['hasMore']}

    def read(self, message):
        return self.client.call('read', message=message)['rawText']

    def find(self, message):
        return message_record(self.client.call('read', message=message))

    def ack(self, agent, message, note=''):
        return receipt_record(self.client.call('ack', agent=agent, message=message, note=note))

    def receipts(self, message):
        return [receipt_record(r) for r in self.client.call('receipts', message=message)['receipts']]

    def inbox_signature(self):
        return (self.client.call('revision')['revision'],)

    def wait_for_mail(self, agent, include_acknowledged, since, limit, deadline, sleep=time.sleep):
        while True:
            remaining = 55 if deadline is None else min(55, max(0, deadline - time.monotonic()))
            result = self.client.call('watch', agent=agent, includeAcknowledged=include_acknowledged,
                                      since=since, limit=min(1000, limit) if limit else 1000, timeout=remaining)
            if result['messages'] or (deadline is not None and time.monotonic() >= deadline):
                return self.list_inbox(agent, include_acknowledged, since, limit)
