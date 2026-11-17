"""`stack-plan --stack <name>`: whether an `up` of the stack would write anything.

It prints the stack's plan and exits by the answer (`cli`), in the
preview's own table of exits: 0 when an `up` would write nothing; 7, the
preview's refusal under `--expect-no-changes`, when it would write a step or
state with no step; anything else when a preview failed and there is no
answer.

A preview's own exit counts only steps, and an `up` that creates, updates,
replaces and deletes nothing still writes the state the program registers.
So the answer comes from at most two runs of the pinned CLI (`preview`): the
preview with `--expect-no-changes`, which settles every step, and after it
the second preview of `kluster.lib.state_only`, which reads every field an
`up` would write with no step.

**The 0 has one known gap**, the layer's and so the operator driver's too:
the secret whose value alone changed that `kluster.lib.state_only` names.

**This package stands in for a flag the CLI does not have.** Its interface
is the one that flag would have: print the plan, exit by whether an `up`
would write. It is part of the layer `kluster.lib.state_only` states, whose
docstring holds the layer's boundary, its removal condition and its
tripwire, for this command and the operator driver both.
"""
