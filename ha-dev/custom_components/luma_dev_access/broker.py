"""Access requests: the owner approves each one on their phone before a session key exists.

A request moves pending → (approved → delivered) → released, or ends denied, expired or
failed. Its id goes to the cloud session; the nonce in the notification's action names goes
only to the phone, so nothing but the owner's tap can approve it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.helpers.storage import Store

from . import keys, tv
from .const import (
    ACTION_APPROVE,
    ACTION_DENY,
    BAD_TOKENS_PER_HOUR,
    CONF_CF_CLIENT_ID,
    CONF_CF_CLIENT_SECRET,
    CONF_MANAGEMENT_KEY,
    CONF_NOTIFY_SERVICE,
    CONF_TOKEN_HASH,
    CONF_TUNNEL_HOSTNAME,
    CONF_TV_HOST,
    CONF_TV_PORT,
    DEFAULT_TUNNEL_HOSTNAME,
    DEFAULT_TV_PORT,
    DOMAIN,
    LOCKOUT,
    LUMA_DOMAIN,
    LUMA_STREAMING_OPTIONS,
    MAX_PENDING,
    MAX_TTL,
    MIN_TTL,
    NOTIFICATION_EVENT,
    PENDING_TIMEOUT,
    REQUESTS_PER_HOUR,
    SWEEP_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)

PENDING = "pending"
INSTALLING = "installing"
APPROVED = "approved"
DELIVERED = "delivered"
DENIED = "denied"
EXPIRED = "expired"
FAILED = "failed"
RELEASED = "released"
# States in which the session key is on the TV.
KEY_ON_TV = {APPROVED, DELIVERED}


class BrokerError(Exception):
    """A request the caller made cannot be honored; `status` is the HTTP status to answer."""

    def __init__(self, status: int, code: str) -> None:
        super().__init__(code)
        self.status = status
        self.code = code


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
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self._store: Store[dict[str, Any]] = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}")
        self._requests: dict[str, Request] = {}
        self._recent: deque[float] = deque()
        self._bad_tokens: deque[float] = deque()
        self._locked_until = 0.0
        # Expiry timers for session keys found on the TV, by request id.
        self._key_timers: dict[str, CALLBACK_TYPE] = {}
        self._unsubs: list[CALLBACK_TYPE] = []
        self.tv: tv.Tv | None = None

    # ---- lifecycle -------------------------------------------------------------------------

    async def async_start(self) -> None:
        stored = await self._store.async_load() or {}
        options = self.entry.options
        self.tv = tv.Tv(
            options[CONF_TV_HOST],
            int(options.get(CONF_TV_PORT, DEFAULT_TV_PORT)),
            self.entry.data[CONF_MANAGEMENT_KEY],
            stored.get("tv_host_key"),
        )
        self.tv.options_supported = stored.get("options_supported")
        self._unsubs.append(self.hass.bus.async_listen(NOTIFICATION_EVENT, self._on_action))
        self._unsubs.append(
            async_track_time_interval(
                self.hass, self._interval_sweep, timedelta(seconds=SWEEP_INTERVAL)
            )
        )
        self.entry.async_create_background_task(self.hass, self._sweep(), f"{DOMAIN} sweep")

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

    async def _save(self) -> None:
        assert self.tv
        await self._store.async_save(
            {"tv_host_key": self.tv.host_key, "options_supported": self.tv.options_supported}
        )

    # ---- tokens and limits -----------------------------------------------------------------

    def check_token(self, token: str | None) -> None:
        now = time.time()
        if now < self._locked_until:
            raise BrokerError(429, "locked")
        if keys.token_matches(token, self.entry.data[CONF_TOKEN_HASH]):
            return
        self._bad_tokens.append(now)
        while self._bad_tokens and self._bad_tokens[0] < now - 3600:
            self._bad_tokens.popleft()
        if len(self._bad_tokens) >= BAD_TOKENS_PER_HOUR:
            self._locked_until = now + LOCKOUT
            self._bad_tokens.clear()
            _LOGGER.warning("Too many wrong dev tokens: locked for an hour")
            self.hass.async_create_task(
                self._notify(
                    "Luma dev access locked",
                    "Too many requests with a wrong token. Access requests are refused for an "
                    "hour. Rotate the token in the integration's options if it leaked.",
                )
            )
        raise BrokerError(401, "bad_token")

    # ---- requests --------------------------------------------------------------------------

    async def create(self, reason: Any, ttl: Any) -> Request:
        now = time.time()
        while self._recent and self._recent[0] < now - 3600:
            self._recent.popleft()
        if len(self._recent) >= REQUESTS_PER_HOUR:
            raise BrokerError(429, "too_many_requests")
        if sum(r.state in (PENDING, INSTALLING) for r in self._requests.values()) >= MAX_PENDING:
            raise BrokerError(429, "too_many_pending")
        try:
            seconds = int(ttl) if ttl is not None else MAX_TTL
        except (TypeError, ValueError):
            raise BrokerError(400, "bad_ttl") from None
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
        await self._notify(
            "Cloud session wants TV access",
            f"{request.reason}\nAccess for {hours}.",
            tag=self._tag(request),
            actions=[
                {"action": ACTION_APPROVE + request.nonce, "title": "Approve"},
                {"action": ACTION_DENY + request.nonce, "title": "Deny", "destructive": True},
            ],
        )
        _LOGGER.info("Access request %s: %s", request.id[:6], request.reason)
        return request

    def _forget_old(self, now: float) -> None:
        """Drop finished requests older than a day so the table stays small."""
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
            self.entry.async_create_background_task(
                self.hass, self._approve(request), f"{DOMAIN} approve"
            )
        else:
            request.set_state(DENIED)
            _LOGGER.info("Access request %s denied", request.id[:6])
            self.hass.async_create_task(self._clear(request))

    async def _approve(self, request: Request) -> None:
        assert self.tv
        expires_at = int(time.time()) + request.ttl
        generated = await self.hass.async_add_executor_job(
            tv.generate_key, f"{keys.TAG}:{request.id}"
        )
        try:
            await self.tv.install(generated, request.id, expires_at)
        except tv.TvError as err:
            _LOGGER.error("Could not add the session key to the TV: %s", err)
            request.error = (
                "tv_host_key_changed" if isinstance(err, tv.HostKeyChanged) else "tv_unreachable"
            )
            request.set_state(FAILED)
            await self._notify(
                "Luma dev access failed",
                f"Could not add the key to the TV: {err}",
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
        _LOGGER.info("Access request %s approved until %s", request.id[:6], expires_at)
        await self._clear(request)

    def _payload(self, private_key: str, expires_at: int) -> dict[str, Any]:
        options = self.entry.options
        payload: dict[str, Any] = {
            "expires_at": datetime.fromtimestamp(expires_at, UTC).isoformat(),
            "ssh": {
                "hostname": options.get(CONF_TUNNEL_HOSTNAME) or DEFAULT_TUNNEL_HOSTNAME,
                "user": "root",
                "private_key": private_key,
            },
            "cloudflare": None,
            "streaming": {},
        }
        client_id = (options.get(CONF_CF_CLIENT_ID) or "").strip()
        client_secret = (options.get(CONF_CF_CLIENT_SECRET) or "").strip()
        if client_id and client_secret:
            payload["cloudflare"] = {"client_id": client_id, "client_secret": client_secret}
        for luma in self.hass.config_entries.async_entries(LUMA_DOMAIN):
            for name, option in LUMA_STREAMING_OPTIONS.items():
                value = (luma.options.get(option) or "").strip()
                if value and name not in payload["streaming"]:
                    payload["streaming"][name] = value
        return payload

    async def result(self, request_id: str, wait: float) -> dict[str, Any]:
        """The approved payload, exactly once. Waits up to `wait` seconds for an answer."""
        request = self._requests.get(request_id)
        if request is None:
            raise BrokerError(404, "unknown_request")
        if request.state in (PENDING, INSTALLING) and wait > 0:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(request.changed.wait(), wait)
            if request.state == INSTALLING:  # the key is being added: a moment more
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(request.changed.wait(), tv.CONNECT_TIMEOUT * 4)
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
        raise BrokerError(status, code)

    async def release(self, request_id: str) -> None:
        request = self._requests.get(request_id)
        if request is None:
            # After a restart the request is gone but its key may still be on the TV.
            if request_id in self._key_timers:
                await self._remove_key(request_id)
                return
            raise BrokerError(404, "unknown_request")
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
        _LOGGER.info("Access request %s released", request_id[:6])

    # ---- session keys on the TV ------------------------------------------------------------

    def _schedule_removal(self, request_id: str, expires_at: int) -> None:
        if cancel := self._key_timers.pop(request_id, None):
            cancel()
        delay = max(0.0, expires_at - time.time())

        @callback
        def _expired(_now: datetime) -> None:
            self._key_timers.pop(request_id, None)
            request = self._requests.get(request_id)
            if request and request.state in KEY_ON_TV:
                request.payload = None
                request.set_state(EXPIRED)
            self.entry.async_create_background_task(
                self.hass, self._remove_key(request_id), f"{DOMAIN} expire key"
            )

        self._key_timers[request_id] = async_call_later(self.hass, delay, _expired)

    async def _remove_key(self, request_id: str) -> None:
        assert self.tv
        if cancel := self._key_timers.pop(request_id, None):
            cancel()
        try:
            await self.tv.remove({request_id})
        except tv.TvError as err:
            # The periodic sweep removes it once the TV answers again.
            _LOGGER.warning("Could not remove session key %s yet: %s", request_id[:6], err)

    async def _interval_sweep(self, _now: datetime) -> None:
        await self._sweep()

    async def _sweep(self) -> None:
        assert self.tv
        try:
            remaining = await self.tv.sweep()
        except tv.TvError as err:
            _LOGGER.debug("Sweep skipped: %s", err)
            return
        finally:
            await self._save()
        for request_id, expires_at in remaining.items():
            if request_id not in self._key_timers:
                self._schedule_removal(request_id, expires_at)

    # ---- notifications ---------------------------------------------------------------------

    async def _notify(
        self,
        title: str,
        message: str,
        *,
        tag: str | None = None,
        actions: list[dict[str, Any]] | None = None,
    ) -> None:
        service = str(self.entry.options.get(CONF_NOTIFY_SERVICE) or "")
        if not service:
            _LOGGER.error("No phone is set to approve Luma dev access")
            return
        data: dict[str, Any] = {}
        call: dict[str, Any] = {"message": message, "data": data}
        if title:
            call["title"] = title
        if tag:
            data["tag"] = tag
        if actions:
            data["actions"] = actions
        await self.hass.services.async_call(
            "notify",
            service.removeprefix("notify."),
            call,
            blocking=False,
        )

    async def _clear(self, request: Request) -> None:
        await self._notify("", "clear_notification", tag=self._tag(request))
