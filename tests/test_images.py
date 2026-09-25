"""The self-built images: what they are built from, and that a published tag never moves.

`images.yml` builds `docker/<image>.Containerfile` with the build args its
`.conf` resolves to, and publishes the result under a tag the image-conf action
derives. Two properties keep a tag the cluster pins serving what was reviewed,
and the cases below hold both (framework/ci.md §4):

- **What an image is built from is written down whole.** Every base is pinned
  by digest, so a rebuild pulls what the files name rather than what a tag
  upstream points to that day.
- **A published tag is never pushed again.** The tag carries a fingerprint of
  those files, so a change to what the image is built from moves the tag and a
  comment edit does not; and a push run whose tag the registry already serves
  pushes nothing for that image.

The action's step is bash in YAML, and the cases run it as it is written, with
the one command that reaches the network -- `skopeo` -- replaced by a fake that
answers the way the registry does.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

ROOT = Path(__file__).parent.parent
DOCKER = ROOT / 'docker'
ACTION = ROOT / '.github' / 'actions' / 'image-conf' / 'action.yml'
WORKFLOW = ROOT / '.github' / 'workflows' / 'images.yml'

#: The owner the workflow runs under, as GitHub spells it; the reference is
#: lowercase, because ghcr paths are.
OWNER = 'Aetf'


def _images() -> list[str]:
    """Every image the workflow builds: it globs the Containerfiles the same way."""
    return sorted(path.name.removesuffix('.Containerfile') for path in DOCKER.glob('*.Containerfile'))


def _load(path: Path) -> dict[str, Any]:
    return cast(dict[str, Any], yaml.safe_load(path.read_text()))


def _architectures() -> list[str]:
    """The architectures `build` runs for, read off its matrix rather than listed again."""
    platforms = cast(list[dict[str, str]], _load(WORKFLOW)['jobs']['build']['strategy']['matrix']['platform'])
    return [platform['arch'] for platform in platforms]


def _conf_step() -> dict[str, Any]:
    steps = cast(list[dict[str, Any]], _load(ACTION)['runs']['steps'])
    (step,) = [step for step in steps if step.get('id') == 'conf']
    return step


# --------------------------------------------------------------------------
# Running the action's step.

#: A stand-in for `skopeo inspect`, answering as the registry does in each of
#: the three cases the step tells apart. The absent case is worded as the
#: skopeo on the runners words it: that one exits 1 for an unknown manifest,
#: where later releases exit 2, so the words are what the step can rely on.
FAKE_SKOPEO = r"""#!/bin/bash
printf '%s\n' "$*" >> "$FAKE_SKOPEO_LOG"
case $FAKE_REGISTRY in
  serves) exit 0 ;;
  unknown)
    echo "level=fatal msg=\"Error parsing image name $3: reading manifest x in y: manifest unknown\"" >&2
    exit 1 ;;
  unreachable)
    echo 'level=fatal msg="pinging container registry ghcr.io: dial tcp: i/o timeout"' >&2
    exit 1 ;;
