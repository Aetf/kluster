"""Every place the gate's test run is written carries one form and one outer bound.

The gate runs the whole suite under an outer `timeout`, a hang guard an order
of magnitude above the run (docs/framework/testing.md §1). The number is
written wherever the command is, and a copy that drifts low is one no run
notices until the suite outgrows it and a run is killed with no summary.

**Where a launch is looked for** differs by what reads the text:

-   **What CI executes** is found by definition: every line of every `run`
    in a workflow or a local action, and every string under a step's `with`
    (an action that takes a command runs it). A launch there is `pytest` or
    `py.test` as a word, a path ending in one, or `-m pytest` -- the
    spellings `EXECUTED_LAUNCH` knows.
-   **The prose** is the set testing.md §1 names, `PROSE`, each document read
    whole; another document points at one of those rather than writing its
    own. Its code is every fenced line outside a Python block, every indented
    code line, and every inline code span, and a launch there is `run
    [options] [--] pytest`: prose also names the `pytest` script and the
    `.venv/bin/pytest` path without running them.

**The form** every launch is held to is `[VAR=value …] timeout <N> mise x [uv]
-- uv run pytest [arguments]`: timed, and through the pinned toolchain.
**Part of the suite** is a launch whose positional argument names a path under
`tests/`, a `.py` file or a node id, a drill's `tests/live` (testing.md §5)
among them, and it carries its own `<N>`. Every other launch is **the gate**,
and every gate launch carries one `<N>`.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Iterator
from pathlib import Path
from typing import NamedTuple, cast

import fences
import pytest
import yaml

ROOT = Path(__file__).parent.parent
GITHUB = ROOT / '.github'

#: The documents that write the gate's command out (testing.md §1).
PROSE = ('AGENTS.md', 'README.md', 'docs/framework/testing.md')

EXECUTED_LAUNCH = re.compile(r'(?:^|[\s;&|(\'"`])(?:\S*/)?py\.?test(?=[\s;&|)\'"`]|$)|-m\s*py\.?test\b')
PROSE_LAUNCH = re.compile(r'\brun(?:\s+-\S+)*(?:\s+--)?\s+pytest\b')
FORM = re.compile(r'(?:[A-Z_][A-Z0-9_]*=\S* )*timeout (\d+) mise x (?:uv )?-- uv run pytest(?: \S+)*')
#: Options whose value is the next word, which is therefore not a test path.
TAKES_A_VALUE = {'-k', '-m', '-p', '-c', '-o', '-n', '--ignore', '--deselect', '--rootdir', '--confcutdir'}
PART_OF_THE_SUITE = re.compile(r'tests/.|.*\.py\b|.*::')
#: A double-backtick span may quote a single backtick (CommonMark §6.1).
SPAN = re.compile(r'(?<!`)(`+)(?!`)(.+?)(?<!`)\1(?!`)', re.DOTALL)


class Launch(NamedTuple):
    place: str
    text: str
    #: The outer bound, or `None` for a launch off the form.
    bound: str | None
    gate: bool


def _part_of_the_suite(arguments: str) -> bool:
    """Whether a positional argument, up to the command's end, selects tests."""
    try:
        words = iter(shlex.split(arguments, comments=True))
    except ValueError:  # a quote opened before the launch closes after it
        words = iter(arguments.split())
    for word in words:
        if any(operator in word for operator in '|;&<>'):
            break
        if word in TAKES_A_VALUE:
            next(words, None)
        elif not word.startswith('-') and PART_OF_THE_SUITE.match(word):
            return True
    return False


def classify(place: str, text: str, *, executed: bool) -> Launch | None:
    """The launch `text` is, or `None` when it launches nothing."""
    launch = (EXECUTED_LAUNCH if executed else PROSE_LAUNCH).search(text)
    if launch is None:
        return None
    form = FORM.fullmatch(text)
    return Launch(place, text, form.group(1) if form else None, not _part_of_the_suite(text[launch.end() :]))


def _fold(text: str) -> str:
    return ' '.join(text.split())


