import json
import os
from pathlib import Path
import pwd
import subprocess
import sys

data = json.load(sys.stdin)
os.umask(0o077)
account = pwd.getpwnam(data['user'])
home = Path(account.pw_dir)
root = Path('/etc/agent-messaging')
root.mkdir(exist_ok=True)
root.chmod(0o755)
user_root = home / '.config/agent-messaging'
user_root.mkdir(parents=True, exist_ok=True)
user_root.chmod(0o700)
os.chown(user_root, account.pw_uid, account.pw_gid)
client = data['client']
client['url'] = 'http://127.0.0.1:18769'
root_client = Path('/root/.config/agent-messaging/client.json')
root_client.parent.mkdir(parents=True, exist_ok=True)
for path in (root / (data['project'] + '-client.json'), user_root / 'client.json', root_client):
    path.write_text(json.dumps(client, indent=2) + '\n')
    path.chmod(0o600)
    if path.parent == user_root: os.chown(path, account.pw_uid, account.pw_gid)
worker = data['delivery']
worker['url'] = client['url']
worker['codexSocketDirectory'] = '/tmp/codex-daemon-' + str(account.pw_uid)
worker['projectConfigs'] = {data['project']: str(user_root / 'client.json')}
worker_path = user_root / 'delivery.json'
worker_path.write_text(json.dumps(worker, indent=2) + '\n')
worker_path.chmod(0o600)
os.chown(worker_path, account.pw_uid, account.pw_gid)
key = root / 'tunnel-key'
if not key.exists():
    subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(key)], check=True, stdout=subprocess.DEVNULL)
(root / 'known-hosts').write_text(data['centralAddress'] + ' ' + data['centralHostKey'] + '\n')
tunnel = ('[Unit]\nDescription=Encrypted central agent mail transport\nAfter=network-online.target\nWants=network-online.target\n\n'
          '[Service]\nType=simple\nExecStart=/usr/bin/ssh -i ' + str(key) + ' -o IdentitiesOnly=yes -o BatchMode=yes'
          ' -o UserKnownHostsFile=' + str(root / 'known-hosts') + ' -o StrictHostKeyChecking=yes -o ExitOnForwardFailure=yes'
          ' -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -NT -L 127.0.0.1:18769:127.0.0.1:8769 root@'
          + data['centralAddress'] + '\nRestart=always\nRestartSec=5\n\n[Install]\nWantedBy=multi-user.target\n')
delivery = ('[Unit]\nDescription=Deliver central mail to host agents\nAfter=agent-messaging-tunnel.service\n'
            'Requires=agent-messaging-tunnel.service\n\n[Service]\nType=simple\nUser=' + account.pw_name
            + '\nWorkingDirectory=/opt/agent-messaging\nExecStart=/usr/bin/python3 -m agent_messaging.delivery --config '
            + str(worker_path) + '\nRestart=on-failure\nRestartSec=5\nUMask=0077\nMemoryMax=128M\n\n'
            '[Install]\nWantedBy=multi-user.target\n')
for name, value in [('agent-messaging-tunnel.service', tunnel), ('agent-messaging-delivery.service', delivery)]:
    path = Path('/etc/systemd/system') / name
    path.write_text(value)
    path.chmod(0o644)
launcher = Path('/usr/local/bin/agent-mail')
launcher.write_text('#!/usr/bin/python3\nimport sys\nsys.path.insert(0,"/opt/agent-messaging")\n'
                    'from agent_messaging.cli import main\nraise SystemExit(main())\n')
launcher.chmod(0o755)
watch = Path('/usr/local/bin/agent-mail-watch')
watch.write_text('#!/bin/sh\nexec /usr/local/bin/agent-mail watch "$@"\n')
watch.chmod(0o755)
print(json.dumps({'publicKey': Path(str(key) + '.pub').read_text().strip()}))
