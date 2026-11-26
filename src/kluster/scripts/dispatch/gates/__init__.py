"""The model gates of `dispatch gates`: one rule each, asked of a change as a reviewer reads it.

A gate is a directory of this package holding `prompt.md` (docs/framework/dispatch.md §3):

-   its `__init__.py` declares `GATE`, a `gate.Spec`: the canonical section
    the prompt quotes, read from the tree under review at run time and never
    stored here; the path patterns that make the gate run and pick what it
    is shown; the extractor that draws the item a review's tag of the gate
    is recorded as (`harvest`); and the exact model id;
-   `prompt.md` holds the question, and the `{canon}` placeholder the
    section is put in;
-   `calibration.json`, where it exists, is the measurement `calibrate` wrote
    (`calibration`), and `cases.jsonl` the labelled set it measured
    against (`cases`).

The set of gates is every such directory (`gate.discover`), so a gate is
added by adding its directory. What a gate's model says can only add a
finding: it approves nothing, edits nothing, and every deterministic check
of `prcheck` runs whatever it says.

The modules, in the order a run reaches them: `extract` reads the change,
`view` renders what of it each gate is shown, `ask` cuts that into calls,
asks each three times through `model` and matches the samples' findings
into places, `calibration` says whether the gate's verdict blocks,
`comment` renders the one pull-request comment that carries the findings
and the evidence behind them, `run` puts those together for
`dispatch gates <pr>`, and `appeal` and `harvest` are the two ways a
disagreement with a gate is recorded.
"""
