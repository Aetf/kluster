"""A `systemctl` a converger can call, and a case can read back.

The device's boot-chain scripts are run by the suites, against temporary trees,
and every one of them asks systemd something: whether a unit is enabled or
running, to enable, start, stop or restart one, or restart one only if it runs,
to read its unit files again. This
is the one stand-in all of them run against, so a converger's conditions are
reachable — a unit the case left disabled, stopped, or failing is answered as
such, and whatever the script does about it shows in the state afterwards.

It keeps what systemd keeps, and no more than the convergers and the health
reading ask about:

-   **units**, as the unit files in the directories it is given plus the ones a
    case ships as the firmware's own; an instance `a@b.service` is found
    through its template `a@.service`, and the drop-ins beside either apply;
-   **enabled and active state**, per unit;
-   **what a unit was started on** — the unit file and drop-ins systemd had
    loaded when it started it, and the files a shipped unit's process reads at
    start, a directory standing for every file in it. systemd reads a unit on
    first use and reads every loaded unit again at `daemon-reload`, which a
    successful `enable` or `disable` also runs, so a start after a unit file or
    drop-in changed but before a reload runs on the old definition, as it does
    on the device;
-   **every invocation**, in order, per program;
-   **the links networkd reports**, with the setup state a case gives each,
    and **the configuration files systemd's tools assemble**, from the
    directories a case gives as the configuration search path.

It refuses what the real one refuses, with the exit status the device's
systemd gives and the substance of its message: a unit with no unit file to
enable, disable, start, stop or ask the enablement of; a start whose unit fails. A
unit with no `[Install]` section is `static` and is not enabled by asking. Of
what `systemctl` prints when it succeeds, it prints what reaches a boot log:
the links `enable` and `disable` make and remove, unless `--quiet`, and the
warning a start on a stale definition gives — besides the state `is-active`
and `is-enabled` answer with. A verb or option no converger uses is refused as
one the stand-in does not model, rather than answered as if it had done
something.

Beside `systemctl` it answers the two other systemd tools the health reading
asks, each for the one question it asks: `networkctl list`, from the links a
case set, and `systemd-analyze cat-config`, from the files on disk, assembled
the way systemd assembles them. Neither changes anything, and each refuses
what it does not model, as `systemctl` does.

The same module is the executable: each tool in `tools` is a stub that hands
its arguments to the function below of its name, with the state directory
baked in. It runs once per call, so this module imports nothing beyond `os`
and `sys`.
"""

from __future__ import annotations

import os
import sys

#: A unit file systemd would enable when asked: it says what wants it.
INSTALLABLE = '[Install]\nWantedBy=multi-user.target\n'

_CALLS = 'calls'
#: What `networkctl list` reports, one link per line.
_LINKS = 'links'
_ENABLED = 'enabled'
_ACTIVE = 'active'
_FAILING = 'failing'
#: The definition systemd holds for a unit, read on first use and read again at `daemon-reload`.
_LOADED = 'loaded'
#: The definition a unit was last started on, and what its process read.
_STARTED = 'started'
#: The files a shipped unit's process reads when it starts, one path per line.
_READS = 'reads'
#: Where a case ships the firmware's own units: lowest in the search path, as
#: the vendor directory is on a device.
_VENDOR = 'vendor'
_KINDS = (_ENABLED, _ACTIVE, _FAILING, _LOADED, _STARTED, _READS)

#: What a unit's definition is serialized as between runs: fields joined by a
#: character no unit file holds.
_SEPARATOR = '\0'
#: What separates a start's definition from what its process read.
_PART = '\x1e'

#: What `is-active` exits with for a unit that is not running.
_INACTIVE = 3
#: What `start`, `stop`, `restart` and `try-restart` exit with for a unit systemd has no file for.
_NOT_FOUND = 5

#: The programs a case can read the invocations of, each with its own log.
SYSTEMCTL = 'systemctl'
NETWORKCTL = 'networkctl'
SYSTEMD_ANALYZE = 'systemd-analyze'
_PROGRAMS = (SYSTEMCTL, NETWORKCTL, SYSTEMD_ANALYZE)

_OPTIONS = {
    'is-active': {'--quiet'},
    'is-enabled': {'--quiet'},
    'enable': {'--quiet'},
    'disable': {'--quiet', '--now'},
}


