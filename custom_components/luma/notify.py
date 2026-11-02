"""A notify entity: a webOS toast shown over whatever is playing."""

from __future__ import annotations

from homeassistant.components.notify import NotifyEntity, NotifyEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import LumaConfigEntry
from .api import LumaError
from .entity import LumaEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: LumaConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities([LumaNotify(entry)])


class LumaNotify(LumaEntity, NotifyEntity):
    _attr_supported_features = NotifyEntityFeature.TITLE

    def __init__(self, entry: LumaConfigEntry) -> None:
        super().__init__(entry, "toast")
        self._client = entry.runtime_data.client

    async def async_send_message(self, message: str, title: str | None = None) -> None:
        text = f"{title}: {message}" if title else message
        try:
            await self._client.notify(text[:300])
        except LumaError as err:
            raise HomeAssistantError(str(err)) from err
