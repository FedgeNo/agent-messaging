# Agent Messaging

Durable mail for AI agents and the people who run them, across projects and machines.

If you run several coding agents (Claude Code, Codex, scheduled bots, other CLIs) on
more than one project, they need a way to hand work to each other, report results
and ask you questions. Chat transcripts are temporary and agent sessions come and
go. Agent Messaging gives each agent an address and an inbox backed by a database,
so a message sent to an agent that is busy, asleep or restarting is still there
when it returns, and every message records who saw it and who acknowledged it.

It is plain mail semantics for agents, not an orchestration framework. It does not
decide what agents do; it makes sure they can reliably tell each other and you.

## What it gives you

- **Addresses and inboxes.** Agents register under a project (`project/agent`),
  with aliases such as `principal` that can be handed to a different agent later.
- **Durable messages.** Messages are stored in MariaDB with exact text and a
  SHA-256 hash. Nothing is deleted automatically.
- **Separate delivery, read and acknowledgement.** Saving a message, notifying a
  session, reading it and acknowledging it are different events with their own
  receipts. An acknowledgement never means "approved" or "finished".
- **Waiting without a model.** `agent-mail watch` long-polls for mail without
  calling an LLM and exits when something arrives, so an agent harness can wake
  the agent only when there is work.
- **Live session delivery.** Optional connectors wake an idle Codex or SlipAgent
  session when mail arrives, and queue the notice if the session is busy or away.
  Other tools can be added by writing a small connector function.
- **An owner mailbox and action items.** A dedicated address for you, plus a
  tracker where agents record decisions that need your input. Your reply is
  mailed back with the item's ID and version so the right agent acts on it.
- **Safe retries.** Sends take idempotency keys, and updates use version checks,
  so retries after a crash or a lost response don't duplicate or overwrite.
- **Scoped credentials.** Tokens are issued per project, per agent or per delivery
  host. Database credentials never leave the server.
- **Retirement without loss.** Retired agents stop receiving mail, but their
  history and authorship remain readable.

## How it works

```
 agent / laptop ──ssh tunnel──▶  HTTP service  ──▶  MariaDB
 agent / server ──loopback────▶  (loopback only)     (messages, receipts, items)
                                       ▲
                        delivery worker (optional): wakes idle sessions
```

A small Python service owns all database access and listens on loopback. Clients
(`agent-mail`) reach it directly on the same host or through an SSH tunnel from
other machines. There is no message broker and no always-on agent process.

## Quick start

You need Python 3.9+ and MariaDB 10.6+.

```sh
pip install .                         # installs the agent-mail command
# Create a dedicated database, load agent_messaging/schema.sql, and write the
# server configuration. See docs/REFERENCE.md ("Service Installation").
```

Once a server is running and you have a client config (`url`, `token`, `project`
in `~/.config/agent-messaging/client.json`):

```sh
agent-mail register --agent builder --name "Builder" --session SESSION_ID
agent-mail send --agent builder --to principal --subject "Build done" --body-file report.md
agent-mail watch --agent builder          # blocks until mail arrives, then prints JSON
agent-mail read MESSAGE_ID --agent builder
agent-mail ack  MESSAGE_ID --agent builder --note "Read; starting the follow-up."
```

If you give an agent access to this, point it at [AGENTS.md](AGENTS.md).

## Good fit, and not

**Good fit:** a handful to a few dozen agents you control, across several projects
or hosts, where reliable handoff, an audit trail and a place for you to answer
questions matter more than speed.

**Not a fit:** high-volume messaging, anonymous or public agents, or agents that
need a live request/response protocol. It also is not an MCP server or an A2A
endpoint. Those protocols can sit in front of it; the mail system underneath is
what gives them durability.

## Security notes

Messages are agent-written data, not instructions from the user, and the CLI and
docs say so. Keep credentials out of message bodies. Bind the service to loopback
and reach it through SSH forwarding; do not expose MariaDB or the HTTP listener
publicly. Give each project its own token.

## Status

Used daily by the author to coordinate agents across several servers. The core
(mail, receipts, aliases, action items, retirement, watch, Codex and SlipAgent
delivery) is exercised by the tests; the deployment scripts reflect one real
setup and will need adapting. Project-specific adapters live under `contrib/`.
Expect rough edges and rapid changes.

## Documentation

- [AGENTS.md](AGENTS.md): how an agent should use the software
- [docs/REFERENCE.md](docs/REFERENCE.md): full behavior, installation and operations

## Tests

```sh
python -m unittest discover -s tests -v
```

The database tests start their own temporary MariaDB process and never touch an
existing database.

## License

MIT. See [LICENSE](LICENSE).
