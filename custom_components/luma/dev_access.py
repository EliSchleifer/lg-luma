"""Developer access (off by default): cloud development sessions SSH to the TV for a few hours,
only after the owner approves each request on their phone.

A session sends its dev token to `POST /api/luma/dev/request`; the token says which TV. Home
Assistant sends an actionable notification to the chosen phone. The action names hold a random
nonce only the phone sees, so nothing but that tap can approve. On approval Home Assistant makes
a new ed25519 key pair, adds the public key to the TV's /home/root/.ssh/authorized_keys over the
home network (see dev_tv.py) and hands the session, once, the private key, the tunnel hostname,
the Cloudflare Access service token and this entry's streaming search keys. Keys are removed on
release, at expiry (at most 8 hours) and by a sweep at start-up and every 15 minutes: Dropbear
has no expiry option, so the sweep is what enforces it.

The endpoints are registered only once some TV turns developer access on, and answer 404 for
every TV that has it off. asyncssh is installed only then, so other installations never get it.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import secrets
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.requirements import async_process_requirements

from . import dev_keys as keys
from .const import (
    CONF_DEV_ACCESS,
    CONF_DEV_CF_ID,
    CONF_DEV_CF_SECRET,
    CONF_DEV_KEY,
    CONF_DEV_PHONE,
    CONF_DEV_SSH_PORT,
    CONF_DEV_TOKEN_HASH,
    CONF_DEV_TUNNEL,
    CONF_MDBLIST_KEY,
    CONF_MOTN_KEY,
    CONF_TMDB_TOKEN,
    DEV_REQUIREMENTS,
    DEV_TOKEN_HEADER,
    DOMAIN,
)

if TYPE_CHECKING:
    from . import dev_tv

_LOGGER = logging.getLogger(__name__)
DATA = f"{DOMAIN}_dev_access"

DEFAULT_SSH_PORT = 22
# Access lasts at most this long; a session may ask for less.
MAX_TTL = 8 * 3600
MIN_TTL = 5 * 60
# An unanswered request expires after this long.
PENDING_TIMEOUT = 10 * 60
# Requests per TV per rolling hour, and how many may wait for an answer at once.
REQUESTS_PER_HOUR = 5
MAX_PENDING = 2
# Wrong tokens per rolling hour before the endpoints lock for an hour.
BAD_TOKENS_PER_HOUR = 10
LOCKOUT = 3600
# How long a result request may wait for the owner's answer.
MAX_WAIT = 30
SWEEP_INTERVAL = timedelta(minutes=15)

ACTION_APPROVE = "LUMA_DEV_APPROVE_"
ACTION_DENY = "LUMA_DEV_DENY_"
NOTIFICATION_EVENT = "mobile_app_notification_action"
NO_STORE = {"Cache-Control": "no-store"}

PENDING = "pending"
INSTALLING = "installing"
APPROVED = "approved"
DELIVERED = "delivered"
DENIED = "denied"
EXPIRED = "expired"
FAILED = "failed"
RELEASED = "released"
KEY_ON_TV = {APPROVED, DELIVERED}


class DevAccessError(Exception):
    """A call cannot be honored; `status` is the HTTP status to answer."""

    def __init__(self, status: int, code: str) -> None:
        super().__init__(code)
        self.status = status
        self.code = code


# ---- entry lifecycle -----------------------------------------------------------------------


def enabled(entry: ConfigEntry) -> bool:
    return bool(
        entry.options.get(CONF_DEV_ACCESS)
        and entry.options.get(CONF_DEV_PHONE)
        and entry.data.get(CONF_DEV_TOKEN_HASH)
        and entry.data.get(CONF_DEV_KEY)
    )


def _fingerprint(entry: ConfigEntry) -> tuple[Any, ...]:
    """What a running broker depends on: a change restarts it."""
    options = entry.options
    return (
        entry.data.get(CONF_HOST),
        entry.data.get(CONF_DEV_TOKEN_HASH),
        entry.data.get(CONF_DEV_KEY),
        options.get(CONF_DEV_PHONE),
        options.get(CONF_DEV_SSH_PORT),
    )


async def async_update(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Start, restart or stop this entry's broker to match its options. Other options (tunnel
    hostname, Cloudflare token, streaming keys) are read per approval and need no restart."""
    registry = _registry(hass)
    running = registry.brokers.get(entry.entry_id)
    if not enabled(entry):
        if running:
            await async_stop(hass, entry)
        return
    if running and running.fingerprint == _fingerprint(entry):
        return
    if running:
        await async_stop(hass, entry)
    if not await _requirements(hass):
        return
    broker = Broker(hass, entry)
    await broker.async_start()
    registry.brokers[entry.entry_id] = broker
    if not registry.views and hass.http is not None:
        for view in (RequestView, ResultView, ReleaseView):
            hass.http.register_view(view())
        registry.views = True
    _LOGGER.info("Developer access is on for %s", entry.title)


