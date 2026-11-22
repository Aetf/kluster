"""`dispatch`: the command line.

    dispatch file --title <title> --body-file <file> --label <label>... --milestone <milestone>
    dispatch claim <issue> <brief> [--operator-word <quote>] [--adopt]
    dispatch queue <issue> <brief> [--operator-word <quote>]
    dispatch abandon <issue> [--operator-word <quote>]
    dispatch audit
    dispatch gates <pr>
    dispatch merge <pr> <head> [--remains <what remains>]

Run from the dispatcher's checkout under `mise x uv -- uv run`, which puts the
pinned `gh` and `jj` on `PATH`. `claim`, `queue`, `abandon` and `merge` name
the dispatcher by `CLAUDE_CODE_SESSION_ID`, which a Claude Code session's
commands inherit, and refuse without it. docs/framework/dispatch.md is the
protocol.
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess as sp
import sys
from pathlib import Path

from kluster.lib import workstation

from . import audit, brief, claim, filing, merge, prcheck, record, serialized
from .forge import ForgeError, Gh

log = logging.getLogger(__name__)

#: The exit of a run that did what it was asked.
DONE = 0
#: The exit of a run refused before it changed anything on the forge.
REFUSED = 1

#: The variable a Claude Code session's commands carry its session id in.
SESSION = 'CLAUDE_CODE_SESSION_ID'

_WORD_HELP = "the operator's words, quoted whole, licensing what would otherwise refuse; recorded beside the claim"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='dispatch',
        description='The dispatch protocol of docs/framework/dispatch.md: each step refuses on its preconditions.',
    )
    commands = parser.add_subparsers(dest='command', required=True)
    filing_ = commands.add_parser(
        'file', help='file a ledger issue', description='File a task carrying area/*, kind/* and an open milestone.'
    )
    _ = filing_.add_argument('--title', required=True)
    _ = filing_.add_argument('--body-file', type=Path, required=True)
    _ = filing_.add_argument('--label', action='append', default=[], help='repeated, one label each')
    _ = filing_.add_argument('--milestone')
    for name, help_ in (
        ('claim', 'label an issue in-flight and post its dispatch record'),
        ('queue', "post an issue's brief and record, and label it cloud/ready"),
    ):
        command = commands.add_parser(name, help=help_, description=f'{help_[0].upper()}{help_[1:]} (dispatch.md §2).')
        _ = command.add_argument('issue', type=int)
        _ = command.add_argument('brief', type=Path, help='the brief, opening with its front matter')
        _ = command.add_argument('--operator-word', help=_WORD_HELP)
        if name == 'claim':
            _ = command.add_argument(
                '--adopt',
                action='store_true',
                help=(
                    'write the record of a claim whose in-flight stands with no record, on --operator-word; '
                    'refused where one exists'
                ),
            )
    abandoning = commands.add_parser(
        'abandon', help="end a dispatch's claim", description='Post the end record and take the claim labels off.'
    )
    _ = abandoning.add_argument('issue', type=int)
    _ = abandoning.add_argument('--operator-word', help=_WORD_HELP)
    _ = commands.add_parser('audit', help="list the ledger's anomalies", description="List the ledger's anomalies.")
    gating = commands.add_parser(
        'gates',
        help="check a pull request's content against its dispatch",
        description='Check the paths a pull request touches, the host names it names, and its sweep (dispatch.md §3).',
    )
    _ = gating.add_argument('pr', type=int)
    merging = commands.add_parser(
        'merge',
        help='merge a pull request and end its dispatch',
        description='Merge a pull request at the head its builder reported, and end its dispatch (dispatch.md §2.1).',
    )
    _ = merging.add_argument('pr', type=int)
    _ = merging.add_argument('head', help="the full head SHA the builder's latest report names")
    _ = merging.add_argument(
        '--remains', help='what is left of an issue the record leaves open, for the comment on it (§4)'
    )
    return parser


def _workspaces() -> list[str]:
    """The `jj` workspaces of the checkout this package runs from."""
    listed = sp.run(
        ['jj', 'workspace', 'list', '-T', 'name ++ "\\n"'],
        cwd=workstation.repo_root(),
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    return listed.stdout.split()


def _gates(number: int) -> int:
    report = prcheck.gates(prcheck.PullGh(), prcheck.Git(workstation.repo_root()), number)
    for check, note in report.notes:
        log.info('%s: %s', check.value, note)
    for check, finding in report.findings:
        log.error('%s: %s', check.value, finding)
    log.info('#%d: %d findings', number, len(report.findings))
    return REFUSED if report.findings else DONE


def _run(args: argparse.Namespace) -> int:
    if args.command == 'gates':
        return _gates(args.pr)
    forge = Gh()
    if args.command == 'file':
        log.info(
            'filed %s',
            filing.file(
                forge,
                title=args.title,
                body=Path(args.body_file).read_text(),
                labels=list[str](args.label),
                milestone=args.milestone,
            ),
        )
        return DONE
    if args.command == 'audit':
        findings = audit.audit(forge, _workspaces())
        for finding in findings:
            print(finding)
        log.info('%d findings', len(findings))
        return DONE
    session = os.environ.get(SESSION)
    if not session:
        log.error('%s is not set: a claim names the dispatcher session that holds it', SESSION)
        return REFUSED
    if args.command == 'abandon':
        claim.abandon(forge, args.issue, session, args.operator_word)
        log.info('#%d is no longer claimed', args.issue)
        return DONE
    if args.command == 'merge':
        merged = merge.merge(
            merge.MergeGh(), merge.Jj(workstation.repo_root()), args.pr, args.head, session, remains=args.remains
        )
        log.info('%s is merged, and its dispatch ended', merged.url)
        return DONE
    content = Path(args.brief).read_bytes()
    dispatch = claim.Dispatch(
        brief=brief.read(content),
        session=session,
        operator_word=args.operator_word,
        adopt=bool(getattr(args, 'adopt', False)),
    )
    if args.command == 'claim':
        verdict = claim.claim(forge, dispatch, args.issue, serialized.read())
    else:
        verdict = claim.queue(forge, dispatch, args.issue, serialized.read(), content.decode())
    for passed in verdict.passed:
        log.warning("past on the operator's word: %s", passed)
    for overlap in verdict.overlaps:
        log.warning('overlap, recorded and settled at merge: %s', overlap)
    log.info('#%d is %s', args.issue, 'claimed' if args.command == 'claim' else 'queued')
    return DONE


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s', stream=sys.stderr)
    args = build_parser().parse_args(argv)
    try:
        return _run(args)
    except (claim.Refused, merge.Refused) as exc:
        for refusal in exc.refusals:
            log.error('refused: %s', refusal)
    except merge.Stopped as exc:
        log.error('stopped: %s', exc)
    except (
        brief.BriefRefused,
        claim.WordRefused,
        filing.FilingRefused,
        prcheck.PullUnreadable,
        record.RecordUnreadable,
        serialized.SetUnreadable,
        ForgeError,
        OSError,
        sp.CalledProcessError,
        sp.TimeoutExpired,
    ) as exc:
        log.error('refused: %s', exc)
    return REFUSED
