"""@exposed decorator for kernel entity methods.

Marks a method for automatic API/CLI route generation.
Domain entities use capability activations instead of @exposed.
"""

import functools


def exposed(func=None, *, name: str = None):
    """Mark a kernel entity method for API/CLI exposure.

    Usage:
        @exposed
        async def rotate_credentials(self):
            ...

        @exposed(name="custom-name")
        async def my_method(self):
            ...
    """

    def decorator(fn):
        fn._exposed = True
        fn._exposed_name = name or fn.__name__

        @functools.wraps(fn)
        async def wrapper(self, *args, **kwargs):
            return await fn(self, *args, **kwargs)

        wrapper._exposed = True
        wrapper._exposed_name = fn._exposed_name
        return wrapper

    if func is not None:
        return decorator(func)
    return decorator
