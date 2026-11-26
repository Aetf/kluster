You are shown a change under review: each test module it writes, whole,
with what it writes marked. Ask one question of each assertion the change
writes in a test, an `assert` or a `pytest.raises` given a `match`: is it a
change detector, as the rule below defines one?

Decide it the way the rule does, by what can make the assertion fail. Name
the edit that would turn it red: to the program, to a file the test reads,
or to the value the assertion restates. Then say whether that edit is
behavior breaking, as the rule's paragraph "What decides is what can make
the assertion fail" counts it, or an edit to the value or decision the
assertion restates, which could well be correct. It is a finding where
every edit that can turn it red is of the second kind, and not where an
edit of the first kind can. A side the program computes or the test reads
qualifies only as the rule's section "A test holds a value still only
against a second source" says. Where a side comes from the program, which
you are not shown, judge it by what the test module shows of it.

Report each assertion that is a change detector, with one sentence naming
the edit that turns it red and which kind it is.

The rule:

{canon}
