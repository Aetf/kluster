"""`dispatch`: the dispatch protocol of docs/framework/dispatch.md, as refusals.

Each subcommand performs one step of the ledger's life, from filing an issue
to merging its pull request and ending the dispatch, and refuses, naming every
precondition that failed, before it changes anything on the forge; `cli`
names the steps. The forge's API is reached only through `forge.Gh`, which a
module whose step needs more of it extends: today `hooks.HookGh`,
`prcheck.PullGh`, `merge.MergeGh` and `gates.forge.GateGh`. Its git remote is
reached by `jj`'s fetch and push in `merge`. The model gates of `gates/` reach
the Messages API, and nothing else here does.
"""