def _names(unit: str) -> tuple[str, ...]:
    """The unit file names a unit is found under, most specific first."""
    stem, dot, suffix = unit.rpartition('.')
    prefix, at, instance = stem.partition('@')
    if dot and at and instance:
        return (unit, f'{prefix}@.{suffix}')
    return (unit,)


def _unit_file(search: tuple[str, ...], unit: str) -> str | None:
    """The file systemd would load a unit from, or nothing if it knows no such unit."""
    for name in _names(unit):
        for directory in search:
            candidate = os.path.join(directory, name)
            if os.path.isfile(candidate):
                return candidate
    return None


def _read(path: str) -> str:
    with open(path, encoding='utf-8') as source:
        return source.read()


def _definition(search: tuple[str, ...], unit_file: str, unit: str) -> str:
    """The unit file and every drop-in that applies, as systemd would read them now.

    Drop-in directories are gathered directory by directory, the instance's
    before the template's within each, and a drop-in's name is taken from the
    first directory that has it — so one earlier in the search path hides one
    later, and within a directory the instance's hides the template's.
    """
    fields = [_read(unit_file)]
    seen: set[str] = set()
    for directory in search:
        for name in _names(unit):
            dropins = os.path.join(directory, f'{name}.d')
            if not os.path.isdir(dropins):
                continue
            for conf in sorted(os.listdir(dropins)):
                path = os.path.join(dropins, conf)
                if conf in seen or not conf.endswith('.conf') or not os.path.isfile(path):
                    continue
                seen.add(conf)
                fields += [conf, _read(path)]
    return _SEPARATOR.join(fields)


def _flag(state: str, kind: str, unit: str) -> str:
    return os.path.join(state, kind, unit)


def _is(state: str, kind: str, unit: str) -> bool:
    return os.path.exists(_flag(state, kind, unit))


def _set(state: str, kind: str, unit: str, on: bool) -> None:
    flag = _flag(state, kind, unit)
    if on:
        with open(flag, 'w', encoding='utf-8'):
            pass
    elif os.path.exists(flag):
        os.unlink(flag)


def _write(path: str, content: str) -> None:
    with open(path, 'w', encoding='utf-8') as sink:
        sink.write(content)


def _load(state: str, search: tuple[str, ...], unit_file: str, unit: str) -> str:
    """The definition systemd holds for a unit, reading it now if it holds none."""
    loaded = _flag(state, _LOADED, unit)
    if not os.path.exists(loaded):
        _write(loaded, _definition(search, unit_file, unit))
    return _read(loaded)


def _reload(state: str, search: tuple[str, ...]) -> None:
    """`daemon-reload`: every loaded unit is read again now, and one whose file went is dropped."""
    for unit in os.listdir(os.path.join(state, _LOADED)):
        unit_file = _unit_file(search, unit)
        if unit_file is None:
            os.unlink(_flag(state, _LOADED, unit))
        else:
            _write(_flag(state, _LOADED, unit), _definition(search, unit_file, unit))


def _start(state: str, search: tuple[str, ...], unit_file: str, unit: str) -> None:
    """Record the unit as running on what systemd holds for it, and on what its process read."""
    definition = _load(state, search, unit_file, unit)
    fields: list[str] = []
    reads = _flag(state, _READS, unit)
    for path in _read(reads).splitlines() if os.path.exists(reads) else []:
        paths = [os.path.join(path, name) for name in sorted(os.listdir(path))] if os.path.isdir(path) else [path]
        for read in paths:
            if os.path.isfile(read):
                fields += [read, _read(read)]
    _set(state, _ACTIVE, unit, True)
    _write(_flag(state, _STARTED, unit), definition + _PART + _SEPARATOR.join(fields))


def _links(search: tuple[str, ...], unit_file: str, unit: str) -> list[str]:
    """The `.wants` links enabling a unit makes, one per target its `[Install]` names."""
    targets = [
        target
        for line in _read(unit_file).splitlines()
        if line.startswith('WantedBy=')
        for target in line.removeprefix('WantedBy=').split()
    ]
    return [os.path.join(search[0], f'{target}.wants', unit) for target in targets]


def _refuse(message: str, status: int = 1) -> int:
    print(message, file=sys.stderr)
    return status


