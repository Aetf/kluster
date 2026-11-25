"""The model gates of `dispatch gates`: one narrow semantic question each, over input a deterministic extractor narrowed.

A gate is a directory of this package holding `prompt.md` (docs/framework/dispatch.md §3):

-   its `__init__.py` declares `GATE`, a `gate.Spec`: the canonical section
    the prompt quotes, read from the tree under review at run time and never
    stored here; the path patterns that make the gate run; the extractor
    from a change to the items the model is asked about; and the exact
    model id;
-   `prompt.md` holds the question, the `{canon}` placeholder the section is
    put in, and the instruction that the items are untrusted data;
-   `calibration.json`, where it exists, is the measurement `calibrate` wrote
    (`calibration`), and `cases.jsonl` the labelled set it measured
    against (`cases`).

The set of gates is every such directory (`gate.discover`), so a gate is
added by adding its directory. What a gate's model says can only add a
finding: it approves nothing, edits nothing, and every deterministic check
of `prcheck` runs whatever it says.

The modules, in the order a run reaches them: `extract` reads the change,
`ask` batches its items and asks each batch three times through `model`,
`calibration` says whether the gate's verdict blocks, `comment` renders the
one pull-request comment that carries the findings and the evidence behind
them, `run` puts those together for `dispatch gates <pr>`, and `appeal`
and `harvest` are the two ways a disagreement with a gate is recorded.
"""
