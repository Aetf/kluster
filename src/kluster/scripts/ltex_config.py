"""`ltex-config`: the prose checker's client configuration, carrying the repository's two word lists whole.

    mise x uv -- uv run ltex-config <output>

`ltex-cli-plus` reads neither `.vscode/ltex.dictionary.en-US.txt` nor
`.vscode/ltex.disabledRules.en-US.txt` on its own, and a `":<path>"`
reference to one in its configuration is dropped in silence, so the words go
into the configuration whole (AGENTS.md, "Writing the prose"). This writes
`<output>` as that configuration, JSON with the dictionary under
`dictionary` and the disabled rules under `disabledRules`, each keyed by the
language. Each list is read from the checkout in the current directory, an
entry to each whitespace-separated word, in the file's order and with its
case kept: the dictionary is case-sensitive, so `homelab` and `Homelab` are
two entries and both go in.

`checks.yml`'s prose step runs it, and its machinery list names this module:
what it writes is what the checker reads, so a change to it checks every
markdown file, as a change to either word list does (framework/ci.md §3).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

#: The language both lists are for, which keys each in the configuration.
LANGUAGE = 'en-US'

#: The configuration's two entries, each named as the list file it carries.
ENTRIES = ('dictionary', 'disabledRules')


def word_list(checkout: Path, entry: str) -> Path:
    """The list file the configuration's `entry` carries, in `checkout`."""
    return checkout / '.vscode' / f'ltex.{entry}.{LANGUAGE}.txt'


def configuration(checkout: Path) -> dict[str, dict[str, list[str]]]:
    """The client configuration for `checkout`: each list's words, whole and in order."""
    return {entry: {LANGUAGE: word_list(checkout, entry).read_text().split()} for entry in ENTRIES}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='ltex-config',
        description="Write ltex-cli-plus's client configuration, carrying the checkout's two word lists whole.",
    )
    _ = parser.add_argument('output', type=Path, help='the file the configuration is written to')
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    output: Path = args.output
    _ = output.write_text(json.dumps(configuration(Path.cwd())))
    return 0


if __name__ == '__main__':
    sys.exit(main())
