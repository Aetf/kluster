"""`dispatch`: the command line.

    dispatch file --title <title> --body-file <file> --label <label>... --milestone <milestone>
    dispatch claim <issue> <brief> [--operator-word <quote>] [--adopt]
    dispatch queue <issue> <brief> [--operator-word <quote>]
    dispatch abandon <issue> [--operator-word <quote>]
    dispatch audit
    dispatch gates <pr>
    dispatch gates calibrate <gate>
    dispatch gates harvest <pr>...
    dispatch gates appeal <pr> <report>
    dispatch merge <pr> <head> [--remains <what remains>]

Run from the dispatcher's checkout under `mise x uv -- uv run`, which puts the
pinned `gh` and `jj` on `PATH`. `claim`, `queue`, `abandon` and `merge` name
the dispatcher by `CLAUDE_CODE_SESSION_ID`, which a Claude Code session's
commands inherit, and refuse without it. `gates` and `gates calibrate` ask
the model gates (`gates/`) with the key the register files for them, which
nothing else here reads. docs/framework/dispatch.md is the protocol.
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess as sp
import sys
from pathlib import Path

import anthropic

from kluster.lib import workstation

from . import audit, brief, claim, filing, merge, prcheck, record, serialized
from .forge import ForgeError, Gh
from .gates import appeal, calibrate, calibration, cases, comment, gate, harvest, model, run
from .gates.forge import GateGh

log = logging.getLogger(__name__)

#: The exit of a run that did what it was asked.
DONE = 0
#: The exit of a run refused before it changed anything on the forge.
REFUSED = 1

#: The variable a Claude Code session's commands carry its session id in.
SESSION = 'CLAUDE_CODE_SESSION_ID'

#: The steps of `dispatch gates` beside the check of a pull request, which is `dispatch gates <pr>`.
GATE_STEPS = ('calibrate', 'harvest', 'appeal')
#: The name the check of a pull request is parsed under, which `<pr>` alone stands for.
RUN = 'run'

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
        help="check a pull request's content against its dispatch, and ask the model gates about it",
        description=(
            'Check a pull request: its paths, its host names and its sweep, then the model gates its paths '
            'trigger, whose findings it posts in one comment (dispatch.md §3). The steps below keep the model '
            'gates honest.'
        ),
        usage='dispatch gates <pr> | calibrate <gate> | harvest <pr>... | appeal <pr> <report>',
    )
    steps = gating.add_subparsers(dest='step', required=True, metavar='<pr> | ' + ' | '.join(GATE_STEPS))
    running = steps.add_parser(RUN)
    _ = running.add_argument('pr', type=int)
    calibrating = steps.add_parser(
        'calibrate',
        help='measure a gate against its cases and write its record',
        description='Ask a gate about every case of its set and write its calibration record (dispatch.md §3).',
    )
    _ = calibrating.add_argument('gate', help='the gate, by the name of its directory with - for _')
    harvesting = steps.add_parser(
        'harvest',
        help="append the cases the reviews of pull requests tag to each gate's set",
        description="Append each place a review tags [gate:<name>] or [gate:<name> clean] to that gate's cases.",
    )
    _ = harvesting.add_argument('prs', type=int, nargs='+', metavar='pr')
    appealing = steps.add_parser(
        'appeal',
        help="record a builder's appeals of gate findings",
        description="Write each `Gate appeal:` line of a builder's report into the findings comment of the head.",
    )
    _ = appealing.add_argument('pr', type=int)
    _ = appealing.add_argument('report', type=Path, help="a file holding the builder's report")
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


def _messages() -> model.Model:
    return model.Messages.with_key(model.key())


def _gates(number: int) -> int:
    forge = GateGh()
    git = prcheck.Git(workstation.repo_root())
    report = prcheck.gates(forge, git, number)
    for check, note in report.notes:
        log.info('%s: %s', check.value, note)
    for check, finding in report.findings:
        log.error('%s: %s', check.value, finding)
    log.info('#%d: %d findings', number, len(report.findings))
    posted = run.review(forge, git, number, gate.discover(), _messages)
    for ran in posted.evidence.gates if posted is not None else ():
        if ran.unheard:
            log.warning('%s, %s, gave no verdict: %s', ran.gate, ran.status, ran.why)
        for entry in ran.findings:
            log.warning('%s, %s: %s%s', ran.gate, ran.status, entry.place, ' (appealed)' if entry.appeal else '')
    if run.refuses(0, posted):
        log.error(
            '#%d: a blocking gate holds a finding or an undecided item no appeal answers, or asked nothing', number
        )
    return REFUSED if run.refuses(len(report.findings), posted) else DONE


def _gate_step(args: argparse.Namespace) -> int:
    if args.step == RUN:
        return _gates(args.pr)
    if args.step == 'calibrate':
        measured = calibrate.calibrate(gate.named(args.gate), workstation.repo_root(), _messages)
        log.info('%s: %s', args.gate, measured.status)
        for shortfall in measured.shortfalls():
            log.warning('%s: %s', args.gate, shortfall)
        return DONE
    if args.step == 'harvest':
        harvested = harvest.harvest(
            GateGh(), harvest.HarvestGit(workstation.repo_root()), list[int](args.prs), gate.discover()
        )
        for note in harvested.notes:
            log.warning('%s', note)
        for name, added in harvested.appended.items():
            log.info('%s: %d cases appended', name, len(added))
        return DONE
    appealed = appeal.record(GateGh(), args.pr, Path(args.report).read_text())
    log.info('#%d: appeals recorded at %s', args.pr, appealed.head[:12])
    return DONE


def _run(args: argparse.Namespace) -> int:
    if args.command == 'gates':
        return _gate_step(args)
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


def arguments(argv: list[str]) -> list[str]:
    """`argv` with `dispatch gates <pr>` spelled as the step it is parsed under."""
    if len(argv) >= 2 and argv[0] == 'gates' and argv[1] not in GATE_STEPS and not argv[1].startswith('-'):
        return ['gates', RUN, *argv[1:]]
    return argv


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s', stream=sys.stderr)
    args = build_parser().parse_args(arguments(sys.argv[1:] if argv is None else argv))
    try:
        return _run(args)
    except (claim.Refused, merge.Refused, appeal.AppealRefused) as exc:
        for refusal in exc.refusals:
            log.error('refused: %s', refusal)
    except merge.Stopped as exc:
        log.error('stopped: %s', exc)
    except (
        brief.BriefRefused,
        calibration.RecordUnreadable,
        cases.CasesUnreadable,
        claim.WordRefused,
        comment.EvidenceTooLong,
        comment.EvidenceUnreadable,
        gate.GateUnreadable,
        model.KeyMissing,
        anthropic.APIError,
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
