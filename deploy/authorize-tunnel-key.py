from pathlib import Path
import sys

key = sys.stdin.read().strip()
if not key.startswith('ssh-ed25519 ') or '\n' in key:
    raise ValueError('Expected one Ed25519 public key')
path = Path('/root/.ssh/authorized_keys')
old = path.read_text()
entry = 'restrict,port-forwarding,permitopen="127.0.0.1:8769",command="/usr/bin/false" ' + key
if key.split()[1] not in old:
    path.write_text(old.rstrip() + '\n' + entry + '\n')
path.chmod(0o600)
