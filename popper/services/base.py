"""Explicit compatibility adapter for the shared HTTP host and injected dependencies."""
import sys

class HostService:
    def __init__(self, host):
        object.__setattr__(self, "_host", host)

    @property
    def dependencies(self):
        return sys.modules["popper.server"]

    def dispatch(self, name, *args, **kwargs):
        member = object.__getattribute__(self, name)
        return member(*args, **kwargs) if callable(member) else member

    def __getattribute__(self, name):
        if name in {"_host", "dispatch", "dependencies"} or name.startswith("__"):
            return object.__getattribute__(self, name)
        host = object.__getattribute__(self, "_host")
        try:
            return getattr(host, name)
        except AttributeError:
            return object.__getattribute__(self, name)

    def __setattr__(self, name, value):
        setattr(self._host, name, value)
