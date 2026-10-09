"""`operator-stack`: the one driver for the stacks no CI job runs (framework/pulumi.md §3.3).

An operator stack (`conventions.identity.OPERATOR_STACKS`) runs only through
this command, which sets the stack's backend and its passphrase on the process
it starts (`kluster.lib.stack_environment`) and never takes a stack from its
caller. `driver` is what a run does; `checkpoint` is what a run of a stack
whose state is committed is checked for, before it starts and before its
checkpoint can be pushed; `progress` says what a run `pulumi` does not show
is still waiting on; `cli` is the command line.
"""
