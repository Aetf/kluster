"""What `test_apps_exim.py` declares the application with, and `declared()`, which the run of every application calls (`test_apps_placement.py`)."""

from __future__ import annotations

from collections.abc import Sequence

from kluster.components.apps.exim import Exim
from kluster.lib.versions import ImagePin
from putils import Component

#: The pin the relay is declared with here: a reference that is no published
#: image, so a container carrying it carries the pin.
IMAGE = ImagePin('registry.example.invalid/exim-relay', 'v9.9.9', f'sha256:{"e" * 64}')

#: The ciphertexts it is handed, each naming what it stands for.
SMTP_CIPHERTEXT = {'smtp_user': 'sealed-account', 'smtp_pass': 'sealed-app-password'}
DKIM = {'tls.key': 'sealed-dkim-key'}


def declared() -> Sequence[Component]:
    """The relay, for the run that declares every application component (`test_apps_placement.py`)."""
    return (Exim('exim', image=IMAGE, smtp=SMTP_CIPHERTEXT, dkim=DKIM),)
