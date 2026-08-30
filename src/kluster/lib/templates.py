"""The one way a rendered configuration file reaches the program that reads it.

Another program's configuration language belongs in a file beside the module
that declares it, not in a Python string literal (`docs/style/python.md`), and
this is the mechanism that brings such a file back
(`docs/rfc/rfc-002-src-layout-and-the-gateway.md` §9.1). It works on
directories as well as on single files, because a directory is the shape the
callers after the first ones need: an application's configuration is a tree that
becomes a config map or the plaintext half of a sealed secret.

**The `.j2` suffix decides, and the suffix is stripped from the key.** A file
named `Caddyfile.j2` is rendered and lands under `Caddyfile`; a file named
`dashboard.json` is copied through byte for byte and lands under its own name.
So a directory holding both kinds needs one call and no globs, and a file that
must keep literal `{{ … }}` — a Grafana dashboard, a Go template some controller
renders later — is safe by construction rather than by the caller remembering
not to pass parameters.

Parameters are a frozen dataclass, which is what puts a template's inputs in a
signature instead of in a bag of names, and a template that reads a name the
caller did not supply is an error at render time rather than an empty line in a
configuration file. Files are found through `importlib.resources`, relative to
the package that owns them, so a template resolves the same from a checkout and
from an installed wheel.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import fields, is_dataclass
from importlib import resources
from importlib.resources.abc import Traversable

from jinja2 import Environment, StrictUndefined

__all__ = ('SUFFIX', 'load', 'render', 'render_tree')

#: What marks a file as rendered rather than copied, and what is taken off the
#: key it lands under.
SUFFIX = '.j2'

#: Fixed for the repository, so that a template behaves the same wherever it is
#: read from. `StrictUndefined` turns a forgotten parameter into a failure;
#: `keep_trailing_newline` keeps a file ending the way the editor left it, which
#: for a unit file or a shell script is part of its content; escaping is off
#: because nothing rendered here is HTML.
_ENVIRONMENT = Environment(
    undefined=StrictUndefined,
    keep_trailing_newline=True,
    autoescape=False,
)


def load(package: str, name: str) -> str:
    """One file inside `package`, exactly as it is on disk.

    `name` is a path relative to the package, so a component's own templates
    are `templates/<file>`. Nothing is rendered: this is what a caller wants
    when the file is the artefact.
    """
    return _read(resources.files(package).joinpath(name))


def render(package: str, name: str, params: object | None = None) -> str:
    """One file inside `package`, rendered with `params`.

    The single-file case, for a caller that wants one string. The `.j2` suffix
    is not consulted here — the caller named the file, so rendering it is the
    request — and `name` carries whatever suffix the file has.
    """
    return _ENVIRONMENT.from_string(load(package, name)).render(_parameters(params))


def render_tree(package: str, directory: str, params: object | None = None) -> Mapping[str, str]:
    """One directory inside `package`, as `{relative path: contents}`.

    Every file below `directory` is included, keyed by its path relative to it;
    a `.j2` file is rendered with `params` and keyed without the suffix, and
    every other file is copied through unchanged.
    """
    parameters = _parameters(params)
    return dict(_walk(resources.files(package).joinpath(directory), '', parameters))


def _walk(node: Traversable, prefix: str, parameters: Mapping[str, object]) -> Iterator[tuple[str, str]]:
    """Every file under `node`, in name order, keyed by `prefix` plus its path."""
    for entry in sorted(node.iterdir(), key=lambda entry: entry.name):
        path = f'{prefix}{entry.name}'
        if entry.is_dir():
            yield from _walk(entry, f'{path}/', parameters)
        elif path.endswith(SUFFIX):
            yield path.removesuffix(SUFFIX), _ENVIRONMENT.from_string(_read(entry)).render(parameters)
        else:
            yield path, _read(entry)


def _read(node: Traversable) -> str:
    return node.read_text(encoding='utf-8')


def _parameters(params: object | None) -> Mapping[str, object]:
    """A parameter object as the names a template's expressions use.

    Shallow rather than `dataclasses.asdict`: a field is handed to the template
    as whatever the call site declared it to be, so a template may reach through
    a nested object's attributes, and a `Mapping` field is still a `Mapping`.
    """
    if params is None:
        return {}
    if not is_dataclass(params) or isinstance(params, type):
        raise TypeError(f'template parameters are an instance of a frozen dataclass, not {type(params).__name__}')
    return {field.name: getattr(params, field.name) for field in fields(params)}