async def async_stop(hass: HomeAssistant, entry: ConfigEntry) -> None:
    broker = _registry(hass).brokers.pop(entry.entry_id, None)
    if broker is not None:
        await broker.async_stop()


async def _requirements(hass: HomeAssistant) -> bool:
    try:
        await async_process_requirements(hass, DOMAIN, DEV_REQUIREMENTS)
    except Exception as err:
        _LOGGER.error("Developer access needs asyncssh, which could not be installed: %s", err)
        return False
    return True


async def forget_host_key(hass: HomeAssistant, entry: ConfigEntry) -> None:
    store = _store(hass, entry)
    stored = await store.async_load() or {}
    stored["tv_host_key"] = None
    await store.async_save(stored)


def _store(hass: HomeAssistant, entry: ConfigEntry) -> Store[dict[str, Any]]:
    return Store(hass, 1, f"{DOMAIN}.dev_access.{entry.entry_id}")


@dataclass
class _Registry:
    brokers: dict[str, Broker] = field(default_factory=dict)
    bad_tokens: deque[float] = field(default_factory=deque)
    locked_until: float = 0.0
    views: bool = False


def _registry(hass: HomeAssistant) -> _Registry:
    if DATA not in hass.data:
        hass.data[DATA] = _Registry()
    return hass.data[DATA]


def _authorize(hass: HomeAssistant, token: str | None) -> Broker:
    """The broker whose TV this token belongs to."""
    registry = _registry(hass)
    if not registry.brokers:
        raise DevAccessError(404, "not_found")
    now = time.time()
    if now < registry.locked_until:
        raise DevAccessError(429, "locked")
    if token:
        digest = hashlib.sha256(token.encode()).hexdigest()
        for broker in registry.brokers.values():
            if secrets.compare_digest(digest, broker.entry.data[CONF_DEV_TOKEN_HASH]):
                return broker
    registry.bad_tokens.append(now)
    while registry.bad_tokens and registry.bad_tokens[0] < now - 3600:
        registry.bad_tokens.popleft()
    if len(registry.bad_tokens) >= BAD_TOKENS_PER_HOUR:
        registry.locked_until = now + LOCKOUT
        registry.bad_tokens.clear()
        _LOGGER.warning("Too many wrong developer access tokens: locked for an hour")
        for broker in registry.brokers.values():
            hass.async_create_task(
                broker.notify(
                    "Luma developer access locked",
                    "Too many requests with a wrong token. Requests are refused for an hour. "
                    "Make a new dev token in Luma's options if it leaked.",
                )
            )
    raise DevAccessError(401, "bad_token")


# ---- requests ------------------------------------------------------------------------------


@dataclass
class Request:
    id: str
    nonce: str
    reason: str
    ttl: int
    created: float
    state: str = PENDING
    expires_at: int | None = None
    payload: dict[str, Any] | None = None
    error: str | None = None
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    timer: CALLBACK_TYPE | None = None

    def set_state(self, state: str) -> None:
        self.state = state
        self.changed.set()
        self.changed = asyncio.Event()


def clean_reason(reason: Any) -> str:
    text = "".join(ch if ch.isprintable() else " " for ch in str(reason or ""))
    return " ".join(text.split())[:200] or "(no reason given)"


