"""Operation-scoped cancellation, interruptible mutexes and background waits."""

from concurrent.futures import Future
from contextlib import contextmanager
from threading import Event, RLock, Thread
from typing import Callable, Generic, Iterator, Optional, Protocol, TypeVar

from cantok import AbstractToken

T = TypeVar('T')
POLL_INTERVAL = 0.01


class StoppedError(RuntimeError):
    def __init__(self) -> None:
        super().__init__('The WASM isolate has been destroyed.')


class Mutex(Protocol):
    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool: ...  # pragma: no cover -- typing-only signature
    def release(self) -> None: ...  # pragma: no cover -- typing-only signature


class Background(Generic[T]):
    """Only use for work that cannot mutate a live isolate after cancellation."""

    def __init__(self, function: Callable[[], T]) -> None:
        self.done = Event()
        self.result: Future[T] = Future()

        def work() -> None:
            try:
                self.result.set_result(function())
            except BaseException as exception:  # noqa: BLE001 -- transfer to the waiting caller
                self.result.set_exception(exception)
            finally:
                self.done.set()

        Thread(target=work, name='throng-wasi-preparation', daemon=True).start()


class Cancellation:
    """Latch the first cancellation or token error for the whole operation."""

    def __init__(self, token: AbstractToken, stopped: Optional[Event] = None) -> None:
        self.token, self.stopped = token, stopped
        self.error: Optional[BaseException] = None
        self._lock = RLock()

    def check(self) -> None:
        with self._lock:
            if self.error is not None:
                raise self.error
            try:
                self.token.check()
                if self.stopped is not None and self.stopped.is_set():
                    raise StoppedError
            except BaseException as exception:
                self.error = exception
                raise

    @contextmanager
    def hold(self, lock: Mutex) -> Iterator[None]:
        self.check()
        while not lock.acquire(timeout=POLL_INTERVAL):
            self.check()
        try:
            self.check()
            yield
        finally:
            lock.release()

    def wait(self, task: Background[T]) -> T:
        self.check()
        while not task.done.wait(POLL_INTERVAL):
            self.check()
        self.check()
        return task.result.result()

    def call(self, function: Callable[[], T]) -> T:
        self.check()
        return self.wait(Background(function))
