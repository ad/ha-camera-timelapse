"""Sensor platform for Camera Timelapse."""
from __future__ import annotations

import os
from pathlib import Path

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_CAMERAS,
    DOMAIN,
    SIGNAL_CAMERAS_UPDATED,
    SIGNAL_SENSOR_UPDATE,
)
from .coordinator import TimeLapseCoordinator, _camera_slug


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Camera Timelapse sensors from a config entry."""
    coordinator: TimeLapseCoordinator = hass.data[DOMAIN][entry.entry_id]

    def _make_sensors(camera_id: str) -> list[SensorEntity]:
        return [
            CameraLastTimelapseSensor(coordinator, entry, camera_id),
            CameraFrameCountSensor(coordinator, entry, camera_id),
            CameraDiskUsageSensor(coordinator, entry, camera_id),
        ]

    cameras: dict = entry.options.get(CONF_CAMERAS, {})
    entities: list[SensorEntity] = []
    for camera_id in cameras:
        entities.extend(_make_sensors(camera_id))
    async_add_entities(entities)

    # Track existing camera ids so we can add sensors for newly added cameras
    known_ids: set[str] = set(cameras.keys())

    @callback
    def _handle_cameras_updated() -> None:
        nonlocal known_ids
        current_ids = set(entry.options.get(CONF_CAMERAS, {}).keys())
        new_ids = current_ids - known_ids
        if new_ids:
            new_entities: list[SensorEntity] = []
            for camera_id in new_ids:
                new_entities.extend(_make_sensors(camera_id))
            async_add_entities(new_entities)
        known_ids = current_ids

    entry.async_on_unload(
        async_dispatcher_connect(
            hass,
            SIGNAL_CAMERAS_UPDATED.format(entry.entry_id),
            _handle_cameras_updated,
        )
    )


class _CameraTimelapseSensorBase(SensorEntity):
    """Base class for Camera Timelapse sensors."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self,
        coordinator: TimeLapseCoordinator,
        entry: ConfigEntry,
        camera_id: str,
    ) -> None:
        self._coordinator = coordinator
        self._entry = entry
        self._camera_id = camera_id
        self._camera_slug = _camera_slug(camera_id)

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, f"{self._entry.entry_id}_{self._camera_slug}")},
            "name": f"Timelapse {self._camera_slug}",
            "manufacturer": "Camera Timelapse",
            "entry_type": "service",
        }

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_SENSOR_UPDATE.format(self._entry.entry_id, self._camera_id),
                self._handle_update,
            )
        )

    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()


class CameraLastTimelapseSensor(_CameraTimelapseSensorBase):
    """Sensor showing path and metadata of the last assembled timelapse."""

    _attr_icon = "mdi:video"
    _attr_translation_key = "last_timelapse"

    def __init__(self, coordinator, entry, camera_id):
        super().__init__(coordinator, entry, camera_id)
        self._attr_unique_id = f"{entry.entry_id}_{self._camera_slug}_last_timelapse"

    @property
    def native_value(self) -> str | None:
        info = self._coordinator._last_timelapse_info.get(self._camera_id)
        if not info:
            return None
        return os.path.basename(info["path"])

    @property
    def extra_state_attributes(self) -> dict:
        info = self._coordinator._last_timelapse_info.get(self._camera_id)
        if not info:
            return {}
        return {
            "path": info["path"],
            "type": info["type"],
            "assembled_at": info["assembled_at"],
        }


class CameraFrameCountSensor(_CameraTimelapseSensorBase):
    """Sensor showing number of frames captured today."""

    _attr_icon = "mdi:camera-burst"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "frames"
    _attr_translation_key = "frames_today"

    def __init__(self, coordinator, entry, camera_id):
        super().__init__(coordinator, entry, camera_id)
        self._attr_unique_id = f"{entry.entry_id}_{self._camera_slug}_frames_today"

    @property
    def native_value(self) -> int:
        return self._coordinator._frame_counts.get(self._camera_id, 0)


class CameraDiskUsageSensor(_CameraTimelapseSensorBase):
    """Sensor showing total disk usage for this camera in MB."""

    _attr_icon = "mdi:harddisk"
    _attr_state_class = SensorStateClass.TOTAL
    _attr_native_unit_of_measurement = "MB"
    _attr_translation_key = "disk_usage"

    def __init__(self, coordinator, entry, camera_id):
        super().__init__(coordinator, entry, camera_id)
        self._attr_unique_id = f"{entry.entry_id}_{self._camera_slug}_disk_usage"

    @property
    def native_value(self) -> float:
        return self._coordinator._disk_usage_mb.get(self._camera_id, 0.0)
