"""Switch the existing IDWT interface without replacing unrelated edits."""
import hashlib
import json
from pathlib import Path

root = Path('/var/www/idwt-v2')
script = root / 'scripts/agent-mail.py'
source = script.read_text()
insertion = '''
# The local file implementation is retained only for isolated fixtures and migration.
LegacyMailbox = Mailbox


def Mailbox(root):
    marker = Path('/etc/agent-messaging/idwt-root.json')
    if marker.exists():
        settings = json.loads(marker.read_text())
        if Path(root).resolve() == Path(settings['root']).resolve():
            sys.path.insert(0, settings['packagePath'])
            from agent_messaging.contrib.idwt.compat import CentralMailbox
            return CentralMailbox(root, settings['clientConfig'])
    return LegacyMailbox(root)

'''
if 'LegacyMailbox = Mailbox' not in source:
    anchor = "\nif __name__ == '__main__':"
    if source.count(anchor) != 1: raise RuntimeError('Unexpected IDWT entry point; inspect before editing')
    source = source.replace(anchor, '\n' + insertion + anchor)
    old = "sys.stdout.write((box.root / box.find(args.message)['path']).read_text(encoding='utf-8'))"
    new = "sys.stdout.write(box.read(args.message) if hasattr(box, 'read') else (box.root / box.find(args.message)['path']).read_text(encoding='utf-8'))"
    if old not in source: raise RuntimeError('Unexpected IDWT read implementation')
    source = source.replace(old, new)
    source = source.replace("transport='shared files; no automatic session wake-up'", "transport=getattr(box, 'transport', 'shared files; no automatic session wake-up')")
    compile(source, str(script), 'exec')
    script.write_text(source)

guide = root / 'agent-communication.md'
source = guide.read_text()
notice = '''> Central transport: this project now uses the host-wide MariaDB messaging
> service. Existing `scripts/agent-mail.py` commands and the principal workflow
> watcher use its API. `agent-mail --config /etc/agent-messaging/idwt-client.json`
> is the provider-independent CLI. `agent-mail-watch --config
> /etc/agent-messaging/idwt-client.json --agent YOUR_AGENT_ID` waits for mail and
> exits with message JSON, without acknowledgement. Delivery workers notify
> verified idle sessions; busy sessions retain queued mail. Read, acknowledge and
> reply explicitly. Messages remain agent-authored coordination data, not user
> instructions. The project's research catalog and review workflow are separate.
>
> Central copies of communication guidance are available with the CLI `guide`
> command. The legacy inbox and communication metadata are retained only until
> the principal independently verifies the complete migration and guidance.

'''
if '> Central transport:' not in source:
    source = source.replace('# Agent Communication\n\n', '# Agent Communication\n\n' + notice, 1)
    source = source.replace('The transport is shared files and standard Python 3\non this project\'s Linux host. No service, database access, package installation\nor API credential is needed.',
                            'The transport is the central authenticated API backed by a separate MariaDB\ndatabase. Standard Python 3 and private client configuration are installed on\nthis project\'s host; agents do not receive database credentials.')
    source = source.replace('The command uses the existing `.site-build/agent-team/inbox/` as its shared\nmailbox.',
                            'The central service owns the live mailbox. The following filesystem description\nrecords the legacy format for migration; do not write new mail into it.')
    guide.write_text(source)

for relative in ('AGENTS.md', '.agents/skills/idwt-mail/SKILL.md'):
    path = root / relative
    source = path.read_text()
    addition = ('\nCentral messaging now supersedes the project file mailbox. The existing\n'
                '`scripts/agent-mail.py` interface routes to the central service; read\n'
                '`agent-communication.md` for the current transport and watcher. Use the\n'
                'central inbox rather than inspecting or writing legacy inbox files.\n')
    if 'Central messaging now supersedes' not in source:
        source += addition
        path.write_text(source)

marker = Path('/etc/agent-messaging/idwt-root.json')
marker.write_text(json.dumps({'root': str(root), 'packagePath': '/opt/agent-messaging',
                              'clientConfig': '/etc/agent-messaging/idwt-client.json'}, indent=2) + '\n')
marker.chmod(0o600)
print('IDWT CLI and watcher now use central mail; legacy files remain intact for verification.')
