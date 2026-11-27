Ask one question of what the change writes, in code or in prose: does it
write into the program an answer a runtime mechanism derives, where the
program should declare the fact the mechanism derives it from, or does it
write a constraint that is not built from the declaration implying it?

The answer is where a pod runs, or what places it there, and a passage
can write it without naming a node, a site or a pool. Judge a passage by
what it does, whatever its words. It breaks the rule where it takes one
of these shapes:

- It writes again, beside a declaration, a value the mechanism already
  reads from that declaration: a requirement on where a pod runs, or a
  record of the nodes that offer what it requests, that the pod's own
  requests or mounts already lead the scheduler to.
- It places a pod that works on a workload's behalf, such as a mover
  that backs up, restores or copies the workload's volume, by a copy of
  the requirements the workload states, rather than by the workload that
  uses the volume. Every passage of that design is a finding: the code
  that builds the copy or hands it on, the field or record that keeps
  it, a check that compares the kept copy with what it was copied from
  and refuses where they differ, and the docstring or comment that
  states it as the design.
- It writes, as a value of the program, something concluded from how
  things stand today rather than declared on each thing it is a fact of,
  so that it agrees only until that fact moves.
- It states where something runs as a bare position, in a title, a
  headline or a description of the component, where the facts that
  lead there are what the program declares.
- It writes a constraint explicitly that is not built from the
  declaration implying it, or that does not say which fact it stands
  for.

A copy, in the rule's sense, is placement handed to one pod taken from
another's requirements. A passage holds to the rule where it does none
of the above, and in particular:

- where a workload's own pod states the constraints its component
  declares, with the holds of what it mounts or requests composed in,
  and where a field or parameter carries what that pod states: that is
  the declaration, not a copy;
- where it says how a mechanism binds a volume or places a pod, which
  pod's placement a binding follows included, without stating the answer
  the mechanism reaches;
- where it refuses a combination of declarations that the mechanism
  itself, by how it binds or places, would leave no node for: that
  refusal holds where nothing is copied, and checks no copy;
- where it gives a placement together with the fact it follows from and
  the declaration that carries it;
- where it writes a constraint for a fact no mechanism reads, built from
  that fact and naming it.

The reason names the answer the passage writes and the declaration or
workload it should come from instead.

The rule, as this repository states it:

{canon}
