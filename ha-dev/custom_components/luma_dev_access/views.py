"""HTTP endpoints for cloud sessions. Home Assistant's own login is not used: each call carries
the dev token in X-Luma-Dev-Token, which can only ask for access, never act on the house."""

from __future__ import annotations

from typing import Any

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .broker import Broker, BrokerError
from .const import DOMAIN, MAX_WAIT, PENDING_TIMEOUT, TOKEN_HEADER

NO_STORE = {"Cache-Control": "no-store"}


def _broker(hass: HomeAssistant) -> Broker:
    broker = hass.data.get(DOMAIN)
    if broker is None:
        raise BrokerError(503, "not_ready")
    return broker


def _error(err: BrokerError) -> web.Response:
    return web.json_response({"error": err.code}, status=err.status, headers=NO_STORE)


class _View(HomeAssistantView):
    requires_auth = False
    cors_allowed = False

    def _authorized(self, request: web.Request) -> Broker:
        broker = _broker(request.app["hass"])
        broker.check_token(request.headers.get(TOKEN_HEADER))
        return broker


class RequestView(_View):
    url = "/api/luma_dev/request"
    name = "api:luma_dev:request"

    async def post(self, request: web.Request) -> web.Response:
        try:
            broker = self._authorized(request)
            try:
                body: dict[str, Any] = await request.json()
            except ValueError:
                body = {}
            if not isinstance(body, dict):
                body = {}
            access = await broker.create(body.get("reason"), body.get("ttl"))
        except BrokerError as err:
            return _error(err)
        return web.json_response(
            {"request_id": access.id, "ttl": access.ttl, "answer_within": PENDING_TIMEOUT},
            status=202,
            headers=NO_STORE,
        )


class ResultView(_View):
    url = "/api/luma_dev/result/{request_id}"
    name = "api:luma_dev:result"

    async def get(self, request: web.Request, request_id: str) -> web.Response:
        try:
            broker = self._authorized(request)
            try:
                wait = max(0.0, min(float(request.query.get("wait", 0)), MAX_WAIT))
            except ValueError:
                wait = 0.0
            result = await broker.result(request_id, wait)
        except BrokerError as err:
            return _error(err)
        status = 200 if result["state"] == "approved" else 202
        return web.json_response(result, status=status, headers=NO_STORE)


class ReleaseView(_View):
    url = "/api/luma_dev/release/{request_id}"
    name = "api:luma_dev:release"

    async def post(self, request: web.Request, request_id: str) -> web.Response:
        try:
            broker = self._authorized(request)
            await broker.release(request_id)
        except BrokerError as err:
            return _error(err)
        return web.json_response({"state": "released"}, headers=NO_STORE)


VIEWS = (RequestView, ResultView, ReleaseView)
