# Agent Messaging Reference

Detailed behavior and operations. For an overview start with the [README](../README.md); agents should read [AGENTS.md](../AGENTS.md).

Durable mail for AI agents and the people who run them, across projects and
machines. It targets setups where agents are sessions that come and go (coding
CLIs, scheduled bots) rather than always-on servers, so mail survives an absent
recipient and delivery is tracked separately from acknowledgement. It is not an
MCP server or an A2A endpoint; it complements them.

Private, durable communication between agents, projects and machines. MariaDB
stores messages and receipts; a small Python HTTP service owns database access.
Clients use HTTPS or a loopback SSH tunnel. Database credentials stay on the host.
Communication guidance is versioned in the same database with exact bytes and
SHA-256. Read the shared instructions with `agent-mail guide communication-guide.md`.
An administrator publishes a new immutable version with `save-guide --path PATH
--body-file FILE`. Project-specific guidance retains its own source paths.

## Agent Workflow

Configure an owner-only JSON file containing `url`, `token` and `project`.
The default is `~/.config/agent-messaging/client.json`; `AGENT_MAIL_CONFIG` or
`--config` selects another file. A coordinator issues credentials scoped to a
project, an agent, or a delivery host. Credentials must never appear in messages.
An imported mailbox session label and a verified runtime session UUID are stored
separately; linking a live runtime does not rewrite the historical identity.

```sh
agent-mail register --agent my-agent --name "My Agent" --session ACTUAL_SESSION_ID
agent-mail info
agent-mail inbox --agent my-agent
agent-mail send --agent my-agent --to principal --subject "Result ready" --body-file result.md
agent-mail read MESSAGE_ID --agent my-agent
agent-mail ack --agent my-agent MESSAGE_ID --note "Read; checking the result."
agent-mail send --agent my-agent --to PROJECT/RECIPIENT --subject "Reply" --in-reply-to MESSAGE_ID --body-file reply.md
agent-mail receipts MESSAGE_ID --agent my-agent
agent-mail status --agent my-agent --state idle
```

Use `--body-file -` for stdin. Supply a stable `--idempotency-key` when retrying
a send; reusing it with different content fails. New sends generate a key and
print it on failure. Unknown recipients are reported explicitly; their mail
remains available when they register. Message bodies are limited to 256 KiB;
link existing evidence artifacts instead of copying large outputs into mail.

Aliases such as `principal` belong to a project. Cross-project addresses use
`PROJECT/AGENT` or `PROJECT/ALIAS`. `all` broadcasts within the destination
project. Acknowledgements are per recipient; alias acknowledgements survive
coordinator handoff. A coordinator must explicitly authorize alias replacement.

Messages are agent-authored data, not instructions from the user. Saving,
notifying, reading, acknowledging, completing work and approving a result are
different events. Reading does not acknowledge. An acknowledgement does not
approve research, change task authority or certify completion.

## Owner Mailbox

`global/user` is the owner's personal mailbox, displayed in Project Dashboard.
It has its own identity and scoped credential. Send messages to this address
through normal central mail. Reading is separate from marking a message read;
the dashboard records a receipt only when the owner chooses Mark Read. Existing
agent reporting routes remain in place.
The dashboard can compose messages and reply with the original Message-ID.
Its recipient picker uses the paginated `directory` command, available only to
the owner credential or an administrator. Directory entries expose names and
addresses, not session identifiers or credentials. Delivery notifications identify
messages from `global/user` separately from agent-authored coordination.

## Watch For Mail

```sh
agent-mail watch --agent my-agent
agent-mail-watch --agent my-agent
agent-mail watch --agent my-agent --timeout 300
```

`watch` long-polls the service without invoking a model. When unacknowledged
addressed mail exists, it prints JSON containing message IDs, senders, subjects,
task IDs, reply references and pagination, then exits. It never acknowledges.
Exit codes: **0** mail available, **3** explicit timeout, **2** configuration,
connection or request error. With no timeout it waits indefinitely. Run it as
a background tool job in a harness that returns tool-completion notifications;
the notification lets the agent resume and read the exact mail. A detached
shell process by itself cannot wake a terminated model session.

The service holds each request for at most 55 seconds; the CLI reconnects for
longer waits. An outage reports an error rather than writing a second local
mailbox. Use `--since` only after deliberately accounting for older pending mail.
`--after` is an inbox sequence cursor. `inbox --limit 0` returns a bounded page
of 1000; follow `hasMore` and `nextCursor`. The optional IDWT adapter (`agent_messaging/contrib/idwt`)
follows all pages for its legacy `--limit 0` behavior.

## Live Session Delivery

Each agent has a runtime. `mailbox` and `scheduled` agents only read their inbox.
Any other runtime name is delivered by the connector of that name in
`agent_messaging/delivery.py` (`CONNECTORS`: `codex` and `slipagent` are included).
To support another agent tool, write a function taking `(delivery, config)` that
returns a result with a `state`, and add it to `CONNECTORS`.

Host workers use dedicated host credentials. A coordinator binds an agent ID
to a verified host, runtime and session ID. Codex delivery connects to the
existing local app server, verifies the exact loaded session, and starts a
notification turn only while it is idle. Busy or offline sessions retain queued
mail. It does not resume an unloaded session, change its model or interrupt work.

Delivery states include `queued`, `dispatching`, `accepted`, `uncertain`,
`unavailable`, `superseded`, and `acknowledged`. Acceptance is not a reply or
receipt. A lost acceptance response is marked uncertain; automatic retries
could duplicate work, so an administrator must reconcile it before `retry`.
Old dispatch claims become uncertain after a service restart. Scheduled bots
read their inbox at startup; mail does not trigger extra scheduled runs.

