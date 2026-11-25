"""`dispatch gates calibrate <name>`: a gate asked about every case of its set, and its record written.

The canonical section is read from the checkout the command runs in, which
is the tree the record's hash of it is held against (`calibration`). Each
case is asked as the gate asks any item, three samples to a batch, so the
record measures the verdict `dispatch gates` would give.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from .ask import Asked, judge
from .calibration import Calibration, digest, measure
from .gate import CASES, RECORD, Gate, GateUnreadable
from .model import Model

log = logging.getLogger(__name__)


def calibrate(gate: Gate, checkout: Path, model: Callable[[], Model]) -> Calibration:
    """Measure `gate` against its cases, with its canon as `checkout` holds it, and write its record."""
    cases = gate.cases()
    if not cases:
        raise GateUnreadable(f'{gate.name} has no cases in {gate.directory / CASES} to measure it against')

    def text(path: str) -> str | None:
        file = checkout / path
        return file.read_text() if file.is_file() else None

    canon = gate.canon(text).text
    log.info('asking %s about %d cases, on %s', gate.name, len(cases), gate.spec.model)
    (judged,) = judge([Asked(gate.system(canon), gate.spec.model, [c.item for c in cases])], model)
    record = measure(cases, judged, cases=digest((gate.directory / CASES).read_text()), hashes=gate.hashes(canon))
    _ = (gate.directory / RECORD).write_text(record.to_json())
    log.info('wrote %s: %s', gate.directory / RECORD, record.status)
    return record
