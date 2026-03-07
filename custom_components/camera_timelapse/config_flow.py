"""Config flow for Camera Timelapse integration."""
from __future__ import annotations

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    ACTION_ADD,
    ACTION_EDIT,
    ACTION_REMOVE,
    CONF_ASSEMBLY_INTERVAL_MINUTES,
    CONF_CAMERA_ENTITY_ID,
    CONF_CAMERAS,
    CONF_FPS,
    CONF_HDR_FRAMES,
    CONF_INTERVAL_MINUTES,
    CONF_KEEP_FRAMES,
    CONF_MAX_RETENTION_DAYS,
    CONF_MODE,
    CONF_OUTPUT_FORMAT,
    CONF_ROLLING_PERIOD_DAYS,
    CONF_STORAGE_PATH,
    CONF_TIME_END,
    CONF_TIME_RANGE_TYPE,
    CONF_TIME_START,
    DEFAULT_ASSEMBLY_INTERVAL_MINUTES,
    DEFAULT_FPS,
    DEFAULT_HDR_FRAMES,
    DEFAULT_INTERVAL_MINUTES,
    DEFAULT_KEEP_FRAMES,
    DEFAULT_MAX_RETENTION_DAYS,
    DEFAULT_MODE,
    DEFAULT_OUTPUT_FORMAT,
    DEFAULT_ROLLING_PERIOD_DAYS,
    DEFAULT_STORAGE_PATH,
    DEFAULT_TIME_RANGE_TYPE,
    DOMAIN,
    FORMAT_APNG,
    FORMAT_GIF,
    FORMAT_MP4,
    MODE_BOTH,
    MODE_DAILY,
    MODE_ROLLING,
    TIME_RANGE_ALWAYS,
    TIME_RANGE_CUSTOM,
    TIME_RANGE_SUNRISE_SUNSET,
)


class CameraTimeLapseConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the initial config flow for Camera Timelapse."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        """Handle the initial setup step."""
        errors = {}

        if user_input is not None:
            storage_path = user_input[CONF_STORAGE_PATH].rstrip("/")
            # Check for duplicate storage path among existing entries
            for entry in self._async_current_entries():
                if entry.data.get(CONF_STORAGE_PATH) == storage_path:
                    errors["base"] = "already_configured"
                    break

            if not errors:
                return self.async_create_entry(
                    title=user_input.get("name", "Camera Timelapse"),
                    data={CONF_STORAGE_PATH: storage_path},
                )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required("name", default="Camera Timelapse"): selector.TextSelector(),
                    vol.Required(CONF_STORAGE_PATH, default=DEFAULT_STORAGE_PATH): selector.TextSelector(
                        selector.TextSelectorConfig(type=selector.TextSelectorType.TEXT)
                    ),
                }
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return CameraTimeLapseOptionsFlow(config_entry)


class CameraTimeLapseOptionsFlow(config_entries.OptionsFlow):
    """Handle options flow for Camera Timelapse (per-camera configuration)."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._entry = config_entry
        self._cameras: dict = dict(config_entry.options.get(CONF_CAMERAS, {}))
        self._editing_camera: str | None = None

    async def async_step_init(self, user_input=None):
        """Show the main action picker."""
        errors = {}

        configured_cameras = list(self._cameras.keys())

        if user_input is not None:
            action = user_input["action"]
            camera_id = user_input.get(CONF_CAMERA_ENTITY_ID)

            if action == ACTION_ADD:
                return await self.async_step_add_camera()
            elif action == ACTION_EDIT:
                if not camera_id:
                    errors[CONF_CAMERA_ENTITY_ID] = "required"
                else:
                    self._editing_camera = camera_id
                    return await self.async_step_camera_settings()
            elif action == ACTION_REMOVE:
                if not camera_id:
                    errors[CONF_CAMERA_ENTITY_ID] = "required"
                else:
                    self._cameras.pop(camera_id, None)
                    return self.async_create_entry(
                        title="",
                        data={CONF_CAMERAS: self._cameras},
                    )

        action_options = [
            selector.SelectOptionDict(value=ACTION_ADD, label="Add a camera"),
        ]
        if configured_cameras:
            action_options += [
                selector.SelectOptionDict(value=ACTION_EDIT, label="Edit a camera"),
                selector.SelectOptionDict(value=ACTION_REMOVE, label="Remove a camera"),
            ]

        schema_dict = {
            vol.Required("action", default=ACTION_ADD): selector.SelectSelector(
                selector.SelectSelectorConfig(options=action_options, mode=selector.SelectSelectorMode.LIST)
            ),
        }

        if configured_cameras:
            schema_dict[vol.Optional(CONF_CAMERA_ENTITY_ID)] = selector.EntitySelector(
                selector.EntitySelectorConfig(
                    domain="camera",
                    include_entities=configured_cameras,
                )
            )

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(schema_dict),
            errors=errors,
        )

    async def async_step_add_camera(self, user_input=None):
        """Select a new camera to add."""
        errors = {}

        if user_input is not None:
            camera_id = user_input[CONF_CAMERA_ENTITY_ID]
            if camera_id in self._cameras:
                errors[CONF_CAMERA_ENTITY_ID] = "already_configured"
            else:
                self._editing_camera = camera_id
                return await self.async_step_camera_settings()

        # Build exclude list: cameras already configured
        already_configured = list(self._cameras.keys())

        schema = vol.Schema(
            {
                vol.Required(CONF_CAMERA_ENTITY_ID): selector.EntitySelector(
                    selector.EntitySelectorConfig(
                        domain="camera",
                        exclude_entities=already_configured,
                    )
                ),
            }
        )

        return self.async_show_form(
            step_id="add_camera",
            data_schema=schema,
            errors=errors,
        )

    async def async_step_camera_settings(self, user_input=None):
        """Configure settings for the selected camera."""
        errors = {}
        camera_id = self._editing_camera
        existing = self._cameras.get(camera_id, {})

        if user_input is not None:
            # Validate custom time range
            if user_input[CONF_TIME_RANGE_TYPE] == TIME_RANGE_CUSTOM:
                if not user_input.get(CONF_TIME_START):
                    errors[CONF_TIME_START] = "required"
                if not user_input.get(CONF_TIME_END):
                    errors[CONF_TIME_END] = "required"

            if not errors:
                cam_config = {
                    CONF_INTERVAL_MINUTES: user_input[CONF_INTERVAL_MINUTES],
                    CONF_TIME_RANGE_TYPE: user_input[CONF_TIME_RANGE_TYPE],
                    CONF_TIME_START: user_input.get(CONF_TIME_START, "07:00:00"),
                    CONF_TIME_END: user_input.get(CONF_TIME_END, "19:00:00"),
                    CONF_MODE: user_input[CONF_MODE],
                    CONF_ROLLING_PERIOD_DAYS: user_input.get(
                        CONF_ROLLING_PERIOD_DAYS, DEFAULT_ROLLING_PERIOD_DAYS
                    ),
                    CONF_OUTPUT_FORMAT: user_input[CONF_OUTPUT_FORMAT],
                    CONF_FPS: user_input[CONF_FPS],
                    CONF_MAX_RETENTION_DAYS: user_input[CONF_MAX_RETENTION_DAYS],
                    CONF_ASSEMBLY_INTERVAL_MINUTES: user_input[CONF_ASSEMBLY_INTERVAL_MINUTES],
                    CONF_HDR_FRAMES: user_input.get(CONF_HDR_FRAMES, DEFAULT_HDR_FRAMES),
                    CONF_KEEP_FRAMES: user_input.get(CONF_KEEP_FRAMES, DEFAULT_KEEP_FRAMES),
                }
                self._cameras[camera_id] = cam_config
                return self.async_create_entry(
                    title="",
                    data={CONF_CAMERAS: self._cameras},
                )

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_INTERVAL_MINUTES,
                    default=existing.get(CONF_INTERVAL_MINUTES, DEFAULT_INTERVAL_MINUTES),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=1, max=1440, step=1, mode=selector.NumberSelectorMode.BOX, unit_of_measurement="min"
                    )
                ),
                vol.Required(
                    CONF_TIME_RANGE_TYPE,
                    default=existing.get(CONF_TIME_RANGE_TYPE, DEFAULT_TIME_RANGE_TYPE),
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[
                            selector.SelectOptionDict(value=TIME_RANGE_SUNRISE_SUNSET, label="Sunrise to sunset"),
                            selector.SelectOptionDict(value=TIME_RANGE_CUSTOM, label="Custom time range"),
                            selector.SelectOptionDict(value=TIME_RANGE_ALWAYS, label="Always (24h)"),
                        ],
                        mode=selector.SelectSelectorMode.LIST,
                    )
                ),
                vol.Optional(
                    CONF_TIME_START,
                    default=existing.get(CONF_TIME_START, "07:00:00"),
                ): selector.TimeSelector(),
                vol.Optional(
                    CONF_TIME_END,
                    default=existing.get(CONF_TIME_END, "19:00:00"),
                ): selector.TimeSelector(),
                vol.Required(
                    CONF_MODE,
                    default=existing.get(CONF_MODE, DEFAULT_MODE),
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[
                            selector.SelectOptionDict(value=MODE_DAILY, label="Daily (one file per day)"),
                            selector.SelectOptionDict(value=MODE_ROLLING, label="Rolling (last N days)"),
                            selector.SelectOptionDict(value=MODE_BOTH, label="Both"),
                        ],
                        mode=selector.SelectSelectorMode.LIST,
                    )
                ),
                vol.Optional(
                    CONF_ROLLING_PERIOD_DAYS,
                    default=existing.get(CONF_ROLLING_PERIOD_DAYS, DEFAULT_ROLLING_PERIOD_DAYS),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=1, max=365, step=1, mode=selector.NumberSelectorMode.BOX, unit_of_measurement="days"
                    )
                ),
                vol.Required(
                    CONF_OUTPUT_FORMAT,
                    default=existing.get(CONF_OUTPUT_FORMAT, DEFAULT_OUTPUT_FORMAT),
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[
                            selector.SelectOptionDict(value=FORMAT_GIF, label="GIF"),
                            selector.SelectOptionDict(value=FORMAT_APNG, label="APNG"),
                            selector.SelectOptionDict(value=FORMAT_MP4, label="MP4 (H.264)"),
                        ],
                        mode=selector.SelectSelectorMode.LIST,
                    )
                ),
                vol.Required(
                    CONF_FPS,
                    default=existing.get(CONF_FPS, DEFAULT_FPS),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=1, max=60, step=1, mode=selector.NumberSelectorMode.BOX, unit_of_measurement="fps"
                    )
                ),
                vol.Required(
                    CONF_MAX_RETENTION_DAYS,
                    default=existing.get(CONF_MAX_RETENTION_DAYS, DEFAULT_MAX_RETENTION_DAYS),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0, max=3650, step=1, mode=selector.NumberSelectorMode.BOX, unit_of_measurement="days"
                    )
                ),
                vol.Required(
                    CONF_ASSEMBLY_INTERVAL_MINUTES,
                    default=existing.get(CONF_ASSEMBLY_INTERVAL_MINUTES, DEFAULT_ASSEMBLY_INTERVAL_MINUTES),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0, max=1440, step=1, mode=selector.NumberSelectorMode.BOX, unit_of_measurement="min"
                    )
                ),
                vol.Optional(
                    CONF_HDR_FRAMES,
                    default=existing.get(CONF_HDR_FRAMES, DEFAULT_HDR_FRAMES),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0, max=5, step=1, mode=selector.NumberSelectorMode.BOX
                    )
                ),
                vol.Optional(
                    CONF_KEEP_FRAMES,
                    default=existing.get(CONF_KEEP_FRAMES, DEFAULT_KEEP_FRAMES),
                ): selector.BooleanSelector(),
            }
        )

        return self.async_show_form(
            step_id="camera_settings",
            data_schema=schema,
            errors=errors,
            description_placeholders={"camera_id": camera_id},
        )
