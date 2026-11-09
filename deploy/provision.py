"""Install private service configuration on the database host as root."""
import grp
import hashlib
import json
import os
from pathlib import Path
import pwd
import secrets
import subprocess

os.umask(0o077)
root = Path('/etc/agent-messaging')
root.mkdir(mode=0o755, exist_ok=True)
root.chmod(0o755)
server = root / 'server.json'
if server.exists():
    raise SystemExit('Service configuration already exists; retain it and deploy code separately')
try: account = pwd.getpwnam('agent-messaging')
except KeyError:
    subprocess.run(['useradd', '--system', '--home-dir', '/var/lib/agent-messaging', '--shell', '/sbin/nologin', 'agent-messaging'], check=True)
    account = pwd.getpwnam('agent-messaging')
password = secrets.token_urlsafe(36)
token = secrets.token_urlsafe(36)
# Password contains only URL-safe characters and is sent over stdin, never argv.
sql = ("CREATE DATABASE agent_messaging CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;\n"
       "CREATE USER 'agent_messaging'@'localhost' IDENTIFIED BY '" + password + "';\n"
       "GRANT SELECT,INSERT,UPDATE ON agent_messaging.* TO 'agent_messaging'@'localhost';\n")
subprocess.run(['mariadb', '--batch'], input=sql, text=True, check=True, stdout=subprocess.DEVNULL)
config = {'bind': '127.0.0.1', 'port': 8769, 'adminTokenHash': hashlib.sha256(token.encode()).hexdigest(),
          'database': {'unix_socket': '/var/lib/mysql/mysql.sock', 'user': 'agent_messaging',
                       'password': password, 'database': 'agent_messaging'}}
server.write_text(json.dumps(config, indent=2) + '\n')
os.chown(server, account.pw_uid, account.pw_gid)
server.chmod(0o600)
admin = root / 'admin.json'
admin.write_text(json.dumps({'url': 'http://127.0.0.1:8769', 'token': token, 'project': 'global'}, indent=2) + '\n')
admin.chmod(0o600)
# Initialize only this new schema with the local administrator socket account.
from agent_messaging.server import connection
bootstrap = dict(config, database={'unix_socket': '/var/lib/mysql/mysql.sock', 'user': 'root', 'database': 'agent_messaging'})
with connection(bootstrap) as db:
    with db.cursor() as cursor:
        for statement in Path('/opt/agent-messaging/agent_messaging/schema.sql').read_text().split(';'):
            if statement.strip(): cursor.execute(statement)
print('Created dedicated database, restricted database account, schema, and private service configuration.')
