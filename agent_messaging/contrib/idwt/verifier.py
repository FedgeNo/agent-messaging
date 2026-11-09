"""Read-only byte verification of the legacy communication store."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path

from ...config import read_config
from ...server import connection
from ...store import Store


def verify(root, config):
    root = Path(root).resolve()
    spec = importlib.util.spec_from_file_location('old_mail', root / 'scripts/agent-mail.py')
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    box = getattr(module, 'LegacyMailbox', module.Mailbox)(root)
    counts = {'messages': 0, 'receipts': 0, 'metadataAndGuidance': 0, 'registeredAgents': 0}
    with connection(dict(config, readTimeout=120)) as db:
        store = Store(db, config['adminTokenHash'])
        for message in box.messages():
            raw = (root / message['path']).read_bytes()
            found = store.rows('SELECT rawText,bodySHA256 FROM messages WHERE projectKey=%s AND legacyPath=%s', ('idwt', message['path']))
            if len(found) != 1 or found[0]['rawText'].encode() != raw or found[0]['bodySHA256'] != hashlib.sha256(raw).hexdigest():
                raise ValueError('Message mismatch: ' + message['path'])
            counts['messages'] += 1
        for path in (box.state / 'acks').glob('*/*.json'):
            old = json.loads(path.read_text())
            found = store.rows('SELECT r.* FROM receipts r JOIN messages m ON m.id=r.messageId '
                               'WHERE m.projectKey=%s AND m.legacyPath=%s AND r.agentId=%s', ('idwt', old['path'], old['agent']))
            if len(found) != 1 or found[0]['note'] != old.get('note', '') or found[0]['acknowledgedAt'] != old['acknowledged_at']:
                raise ValueError('Receipt mismatch: ' + str(path))
            counts['receipts'] += 1
        paths = list(box.state.rglob('*'))
        paths.extend(root / p for p in ('agent-communication.md', 'AGENTS.md', '.agents/skills/idwt-mail/SKILL.md',
                                        '.agents/skills/idwt-principal/SKILL.md', 'database/research-workflow.py', 'scripts/agent-mail.py'))
        for path in paths:
            if not path.is_file() or path.is_symlink(): continue
            content = path.read_bytes(); hashed = hashlib.sha256(content).hexdigest()
            found = store.rows('SELECT content FROM documents WHERE projectKey=%s AND sourcePath=%s AND sha256=%s',
                               ('idwt', str(path.relative_to(root)), hashed))
            if len(found) != 1 or found[0]['content'] != content: raise ValueError('Metadata/guidance mismatch: ' + str(path))
            counts['metadataAndGuidance'] += 1
        for old in box.agents():
            found = store.rows('SELECT sessionId FROM agents WHERE projectKey=%s AND agentId=%s', ('idwt', old['agent']))
            if len(found) != 1 or found[0]['sessionId'] != old['session']: raise ValueError('Registration mismatch: ' + old['agent'])
            counts['registeredAgents'] += 1
        principal = box.coordinator()
        actual = store.rows('SELECT agentId FROM aliases WHERE projectKey=%s AND alias=%s', ('idwt', 'principal'))
        if not actual or actual[0]['agentId'] != principal['agent']: raise ValueError('Principal route changed unexpectedly')
    return dict(verified=True, counts=counts, principal=principal['agent'], researchDatabaseUntouched=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True)
    parser.add_argument('--config', required=True)
    args = parser.parse_args()
    print(json.dumps(verify(args.root, read_config(args.config)), indent=2))


if __name__ == '__main__': main()
