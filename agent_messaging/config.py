import json
import os
from pathlib import Path
import stat


def read_config(path):
    path = Path(path).expanduser()
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077:
        raise ValueError('Configuration must be a regular owner-only file: ' + str(path))
    return json.loads(path.read_text())


def client_path():
    return os.environ.get('AGENT_MAIL_CONFIG', str(Path.home() / '.config/agent-messaging/client.json'))
