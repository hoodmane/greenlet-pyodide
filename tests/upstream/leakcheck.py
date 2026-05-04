# Stubbed leakcheck for the greenlet-pyodide port.
#
# Upstream's leakcheck pulls in psutil + objgraph and inspects the
# refcount of every Python object in the process across many repeated
# runs of each test. That isn't meaningful for our pure-Python port
# (and it doesn't even import in a Pyodide build), so we provide
# pass-through decorators here.

from functools import wraps


def wrap_refcount(method):
    @wraps(method)
    def wrapper(self, *a, **kw):
        return method(self, *a, **kw)
    return wrapper


def fails_leakcheck(method):
    return method


def ignores_leakcheck(method):
    return method
