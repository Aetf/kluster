"""`gate: conclusion`: does this write an answer a runtime mechanism derives, or a constraint not built from its declaration?

The canon is style/pulumi.md's "The program declares constraints, not
conclusions". The gate is shown each file the change writes in the
components, the Kubernetes library and the conventions (`CODE`), where the
program declares a workload and where it runs, and the brief of an issue in
`area/cluster` (`BRIEFS`). What the model is asked is whether a passage the
change writes, in code or in prose, writes an answer the scheduler or a
volume binding derives, where the program should declare the fact it is
derived from, or writes a constraint that is not built from the
declaration implying it. The prompt names the shapes such a passage takes
and the ones that hold to the rule, so the model judges a passage by what
it does rather than by the words it uses.
"""

from __future__ import annotations

from ..gate import Canon, Spec

#: Where the program declares a workload and where it runs.
CODE = ('src/kluster/components/**', 'src/kluster/lib/k8s*', 'src/kluster/conventions/**')
#: The briefs of an issue in `area/cluster`, wherever that area falls among the issue's.
BRIEFS = (':brief/cluster/**', ':brief/**/cluster/**')

GATE = Spec(
    canon=Canon('docs/style/pulumi.md', 'The program declares constraints, not conclusions'),
    trigger=(*CODE, *BRIEFS),
)