def _record(state: str, program: str, argv: list[str]) -> None:
    with open(os.path.join(state, f'{_CALLS}-{program}'), 'a', encoding='utf-8') as calls:
        calls.write(' '.join(argv) + '\n')


def systemctl(state: str, search: tuple[str, ...], argv: list[str]) -> int:
    """One invocation: what the real `systemctl` would do and exit with."""
    _record(state, SYSTEMCTL, argv)

    options = [argument for argument in argv if argument.startswith('-')]
    words = [argument for argument in argv if not argument.startswith('-')]
    verb, units = (words[0], words[1:]) if words else ('', [])
    for option in options:
        if option not in _OPTIONS.get(verb, set[str]()):
            return _refuse(f'fake systemctl: {option} is not modeled for {verb or "no verb"}')

    if verb == 'daemon-reload':
        _reload(state, search)
        return 0
    if verb not in {'is-active', 'is-enabled', 'enable', 'disable', 'start', 'stop', 'restart', 'try-restart'}:
        return _refuse(f'fake systemctl: the verb {verb!r} is not modeled')
    if len(units) != 1:
        return _refuse(f'fake systemctl: {verb} is modeled for one unit at a time, not {units}')
    unit = units[0]
    quiet = '--quiet' in options
    unit_file = _unit_file(search, unit)

    if verb == 'is-active':
        active = _is(state, _ACTIVE, unit)
        if not quiet:
            print('active' if active else 'inactive')
        return 0 if active else _INACTIVE

    if verb == 'is-enabled':
        if unit_file is None:
            return _refuse(f'Failed to get unit file state for {unit}: No such file or directory')
        static = '[Install]' not in _read(unit_file).splitlines()
        answer = 'static' if static else 'enabled' if _is(state, _ENABLED, unit) else 'disabled'
        if not quiet:
            print(answer)
        return 1 if answer == 'disabled' else 0

    if verb in {'enable', 'disable'}:
        if unit_file is None:
            return _refuse(f'Failed to {verb} unit: Unit file {unit} does not exist.')
        links = _links(search, unit_file, unit)
        changed = bool(links) and _is(state, _ENABLED, unit) != (verb == 'enable')
        _set(state, _ENABLED, unit, bool(links) and verb == 'enable')
        if changed and not quiet:
            for link in links:
                print(
                    f'Created symlink {link} -> {unit_file}.' if verb == 'enable' else f'Removed {link}.',
                    file=sys.stderr,
                )
        _reload(state, search)
        if verb == 'enable' and not links:
            print(
                'The unit files have no installation config (WantedBy=, RequiredBy=, Also=, Alias= settings in the '
                '[Install] section, and DefaultInstance= for template units). This means they are not meant to be '
                'enabled using systemctl.',
                file=sys.stderr,
            )
        if '--now' in options:
            _set(state, _ACTIVE, unit, False)
        return 0

    if verb == 'stop':
        # Stopping a unit that is not running is not an error; naming one
        # systemd has no file for is.
        if unit_file is None:
            return _refuse(f'Failed to stop {unit}: Unit {unit} not loaded.', _NOT_FOUND)
        _set(state, _ACTIVE, unit, False)
        return 0

    # start, restart and try-restart
    if unit_file is None:
        return _refuse(f'Failed to {verb} {unit}: Unit {unit} not found.', _NOT_FOUND)
    if _is(state, _LOADED, unit) and _load(state, search, unit_file, unit) != _definition(search, unit_file, unit):
        print(
            f'Warning: The unit file, source configuration file or drop-ins of {unit} changed on disk. '
            "Run 'systemctl daemon-reload' to reload units.",
            file=sys.stderr,
        )
    if verb == 'start' and _is(state, _ACTIVE, unit):
        return 0
    # Restart if running, and otherwise nothing: a unit that is not running is
    # not started by it.
    if verb == 'try-restart' and not _is(state, _ACTIVE, unit):
        return 0
    if _is(state, _FAILING, unit):
        _set(state, _ACTIVE, unit, False)
        return _refuse(
            f'Job for {unit} failed because the control process exited with error code.\n'
            f'See "systemctl status {unit}" and "journalctl -xe" for details.'
        )
    _start(state, search, unit_file, unit)
    return 0


