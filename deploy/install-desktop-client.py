"""Install the desktop client and persistent encrypted transport."""
import argparse
import json
import os
from pathlib import Path
import shlex
import subprocess

parser = argparse.ArgumentParser()
parser.add_argument('--host', required=True)
parser.add_argument('--port', type=int, default=18769)
args = parser.parse_args()
os.umask(0o077)
root = Path.home() / '.config/agent-messaging'
root.mkdir(parents=True, exist_ok=True)
root.chmod(0o700)
remote = ('import json; from pathlib import Path; p=Path("/etc/agent-messaging"); '
          'names=["coordinator-client.json","desktop-delivery.json"]+[x.name for x in p.glob("*-client.json")]; '
          'print(json.dumps({n:json.loads((p/n).read_text()) for n in names}))')
data = json.loads(subprocess.check_output(['ssh', '-T', '-F', str(Path.home() / '.ssh/config'), '-o', 'BatchMode=yes',
                                           args.host, 'python3 -c ' + shlex.quote(remote)], text=True))
for name, config in data.items():
    config['url'] = 'http://127.0.0.1:' + str(args.port)
    if name == 'desktop-delivery.json':
        config['codexSocketDirectory'] = '/tmp/codex-daemon-' + str(os.getuid())
        config['projectConfigs'] = {key: str(root / Path(value).name) for key, value in config['projectConfigs'].items()}
        config['projectConfigs']['global'] = str(root / 'client.json')
    target = root / ('client.json' if name == 'coordinator-client.json' else name)
    target.write_text(json.dumps(config, indent=2) + '\n')
    target.chmod(0o600)
source = Path(__file__).resolve().parents[1]
unit_dir = Path.home() / '.config/systemd/user'
unit_dir.mkdir(parents=True, exist_ok=True)
ssh = '/usr/bin/ssh -F ' + str(Path.home() / '.ssh/config')
tunnel = ('[Unit]\nDescription=Encrypted transport to central agent messaging\n\n[Service]\nType=simple\nExecStart='
          + ssh + ' -o BatchMode=yes -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3'
          + ' -NT -L 127.0.0.1:' + str(args.port) + ':127.0.0.1:8769 ' + args.host
          + '\nRestart=always\nRestartSec=5\n\n[Install]\nWantedBy=default.target\n')
(unit_dir / 'agent-messaging-tunnel.service').write_text(tunnel)
worker = ('[Unit]\nDescription=Deliver central mail to desktop agents\nAfter=agent-messaging-tunnel.service\n'
          'Requires=agent-messaging-tunnel.service\n\n[Service]\nType=simple\nExecStart=' + str(source / '.venv/bin/python')
          + ' -m agent_messaging.delivery --config ' + str(root / 'desktop-delivery.json')
          + '\nRestart=on-failure\nRestartSec=5\nUMask=0077\nMemoryMax=128M\n\n[Install]\nWantedBy=default.target\n')
(unit_dir / 'agent-messaging-delivery.service').write_text(worker)
bin_dir = Path.home() / '.local/bin'
bin_dir.mkdir(parents=True, exist_ok=True)
for name, command in [('agent-mail', str(source / '.venv/bin/agent-mail')), ('agent-mail-watch', str(source / '.venv/bin/agent-mail') + ' watch')]:
    target = bin_dir / name
    target.write_text('#!/bin/sh\nexec ' + command + ' "$@"\n')
    target.chmod(0o755)
subprocess.run(['systemctl', '--user', 'daemon-reload'], check=True)
subprocess.run(['systemctl', '--user', 'enable', '--now', 'agent-messaging-tunnel.service', 'agent-messaging-delivery.service'], check=True)
print('Installed desktop client, private project configurations, SSH tunnel and delivery worker.')
