"""Whether the vendored agent skills still match upstream.

`.agents/skills/` holds copies of skills installed by `npx skills` from
`pulumi/agent-skills`. Nothing else watches them: renovate cannot, because
`skills-lock.json` pins by content hash rather than by version, so there is no
`currentValue` for it to compare (recorded in `renovate.json5`'s
`ignorePaths`).

The lock file's own `computedHash` is not usable for this either. Upstream
computes it over the *source* directory and then drops `metadata.json`,
dotfiles and some directories while installing, so it cannot be recomputed
from what landed on disk -- their known defect, vercel-labs/skills#806.

So this compares bytes instead: fetch the source repository, and for every
file the install actually produced, check it still matches the file it came
from. That answers the question the hash was supposed to ("is our copy what
upstream says") without depending on a number that cannot be reproduced.
"""

from __future__ import annotations

import io
import json
import logging
import sys
import tarfile
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[3]
LOCK = ROOT / 'skills-lock.json'
INSTALLED = ROOT / '.agents' / 'skills'

#: A source repository groups skills under `<namespace>/skills/<name>/`, and
#: the lock file records the repository but not the namespace -- so the
#: namespace is discovered rather than assumed.
UPSTREAM_MARKER = '/skills/'

#: Files the installer deliberately does not copy (vercel-labs/skills#806).
#: Their absence locally is not drift.
NOT_INSTALLED = ('metadata.json',)


@dataclass
class Drift:
    changed: list[str] = field(default_factory=list)
    removed_upstream: list[str] = field(default_factory=list)
    added_upstream: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.changed or self.removed_upstream or self.added_upstream)


def sources() -> dict[str, str]:
    """Skill name -> the `owner/repo` it was installed from."""
    lock = json.loads(LOCK.read_text())
    return {name: str(entry['source']) for name, entry in lock['skills'].items()}


def fetch(repo: str, ref: str = 'HEAD') -> dict[str, bytes]:
    """Every file the source repository keeps under a `skills/` directory.

    Keyed by the path below the tarball root, so the namespace is still
    visible to `compare`.
    """
    url = f'https://codeload.github.com/{repo}/tar.gz/{ref}'
    with urllib.request.urlopen(url, timeout=300) as response:
        payload = response.read()

    files: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(payload), mode='r:gz') as archive:
        for member in archive.getmembers():
            if not member.isfile():
                continue
            # The tarball's top level is a single generated directory name.
            _, _, path = member.name.partition('/')
            if UPSTREAM_MARKER not in f'/{path}':
                continue
            handle = archive.extractfile(member)
            if handle is None:  # pragma: no cover - directories are filtered above
                continue
            files[path] = handle.read()
    return files


def compare(skill: str, upstream: dict[str, bytes]) -> Drift:
    drift = Drift()
    local_root = INSTALLED / skill
    local = {
        str(path.relative_to(local_root)): path.read_bytes() for path in sorted(local_root.rglob('*')) if path.is_file()
    }
    marker = f'{UPSTREAM_MARKER}{skill}/'
    remote = {path.split(marker, 1)[1]: content for path, content in upstream.items() if marker in f'/{path}'}
    if not remote:
        raise RuntimeError(f'{skill} is in the lock file but not in the source repository')

    for name, content in local.items():
        if name not in remote:
            drift.removed_upstream.append(f'{skill}/{name}')
        elif remote[name] != content:
            drift.changed.append(f'{skill}/{name}')

    for name in remote:
        if name in local or name in NOT_INSTALLED or Path(name).name.startswith('.'):
            continue
        drift.added_upstream.append(f'{skill}/{name}')
    return drift


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
    _ = argv

    by_source: dict[str, list[str]] = {}
    for skill, source in sources().items():
        by_source.setdefault(source, []).append(skill)

    total = Drift()
    for source, skills in sorted(by_source.items()):
        log.info('fetching %s', source)
        upstream = fetch(source)
        for skill in sorted(skills):
            drift = compare(skill, upstream)
            total.changed += drift.changed
            total.removed_upstream += drift.removed_upstream
            total.added_upstream += drift.added_upstream

    if not total:
        log.info('%d vendored skills match upstream', sum(len(s) for s in by_source.values()))
        return 0

    for path in total.changed:
        log.error('changed upstream: %s', path)
    for path in total.removed_upstream:
        log.error('gone upstream: %s', path)
    for path in total.added_upstream:
        log.error('new upstream: %s', path)
    log.error('run `npx skills update` and commit the result')
    return 1


if __name__ == '__main__':
    sys.exit(main())
