"""`appliance-image` prints the parts of the reference each appliance image is pinned by.

`checks.yml`'s Podman step pulls each image by the digest the line names and
tags it with its repository and tag, so the reference the cases run resolves
locally only when the three parts are that reference's own (framework/ci.md
§3). The case holds each line to the reference `settings` pins.
"""

from __future__ import annotations

import pytest

from kluster.lib.state_backend import settings
from kluster.scripts import appliance_image

#: Each image by the name the step asks for it by, and the reference the cases run it by.
PINNED = {'postgres': settings.POSTGRES_IMAGE, 'pgbouncer': settings.PGBOUNCER_IMAGE}


@pytest.mark.parametrize('image', sorted(PINNED))
def test_the_line_is_the_pinned_references_repository_tag_and_digest(
    image: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert appliance_image.main([image]) == 0

    repository, tag, digest = capsys.readouterr().out.split()
    assert f'{repository}:{tag}@{digest}' == PINNED[image]
