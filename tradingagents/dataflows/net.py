"""HTTP helpers shared by the vendors.

Two things every offshore endpoint needs: a timeout that fails fast when the
host cannot be reached from here, and a memory of that failure so one blocked
API does not spend the same wait on every tool call of every analysis.
"""

import contextlib
import logging
import os
import socket
import ssl
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse

import requests

logger = logging.getLogger(__name__)

# Split connect/read timeouts. A connect that does not finish in a few seconds
# means the host is unreachable from this network (firewalled, or offshore and
# blocked); no read budget rescues that, while a flat 30s spends half a minute
# per call on requests that were never going to arrive. Keep the read budget
# generous enough for a slow-but-working endpoint.
CONNECT_TIMEOUT = 3.0
READ_TIMEOUT = 10.0
DEFAULT_TIMEOUT: tuple[float, float] = (CONNECT_TIMEOUT, READ_TIMEOUT)

# ``urlopen`` takes one socket timeout covering connect and read together.
SOCKET_TIMEOUT = 5.0

# Budget for code that opens a socket itself and never sets a timeout. The
# BaoStock SDK does a bare ``socket.socket()`` and then ``recv()`` in a loop, so
# a server that accepts the connection and then stalls holds the whole run for
# as long as the caller lets it: Python's default socket timeout is ``None``,
# meaning block forever. A socket copies the default when it is created, so
# ``bounded_sockets`` sets it around the exchange and restores it after, which
# ceilings that socket's connect and every read on it without touching the rest
# of the process. TRADINGAGENTS_RAW_SOCKET_TIMEOUT overrides the ceiling; 0
# hands the budget back to the caller for anyone who wants the old behavior.
RAW_SOCKET_TIMEOUT = 15.0
_RAW_SOCKET_ENV = "TRADINGAGENTS_RAW_SOCKET_TIMEOUT"

# How long a host that failed to connect stays parked. Long enough to cover a
# whole batch of analyses, short enough that a transient outage does not black
# the vendor for the life of a long-running process. TRADINGAGENTS_HOST_BLOCK_COOLDOWN
# overrides it in seconds; 0 turns the parking off for callers who would rather
# pay the fast failure on every call.
_BLOCK_COOLDOWN_SECONDS = 600.0
_BLOCK_COOLDOWN_ENV = "TRADINGAGENTS_HOST_BLOCK_COOLDOWN"

_blocked_until: dict[str, float] = {}
_lock = threading.Lock()


def raw_socket_timeout() -> float:
    """Seconds a self-managed socket may block, honoring the environment override."""
    raw = os.getenv(_RAW_SOCKET_ENV, "").strip()
    if not raw:
        return RAW_SOCKET_TIMEOUT
    try:
        return max(0.0, float(raw))
    except ValueError:
        logger.warning(
            "%s=%r is not a number of seconds; using %ds",
            _RAW_SOCKET_ENV, raw, RAW_SOCKET_TIMEOUT,
        )
        return RAW_SOCKET_TIMEOUT


@contextlib.contextmanager
def bounded_sockets(seconds: "float | None" = None):
    """Give sockets created inside the block a timeout instead of none.

    The SDKs this exists for build their socket themselves and read it in a
    loop, so a server that stops mid-response would never be given up on. The
    timeout is copied when the socket is created, which is why restoring the
    previous default on the way out is safe: a socket opened inside keeps its
    ceiling, and anything the process opens later is unaffected.
    """
    budget = raw_socket_timeout() if seconds is None else seconds
    previous = socket.getdefaulttimeout()
    socket.setdefaulttimeout(budget)
    try:
        yield
    finally:
        socket.setdefaulttimeout(previous)


def block_cooldown() -> float:
    """Seconds a host stays parked, honoring the environment override."""
    raw = os.getenv(_BLOCK_COOLDOWN_ENV, "").strip()
    if not raw:
        return _BLOCK_COOLDOWN_SECONDS
    try:
        return max(0.0, float(raw))
    except ValueError:
        logger.warning(
            "%s=%r is not a number of seconds; using %ds",
            _BLOCK_COOLDOWN_ENV, raw, _BLOCK_COOLDOWN_SECONDS,
        )
        return _BLOCK_COOLDOWN_SECONDS