def networkctl(state: str, argv: list[str]) -> int:
    """`networkctl list`: every link a case set, with the setup state it gave.

    Its columns are networkctl's, `IDX LINK TYPE OPERATIONAL SETUP`, with the
    header and the count line it prints around them unless `--no-legend`.
    """
    _record(state, NETWORKCTL, argv)
    options = [argument for argument in argv if argument.startswith('-')]
    words = [argument for argument in argv if not argument.startswith('-')]
    for option in options:
        if option not in {'--no-legend', '--no-pager'}:
            return _refuse(f'fake networkctl: {option} is not modeled')
    if words != ['list']:
        return _refuse(f'fake networkctl: {" ".join(words) or "no verb"} is not modeled; only list is')
    path = os.path.join(state, _LINKS)
    links = [line.split() for line in _read(path).splitlines()] if os.path.exists(path) else []
    legend = '--no-legend' not in options
    if legend:
        print('IDX LINK             TYPE     OPERATIONAL SETUP')
    for index, (name, kind, operational, setup) in enumerate(links, start=1):
        print(f'{index:>3} {name:<16} {kind:<8} {operational:<11} {setup}')
    if legend:
        print(f'\n{len(links)} links listed.')
    return 0


def systemd_analyze(config_path: tuple[str, ...], argv: list[str], state: str) -> int:
    """`systemd-analyze cat-config <name>`: the main file and its drop-ins, as systemd assembles them.

    `config_path` is the configuration search path, most specific first, as
    `/etc`, `/run` and `/usr/lib` are on a device. The main file is the first
    of the path that holds one; the drop-ins are every `.conf` in a `<name>.d`
    of any of them, one of a name taken from the first directory that has it,
    in name order. Each file is printed under a `# <path>` line, with an empty
    line between files, and a missing main file is said to be missing.
    """
    _record(state, SYSTEMD_ANALYZE, argv)
    options = [argument for argument in argv if argument.startswith('-')]
    words = [argument for argument in argv if not argument.startswith('-')]
    for option in options:
        if option != '--no-pager':
            return _refuse(f'fake systemd-analyze: {option} is not modeled')
    if len(words) != 2 or words[0] != 'cat-config':
        return _refuse(
            f'fake systemd-analyze: {" ".join(words) or "no verb"} is not modeled; only cat-config <name> is'
        )
    name = words[1]
    main = next(
        (os.path.join(root, name) for root in config_path if os.path.exists(os.path.join(root, name))),
        None,
    )
    dropins: dict[str, str] = {}
    for root in config_path:
        directory = os.path.join(root, f'{name}.d')
        if not os.path.isdir(directory):
            continue
        for conf in os.listdir(directory):
            path = os.path.join(directory, conf)
            if conf.endswith('.conf') and conf not in dropins and os.path.isfile(path):
                dropins[conf] = path
    if main is None:
        print(f'# Main configuration file {name} not found')
    files: list[str] = [] if main is None else [main]
    files += [dropins[conf] for conf in sorted(dropins)]
    for position, path in enumerate(files):
        print(f'{"" if position == 0 else chr(10)}# {path}')
        for line in _read(path).splitlines():
            print(line)
    return 0


class Definition:
    """What a unit was started on: its unit file, its drop-ins by name, and what its process read by path."""

    def __init__(self, serialized: str) -> None:
        definition, read = serialized.split(_PART)
        unit, *dropins = definition.split(_SEPARATOR)
        files = read.split(_SEPARATOR) if read else []
        self.unit: str = unit
        self.dropins: dict[str, str] = dict(zip(dropins[::2], dropins[1::2], strict=True))
        self.reads: dict[str, str] = dict(zip(files[::2], files[1::2], strict=True))