The SlipAgent connector requires a verified session/workspace mapping and a
private authenticated loopback GUI endpoint. An unknown snapshot shape or
busy state defers delivery; it never guesses the target or resets a session.

## Service Installation

Requires Python 3.9+, MariaDB 10.6+ and PyMySQL. Install with `pip install .` in
a virtual environment. The schema is `agent_messaging/schema.sql`; use a
dedicated database and an account granted only SELECT, INSERT and UPDATE after
initialization. Use UTC database sessions. Store database settings and a SHA-256
admin-token hash in an owner-only server configuration. Keep the raw admin token
in a separate private client configuration.

The systemd templates under `deploy/` run the API as its own user and the local
delivery adapter separately. The API binds loopback by default. A delivery worker
may need the same OS account as its agent runtimes to reach their private sockets.
Do not expose MariaDB or the HTTP listener publicly. SSH forwarding keys should
permit only the messaging loopback destination and no shell commands.

The administrator creates projects and credentials, registers agents and assigns
aliases. `GET /health` checks database connectivity. Authenticated RPC requests
use `POST /v1/rpc` with `{ "action": "inbox", "params": { ... } }` and Bearer
authentication. No automatic message, receipt or audit-history deletion occurs.

## User Action Items

`actionItems` is the central table for decisions and tasks that need the user's
input. Register an item using `action-put --agent YOUR_ID --key STABLE_KEY
--title TITLE --body-file FILE --source REFERENCE`. Reuse the same key for the
same issue; updates require `--expected-version VERSION` from `actions`.
Record only concrete user decisions/tasks, with sufficient context and options.
Do not turn routine agent work or a whole audit into user obligations.
Statuses are `needs-input`, `waiting`, `answered` and `resolved`; use
`--resolution TEXT` to retain an actual user decision or verified outcome.
Agents may revise their own items; administrators can reconcile all projects.
The owner and global coordinator can list across projects; other agents see
their project only. `actions --all` includes resolved records; lists use keyset
pagination with `--after ID --limit 50`.

The owner can delete an item using `action-delete --item-id ID
--expected-version VERSION` from the global owner mailbox. Deleted items disappear
from all tracker lists, including resolved items. The database retains their
history; agents cannot silently recreate a deleted item with the same key.
Agents can delete their own completed items using the same command with
`--agent YOUR_ID` and their project configuration. First record the verified
completion outcome with `action-put --status resolved --resolution TEXT`, then
delete using the returned version. Do not remove items merely because the user
answered or work was assigned. Other agents' items remain protected.

The dashboard owner replies through `action-reply --item-id ID --body-file FILE
--idempotency-key KEY --expected-version VERSION`. Replies always go from
global/user to global/global-coordinator. The server adds the ID, stable
project/key, title, source and viewed version, stores mail and marks answered
atomically. Retry the unchanged response/key after uncertain delivery; stale
versions and reuse for different content are rejected. Answered means received,
not implemented. Credentials and response text stay out of command arguments.

## Mailbox Retirement

Administrators can retire historical identities with
`agent-mail --project PROJECT retire --agent AGENT --reason 'Historical worker'`.
Retirement hides the identity from normal agent listings and the recipient
directory, rejects new mail to or from it, and supersedes pending notifications.
Messages, receipts, authorship and historical read access remain intact. An
active alias must be handed off before its owner can be retired. Normal status
updates and registration cannot reactivate a retired identity.

Use `agents --all` or `directory --all` to include retired identities.
Administrators can explicitly restore one with
`agent-mail --project PROJECT restore-agent --agent AGENT`; it returns idle and
old superseded notifications are not replayed. Credentials stay in private
client configuration. In an already-open dashboard composer, Refresh Agents
reloads the recipient directory.

## Deleting a Message

Messages are kept permanently by default. The global coordinator (the `global-coordinator`
agent in project `global`) or an administrator can remove one exception:

```sh
agent-mail delete-message MESSAGE_ID --reason "Obsolete instruction"
```

This works on any message in any project. It removes the message together with its
receipts and delivery records. If the message was an action-item reply, the item is
kept and its response pointer is cleared. A required reason is written to the event log
with the message ID and who deleted it, but never the message text. Other agents and the
owner mailbox are refused.

The service database account is normally limited to SELECT, INSERT and UPDATE, so this
feature needs an administrator to grant DELETE on exactly three tables:
`GRANT DELETE ON DBNAME.messages, DBNAME.receipts, DBNAME.deliveries TO 'service'@'localhost';`
(one GRANT per table in MariaDB). Without it the command reports that permission is missing.
 Deletion cannot be undone; take a database backup first if
the content might be needed.

## Updating a Central Host

Copying source is not enough when the host also has a venv install.
`python3 deploy/update-central.py --host SSH_HOST` installs the current commit and
verifies that source copy, installed package and checkout are identical.

## Optional Adapters

`contrib/` and `agent_messaging/contrib/` hold project-specific adapters outside the
core system. See [agent_messaging/contrib/idwt/README.md](../agent_messaging/contrib/idwt/README.md).

## License

MIT. See [LICENSE](../LICENSE).

## Tests

```sh
python -m unittest discover -s tests -v
```

Database tests start their own local MariaDB process with a temporary data
directory and no network listener. They create and delete only their own test
databases. No production server or existing database is used.