class Broker:
    """One TV's access requests and the session keys on it."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.fingerprint = _fingerprint(entry)
        self._store = _store(hass, entry)
        self._requests: dict[str, Request] = {}
        self._recent: deque[float] = deque()
        # Expiry timers for session keys on the TV, by request id.
        self._key_timers: dict[str, CALLBACK_TYPE] = {}
        self._unsubs: list[CALLBACK_TYPE] = []
        self._tasks: set[asyncio.Task] = set()
        self.tv: dev_tv.Tv | None = None

    async def async_start(self) -> None:
        from . import dev_tv

        self._dev_tv = dev_tv
        stored = await self._store.async_load() or {}
        self.tv = dev_tv.Tv(
            self.entry.data[CONF_HOST],
            int(self.entry.options.get(CONF_DEV_SSH_PORT) or DEFAULT_SSH_PORT),
            self.entry.data[CONF_DEV_KEY],
            stored.get("tv_host_key"),
        )
        self.tv.options_supported = stored.get("options_supported")
        self._unsubs.append(self.hass.bus.async_listen(NOTIFICATION_EVENT, self._on_action))
        self._unsubs.append(
            async_track_time_interval(self.hass, self._interval_sweep, SWEEP_INTERVAL)
        )
        self._background(self._sweep())

    async def async_stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        for cancel in self._key_timers.values():
            cancel()
        self._key_timers.clear()
        for request in self._requests.values():
            if request.timer:
                request.timer()
            request.changed.set()
        for task in list(self._tasks):
            task.cancel()

    def _background(self, coro) -> None:
        task = self.hass.async_create_background_task(coro, f"{DOMAIN} dev access")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _save(self) -> None:
        assert self.tv
        await self._store.async_save(
            {"tv_host_key": self.tv.host_key, "options_supported": self.tv.options_supported}
        )

    async def create(self, reason: Any, ttl: Any) -> Request:
        now = time.time()
        while self._recent and self._recent[0] < now - 3600:
            self._recent.popleft()
        if len(self._recent) >= REQUESTS_PER_HOUR:
            raise DevAccessError(429, "too_many_requests")
        if sum(r.state in (PENDING, INSTALLING) for r in self._requests.values()) >= MAX_PENDING:
            raise DevAccessError(429, "too_many_pending")
        try:
            seconds = int(ttl) if ttl is not None else MAX_TTL
        except (TypeError, ValueError):
            raise DevAccessError(400, "bad_ttl") from None
        seconds = max(MIN_TTL, min(MAX_TTL, seconds))
        self._recent.append(now)
        request = Request(
            id=secrets.token_urlsafe(16),
            nonce=secrets.token_urlsafe(16),
            reason=clean_reason(reason),
            ttl=seconds,
            created=now,
        )
        self._requests[request.id] = request
        self._forget_old(now)
        request.timer = async_call_later(
            self.hass, PENDING_TIMEOUT, lambda _now: self._expire_pending(request)
        )
        hours = f"{seconds / 3600:g} h" if seconds >= 3600 else f"{seconds // 60} min"
        await self.notify(
            f"Cloud session wants SSH access to {self.entry.title}",
            f"{request.reason}\nAccess for {hours}.",
            tag=self._tag(request),
            actions=[
                {"action": ACTION_APPROVE + request.nonce, "title": "Approve"},
                {"action": ACTION_DENY + request.nonce, "title": "Deny", "destructive": True},
            ],
        )
        _LOGGER.info("Developer access request %s: %s", request.id[:6], request.reason)
        return request

    def _forget_old(self, now: float) -> None:
        for request_id, request in list(self._requests.items()):
            finished = request.state in (DENIED, EXPIRED, FAILED, RELEASED)
            if finished and request.created < now - 86400:
                del self._requests[request_id]

    @staticmethod
    def _tag(request: Request) -> str:
        return f"luma_dev_{request.id[:12]}"

    @callback
    def _expire_pending(self, request: Request) -> None:
        request.timer = None
        if request.state == PENDING:
            request.set_state(EXPIRED)
            self.hass.async_create_task(self._clear(request))

    @callback
    def _on_action(self, event: Event) -> None:
        action = str(event.data.get("action") or "")
        if action.startswith(ACTION_APPROVE):
            approve, nonce = True, action[len(ACTION_APPROVE) :]
        elif action.startswith(ACTION_DENY):
            approve, nonce = False, action[len(ACTION_DENY) :]
        else:
            return
        request = next((r for r in self._requests.values() if r.nonce == nonce), None)
        if request is None or request.state != PENDING:
            return
        if request.timer:
            request.timer()
            request.timer = None
        if approve:
            request.set_state(INSTALLING)
            self._background(self._approve(request))
        else:
            request.set_state(DENIED)
            _LOGGER.info("Developer access request %s denied", request.id[:6])
            self.hass.async_create_task(self._clear(request))

    async def _approve(self, request: Request) -> None:
        assert self.tv
        expires_at = int(time.time()) + request.ttl
        generated = await self.hass.async_add_executor_job(keys.generate_key)
        try:
            await self.tv.install(generated, request.id, expires_at)
        except self._dev_tv.TvError as err:
            _LOGGER.error("Could not add the session key to the TV: %s", err)
            changed = isinstance(err, self._dev_tv.HostKeyChanged)
            request.error = "tv_host_key_changed" if changed else "tv_unreachable"
            request.set_state(FAILED)
            await self.notify(
                "Luma developer access failed",
                f"Could not add the key to {self.entry.title}: {err}",
                tag=self._tag(request),
            )
            return
        finally:
            await self._save()
        if request.state != INSTALLING:  # released while the key was being added
            await self._remove_key(request.id)
            return
        request.expires_at = expires_at
        request.payload = self._payload(generated.private_key, expires_at)
        self._schedule_removal(request.id, expires_at)
        request.set_state(APPROVED)
        _LOGGER.info("Developer access request %s approved until %s", request.id[:6], expires_at)
        await self._clear(request)

    def _payload(self, private_key: str, expires_at: int) -> dict[str, Any]:
        options = self.entry.options
        tunnel = (options.get(CONF_DEV_TUNNEL) or "").strip()
        payload: dict[str, Any] = {
            "expires_at": datetime.fromtimestamp(expires_at, UTC).isoformat(),
            "tv": self.entry.title,
            "ssh": {
                # Without a tunnel, sessions reach the TV directly (on the home network).
                "hostname": tunnel or self.entry.data[CONF_HOST],
                "port": DEFAULT_SSH_PORT
                if tunnel
                else int(options.get(CONF_DEV_SSH_PORT) or DEFAULT_SSH_PORT),
                "tunnel": bool(tunnel),
                "user": "root",
                "private_key": private_key,
            },
            "cloudflare": None,
            "streaming": {},
        }
        client_id = (options.get(CONF_DEV_CF_ID) or "").strip()
        client_secret = (options.get(CONF_DEV_CF_SECRET) or "").strip()
        if client_id and client_secret:
            payload["cloudflare"] = {"client_id": client_id, "client_secret": client_secret}
        for name, option in (
            ("motn", CONF_MOTN_KEY),
            ("tmdb", CONF_TMDB_TOKEN),
            ("mdblist", CONF_MDBLIST_KEY),
        ):
            if value := (options.get(option) or "").strip():
                payload["streaming"][name] = value
        return payload

    async def result(self, request_id: str, wait: float) -> dict[str, Any]:
        """The approved payload, exactly once. Waits up to `wait` seconds for an answer."""
        request = self._requests.get(request_id)
        if request is None:
            raise DevAccessError(404, "unknown_request")
        if request.state in (PENDING, INSTALLING) and wait > 0:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(request.changed.wait(), wait)
            if request.state == INSTALLING:  # the key is being added: a moment more
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(request.changed.wait(), 60)
        if request.state in (PENDING, INSTALLING):
            return {"state": request.state}
        if request.state == APPROVED:
            payload, request.payload = request.payload, None
            request.set_state(DELIVERED)
            return {"state": APPROVED, **(payload or {})}
        status, code = {
            DENIED: (403, "denied"),
            EXPIRED: (410, "expired"),
            FAILED: (502, request.error or "failed"),
            DELIVERED: (410, "already_delivered"),
            RELEASED: (410, "released"),
        }[request.state]
        raise DevAccessError(status, code)

    async def release(self, request_id: str) -> None:
        request = self._requests.get(request_id)
        if request is None:
            # After a restart the request is gone but its key may still be on the TV.
            if request_id in self._key_timers:
                await self._remove_key(request_id)
                return
            raise DevAccessError(404, "unknown_request")
        previous = request.state
        if request.timer:
            request.timer()
            request.timer = None
        request.payload = None
        request.set_state(RELEASED)
        if previous in KEY_ON_TV or request_id in self._key_timers:
            await self._remove_key(request_id)
        elif previous == PENDING:
            await self._clear(request)
        _LOGGER.info("Developer access request %s released", request_id[:6])

    def _schedule_removal(self, request_id: str, expires_at: int) -> None:
        if cancel := self._key_timers.pop(request_id, None):
            cancel()

        @callback
        def _expired(_now: datetime) -> None:
            self._key_timers.pop(request_id, None)
            request = self._requests.get(request_id)
            if request and request.state in KEY_ON_TV:
                request.payload = None
                request.set_state(EXPIRED)
            self._background(self._remove_key(request_id))

        delay = max(0.0, expires_at - time.time())
        self._key_timers[request_id] = async_call_later(self.hass, delay, _expired)

    async def _remove_key(self, request_id: str) -> None:
        assert self.tv
        if cancel := self._key_timers.pop(request_id, None):
            cancel()
        try:
            await self.tv.remove({request_id})
        except self._dev_tv.TvError as err:
            # The periodic sweep removes it once the TV answers again.
            _LOGGER.warning("Could not remove session key %s yet: %s", request_id[:6], err)

    async def _interval_sweep(self, _now: datetime) -> None:
        await self._sweep()

    async def _sweep(self) -> None:
        assert self.tv
        try:
            remaining = await self.tv.sweep()
        except self._dev_tv.TvError as err:
            _LOGGER.debug("Developer access sweep skipped: %s", err)
            return
        finally:
            await self._save()
        for request_id, expires_at in remaining.items():
            if request_id not in self._key_timers:
                self._schedule_removal(request_id, expires_at)

    async def notify(
        self,
        title: str,
        message: str,
        *,
        tag: str | None = None,
        actions: list[dict[str, Any]] | None = None,
    ) -> None:
        service = str(self.entry.options.get(CONF_DEV_PHONE) or "").removeprefix("notify.")
        if not service:
            return
        data: dict[str, Any] = {}
        if tag:
            data["tag"] = tag
        if actions:
            data["actions"] = actions
        call: dict[str, Any] = {"message": message, "data": data}
        if title:
            call["title"] = title
        await self.hass.services.async_call("notify", service, call, blocking=False)

    async def _clear(self, request: Request) -> None:
        await self.notify("", "clear_notification", tag=self._tag(request))


# ---- HTTP endpoints ------------------------------------------------------------------------


def _error(err: DevAccessError) -> web.Response:
    return web.json_response({"error": err.code}, status=err.status, headers=NO_STORE)


class _View(HomeAssistantView):
    """Home Assistant's own login is not used: each call carries the dev token, which can only
    ask for access to one TV, never act on the house."""

    requires_auth = False
    cors_allowed = False

    def _broker(self, request: web.Request) -> Broker:
        return _authorize(request.app["hass"], request.headers.get(DEV_TOKEN_HEADER))


class RequestView(_View):
    url = "/api/luma/dev/request"
    name = "api:luma:dev:request"

    async def post(self, request: web.Request) -> web.Response:
        try:
            broker = self._broker(request)
            try:
                body = await request.json()
            except ValueError:
                body = {}
            if not isinstance(body, dict):
                body = {}
            access = await broker.create(body.get("reason"), body.get("ttl"))
        except DevAccessError as err:
            return _error(err)
        return web.json_response(
            {"request_id": access.id, "ttl": access.ttl, "answer_within": PENDING_TIMEOUT},
            status=202,
            headers=NO_STORE,
        )


class ResultView(_View):
    url = "/api/luma/dev/result/{request_id}"
    name = "api:luma:dev:result"

    async def get(self, request: web.Request, request_id: str) -> web.Response:
        try:
            broker = self._broker(request)
            try:
                wait = max(0.0, min(float(request.query.get("wait", 0)), MAX_WAIT))
            except ValueError:
                wait = 0.0
            result = await broker.result(request_id, wait)
        except DevAccessError as err:
            return _error(err)
        status = 200 if result["state"] == APPROVED else 202
        return web.json_response(result, status=status, headers=NO_STORE)


class ReleaseView(_View):
    url = "/api/luma/dev/release/{request_id}"
    name = "api:luma:dev:release"

    async def post(self, request: web.Request, request_id: str) -> web.Response:
        try:
            await self._broker(request).release(request_id)
        except DevAccessError as err:
            return _error(err)
        return web.json_response({"state": RELEASED}, headers=NO_STORE)
