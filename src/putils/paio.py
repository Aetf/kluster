"""
Pulumi/asyncio helpers.

Most of this just deals with annoying boilerplate (@task, @background).

`async_output` and `resolve` bridge native async/await with Pulumi outputs;
see docs/rfc-001-native-async-inputs.md.

From https://github.com/dingbots/putils/blob/master/putils/aws.py
"""

import asyncio
import contextvars
import functools
import inspect
import traceback
from typing import Any, Awaitable, Callable, ParamSpec, Self, TypeVar, overload, TypeAlias

import pulumi
import pulumi.runtime
from pulumi.output import contains_unknowns

__all__ = 'task', 'background', 'async_output', 'resolve', 'UnknownValueException'


T = TypeVar('T')


@overload
def mkfuture(val: Awaitable[T]) -> asyncio.Future[T]: ...


@overload
def mkfuture(val: T) -> asyncio.Future[T]: ...


def mkfuture(val) -> asyncio.Future[Any]:
    """
    Wrap the given value in a future (turn into a task).

    Intelligentally handles awaitables vs not.

    Note: Does not perform error handling for the task.
    """
    if inspect.isawaitable(val):
        return asyncio.ensure_future(val)
    else:
        f = asyncio.get_event_loop().create_future()
        f.set_result(val)
        return f


Nested: TypeAlias = T | Awaitable['Nested[T]']


async def unwrap(value: Nested[T]) -> T:
    """
    Resolve all the awaitables, returing a simple value.

    This is to make sure awaitables boxing awaitables get handled.
    This shouldn't happen in proper programs, but async can be hard.
    """
    # Implemented using recursive function to satisfy the typing system
    # See https://stackoverflow.com/a/77836491
    if isinstance(value, Awaitable):
        next_value = await value
        if __debug__ and inspect.isawaitable(next_value):
            pulumi.warn(f'Programming error: nested awaitables: {next_value}')
        return await unwrap(next_value)
    else:
        return value


Param = ParamSpec('Param')


def task(func: Callable[Param, Awaitable[T]]):
    """
    Decorator to turn coroutines into tasks.

    Will also log errors, so failures don't go unreported.
    """

    async def runner(*pargs, **kwargs):
        try:
            return await func(*pargs, **kwargs)
        except Exception:
            traceback.print_exc()
            pulumi.error(f'Error in {func}')
            raise

    @functools.wraps(func)
    def wrapper(*pargs, **kwargs):
        return asyncio.create_task(runner(*pargs, **kwargs))

    return wrapper


def background(func):
    """
    Turns a synchronous function into an async one by running it in a
    background thread.
    """

    @functools.wraps(func)
    def wrapper(*pargs, **kwargs):
        loop = asyncio.get_running_loop()
        return loop.run_in_executor(None, functools.partial(func, *pargs, **kwargs))

    return wrapper


def from_nothing() -> tuple[pulumi.Output, asyncio.Future[Any]]:
    fut = asyncio.Future()
    return pulumi.Output.from_input(fut), fut


class UnknownValueException(Exception):
    """
    Raised by `resolve` during preview when an awaited output is unknown.

    Aborts the enclosing `async_output` coroutine early; `async_output`
    catches it and marks its output as unknown. User code normally should
    not catch this.
    """


#: Resources awaited via `resolve` in the current task tree. `async_output`
#: installs a fresh set before running its coroutine. Tasks spawned inside
#: (including via asyncio.gather) inherit the same set instance, so in-place
#: mutations propagate back regardless of task nesting.
_current_dependencies: contextvars.ContextVar[set[pulumi.Resource] | None] = contextvars.ContextVar(
    '_current_dependencies', default=None
)


class _ResolvedOutputs:
    """Awaitable for one or more outputs. See `resolve`."""

    def __init__(self, outputs: tuple):
        self._outputs = outputs

    def __await__(self):
        # Record the resources behind every awaited output *before* waiting on
        # values, so dependencies are captured even if the wait aborts early
        # on an unknown during preview.
        deps = _current_dependencies.get()
        for out in self._outputs:
            if isinstance(out, pulumi.Output) and deps is not None:
                deps.update((yield from out.resources().__await__()))

        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()

        def on_resolved(values):
            def settle():
                if fut.done():
                    return
                if pulumi.runtime.is_dry_run() and contains_unknowns(values):
                    fut.set_exception(UnknownValueException())
                else:
                    fut.set_result(values[0] if len(self._outputs) == 1 else tuple(values))

            loop.call_soon_threadsafe(settle)

        # run_with_unknowns so the callback still fires during preview instead
        # of never resolving.
        pulumi.Output.all(*self._outputs).apply(on_resolved, run_with_unknowns=True)
        return (yield from fut.__await__())


def resolve(*outputs: pulumi.Output | Any):
    """
    Natively await one or more Pulumi outputs inside an `async_output` coroutine.

    Returns the plain value for a single argument, or a tuple of values for
    several:

        vpc_id = await resolve(vpc.id)
        vpc_id, subnet_id = await resolve(vpc.id, subnet.id)

    The resources behind the outputs are recorded as dependencies of the
    enclosing `async_output`. During preview, raises `UnknownValueException`
    if any awaited value is unknown.
    """
    if not outputs:
        raise TypeError('resolve() requires at least one output')
    return _ResolvedOutputs(outputs)


def async_output(fn: Callable[[], Awaitable[T]] | Awaitable[T]) -> pulumi.Output[T]:
    """
    Run a coroutine and expose its result as a `pulumi.Output`, usable
    directly as a resource input.

    The coroutine awaits other outputs via `resolve`, which records the
    resources behind them; the returned Output carries those as dependencies
    so the Pulumi DAG stays intact. During preview, if the coroutine hits an
    unknown value, the returned Output becomes unknown -- other inputs of the
    same resource are unaffected, preserving fine-grained diffs.

    Accepts either a coroutine function (called immediately) or a coroutine
    object.
    """

    async def run() -> tuple[set[pulumi.Resource], Any, bool]:
        deps: set[pulumi.Resource] = set()
        token = _current_dependencies.set(deps)
        try:
            value = await unwrap(fn() if callable(fn) else fn)
            return deps, value, True
        except UnknownValueException:
            if not pulumi.runtime.is_dry_run():
                # resolve() only aborts during preview; anything else is a bug.
                raise
            return deps, None, False
        except Exception:
            traceback.print_exc()
            pulumi.error(f'Error in async_output {fn}')
            raise
        finally:
            _current_dependencies.reset(token)

    result = asyncio.ensure_future(run())

    async def pick(index: int):
        return (await result)[index]

    return pulumi.Output(
        asyncio.ensure_future(pick(0)),  # resources
        asyncio.ensure_future(pick(1)),  # value
        asyncio.ensure_future(pick(2)),  # is_known
        mkfuture(False),  # is_secret
    )
