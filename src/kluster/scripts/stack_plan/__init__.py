"""`stack-plan --stack <name>`: whether an `up` of the stack would write anything.

It prints the stack's plan and exits by the answer (`cli`), in the
preview's own table of exits: 0 when an `up` would write nothing; 7, the
preview's refusal under `--expect-no-changes`, when it would write a step or
state with no step; anything else when a preview failed and there is no
answer.

A preview's own exit counts only steps, and an `up` that creates, updates,
replaces and deletes nothing still writes the state the program registers:
a resource option moved on a `same` step -- `protect`, `retainOnDelete`,
`dependsOn`, an alias -- and an output a resource registers. So the answer
comes from at most two runs of the pinned CLI (`preview`): the preview with
`--expect-no-changes`, which settles every step, and after it a JSON
preview whose states and outputs `comparison` reads field by field.

**The 0 has one known gap**: a secret whose value changes while nothing
else in the program does. Both surfaces the command reads print a secret as
the same placeholder, so its old and new values compare equal. A secret
derived from a resource changes with that resource's step, which the preview
counts; one derived from configuration alone is the change a 0 does not
see.

**This package stands in for a flag the CLI does not have.** Its interface
is the one that flag would have: print the plan, exit by whether an `up`
would write. The layer is this package, `tests/test_stack_plan.py`,
`tests/test_stack_plan_engine.py`, the `stack-plan` entry in
`pyproject.toml`'s `[project.scripts]`, its name in `AGENTS.md`'s list of
console scripts, and, in framework/testing.md §8, the paragraph naming the
engine module and the module's entry in that section's census.

It is removed when the pinned CLI offers a preview whose exit status counts
state-only and output-only changes. Where that is `--expect-no-changes`
itself counting them (pulumi/pulumi#5087), a caller runs that preview in
this command's place and keeps reading the same exit. Where it is a detailed
exit code (pulumi/pulumi#25303), the new flag may bring statuses of its own,
and the caller's reading of the exit takes them in as it replaces the
command. The first route has a tripwire: `tests/test_stack_plan_engine.py`'s
`test_the_bare_preview_still_exits_0_on_a_change_no_step_carries` is red,
for each change it lists, once the pinned preview refuses that change, so
the pin bump that brings the fix fails on it. It cannot see the second
route: a new flag changes no exit that exists today, so that half of the
condition stands in this docstring alone, where a pin bump's reviewer reads
it.
"""
