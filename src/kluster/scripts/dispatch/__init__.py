"""`dispatch`: the dispatch protocol of docs/framework/dispatch.md, as refusals.

Each subcommand performs one step of the ledger's life — filing an issue,
claiming it for a dispatch, queueing it for a cloud session, abandoning a
dispatch, auditing the ledger — and refuses, naming every precondition that
failed, before it changes anything on the forge. `cli` is the command line;
`forge` is the one place the forge is reached.
"""
