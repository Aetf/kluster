"""The drill recipient file, pointed away from the checkout's own.

`config.DRILL_RECIPIENT_FILE` is the committed file under `deploy/state-backend/`,
and what it holds decides the recipient list a render writes and a dump
encrypts to. A suite about what the code does with that file cannot let the
repository's state decide its cases, so it moves the file into a directory of
its own, where the file is absent until a case writes it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kluster.scripts.state_backend import config


def redirect(monkeypatch: pytest.MonkeyPatch, directory: Path) -> Path:
    """Point `config.DRILL_RECIPIENT_FILE` into `directory`, and return the path.

    Nothing is written there: the file stays absent until a case writes it.
    """
    path = directory / config.DRILL_RECIPIENT
    monkeypatch.setattr(config, 'DRILL_RECIPIENT_FILE', path)
    return path
