"""A button that closes the camera overlay on the TV."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import LumaConfigEntry
from .api import LumaError
from .entity import LumaEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: LumaConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities([LumaDismissButton(entry)])


class LumaDismissButton(LumaEntity, ButtonEntity):
    def __init__(self, entry: LumaConfigEntry) -> None:
        super().__init__(entry, "dismiss_overlay")
        self._client = entry.runtime_data.client

    async def async_press(self) -> None:
        try:
            await self._client.dismiss()
        except LumaError as err:
            raise HomeAssistantError(str(err)) from err
