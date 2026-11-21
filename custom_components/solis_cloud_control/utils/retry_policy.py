import asyncio
import logging
import time
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

_LOGGER = logging.getLogger(__name__)


class MonotonicTimeProvider(Protocol):
    def __call__(self) -> float: ...


class SleepFunction(Protocol):
    def __call__(self, delay: float) -> Awaitable[None]: ...


class RetryPolicy[ErrorT: Exception]:
    def __init__(
        self,
        retryable_exception: type[ErrorT],
        initial_delay_seconds: float = 1.0,
        delay_multiplier: float = 2.0,
        monotonic_time: MonotonicTimeProvider = time.monotonic,
        sleep: SleepFunction = asyncio.sleep,
    ) -> None:
        self._initial_delay_seconds = initial_delay_seconds
        self._delay_multiplier = delay_multiplier
        self._retryable_exception = retryable_exception
        self._monotonic_time = monotonic_time
        self._sleep = sleep

    async def __call__[ResultT](
        self,
        operation_closure: Callable[[], Awaitable[ResultT]],
        max_retry_time: float,
        *,
        retry_delay: Callable[[ErrorT], float | None] | None = None,
    ) -> ResultT:
        start_time = self._monotonic_time()
        delay = self._initial_delay_seconds

        attempt = 0

        while True:
            try:
                return await operation_closure()
            except self._retryable_exception as err:
                elapsed_time = self._monotonic_time() - start_time

                if elapsed_time >= max_retry_time:
                    raise err

                minimum_delay = retry_delay(err) if retry_delay is not None else 0.0
                if minimum_delay is None or minimum_delay >= max_retry_time - elapsed_time:
                    raise

                attempt += 1
                _LOGGER.warning(
                    "Retrying due to error: %s (attempt %d, elapsed time: %.1fs)",
                    str(err),
                    attempt,
                    elapsed_time,
                )

                await self._sleep(min(max(delay, minimum_delay), max_retry_time - elapsed_time))
                delay *= self._delay_multiplier