def _step_strings(node: object, *, under_with: bool = False) -> Iterator[str]:
    if isinstance(node, dict):
        for key, value in cast('dict[object, object]', node).items():
            if key == 'run' and isinstance(value, str):
                yield value
            else:
                yield from _step_strings(value, under_with=under_with or key == 'with')
    elif isinstance(node, list):
        for item in cast('list[object]', node):
            yield from _step_strings(item, under_with=under_with)
    elif under_with and isinstance(node, str):
        yield node


def _scripts(document: object) -> Iterator[str]:
    """Every command line a parsed workflow or action runs, continuations joined."""
    for script in _step_strings(document):
        for line in script.replace('\\\n', ' ').splitlines():
            yield _fold(line)


def _workflows_and_actions() -> list[Path]:
    patterns = ('workflows/*.yml', 'workflows/*.yaml', 'actions/*/action.yml', 'actions/*/action.yaml')
    return sorted(path for pattern in patterns for path in GITHUB.glob(pattern))


def _executed() -> Iterator[Launch]:
    for path in _workflows_and_actions():
        for line in _scripts(yaml.safe_load(path.read_text())):
            if launch := classify(str(path.relative_to(ROOT)), line, executed=True):
                yield launch


def _prose_code(text: str) -> Iterator[str]:
    """Every fenced line but Python's, every indented code line, every span.

    Spans pair one paragraph at a time, the way CommonMark pairs them. An
    indented line is code only where it cannot continue a paragraph: after a
    break, and with no backtick in it.
    """
    paragraph: list[str] = []
    after_break = True
    for line in fences.lines(text):
        code = line.fence is None and after_break and line.text.startswith('    ') and '`' not in line.text
        if line.fence is None and not code and line.text.strip():
            paragraph.append(line.text)
            after_break = False
            continue
        yield from (span for _, span in SPAN.findall('\n'.join(paragraph)))
        paragraph, after_break = [], True
        if code or (line.fence is not None and line.fence.info != 'python'):
            # A shell comment is not part of the command, and a fence
            # quoting a workflow step carries the step's own key.
            yield re.sub(r'^\s*(?:-\s*)?run:\s*', '', re.sub(r'\s#.*$', '', line.text))
    yield from (span for _, span in SPAN.findall('\n'.join(paragraph)))


def _prose() -> Iterator[Launch]:
    for name in PROSE:
        for code in _prose_code((ROOT / name).read_text()):
            if launch := classify(name, _fold(code), executed=False):
                yield launch


def every_launch() -> list[Launch]:
    return [*_executed(), *_prose()]


@pytest.mark.parametrize(
    ('text', 'executed', 'bound', 'gate'),
    [
        ('timeout 1200 mise x -- uv run pytest -q', True, '1200', True),
        ('timeout 1200 mise x -- uv run pytest -q -p no:cacheprovider -n auto', True, '1200', True),
        # Every spelling a shell runs the suite by is a launch, and each one
        # off the form is refused as such.
        ('mise x -- pytest -q', True, None, True),
        ('timeout 300 mise x -- uv run --frozen pytest -q', True, None, True),
        ('timeout 300 mise x -- uv run -- pytest -q', True, None, True),
        ('timeout 300 .venv/bin/pytest -q', True, None, True),
        ('. .venv/bin/activate && timeout 300 pytest -q', True, None, True),
        ('timeout 300 pytest -q', True, None, True),
        ('python -mpytest -q', True, None, True),
        ('python -m pytest -q', True, None, True),
        ('mise x -- uv run py.test -q', True, None, True),
        ("bash -c 'pytest -q'", True, None, True),
        # An argument that is an option's value, that names the whole suite,
        # or that is not a test path at all leaves the launch the gate, and so
        # does anything after the command ends.
        ('timeout 300 mise x -- uv run pytest tests -q', True, '300', True),
        ('timeout 300 mise x -- uv run pytest tests/ -q', True, '300', True),
        ('timeout 300 mise x -- uv run pytest . -q', True, '300', True),
        ('timeout 300 mise x -- uv run pytest -q --ignore tests/live', True, '300', True),
        ('timeout 300 mise x -- uv run pytest --deselect tests/test_b2.py::test_one', True, '300', True),
        ('timeout 300 mise x -- uv run pytest -p tests.plugin', True, '300', True),
        ('timeout 300 mise x -- uv run pytest -q --durations 10', True, '300', True),
        ('timeout 300 mise x -- uv run pytest -q -k "not live"', True, '300', True),
        ('timeout 300 mise x -- uv run pytest -q 2>&1 | tee pytest.log', True, '300', True),
        ('timeout 300 mise x -- uv run pytest -q && echo ok', True, '300', True),
        # Part of the suite is not the gate, and is held to the form all the same.
        ('RUN_LIVE_DRILLS=1 timeout 600 mise x uv -- uv run pytest tests/live -s', True, '600', False),
        ('uv run pytest tests/live', True, None, False),
        ('timeout 60 mise x -- uv run pytest test_b2.py', True, '60', False),
        ('timeout 60 mise x -- uv run pytest -q test_b2::test_one', True, '60', False),
        # Prose launches only through `run`; it names the script and its path
        # without running them.
        ('timeout 1200 mise x uv -- uv run pytest', False, '1200', True),
        ('uv run pytest', False, None, True),
        ('mise x uv -- uv run --frozen -- pytest', False, None, True),
    ],
)
def test_a_launch_is_classified_by_its_spelling(text: str, executed: bool, bound: str | None, gate: bool) -> None:
    assert classify('here', text, executed=executed) == Launch('here', text, bound, gate)