esac
exit 99
"""


@dataclass(frozen=True)
class Conf:
    returncode: int
    outputs: dict[str, str]
    args: dict[str, str]
    inspected: list[str]
    stderr: str

    @property
    def ref(self) -> str:
        return self.outputs['ref']


def _run_conf(tree: Path, scratch: Path, image: str, *, arch: str = '', registry: str = 'unknown') -> Conf:
    """Run the conf step from `tree`, the way the runner does from a checkout."""
    scratch.mkdir(parents=True, exist_ok=True)
    fake = scratch / 'bin' / 'skopeo'
    fake.parent.mkdir(exist_ok=True)
    fake.write_text(FAKE_SKOPEO)
    fake.chmod(0o755)
    output = scratch / 'output'
    output.write_text('')
    log = scratch / 'skopeo.log'
    log.write_text('')
    temp = scratch / 'runner-temp'
    temp.mkdir(exist_ok=True)
    env = {
        'PATH': f'{fake.parent}{os.pathsep}{os.environ["PATH"]}',
        'IMAGE_NAME': image,
        'ARCH': arch,
        'RUNNER_TEMP': str(temp),
        'GITHUB_OUTPUT': str(output),
        'GITHUB_REPOSITORY_OWNER': OWNER,
        'FAKE_REGISTRY': registry,
        'FAKE_SKOPEO_LOG': str(log),
    }
    done = subprocess.run(
        ['bash', '-c', cast(str, _conf_step()['run'])],
        cwd=tree,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    outputs = dict(line.split('=', 1) for line in output.read_text().splitlines())
    args: dict[str, str] = {}
    if 'args-file' in outputs:
        for line in Path(outputs['args-file']).read_text().splitlines():
            key, value = line.split('=', 1)
            args[key] = value
    return Conf(done.returncode, outputs, args, log.read_text().splitlines(), done.stderr)


def _copy_docker(tmp_path: Path) -> Path:
    tree = tmp_path / 'tree'
    shutil.copytree(DOCKER, tree / 'docker')
    return tree


def test_the_step_the_cases_run_is_the_one_the_runner_runs() -> None:
    """The inputs arrive through the environment, so the script above is the whole step.

    An `${{ inputs.* }}` expression written into the script would be
    substituted by the runner and not by these cases, which would then be
    running a different script from the one that publishes.
    """
    step = _conf_step()
    assert step['env'] == {'IMAGE_NAME': '${{ inputs.image }}', 'ARCH': '${{ inputs.arch }}'}
    assert '${{' not in step['run']


# --------------------------------------------------------------------------
# What an image is built from.

FROM = re.compile(r'^FROM\s+(?:--platform=\S+\s+)?(?P<image>\S+)(?:\s+AS\s+(?P<stage>\S+))?\s*$', re.IGNORECASE)
VARIABLE = re.compile(r'\$(?:\{(\w+)\}|(\w+))')

#: A base named by tag and digest both: the digest is what is pulled, the tag
#: is what renovate reads the next version off and what a reader recognizes.
PINNED = re.compile(r'^[^\s@$]+:[\w][\w.-]*@sha256:[0-9a-f]{64}$')


#: An image a `COPY` or a `RUN` mount reads from, where it names one rather
#: than a stage: `COPY --from=<ref>`, `RUN --mount=…,from=<ref>`.
FROM_OPTION = re.compile(r'(?:--from=|--mount=\S*?\bfrom=)(?P<image>[^\s,]+)')


def _bases(recipe: str, args: dict[str, str]) -> list[str]:
    """Every image the recipe pulls, with the build args substituted.

    A stage's base, and an image a `COPY --from=` or a mount's `from=` names
    directly -- each is pulled the way a base is, so each is pinned the same
    way. Stages built earlier, by name or by index, and `scratch` are left out.
    """
    stages: set[str] = set()
    bases: list[str] = []
    count = 0

    def resolve(image: str) -> str:
        return VARIABLE.sub(lambda m: args.get(m.group(1) or m.group(2), m.group(0)), image)

    for line in recipe.splitlines():
        found = FROM.match(line)
        if found is not None:
            image = resolve(found.group('image'))
            if image != 'scratch' and image not in stages:
                bases.append(image)
            if found.group('stage'):
                stages.add(found.group('stage'))
            stages.add(str(count))
            count += 1
            continue
        for option in FROM_OPTION.finditer(line):
            image = resolve(option.group('image'))
            if image not in stages:
                bases.append(image)
    return bases


@pytest.mark.parametrize('image', _images())
def test_every_base_is_pinned_by_digest(image: str, tmp_path: Path) -> None:
    """Every `FROM` resolves, for every architecture built, to a tag with its digest.

    Resolved the way the build resolves it -- with the build args the conf
    step writes for that architecture, and `TARGETARCH` -- because a base
    spelled through a build arg is pinned in the conf rather than in the
    Containerfile, and one pinned per architecture is pinned in a key only that
    architecture's build reads.
    """
    recipe = (DOCKER / f'{image}.Containerfile').read_text()
    for arch in _architectures():
        conf = _run_conf(ROOT, tmp_path / arch, image, arch=arch)
        assert conf.returncode == 0, conf.stderr
        bases = _bases(recipe, {**conf.args, 'TARGETARCH': arch})
        assert bases, f'{image} names no base'
        unpinned = [base for base in bases if not PINNED.match(base)]
        assert unpinned == [], f'{image} ({arch}) builds from a base not pinned by digest: {unpinned}'


PINNED_DIGEST = 'sha256:' + '0' * 64

#: Recipes reading an image other than through `FROM`, each with whether
#: what it reads is pinned.
READS_BESIDE_FROM = {
    'COPY --from=, by tag': ('FROM scratch\nCOPY --from=docker.io/library/alpine:3.22 /a /a\n', False),
    'COPY --from=, by tag and digest': (
        f'FROM scratch\nCOPY --from=docker.io/library/alpine:3.22@{PINNED_DIGEST} /a /a\n',
        True,
    ),
    'a mount from=, by tag': (
        'FROM scratch AS base\nRUN --mount=type=bind,from=docker.io/library/alpine:3.22,target=/x true\n',
        False,
    ),
    'a mount from=, by tag and digest': (
        f'FROM scratch AS base\nRUN --mount=type=bind,from=docker.io/library/alpine:3.22@{PINNED_DIGEST},target=/x true\n',
        True,
    ),
    'COPY --from= a stage, by name and by index': (
        f'FROM docker.io/library/alpine:3.22@{PINNED_DIGEST} AS src\nFROM scratch\n'
        'COPY --from=src /a /a\nCOPY --from=0 /b /b\nRUN --mount=from=src,target=/x true\n',
        True,
    ),
}


@pytest.mark.parametrize('case', READS_BESIDE_FROM)
def test_an_image_read_beside_from_is_pinned_like_a_base(case: str) -> None:
    """The census above reads these too: a `COPY --from=` an image is a pull the fingerprint cannot see move."""
    recipe, pinned = READS_BESIDE_FROM[case]
    bases = _bases(recipe, {})
    assert bases or pinned, case
    assert all(PINNED.match(base) for base in bases) == pinned, bases


#: How a Containerfile reads a file: from another stage, or from a URL.
COPY_OR_ADD = re.compile(r'^(COPY|ADD)\s+(?P<rest>.*)$', re.IGNORECASE | re.MULTILINE)

#: A `RUN` mount, whose options say where its files come from.
MOUNT = re.compile(r'--mount=(?P<options>\S+)')


@pytest.mark.parametrize('image', _images())
def test_nothing_is_read_from_the_build_context(image: str) -> None:
    """The fingerprint reads the Containerfile and the conf, so nothing else may shape the image.

    A `COPY` of a file from the build context -- the repository root, the way
    the workflow calls buildah -- would be an input the fingerprint does not
    see: a change to that file would rebuild the image under the tag it already
    has, and the publish would leave the old image standing without a word.
    """
    recipe = (DOCKER / f'{image}.Containerfile').read_text()
    for found in COPY_OR_ADD.finditer(recipe):
        rest = found.group('rest')
        sources = [word for word in rest.split()[:-1] if not word.startswith('--')]
        from_stage = any(word.startswith('--from=') for word in rest.split())
        from_url = all(source.startswith(('https://', 'http://')) for source in sources)
        assert from_stage or from_url, f'{image} reads the build context: {found.group(0)}'
    # A bind mount's source is the build context unless it names a stage, and
    # `bind` is the type a mount has when it names none.
    for found in MOUNT.finditer(recipe):
        options: dict[str, str] = {}
        for option in found.group('options').split(','):
            key, _, value = option.partition('=')
            options[key] = value
        if options.get('type', 'bind') == 'bind':
            assert 'from' in options, f'{image} mounts the build context: {found.group(0)}'


# --------------------------------------------------------------------------
# The tag.

FINGERPRINTED = re.compile(r'^(?P<name>ghcr\.io/[^:]+):(?P<tag>.+)-(?P<fingerprint>[0-9a-f]{12})$')


def _conf_tag(tree: Path, image: str) -> tuple[str, str]:
    """The conf's own IMAGE and TAG, sourced the way the step sources them."""
    done = subprocess.run(
        ['bash', '-c', 'set -a; . "docker/$1.conf"; printf "%s\\n%s\\n" "$IMAGE" "$TAG"', 'conf', image],
        cwd=tree,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    name, tag = done.stdout.splitlines()
    return name, tag


@pytest.mark.parametrize('image', _images())
def test_the_tag_is_the_conf_tag_and_a_fingerprint(image: str, tmp_path: Path) -> None:
    """One reference per image, whatever the architecture: the manifest stitches the two under it."""
    name, tag = _conf_tag(ROOT, image)
    refs = {_run_conf(ROOT, tmp_path / (arch or 'none'), image, arch=arch).ref for arch in ['', *_architectures()]}
    (ref,) = refs
    found = FINGERPRINTED.match(ref)
    assert found is not None, ref
    assert found.group('name') == f'ghcr.io/{OWNER.lower()}/{name}'
    assert found.group('tag') == tag


def _edit(path: Path, old: str, new: str) -> None:
    text = path.read_text()
    assert text.count(old) >= 1, f'{path.name} carries no {old!r}'
    path.write_text(text.replace(old, new, 1))


@pytest.mark.parametrize('image', _images())
def test_a_comment_edit_keeps_the_tag(image: str, tmp_path: Path) -> None:
    """A comment or a blank line, in either file, is not something the image is built from."""
    tree = _copy_docker(tmp_path)
    before = _run_conf(tree, tmp_path / 'before', image).ref
    for path in (tree / 'docker' / f'{image}.Containerfile', tree / 'docker' / f'{image}.conf'):
        path.write_text('# a comment\n\n' + path.read_text().replace('\n#', '\n# reworded\n#', 1) + '\n  # trailing\n')
    assert _run_conf(tree, tmp_path / 'after', image).ref == before


def _flip_hex_after(anchor: str) -> Callable[[Path], None]:
    """Change the first hex digit after `anchor`, whatever it is, as a bump of that value does.

    Whatever it is, so that the edit is one whichever value the file holds:
    a digit spelled out here would be the one a renovate refresh replaces.
    """

    def change(path: Path) -> None:
        text = path.read_text()
        found = re.search(f'(?:{anchor})([0-9a-f])', text)
        assert found is not None, f'{path.name} carries nothing after {anchor!r}'
        flipped = '1' if found.group(1) == '0' else '0'
        path.write_text(text[: found.start(1)] + flipped + text[found.end(1) :])

    return change


_flip_digest = _flip_hex_after('sha256:')


#: Edits to what an image is built from, each of which has to move its tag.
BUILT_FROM_EDITS: dict[str, tuple[str, str, Callable[[Path], None]]] = {
    'a digest in the Containerfile': ('emailproxy', 'Containerfile', _flip_digest),
    'a digest in the conf': ('pgcron-cnpg', 'conf', _flip_digest),
    'a per-architecture digest in the conf': (
        'vchord-cnpg',
        'conf',
        _flip_hex_after(r'_ARM64=[^@\s]+@sha256:'),
    ),
    'an instruction': ('golinks', 'Containerfile', lambda path: _edit(path, 'EXPOSE 8067', 'EXPOSE 8068')),
    'a build arg the tag does not carry': (
        'golinks',
        'conf',
        _flip_hex_after('GOLINKS_COMMIT='),
    ),
}


@pytest.mark.parametrize('edit', BUILT_FROM_EDITS)
def test_what_an_image_is_built_from_moves_its_tag(edit: str, tmp_path: Path) -> None:
    """Otherwise the change would be built under a tag already published, and never published at all."""
    image, suffix, change = BUILT_FROM_EDITS[edit]
    tree = _copy_docker(tmp_path)
    before = _run_conf(tree, tmp_path / 'before', image).ref
    change(tree / 'docker' / f'{image}.{suffix}')
    assert _run_conf(tree, tmp_path / 'after', image).ref != before


def test_a_per_architecture_key_reaches_only_its_own_build(tmp_path: Path) -> None:
    """`<KEY>_<ARCH>` reaches the build for that architecture as `<KEY>`, and the other's does not."""
    for arch in _architectures():
        conf = _run_conf(ROOT, tmp_path / arch, 'vchord-cnpg', arch=arch)
        assert conf.args['PGVECTO_RS'] == conf.args[f'PGVECTO_RS_{arch.upper()}']
        # The relation the conf states, not today's release: the tag names
        # the server major the base carries, and this architecture.
        pattern = rf'pg{conf.args["PG_MAJOR"]}-v[\w.]+-{arch}@sha256:[0-9a-f]{{64}}'
        assert re.fullmatch(pattern, conf.args['PGVECTO_RS']), conf.args['PGVECTO_RS']
    assert 'PGVECTO_RS' not in _run_conf(ROOT, tmp_path / 'none', 'vchord-cnpg').args


@pytest.mark.parametrize('image', _images())
def test_per_architecture_pins_name_one_release(image: str, tmp_path: Path) -> None:
    """The architectures' references to one upstream differ by the architecture alone.

    Upstream has published releases for one architecture only, so a grouped
    renovate pull request can move one line and not the other, and a bump
    under `docker/` previews as a zero diff, which is merged unattended: the
    image would ship each architecture a different release under one tag.
    """
    arches = _architectures()
    conf = _run_conf(ROOT, tmp_path, image)
    stems: dict[str, dict[str, str]] = {}
    for key, value in conf.args.items():
        for arch in arches:
            if key.endswith(f'_{arch.upper()}'):
                stems.setdefault(key.removesuffix(f'_{arch.upper()}'), {})[arch] = value
    for stem, values in stems.items():
        assert sorted(values) == sorted(arches), f'{stem} is pinned for {sorted(values)} only'
        releases: set[str] = set()
        for arch, value in values.items():
            tag = value.split('@', 1)[0]
            assert tag.endswith(f'-{arch}'), f'{stem}_{arch.upper()} names {tag}, not an {arch} image'
            releases.add(tag.removesuffix(f'-{arch}'))
        assert len(releases) == 1, f'{stem} names more than one release: {sorted(releases)}'


def test_a_conf_key_cannot_replace_the_architecture_being_built(tmp_path: Path) -> None:
    """The conf is sourced with every key exported, and it does not get to say which build this is."""
    tree = _copy_docker(tmp_path)
    conf_file = tree / 'docker' / 'vchord-cnpg.conf'
    conf_file.write_text(conf_file.read_text() + '\nARCH=arm64\n')
    conf = _run_conf(tree, tmp_path / 'run', 'vchord-cnpg', arch='amd64')
    assert conf.returncode == 0, conf.stderr
    assert conf.args['PGVECTO_RS'] == conf.args['PGVECTO_RS_AMD64']


# --------------------------------------------------------------------------
# The selection.


def _select_step() -> dict[str, Any]:
    steps = cast(list[dict[str, Any]], _load(WORKFLOW)['jobs']['select-images']['steps'])
    (step,) = [step for step in steps if step.get('id') == 'select']
    return step


def _git(tree: Path, *args: str) -> None:
    subprocess.run(
        ['git', '-c', 'user.name=t', '-c', 'user.email=t@example.invalid', '-c', 'commit.gpgsign=false', *args],
        cwd=tree,
        capture_output=True,
        timeout=30,
        check=True,
    )


def _select(tmp_path: Path, event: str, changed: str) -> list[str]:
    """What the selection picks when the last commit changed `changed`, as a run of `event` sees it."""
    tree = tmp_path / event
    for name in ('alpha', 'beta'):
        (tree / 'docker').mkdir(parents=True, exist_ok=True)
        (tree / 'docker' / f'{name}.Containerfile').write_text('FROM scratch\n')
    (tree / '.github' / 'workflows').mkdir(parents=True)
    (tree / '.github' / 'workflows' / 'images.yml').write_text('name: images\n')
    _git(tree, 'init', '-q')
    _git(tree, 'add', '-A')
    _git(tree, 'commit', '-qm', 'base')
    (tree / changed).write_text((tree / changed).read_text() + '# changed\n')
    _git(tree, 'commit', '-qam', 'change')
    output = tree.parent / f'{event}.output'
    output.write_text('')
    step = _select_step()
    env = {
        'PATH': os.environ['PATH'],
        'EVENT': event,
        'BASE': cast(str, step['env']['BASE']),
        'GITHUB_OUTPUT': str(output),
    }
    subprocess.run(['bash', '-c', cast(str, step['run'])], cwd=tree, env=env, timeout=30, check=True)
    (line,) = output.read_text().splitlines()
    return cast(list[str], json.loads(line.removeprefix('images=')))


def test_the_selection_step_the_cases_run_is_the_one_the_runner_runs() -> None:
    step = _select_step()
    assert step['env'] == {'EVENT': '${{ github.event_name }}', 'BASE': 'HEAD^1'}
    assert '${{' not in step['run']


@pytest.mark.parametrize('event', ['push', 'workflow_dispatch'])
def test_a_run_on_main_selects_every_image(event: str, tmp_path: Path) -> None:
    """The published check skips what is there; a push narrowed by its diff would lose images.

    The concurrency group keeps one push run waiting and a newer push cancels
    it, so an image changed only by the cancelled run would be selected by no
    run at all, and nothing would report it.
    """
    assert _select(tmp_path, event, 'docker/alpha.Containerfile') == ['alpha', 'beta']


def test_a_pull_request_selects_the_images_it_changes(tmp_path: Path) -> None:
    assert _select(tmp_path, 'pull_request', 'docker/alpha.Containerfile') == ['alpha']


def test_a_pull_request_changing_the_machinery_selects_every_image(tmp_path: Path) -> None:
    assert _select(tmp_path, 'pull_request', '.github/workflows/images.yml') == ['alpha', 'beta']


# --------------------------------------------------------------------------
# The publish.


def test_a_tag_the_registry_serves_reads_as_published(tmp_path: Path) -> None:
    conf = _run_conf(ROOT, tmp_path, 'emailproxy', registry='serves')
    assert conf.returncode == 0, conf.stderr
    assert conf.outputs['published'] == 'true'
    assert conf.inspected == [f'inspect --raw docker://{conf.ref}']


def test_a_tag_the_registry_does_not_know_reads_as_unpublished(tmp_path: Path) -> None:
    conf = _run_conf(ROOT, tmp_path, 'emailproxy', registry='unknown')
    assert conf.returncode == 0, conf.stderr
    assert conf.outputs['published'] == 'false'


def test_a_registry_that_does_not_answer_stops_the_job(tmp_path: Path) -> None:
    """Fail closed: read as absent, an outage would push over whatever the tag serves."""
    conf = _run_conf(ROOT, tmp_path, 'emailproxy', registry='unreachable')
    assert conf.returncode != 0
    assert 'published' not in conf.outputs
    assert 'Could not tell whether' in conf.stderr


#: What pushes a tag, in a step of the workflow.
PUSH = re.compile(r'\bbuildah\s+(?:manifest\s+)?push\b')
PUBLISHED = "steps.conf.outputs.published != 'true'"
ON_MAIN = "github.ref == 'refs/heads/main'"


@dataclass(frozen=True)
class Push:
    job: str
    step: str
    after_conf: bool
    #: The conditions its job and its step both have to meet, or None where
    #: either is anything but a plain conjunction, which is not read further.
    conjuncts: list[str] | None


def _conjuncts(condition: str) -> list[str] | None:
    condition = condition.strip()
    if condition.startswith('${{') and condition.endswith('}}'):
        condition = condition[3:-2]
    if '||' in condition or '(' in condition.replace('always()', ''):
        return None
    return [part.strip() for part in condition.split('&&') if part.strip()]


def _pushes() -> list[Push]:
    """Every step of the workflow that pushes a tag, with what gates it."""
    found: list[Push] = []
    for name, job in cast(dict[str, dict[str, Any]], _load(WORKFLOW)['jobs']).items():
        after_conf = False
        job_conjuncts = _conjuncts(cast(str, job.get('if', '')))
        for step in cast(list[dict[str, Any]], job.get('steps', [])):
            if step.get('uses') == './.github/actions/image-conf' and step.get('id') == 'conf':
                after_conf = True
            if PUSH.search(cast(str, step.get('run', ''))):
                step_conjuncts = _conjuncts(cast(str, step.get('if', '')))
                conjuncts = None if job_conjuncts is None or step_conjuncts is None else job_conjuncts + step_conjuncts
                found.append(Push(name, cast(str, step.get('name', '')), after_conf, conjuncts))
    return found


def test_every_push_is_held_by_the_published_check() -> None:
    """Every step that pushes runs only when its job's conf step found the tag unpublished.

    Read off the workflow rather than trusted to the two steps written today,
    and read as a condition rather than a string: the check has to be one of
    the terms every one of which must hold. A push added to the build step,
    whose condition names the check beside an `||` that lets every pull
    request through, would push over a published tag.
    """
    pushes = _pushes()
    assert len(pushes) >= 2, 'the census found neither the per-architecture push nor the manifest push'
    for push in pushes:
        assert push.after_conf, f'{push.job}: a push before the conf step: {push.step}'
        assert push.conjuncts is not None, (
            f'{push.job}: a push under a condition that is not a conjunction: {push.step}'
        )
        assert PUBLISHED in push.conjuncts, f'{push.job}: a push not held by the published check: {push.step}'


def test_only_main_publishes() -> None:
    """A dispatch from a branch builds, and pushes nothing.

    Otherwise it would publish an unreviewed image under a well-formed tag,
    or, with `main`'s files, race `main`'s own run from its own concurrency
    group and stitch the same tag twice.
    """
    for push in _pushes():
        assert push.conjuncts is not None and ON_MAIN in push.conjuncts, f'{push.job}: publishes off main: {push.step}'


# --------------------------------------------------------------------------
# Renovate keeps the pins current.

#: What `renovate.json5` reads a conf's pins with, spelled exactly as that file
#: holds them: a version with its digest on the next line, and a whole
#: `<tag>@<digest>` in one value.
CONF_VERSION_MATCH_STRING = (
    r'# renovate: datasource=(?<datasource>\S+) depName=(?<depName>\S+)'
    r'(?: versioning=(?<versioning>\S+))?(?: extractVersion=(?<extractVersion>\S+))?'
    r'\s+[A-Za-z_][A-Za-z0-9_]*=(?<currentValue>\d[\w.+-]*)'
    r'(?:\s+[A-Za-z_][A-Za-z0-9_]*_DIGEST=(?<currentDigest>sha256:[0-9a-f]{64}))?'
)
CONF_REFERENCE_MATCH_STRING = (
    r'# renovate: datasource=(?<datasource>\S+) depName=(?<depName>\S+)(?: versioning=(?<versioning>\S+))?'
    r'\s+[A-Za-z_][A-Za-z0-9_]*=(?<currentValue>[A-Za-z][\w.-]*)@(?<currentDigest>sha256:[0-9a-f]{64})'
)


def _as_renovate_spells_it(pattern: str) -> str:
    """The JSON5 single-quoted string `renovate.json5` holds a pattern in."""
    return "'" + pattern.replace('\\', '\\\\') + "'"


def _as_python_spells_it(pattern: str) -> re.Pattern[str]:
    """Python spells a named group `(?P<...>`, renovate's regex engine `(?<...>`."""
    return re.compile(pattern.replace('(?<', '(?P<'))


@pytest.mark.parametrize('image', _images())
def test_renovate_reads_every_digest_a_conf_pins(image: str) -> None:
    """Every digest in a conf is one renovate captures, beside the tag it belongs to.

    Renovate reads its configuration from the default branch, so a digest
    that no pattern reaches would sit unrefreshed and report nothing -- or,
    beside a version renovate does move, stay behind when the version moves,
    and a build pulls by digest, so it would build the old image under the new
    version's name.
    """
    config = (ROOT / 'renovate.json5').read_text()
    assert _as_renovate_spells_it(CONF_VERSION_MATCH_STRING) in config
    assert _as_renovate_spells_it(CONF_REFERENCE_MATCH_STRING) in config

    text = (DOCKER / f'{image}.conf').read_text()
    pinned = set(re.findall(r'sha256:[0-9a-f]{64}', text))
    captured: set[str] = set()
    for pattern in (CONF_VERSION_MATCH_STRING, CONF_REFERENCE_MATCH_STRING):
        for found in _as_python_spells_it(pattern).finditer(text):
            if found.group('currentDigest'):
                assert found.group('datasource') == 'docker', found.group(0)
                assert found.group('currentValue'), found.group(0)
                captured.add(found.group('currentDigest'))
    assert captured == pinned


@pytest.mark.parametrize('image', _images())
def test_a_reference_pin_names_a_versioning_its_tag_reads_under(image: str) -> None:
    """A `<tag>@<digest>` pin is one renovate reads releases off, not only digest refreshes.

    Its tag is not a version by itself (`pg15-v0.3.0-amd64`), so under the
    default versioning renovate would refresh the digest and never offer a
    release: the pin would sit on its release without a word. The hint's
    regex versioning is what reads one, and it has to match the tag it sits
    above.
    """
    text = (DOCKER / f'{image}.conf').read_text()
    for found in _as_python_spells_it(CONF_REFERENCE_MATCH_STRING).finditer(text):
        versioning = found.group('versioning') or ''
        assert versioning.startswith('regex:'), f'{found.group("currentValue")} names no regex versioning'
        assert _as_python_spells_it(versioning.removeprefix('regex:')).fullmatch(found.group('currentValue')), (
            versioning
        )
