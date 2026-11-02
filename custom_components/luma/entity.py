"""Shared device information for a TV running Luma."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity

from .const import CONF_DEVICE_ID, DOMAIN


class LumaEntity(Entity):
    _attr_has_entity_name = True

    def __init__(self, entry: ConfigEntry, key: str) -> None:
        device_id = entry.data[CONF_DEVICE_ID]
        self._attr_unique_id = f"{device_id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device_id)},
            name=entry.title,
            manufacturer="LG",
            model="webOS TV with Luma",
        )
