"""`appliance-image`: one of the state-backend appliance's pinned images, as the repository, tag and digest it is pinned by.

    mise x uv -- uv run appliance-image postgres

It prints `<repository> <tag> <digest>` on one line, the three parts of the
reference `kluster.lib.state_backend.settings` pins the image by
(`POSTGRES_IMAGE`, `PGBOUNCER_IMAGE`). `checks.yml`'s Podman step reads the
line to pull the image by its digest and tag it with its repository and tag,
which is what lets the pinned reference resolve locally for the cases that
run it (framework/ci.md §3).
"""

from __future__ import annotations

import argparse
import sys

from kluster.lib.state_backend import settings

#: Each image by the name it is asked for: its repository, tag and digest.
IMAGES = {
    'postgres': (settings.POSTGRES_REPOSITORY, settings.POSTGRES_TAG, settings.POSTGRES_DIGEST),
    'pgbouncer': (settings.PGBOUNCER_REPOSITORY, settings.PGBOUNCER_TAG, settings.PGBOUNCER_DIGEST),
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='appliance-image',
        description="Print one of the appliance's pinned images as `<repository> <tag> <digest>`.",
    )
    _ = parser.add_argument('image', choices=sorted(IMAGES), help='the image')
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    image: str = args.image
    print(*IMAGES[image])
    return 0


if __name__ == '__main__':
    sys.exit(main())
