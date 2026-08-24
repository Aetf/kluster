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
from typing import Any, Awaitable, Callable, ParamSpec, TypeAlias, TypeVar, cast

import pulumi
import pulumi.runtime
from pulumi.output import contains_unknowns

__all__ = 'task', 'background', 'async_output', 'resolve', 'UnknownValueException'


T = TypeVar('T')

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
        # The recursion is what the type system cannot follow: `await` on a
        # Nested[T] yields another Nested[T], which is only T at the bottom.
        next_value = cast('Nested[T]', await value)
        if __debug__ and inspect.isawaitable(next_value):
            pulumi.warn(f'Programming error: nested awaitables: {next_value}')
        return await unwrap(next_value)
    else:
        return value


def _log_error(what: object) -> None:
    """Log the current exception with traceback, so failures don't go unreported."""
    traceback.print_exc()
    pulumi.error(f'Error in {what}')


Param = ParamSpec('Param')


def task(func: Callable[Param, Awaitable[T]]) -> Callable[Param, 'asyncio.Task[T]']:
    """
    Decorator to turn coroutines into tasks.

    Will also log errors, so failures don't go unreported.
    """

    async def runner(*pargs: Param.args, **kwargs: Param.kwargs) -> T:
        try:
            return await func(*pargs, **kwargs)
        except Exception:
            _log_error(func)
            raise

    @functools.wraps(func)
    def wrapper(*pargs: Param.args, **kwargs: Param.kwargs) -> 'asyncio.Task[T]':
        return asyncio.create_task(runner(*pargs, **kwargs))

    return wrapper


def background(func: Callable[Param, T]) -> Callable[Param, Awaitable[T]]:
    """
    Turns a synchronous function into an async one by running it in a
    background thread.
    """

    @functools.wraps(func)
    def wrapper(*pargs: Param.args, **kwargs: Param.kwargs) -> Awaitable[T]:
        loop = asyncio.get_running_loop()
        return loop.run_in_executor(None, functools.partial(func, *pargs, **kwargs))

    return wrapper


class UnknownValueException(Exception):
    """
    Raised by `resolve` during preview when an awaited output is unknown.

    Aborts the enclosing `async_output` coroutine early; `async_output`
    catches it and marks its output as unknown. User code normally should
    not catch this.
    """


class _AsyncOutputCtx:
    """Per-`async_output` accumulator for what `resolve` observed."""

    __slots__ = ('deps', 'secret')

    def __init__(self) -> None:
        self.deps: set[pulumi.Resource] = set()
        self.secret: bool = False


#: Context of the enclosing `async_output` coroutine. `async_output` installs
#: a fresh instance before running its coroutine. Tasks spawned inside
#: (including via asyncio.gather) inherit the same instance, so in-place
#: mutations propagate back regardless of task nesting.
_async_output_ctx: contextvars.ContextVar[_AsyncOutputCtx | None] = contextvars.ContextVar(
    '_async_output_ctx', default=None
)


def resolve(*outputs: 'pulumi.Output[Any] | Any') -> Awaitable[Any]:
    """
    Natively await one or more Pulumi outputs inside an `async_output` coroutine.

    Returns the plain value for a single argument, or a tuple of values for
    several:

        vpc_id = await resolve(vpc.id)
        vpc_id, subnet_id = await resolve(vpc.id, subnet.id)

    The resources behind the outputs are recorded as dependencies of the
    enclosing `async_output`, and their secretness propagates to it. During
    preview, raises `UnknownValueException` if any awaited value is unknown.

    Only valid inside an `async_output` coroutine. Anywhere else it raises
    `RuntimeError`: dependencies would be silently dropped, and an unknown
    during preview would crash the program instead of degrading to an
    unknown output.
    """
    if not outputs:
        raise TypeError('resolve() requires at least one output')
    return _resolve(outputs)


async def _resolve(outputs: tuple[Any, ...]) -> Any:
    ctx = _async_output_ctx.get()
    if ctx is None:
        raise RuntimeError(
            'resolve() must be awaited inside an async_output coroutine; '
            'outside one, dependency tracking and preview unknown-handling cannot work'
        )

    outs = [pulumi.Output.from_input(o) for o in outputs]
    # Record dependencies and secretness *before* waiting on values, so they
    # are captured even if the wait aborts on an unknown during preview.
    for resources in await asyncio.gather(*(o.resources() for o in outs)):
        ctx.deps.update(resources)
    ctx.secret = ctx.secret or any(await asyncio.gather(*(o.is_secret() for o in outs)))

    # Exceptions from upstream outputs (e.g. a failed resource registration)
    # propagate naturally out of these awaits.
    values = await asyncio.gather(*(o.future(with_unknowns=True) for o in outs))
    if pulumi.runtime.is_dry_run() and (
        contains_unknowns(values) or not all(await asyncio.gather(*(o.is_known() for o in outs)))
    ):
        raise UnknownValueException()
    return values[0] if len(outputs) == 1 else tuple(values)


def async_output(fn: Callable[[], Awaitable[T]] | Awaitable[T]) -> pulumi.Output[T]:
    """
    Run a coroutine and expose its result as a `pulumi.Output`, usable
    directly as a resource input.

    The coroutine awaits other outputs via `resolve`, which records the
    resources behind them; the returned Output carries those as dependencies
    so the Pulumi DAG stays intact, and is marked secret if any resolved
    output was secret. During preview, if the coroutine hits an unknown
    value, the returned Output becomes unknown -- other inputs of the same
    resource are unaffected, preserving fine-grained diffs.

    Accepts either a coroutine function (called immediately) or a coroutine
    object.
    """

    async def run() -> tuple[set[pulumi.Resource], Any, bool, bool]:
        ctx = _AsyncOutputCtx()
        token = _async_output_ctx.set(ctx)
        try:
            value = await unwrap(fn() if callable(fn) else fn)
            return ctx.deps, value, True, ctx.secret
        except UnknownValueException:
            if not pulumi.runtime.is_dry_run():
                # resolve() only aborts during preview; anything else is a bug.
                raise
            return ctx.deps, None, False, ctx.secret
        except Exception:
            _log_error(f'async_output {fn}')
            raise
        finally:
            _async_output_ctx.reset(token)

    result = asyncio.ensure_future(run())

    async def pick(index: int) -> Any:
        return (await result)[index]

    # Output's constructor is untyped in the SDK; the coroutines above supply
    # exactly the four futures it expects.
    return cast(
        'pulumi.Output[T]',
        pulumi.Output(
            asyncio.ensure_future(pick(0)),  # resources
            asyncio.ensure_future(pick(1)),  # value
            asyncio.ensure_future(pick(2)),  # is_known
            asyncio.ensure_future(pick(3)),  # is_secret
        ),
    )
