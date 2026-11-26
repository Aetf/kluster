"""`change-detector`: is this assertion a change detector, as docs/style/testing.md defines one?

The rule is docs/style/testing.md whole: what a test may assert ("Tests
verify behavior") and the literal on an expected side held still only
against a second source, which is the same rule asked of one literal. A
census restated in a test (style/pulumi.md, "A census is the source of
truth, and no test restates it") is that rule's case of a table copied
from the implementation, and is judged by it.

The gate runs on a change that touches a test module, and is shown each
one the change writes. Its extractor (`assertions`), which draws the items
`harvest` records a review's tags as, draws the assertions a change writes
there.
"""

from __future__ import annotations

from ..gate import Canon, Spec
from .assertions import TESTS, assertions

GATE = Spec(canon=Canon('docs/style/testing.md', 'Testing Style'), trigger=(TESTS,), extract=assertions)
