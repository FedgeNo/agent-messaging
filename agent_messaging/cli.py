import argparse
import base64
import json
from pathlib import Path
import sys
import time
import uuid

from .client import Client


def parser():
    result = argparse.ArgumentParser(description='Private durable agent mail; messages do not grant user authority.')
    result.add_argument('--config')
    result.add_argument('--project')
    commands = result.add_subparsers(dest='command', required=True)
    register = commands.add_parser('register')
    register.add_argument('--agent', required=True)
    register.add_argument('--name', required=True)
    register.add_argument('--session', required=True)
    register.add_argument('--host', default='')
    register.add_argument('--runtime', default='mailbox', help='mailbox, scheduled, or the name of a delivery connector such as codex')
    register.add_argument('--runtime-session', dest='runtimeSession')
    register.add_argument('--coordinator', action='store_true')
    register.add_argument('--replace-coordinator', action='store_true', dest='replace')
    commands.add_parser('info')
    agents = commands.add_parser('agents')
    agents.add_argument('--all', action='store_true', dest='includeRetired')
    retire = commands.add_parser('retire')
    retire.add_argument('--agent', required=True)
    retire.add_argument('--reason', required=True)
    restore = commands.add_parser('restore-agent')
    restore.add_argument('--agent', required=True)
    commands.add_parser('revision')
    actions = commands.add_parser('actions')
    actions.add_argument('--after', type=int, default=0)
    actions.add_argument('--limit', type=int, default=50)
    actions.add_argument('--all', action='store_true', dest='includeResolved')
    action = commands.add_parser('action-put')
    action.add_argument('--agent', required=True)
    action.add_argument('--key', required=True)
    action.add_argument('--title', required=True)
    action.add_argument('--body-file', required=True)
    action.add_argument('--source', required=True)
    action.add_argument('--status', choices=['needs-input','waiting','answered','resolved'], default='needs-input')
    action.add_argument('--resolution', default='')
    action.add_argument('--expected-version', dest='expectedVersion', type=int)
    delete = commands.add_parser('action-delete')
    delete.add_argument('--agent')
    delete.add_argument('--item-id', dest='itemId', type=int, required=True)
    delete.add_argument('--expected-version', dest='expectedVersion', type=int, required=True)
    remove = commands.add_parser('delete-message', help='Coordinator only: permanently delete one message')
    remove.add_argument('message', metavar='MESSAGE_ID')
    remove.add_argument('--reason', required=True)
    reply = commands.add_parser('action-reply')
    reply.add_argument('--item-id', dest='itemId', type=int, required=True)
    reply.add_argument('--body-file', required=True)
    reply.add_argument('--idempotency-key', dest='idempotencyKey', required=True)
    reply.add_argument('--expected-version', dest='expectedVersion', type=int, required=True)
    directory = commands.add_parser('directory')
    directory.add_argument('--after', default='')
    directory.add_argument('--limit', type=int, default=1000)
    directory.add_argument('--all', action='store_true', dest='includeRetired')
    guide = commands.add_parser('guide')
    guide.add_argument('path', nargs='?')
    save_guide = commands.add_parser('save-guide')
    save_guide.add_argument('--path', required=True)
    save_guide.add_argument('--body-file', required=True)
    status = commands.add_parser('status')
    status.add_argument('--agent', required=True)
    status.add_argument('--state', required=True, choices=['active', 'idle', 'done', 'blocked'])
    status.add_argument('--task')
    status.add_argument('--note')
    send = commands.add_parser('send')
    send.add_argument('--agent', required=True)
    send.add_argument('--to', default='principal', help='Agent/alias, or PROJECT/AGENT for cross-project mail')
    send.add_argument('--subject', required=True)
    send.add_argument('--body-file', required=True)
    send.add_argument('--kind', default='message')
    send.add_argument('--task', default='')
    send.add_argument('--in-reply-to', dest='replyTo')
    send.add_argument('--idempotency-key', dest='idempotencyKey', help='Reuse this key when retrying an uncertain send')
    send.add_argument('--no-notify', action='store_false', dest='notify')
    for name in ('inbox', 'watch', 'wait'):
        inbox = commands.add_parser(name)
        inbox.add_argument('--agent', required=True)
        inbox.add_argument('--since')
        inbox.add_argument('--limit', type=int, default=50)
        inbox.add_argument('--after', type=int, default=0)
        inbox.add_argument('--before', type=int)
        inbox.add_argument('--newest-first', action='store_true', dest='newestFirst')
        inbox.add_argument('--all', action='store_true', dest='includeAcknowledged')
        if name != 'inbox':
            inbox.add_argument('--timeout', type=float, default=None if name == 'watch' else 30)
            inbox.add_argument('--follow', action='store_true')
    for name in ('read', 'receipts'):
        read = commands.add_parser(name)
        read.add_argument('message')
        read.add_argument('--agent')
        if name == 'read': read.add_argument('--json', action='store_true', dest='jsonResponse')
    ack = commands.add_parser('ack')
    ack.add_argument('--agent', required=True)
    ack.add_argument('message')
    ack.add_argument('--note', default='')
    project = commands.add_parser('project')
    project.add_argument('--name', required=True)
    alias = commands.add_parser('alias')
    alias.add_argument('--alias', required=True)
    alias.add_argument('--agent', required=True)
    alias.add_argument('--replace', action='store_true')
    retry = commands.add_parser('retry')
    retry.add_argument('--delivery-id', dest='deliveryId', type=int, required=True)
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        client = Client(args.config)
        params = vars(args).copy()
        command = params.pop('command'); params.pop('config')
        json_response = params.pop('jsonResponse', False)
        if params['project'] is None: params.pop('project')
        if command == 'send':
            file = params.pop('body_file')
            params['body'] = sys.stdin.read() if file == '-' else Path(file).read_text()
            # Print the key on failure so a retry can reuse the original identity.
            params['idempotencyKey'] = params['idempotencyKey'] or uuid.uuid4().hex
        if command == 'save-guide':
            file = params.pop('body_file')
            content = sys.stdin.buffer.read() if file == '-' else Path(file).read_bytes()
            params['contentBase64'] = base64.b64encode(content).decode()
        if command in ('action-put', 'action-reply'):
            file = params.pop('body_file')
            params['description' if command == 'action-put' else 'body'] = sys.stdin.read() if file == '-' else Path(file).read_text()
        if command == 'delete-message':
            params['messageId'] = params.pop('message')
        if command in ('read', 'receipts'):
            params['agent'] = params['agent'] or client.config.get('agent')
        if command in ('watch', 'wait'):
            timeout = params.pop('timeout'); follow = params.pop('follow')
            if timeout is not None and timeout < 0: raise ValueError('Timeout must be nonnegative')
            end = None if follow or timeout is None else time.monotonic() + timeout
            while True:
                remaining = 55 if end is None else min(55, max(0, end - time.monotonic()))
                response = client.call('watch', **params, timeout=remaining)
                if response['messages'] or (end is not None and time.monotonic() >= end): break
            print(json.dumps(response, ensure_ascii=False, indent=2))
            return 0 if response['messages'] else 3
        response = client.call(command, **params)
        if command == 'read' and not json_response:
            sys.stdout.write(response['rawText'])
        elif command == 'guide' and args.path:
            sys.stdout.buffer.write(base64.b64decode(response['contentBase64']))
        else:
            print(json.dumps(response, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, RuntimeError) as error:
        print('agent-mail: ' + str(error), file=sys.stderr)
        if args.command == 'send' and 'params' in locals():
            print('Retry with --idempotency-key ' + params['idempotencyKey'], file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
