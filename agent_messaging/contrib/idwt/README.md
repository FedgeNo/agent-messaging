# IDWT Adapter (optional)

Project-specific; not needed for general use. The old module names `agent_messaging.compat`, `import_idwt` and `verify_idwt` remain as aliases.

The cutover helper is `contrib/idwt/cutover.py`.


`python -m agent_messaging.contrib.idwt.importer --root CHECKOUT --config SERVER_CONFIG`
imports registrations, messages and receipts into the separate messaging
database. It preserves exact message text with SHA-256 and verifies readback.
Historical mail does not wake sessions. Duplicate legacy IDs are preserved with
distinct central IDs; original filenames remain lookup keys. Changed previously
imported messages stop the import rather than overwrite history. Legacy files
remain intact.

`agent_messaging.contrib.idwt.compat.CentralMailbox` preserves IDWT's CLI and workflow watcher
interface while routing reads and writes through the central API. Cutover must
include a final incremental import after switching writers. Verify the current
principal, historical acknowledgements and pending inbox before declaring the
old transport superseded. Research storage and scientific intake stay separate.

