from contextlib import ExitStack


class ScopeBase:
    """Shared sync/async context-manager plumbing: subclasses implement _enter and _exit."""

    def __init__(self) -> None:
        self._stack = ExitStack()

    def _enter(self):
        raise NotImplementedError

    def _exit(self, exc: BaseException | None) -> None:
        raise NotImplementedError

    def __enter__(self):
        return self._enter()

    def __exit__(self, exc_type, exc, tb) -> bool:
        try:
            self._exit(exc)
        finally:
            self._stack.__exit__(exc_type, exc, tb)
        return False

    async def __aenter__(self):
        return self._enter()

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return self.__exit__(exc_type, exc, tb)