class FakeSystemd:
    """The stand-in, as a case sets it up and reads it back.

    `unit_path` is where the unit files a converger writes live, searched in
    order; the firmware's own units follow, which a case ships with `ship`.
    `config_path` is where `systemd-analyze cat-config` looks, most specific
    first. Prepending `tools` to `PATH` is what makes the script under test
    find these tools rather than the host's.
    """

    def __init__(
        self,
        root: os.PathLike[str],
        *,
        unit_path: tuple[os.PathLike[str], ...] = (),
        config_path: tuple[os.PathLike[str], ...] = (),
    ) -> None:
        import shlex  # the executable imports this module once per call and never needs it

        self._root = os.fspath(root)
        self._state = os.path.join(self._root, 'state')
        self._vendor = os.path.join(self._root, _VENDOR)
        self.tools: str = os.path.join(self._root, 'tools')
        for directory in (self.tools, self._vendor, *(os.path.join(self._state, kind) for kind in _KINDS)):
            os.makedirs(directory)
        self._search = (*(os.fspath(directory) for directory in unit_path), self._vendor)

        # A shell stub rather than a shebang naming the interpreter: the kernel
        # splits a shebang line at spaces and truncates it, and the path of
        # the interpreter running this suite is not ours to constrain.
        config = tuple(os.fspath(directory) for directory in config_path)
        calls = {
            SYSTEMCTL: f'systemctl({self._state!r}, {self._search!r}, sys.argv[1:])',
            NETWORKCTL: f'networkctl({self._state!r}, sys.argv[1:])',
            SYSTEMD_ANALYZE: f'systemd_analyze({config!r}, sys.argv[1:], {self._state!r})',
        }
        for tool, call in calls.items():
            program = (
                f'import sys; sys.path.insert(0, {os.path.dirname(os.path.abspath(__file__))!r}); import fake_systemd; '
                f'sys.exit(fake_systemd.{call})'
            )
            stub = os.path.join(self.tools, tool)
            _write(stub, f'#!/bin/sh\nexec {shlex.quote(sys.executable)} -IS -c {shlex.quote(program)} "$@"\n')
            os.chmod(stub, 0o755)

    def ship(self, unit: str, content: str = INSTALLABLE, *, reads: tuple[os.PathLike[str], ...] = ()) -> None:
        """A unit the firmware carries, which no converger writes, and the files its process reads at start."""
        _write(os.path.join(self._vendor, unit), content)
        _write(_flag(self._state, _READS, unit), ''.join(f'{os.fspath(path)}\n' for path in reads))

    def _known(self, unit: str) -> str:
        unit_file = _unit_file(self._search, unit)
        if unit_file is None:
            raise ValueError(f'{unit} has no unit file for this systemd to know it by')
        return unit_file

    def enable(self, unit: str) -> None:
        """The unit was enabled before the case began."""
        _ = self._known(unit)
        _set(self._state, _ENABLED, unit, True)

    def activate(self, unit: str) -> None:
        """The unit was running before the case began, on what its files say now."""
        _start(self._state, self._search, self._known(unit), unit)

    def stop(self, unit: str) -> None:
        """The unit went down outside the converger: it exited, or somebody stopped it."""
        _set(self._state, _ACTIVE, unit, False)

    def link(self, name: str, *, kind: str = 'ether', operational: str = 'routable', setup: str = 'unmanaged') -> None:
        """A link the kernel has, as `networkctl list` reports it; set again, it replaces what it said."""
        path = os.path.join(self._state, _LINKS)
        lines = [line for line in (_read(path).splitlines() if os.path.exists(path) else []) if line.split()[0] != name]
        _write(path, ''.join(f'{line}\n' for line in [*lines, f'{name} {kind} {operational} {setup}']))

    def failing(self, unit: str, fails: bool = True) -> None:
        """Whether starting the unit fails, as a unit whose process exits non-zero does."""
        _set(self._state, _FAILING, unit, fails)

    @property
    def enabled(self) -> frozenset[str]:
        return frozenset(os.listdir(os.path.join(self._state, _ENABLED)))

    @property
    def active(self) -> frozenset[str]:
        return frozenset(os.listdir(os.path.join(self._state, _ACTIVE)))

    def started_on(self, unit: str) -> Definition | None:
        """What the unit was last started on, or nothing if it never was."""
        started = _flag(self._state, _STARTED, unit)
        return Definition(_read(started)) if os.path.exists(started) else None

    def take_calls(self, program: str = SYSTEMCTL) -> list[str]:
        """Every invocation of `program` since the last take, as its arguments joined by spaces."""
        if program not in _PROGRAMS:
            raise ValueError(f'{program} is not a tool this stand-in provides')
        path = os.path.join(self._state, f'{_CALLS}-{program}')
        if not os.path.exists(path):
            return []
        calls = _read(path).splitlines()
        os.unlink(path)
        return calls
