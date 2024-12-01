"""
Pulumi/asyncio helpers.

Most of this just deals with annoying boilerplate (@task, @background).

From https://github.com/dingbots/putils/blob/master/putils/aws.py
"""

import asyncio
import functools
import inspect
import traceback
from typing import Any, Awaitable, Callable, ParamSpec, Self, TypeVar, overload, TypeAlias

import pulumi

__all__ = 'task', 'background'


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
