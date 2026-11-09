"""Update the central server's installed package and prove it matches this checkout.

The central host keeps a source copy and a separate venv install. Copying source
alone leaves the venv's `agent-mail` stale, so this syncs both and verifies them.
Run from the repository: python3 deploy/update-central.py --host CENTRAL_SSH_HOST
"""
import argparse
import hashlib
from pathlib import Path
import subprocess
import tempfile

parser = argparse.ArgumentParser()
parser.add_argument('--host', required=True)
parser.add_argument('--root', default='/opt/agent-messaging', help='install root on the central host')
parser.add_argument('--restart', action='store_true', help='restart services after installing (not normally needed)')
args = parser.parse_args()
source = Path(__file__).resolve().parents[1]
config = ['-F', str(Path.home() / '.ssh/config'), '-o', 'BatchMode=yes']
root = args.root
venv = root + '/.venv'


def ssh(command, **kw):
    return subprocess.run(['ssh', '-T'] + config + [args.host, command], check=True, text=True, capture_output=True, **kw).stdout

site = ssh(venv + "/bin/python -c 'import sysconfig; print(sysconfig.get_paths()[\"purelib\"])'").strip() + '/agent_messaging'

if subprocess.run(['git', '-C', str(source), 'status', '--porcelain', '--', 'agent_messaging', 'pyproject.toml'],
                  capture_output=True, text=True, check=True).stdout.strip():
    raise SystemExit('Uncommitted package changes; commit first so the server matches a known revision.')
revision = subprocess.check_output(['git', '-C', str(source), 'rev-parse', '--short', 'HEAD'], text=True).strip()
with tempfile.TemporaryDirectory() as work:
    subprocess.run(['python3', '-m', 'pip', 'wheel', '--no-deps', '-q', '-w', work, str(source)], check=True)
    wheel = next(Path(work).glob('*.whl'))
    subprocess.run(['scp', '-q'] + config + [str(wheel), args.host + ':/root/' + wheel.name], check=True)
    # Source copy (what the services import) and installed copy (what agent-mail runs).
    archive = subprocess.Popen(['tar', '-C', str(source), '--exclude=__pycache__', '-cf', '-', 'agent_messaging', 'bin'], stdout=subprocess.PIPE)
    subprocess.run(['ssh', '-T'] + config + [args.host, 'cd ' + root + ' && tar --no-same-owner -xf -'], stdin=archive.stdout, check=True)
    archive.stdout.close()
    if archive.wait(): raise SystemExit('Source transfer failed')
    ssh('cp -a ' + site + ' /root/agent_messaging.installed.bak-$(date +%Y%m%d%H%M%S); '
        + venv + '/bin/pip install -q --no-deps --no-index --force-reinstall /root/' + wheel.name
        + '; rm -f /root/' + wheel.name)
if args.restart:
    ssh('systemctl restart agent-messaging agent-messaging-delivery')

# Verify: installed == source == this checkout, and services are up.
package = source / 'agent_messaging'
local = {str(p.relative_to(package)): hashlib.sha256(p.read_bytes()).hexdigest()
         for p in package.rglob('*') if p.suffix in ('.py', '.sql') and '__pycache__' not in p.parts}
for label, path in (('source', root + '/agent_messaging'), ('installed', site)):
    remote = dict((name[2:], digest) for digest, name in (line.split() for line in ssh('cd ' + path + " && find . -type f \\( -name '*.py' -o -name '*.sql' \\) -not -path '*/__pycache__/*' -exec sha256sum {} +").splitlines()))
    if remote != local:
        raise SystemExit(label + ' copy differs from checkout in: ' + ', '.join(sorted(k for k in set(local) | set(remote) if local.get(k) != remote.get(k))))
state = ssh('systemctl is-active agent-messaging agent-messaging-delivery').split()
if state != ['active', 'active']: raise SystemExit('Services not active: ' + ' '.join(state))
print('Central host ' + args.host + ' matches ' + revision + ': source and installed package identical, services active.')
