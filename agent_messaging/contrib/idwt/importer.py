"""Preserve IDWT's mailbox exactly without touching its research catalog."""
import argparse
from collections import Counter
import hashlib
import importlib.util
import json
from pathlib import Path

from ...config import read_config
from ...server import connection
from ...store import Store, now, digest


def import_mail(root, config, project='idwt'):
    config = dict(config, readTimeout=120)
    root = Path(root).resolve()
    spec = importlib.util.spec_from_file_location('legacy_agent_mail', root / 'scripts/agent-mail.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    box = getattr(module, 'LegacyMailbox', module.Mailbox)(root)
    messages = box.messages()
    counts = Counter(m['id'] for m in messages)
    ids = {m['path']: m['id'] if counts[m['id']] == 1 else 'legacy-' + digest(m['path']) for m in messages}
    imported_messages, imported_receipts, documents_verified = 0, 0, 0
    with connection(config) as db:
        store = Store(db, config['adminTokenHash'])
        admin = {'role': 'admin'}
        store.project(admin, project, 'IDWT')
        for agent in box.agents():
            existing = store.rows('SELECT agentId FROM agents WHERE projectKey=%s AND agentId=%s', (project, agent['agent']))
            if not existing:
                store.execute('INSERT INTO agents(projectKey,agentId,name,sessionId,declaredState,task,note,createdAt,updatedAt) '
                              'VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                              (project, agent['agent'], agent['name'], agent['session'], agent['state'], agent.get('task', ''),
                               agent.get('note', ''), agent['created_at'], agent['updated_at']))
        # Imported historical mail never queues live notifications.
        for index, message in enumerate(messages):
            raw = (root / message['path']).read_bytes().decode('utf-8')
            hashed = digest(raw)
            existing = store.rows('SELECT id,bodySHA256,rawText FROM messages WHERE legacyPath=%s AND projectKey=%s', (message['path'], project))
            if existing:
                if existing[0]['bodySHA256'] != hashed or existing[0]['rawText'] != raw:
                    raise ValueError('Previously imported legacy message changed: ' + message['path'])
                continue
            message_id = ids[message['path']]
            original_reply = message['in_reply_to']
            reply = original_reply if original_reply and counts.get(original_reply) == 1 else None
            created = message['created_at'].replace('+00:00', 'Z')
            body = raw.split('\n\n', 1)[-1]
            store.execute('INSERT INTO messages(id,projectKey,senderProject,senderId,recipient,subject,kind,task,replyTo,body,rawText,'
                          'bodySHA256,createdAt,legacyPath,legacyId,metadataWarning) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                          (message_id, project, project, message['sender'], 'all' if message['to'] == '*' else message['to'],
                           message['subject'], message['kind'], message['task'], reply, body, raw, hashed, created,
                           message['path'], message['id'], message['metadata_warning']))
            stored = store.rows('SELECT rawText,bodySHA256 FROM messages WHERE id=%s', (message_id,))[0]
            if stored['rawText'] != raw or stored['bodySHA256'] != hashed: raise ValueError('Message readback failed')
            imported_messages += 1
            if index % 100 == 99: db.commit()
        for path in sorted((box.state / 'acks').glob('*/*.json')):
            receipt = json.loads(path.read_text())
            message_id = ids.get(receipt['path'])
            if not message_id: raise ValueError('Receipt points to missing legacy message: ' + str(path))
            existing = store.rows('SELECT * FROM receipts WHERE messageId=%s AND projectKey=%s AND agentId=%s',
                                  (message_id, project, receipt['agent']))
            if existing:
                if existing[0]['note'] != receipt.get('note', '') or existing[0]['acknowledgedAt'] != receipt['acknowledged_at']:
                    raise ValueError('Previously imported legacy receipt changed: ' + str(path))
                continue
            store.execute('INSERT INTO receipts(messageId,projectKey,agentId,addressedAs,acknowledgedAt,note) VALUES(%s,%s,%s,%s,%s,%s)',
                          (message_id, project, receipt['agent'], receipt.get('addressed_as', 'principal'), receipt['acknowledged_at'], receipt.get('note', '')))
            stored = store.rows('SELECT note,acknowledgedAt FROM receipts WHERE messageId=%s AND projectKey=%s AND agentId=%s',
                                (message_id, project, receipt['agent']))[0]
            if stored['note'] != receipt.get('note', '') or stored['acknowledgedAt'] != receipt['acknowledged_at']:
                raise ValueError('Receipt readback failed')
            imported_receipts += 1
            if imported_receipts % 100 == 0: db.commit()
        principal = box.coordinator()
        if principal:
            current = store.rows('SELECT agentId FROM aliases WHERE projectKey=%s AND alias=%s', (project, 'principal'))
            if not current: store.set_alias(admin, project, 'principal', principal['agent'])
            elif current[0]['agentId'] != principal['agent']: raise ValueError('Central and legacy principal disagree')
        paths = list((box.state).rglob('*'))
        paths.extend(root / path for path in ('agent-communication.md', 'AGENTS.md', '.agents/skills/idwt-mail/SKILL.md',
                                              '.agents/skills/idwt-principal/SKILL.md', 'database/research-workflow.py', 'scripts/agent-mail.py'))
        for path in sorted(paths):
            if not path.is_file() or path.is_symlink(): continue
            content = path.read_bytes(); hashed = hashlib.sha256(content).hexdigest()
            relative = str(path.relative_to(root))
            store.execute('INSERT IGNORE INTO documents(projectKey,sourcePath,sha256,content,createdAt) VALUES(%s,%s,%s,%s,%s)',
                          (project, relative, hashed, content, now()))
            stored = store.rows('SELECT content FROM documents WHERE projectKey=%s AND sourcePath=%s AND sha256=%s',
                                (project, relative, hashed))[0]['content']
            if stored != content: raise ValueError('Communication document readback failed: ' + relative)
            documents_verified += 1
            if documents_verified % 100 == 0: db.commit()
        store.event(project, 'import', 'idwt-mailbox')
    return {'messagesImported': imported_messages, 'receiptsImported': imported_receipts,
            'messagesVerified': len(messages), 'agentsInspected': len(box.agents()), 'documentsVerified': documents_verified,
            'legacyFilesPreserved': True}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True)
    parser.add_argument('--config', required=True)
    args = parser.parse_args()
    print(json.dumps(import_mail(args.root, read_config(args.config)), indent=2))


if __name__ == '__main__': main()