def host_of(url: str) -> str:
    """The parking-lot key for a URL: its ``host[:port]``, not the whole path."""
    parsed = urlparse(url)
    return parsed.netloc or parsed.path.split("/")[0]


def is_host_blocked(url: str) -> bool:
    """Whether recent connect failures have parked this host (and not expired)."""
    key = host_of(url)
    with _lock:
        until = _blocked_until.get(key)
        if until is None:
            return False
        if time.monotonic() >= until:
            del _blocked_until[key]
            return False
        return True


def block_host(url: str, reason: str = "") -> None:
    """Park a host after a connect-level failure.

    Logged once per parking, not once per skipped call: the point is to stop
    repeating the same warning for every topic an agent asks about.
    """
    cooldown = block_cooldown()
    if cooldown <= 0:
        return
    key = host_of(url)
    with _lock:
        first = key not in _blocked_until
        _blocked_until[key] = time.monotonic() + cooldown
    if first:
        logger.warning(
            "%s unreachable (%s); skipping it for %s",
            key, reason or "connect failed", _minutes_text(cooldown),
        )


def clear_host_block(url: str) -> None:
    """Forget a parking once the host answers, so a recovery is used at once."""
    with _lock:
        _blocked_until.pop(host_of(url), None)


def reset_host_blocks() -> None:
    """Empty the parking lot (for tests and long-lived processes)."""
    with _lock:
        _blocked_until.clear()


def _minutes_text(seconds: float) -> str:
    """How to say the cooldown to a reader: whole minutes, else seconds."""
    minutes = seconds / 60
    if minutes >= 1 and abs(minutes - round(minutes)) < 1e-9:
        return f"{int(round(minutes))} minutes"
    if minutes >= 1:
        return f"{minutes:.1f} minutes"
    return f"{seconds:g} seconds"


def blocked_notice(url: str) -> str:
    """What to hand the agent instead of a request that cannot arrive."""
    key = host_of(url)
    return (
        f"{key} could not be reached from this network earlier in this run, so "
        f"it is skipped for {_minutes_text(block_cooldown())}. Report this data "
        f"as unavailable; do not retry it or fabricate values."
    )


def is_connect_level_failure(exc: BaseException) -> bool:
    """Whether a failure happened before the host could answer at all.

    Only that justifies parking a host: a refused connection, an unresolvable
    name, or a connection that never opened says nothing about the data asked
    for, while an HTTP status, a parse error, or a read that stalled on a live
    connection proves the host is reachable and must not park it.

    ``urlopen`` cannot separate a connect stall from a slow read (one socket
    timeout covers both), so a vendor using it may park on a merely slow reply;
    the cooldown and the short budget bound what that costs.
    """
    if isinstance(exc, HTTPError):
        return False
    if isinstance(exc, (requests.ConnectTimeout, requests.ConnectionError, URLError)):
        return True
    return isinstance(
        exc, (ConnectionError, TimeoutError, socket.gaierror, socket.herror, ssl.SSLError)
    )


def get_scrubbed(
    url: str, *, params: dict, timeout: float | tuple[float, float], secret: str, passthrough=()
):
    """``requests.get`` plus ``raise_for_status``, with ``secret`` kept out of errors.

    Vendors that authenticate with a query parameter put the key in the URL, and
    requests quotes the full URL in HTTP, connection and timeout errors, so any
    log or traceback that records one would carry the key (#1324). A requests
    error is re-raised as the same class with the key replaced and nothing
    attached: no request or response (both hold the URL) and no exception chain,
    which is why this raises after the ``except`` block rather than inside it.
    Statuses in ``passthrough`` are returned for the caller to handle.
    """
    try:
        response = requests.get(url, params=params, timeout=timeout)
        if response.status_code not in passthrough:
            response.raise_for_status()
        return response
    except requests.RequestException as exc:
        error = type(exc)(str(exc).replace(secret, "***")) if secret else exc
    raise error


def vendor_reachable(url: str, timeout: float = 5.0) -> bool:
    """Whether the vendor answers at all, for telling silence from an outage.

    A client that returns an empty result instead of raising leaves those two
    cases indistinguishable. Called only when a result is empty, and only for a
    host that is not parked: probing a blocked host would pay the wait back.
    """
    if is_host_blocked(url):
        return False
    try:
        requests.head(url, timeout=timeout, allow_redirects=True)
        clear_host_block(url)
        return True
    except requests.RequestException:
        return False
