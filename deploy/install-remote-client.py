"""Install a client host with a forwarding-only SSH key and scoped API token."""
import argparse
import json
from pathlib import Path
import shlex
import subprocess

parser = argparse.ArgumentParser()
parser.add_argument('--host', required=True)
parser.add_argument('--project', required=True)
parser.add_argument('--central-host', required=True)
args = parser.parse_args()
ssh_config = str(Path.home() / '.ssh/config')


def ssh(host, command, input=None):
    return subprocess.check_output(['ssh', '-T', '-F', ssh_config, '-o', 'BatchMode=yes', host, command], input=input, text=True)


central_settings = dict(line.split(' ', 1) for line in subprocess.check_output(['ssh', '-G', '-F', ssh_config, args.central_host], text=True).splitlines())
if central_settings['port'] != '22': raise RuntimeError('Specify the central forwarding port in this installer before proceeding')
user = ssh(args.host, 'id -un').strip()
code = ('import json; from pathlib import Path; p=Path("/etc/agent-messaging"); '
        'print(json.dumps({"client":json.loads((p/' + repr(args.project + '-client.json') + ').read_text()),'
        '"delivery":json.loads((p/' + repr(args.project + '-delivery.json') + ').read_text())}))')
private = json.loads(ssh(args.central_host, 'python3 -c ' + shlex.quote(code)))
private.update(user=user, project=args.project, centralAddress=central_settings['hostname'],
               centralHostKey=ssh(args.central_host, 'cat /etc/ssh/ssh_host_ed25519_key.pub').strip())
source = Path(__file__).resolve().parents[1]
archive = subprocess.Popen(['tar', '-C', str(source), '--exclude=__pycache__', '-cf', '-', 'agent_messaging', 'bin'], stdout=subprocess.PIPE)
install = subprocess.run(['ssh', '-T', '-F', ssh_config, '-o', 'BatchMode=yes', args.host,
                          'sudo -n install -d -m 755 /opt/agent-messaging\ncd /opt/agent-messaging\nsudo -n tar --no-same-owner -xf -'], stdin=archive.stdout, check=True)
archive.stdout.close()
if archive.wait(): raise RuntimeError('Source transfer failed')
setup = (Path(__file__).with_name('configure-client-host.py')).read_text()
result = json.loads(ssh(args.host, 'sudo -n python3 -c ' + shlex.quote(setup), json.dumps(private)))
authorize = Path(__file__).with_name('authorize-tunnel-key.py').read_text()
ssh(args.central_host, 'python3 -c ' + shlex.quote(authorize), result['publicKey'])
print(ssh(args.host, 'sudo -n systemctl daemon-reload\nsudo -n systemctl enable --now agent-messaging-tunnel.service agent-messaging-delivery.service\nagent-mail info'))