@pytest.mark.parametrize(
    ('text', 'executed'),
    [
        ('uv pip install pytest-timeout', True),
        ('.venv/bin/pytest', False),
        ('the `pytest` script', False),
    ],
)
def test_a_mention_is_not_a_launch(text: str, executed: bool) -> None:
    assert classify('here', text, executed=executed) is None


STEPS = """\
defaults:
  run:
    shell: bash
jobs:
  t:
    steps:
      - run: |
          echo one
          timeout 1200 mise x -- uv run \\
            pytest -q
      - uses: some/retry@v1
        with:
          command: timeout 300 mise x -- uv run pytest -q
"""


def test_a_workflow_runs_its_run_lines_and_its_with_strings() -> None:
    assert list(_scripts(yaml.safe_load(STEPS))) == [
        'echo one',
        'timeout 1200 mise x -- uv run pytest -q',
        'timeout 300 mise x -- uv run pytest -q',
    ]


def test_the_local_actions_are_read_beside_the_workflows() -> None:
    assert any(path.parent.parent == GITHUB / 'actions' for path in _workflows_and_actions())


QUOTING = """\
Prose with `a span`.

    indented code

````markdown
```bash
inside a quoted fence
```
still quoted
````

~~~yaml
- run: a quoted step
~~~

```python
python is not a command
```

-   A list item.

    ```bash
    a fence in a list item
    ```

  ```sh
  a fence indented two spaces
  ```

``Trust them with `mise trust`.`` and then
`a span after a double-backtick one`.
"""


def test_the_prose_code_is_every_code_line_but_pythons() -> None:
    assert sorted(_fold(code) for code in _prose_code(QUOTING)) == sorted(
        [
            'a span',
            'indented code',
            '```bash',
            'inside a quoted fence',
            '```',
            'still quoted',
            'a quoted step',
            'a fence in a list item',
            'a fence indented two spaces',
            'Trust them with `mise trust`.',
            'a span after a double-backtick one',
        ]
    )


def test_the_scan_finds_the_places_testing_md_names() -> None:
    """The positive control: the form test alone would pass on a scan that finds nothing."""
    places = {launch.place for launch in every_launch() if launch.gate}
    named = {'.github/workflows/checks.yml', '.github/workflows/sdk-regenerate.yml', *PROSE}

    assert named <= places, f'the scan no longer finds {sorted(named - places)}'


def test_every_launch_is_written_in_the_form() -> None:
    off_form = [(launch.place, launch.text) for launch in every_launch() if launch.bound is None]

    assert off_form == [], (
        'a launch of pytest runs as `timeout <N> mise x [uv] -- uv run pytest [arguments]`; '
        'prose that names the command without writing it out is reworded rather than timed'
    )


def test_every_gate_launch_carries_one_outer_bound() -> None:
    bounds: dict[str, list[str]] = {}
    for launch in every_launch():
        if launch.gate and launch.bound is not None:
            bounds.setdefault(launch.bound, []).append(launch.place)

    assert len(bounds) == 1, f'the outer bound differs between places: {bounds}'
