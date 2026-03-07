"""Coordinator for Camera Timelapse: scheduling, capture, assembly, and cleanup."""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_point_in_time, async_track_time_interval
from homeassistant.helpers.storage import Store
import homeassistant.util.dt as dt_util

_STORAGE_VERSION = 1

from .const import (
    CONF_ASSEMBLY_INTERVAL_MINUTES,
    CONF_CAMERAS,
    CONF_FPS,
    CONF_HDR_FRAMES,
    CONF_INTERVAL_MINUTES,
    CONF_KEEP_FRAMES,
    CONF_PLACEHOLDER_IMAGE,
    DEFAULT_PLACEHOLDER_IMAGE,
    CONF_MAX_RETENTION_DAYS,
    CONF_MODE,
    CONF_OUTPUT_FORMAT,
    CONF_ROLLING_PERIOD_DAYS,
    CONF_STORAGE_PATH,
    CONF_TIME_END,
    CONF_TIME_RANGE_TYPE,
    CONF_TIME_START,
    DEFAULT_HDR_FRAMES,
    DEFAULT_KEEP_FRAMES,
    DOMAIN,
    EVENT_TIMELAPSE_READY,
    FORMAT_APNG,
    FORMAT_GIF,
    FORMAT_MP4,
    MODE_BOTH,
    MODE_DAILY,
    MODE_ROLLING,
    ROLLING_DEBOUNCE_SECONDS,
    SIGNAL_CAMERAS_UPDATED,
    SIGNAL_SENSOR_UPDATE,
    TIME_RANGE_ALWAYS,
    TIME_RANGE_CUSTOM,
    TIME_RANGE_SUNRISE_SUNSET,
)

_LOGGER = logging.getLogger(__name__)


class TimeLapseCoordinator:
    """Manages all camera timelapse scheduling, capture, and assembly."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.storage_path: str = entry.data[CONF_STORAGE_PATH]
        # Unsub callables keyed by camera entity_id
        self._capture_unsubs: dict[str, Callable] = {}
        self._assembly_unsubs: dict[str, Callable] = {}
        self._periodic_assembly_unsubs: dict[str, Callable] = {}
        # Last rolling assembly timestamp per camera (for debounce)
        self._last_rolling: dict[str, datetime] = {}
        # Serialise write operations per camera
        self._write_locks: dict[str, asyncio.Lock] = {}
        # Track last cleanup date per camera (to run cleanup once per day for rolling mode)
        self._last_cleanup_date: dict[str, date] = {}
        # Sensor state
        self._last_timelapse_info: dict[str, dict] = {}
        self._frame_counts: dict[str, int] = {}
        self._frame_count_dates: dict[str, date] = {}
        self._disk_usage_mb: dict[str, float] = {}
        # Persistent store for last periodic assembly times
        self._store: Store | None = None
        self._last_periodic_assembly: dict[str, datetime] = {}
        # Path of the most recently captured frame per camera (for image entity)
        self._latest_frame_path: dict[str, Path] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def async_setup(self) -> None:
        """Read options and schedule capture/assembly tasks for each camera."""
        # Load persistent state
        self._store = Store(
            self.hass, _STORAGE_VERSION,
            f"{DOMAIN}.{self.entry.entry_id}.periodic_assembly",
        )
        stored: dict = await self._store.async_load() or {}
        for cam_id, ts in stored.items():
            try:
                self._last_periodic_assembly[cam_id] = datetime.fromisoformat(ts)
            except (ValueError, TypeError):
                pass

        # Create default placeholder background if not yet present
        await self.hass.async_add_executor_job(
            _create_default_placeholder, self.storage_path
        )

        cameras: dict = self.entry.options.get(CONF_CAMERAS, {})
        for camera_id, cam_config in cameras.items():
            await self._schedule_camera(camera_id, cam_config)

        # Re-schedule when options change (user edits via OptionsFlow)
        self.entry.async_on_unload(
            self.entry.add_update_listener(self._handle_options_update)
        )

        # Restore frames_today counters from disk
        await self._restore_frame_counts()

        # Startup recovery: assemble yesterday's missing daily timelapses
        await self._recover_missing_daily()

        # Run cleanup for all cameras — catches any missed cleanup windows
        # (e.g. HA was restarted before the daily assembly trigger fired).
        # async_cleanup_camera is idempotent and safe to call at any time.
        for camera_id in cameras:
            await self.async_cleanup_camera(camera_id)

    async def async_unload(self) -> None:
        """Cancel all scheduled tasks."""
        for unsub in list(self._capture_unsubs.values()):
            unsub()
        for unsub in list(self._assembly_unsubs.values()):
            unsub()
        for unsub in list(self._periodic_assembly_unsubs.values()):
            unsub()
        self._capture_unsubs.clear()
        self._assembly_unsubs.clear()
        self._periodic_assembly_unsubs.clear()

    async def _handle_options_update(
        self, hass: HomeAssistant, entry: ConfigEntry
    ) -> None:
        """Called when OptionsFlow saves new data. Re-schedule affected cameras."""
        new_cameras: dict = entry.options.get(CONF_CAMERAS, {})
        old_ids = set(self._capture_unsubs.keys())
        new_ids = set(new_cameras.keys())

        # Remove cameras that were deleted
        for camera_id in old_ids - new_ids:
            self._cancel_camera(camera_id)

        # Add or update cameras
        for camera_id, cam_config in new_cameras.items():
            if camera_id in old_ids:
                self._cancel_camera(camera_id)
            await self._schedule_camera(camera_id, cam_config)

        async_dispatcher_send(hass, SIGNAL_CAMERAS_UPDATED.format(entry.entry_id))

    # ------------------------------------------------------------------
    # Scheduling helpers
    # ------------------------------------------------------------------

    async def _schedule_camera(self, camera_id: str, config: dict) -> None:
        """Register capture interval and optional daily assembly trigger."""
        if camera_id not in self._write_locks:
            self._write_locks[camera_id] = asyncio.Lock()

        interval = timedelta(minutes=int(config[CONF_INTERVAL_MINUTES]))

        @callback
        def _capture_cb(now: datetime, cid: str = camera_id) -> None:
            self.hass.async_create_task(self.async_capture_frame(cid))

        self._capture_unsubs[camera_id] = async_track_time_interval(
            self.hass,
            _capture_cb,
            interval,
        )

        # Daily assembly is not needed for pure rolling mode
        mode = config.get(CONF_MODE, MODE_DAILY)
        if mode in (MODE_DAILY, MODE_BOTH):
            await self._schedule_assembly_trigger(camera_id, config)

            # Periodic mid-day assembly if configured
            assembly_interval = int(
                config.get(CONF_ASSEMBLY_INTERVAL_MINUTES, 0)
            )
            if assembly_interval > 0:
                self._schedule_periodic_assembly(camera_id, config, assembly_interval)

    def _cancel_camera(self, camera_id: str) -> None:
        """Cancel capture and assembly tasks for a camera."""
        if camera_id in self._periodic_assembly_unsubs:
            self._periodic_assembly_unsubs.pop(camera_id)()
        if camera_id in self._capture_unsubs:
            self._capture_unsubs.pop(camera_id)()
        if camera_id in self._assembly_unsubs:
            self._assembly_unsubs.pop(camera_id)()

    def _schedule_periodic_assembly(
        self, camera_id: str, config: dict, interval_minutes: int
    ) -> None:
        """Schedule next periodic assembly via one-shot point-in-time trigger.

        Uses the stored last-run timestamp to compute the next fire time, so
        the interval survives HA restarts. If one or more intervals were missed
        while HA was down, a single catch-up run is fired after a short delay
        instead of running multiple times.
        """
        interval = timedelta(minutes=interval_minutes)
        now = dt_util.now()
        last_run = self._last_periodic_assembly.get(camera_id)

        if last_run is None:
            # First time ever — wait one full interval before assembling
            next_run = now + interval
        else:
            next_run = last_run + interval
            if next_run <= now:
                # Missed one or more intervals: fire once soon, then resume normally
                next_run = now + timedelta(seconds=30)

        # Cancel any existing one-shot for this camera
        if camera_id in self._periodic_assembly_unsubs:
            self._periodic_assembly_unsubs.pop(camera_id)()

        @callback
        def _cb(
            _t: datetime,
            cid: str = camera_id,
            cfg: dict = config,
            mins: int = interval_minutes,
        ) -> None:
            self.hass.async_create_task(self._run_periodic_assembly(cid, cfg, mins))

        self._periodic_assembly_unsubs[camera_id] = async_track_point_in_time(
            self.hass, _cb, next_run
        )
        _LOGGER.debug(
            "Next periodic assembly for %s scheduled at %s", camera_id, next_run
        )

    async def _run_periodic_assembly(
        self, camera_id: str, config: dict, interval_minutes: int
    ) -> None:
        """Execute periodic assembly, persist timestamp, schedule next run."""
        now = dt_util.now()
        self._last_periodic_assembly[camera_id] = now
        await self._save_assembly_times()
        await self.async_assemble_daily(camera_id, now.date())
        self._schedule_periodic_assembly(camera_id, config, interval_minutes)

    async def _save_assembly_times(self) -> None:
        """Persist last periodic assembly timestamps to HA storage."""
        if self._store is None:
            return
        await self._store.async_save(
            {k: v.isoformat() for k, v in self._last_periodic_assembly.items()}
        )

    async def _schedule_assembly_trigger(self, camera_id: str, config: dict) -> None:
        """Schedule the daily assembly at end-of-active-window or 23:59."""
        now = dt_util.now()
        time_range_type = config.get(CONF_TIME_RANGE_TYPE, TIME_RANGE_SUNRISE_SUNSET)

        if time_range_type == TIME_RANGE_SUNRISE_SUNSET:
            trigger_time = self._get_sunset_today()
            # If sunset has already passed today, trigger at midnight and reassemble
            if trigger_time is None or trigger_time <= now:
                trigger_time = now.replace(hour=23, minute=59, second=0, microsecond=0)
                if trigger_time <= now:
                    trigger_time = trigger_time + timedelta(days=1)
        elif time_range_type == TIME_RANGE_CUSTOM:
            end_str = config.get(CONF_TIME_END, "19:00:00")
            h, m = int(end_str[:2]), int(end_str[3:5])
            trigger_time = now.replace(hour=h, minute=m, second=0, microsecond=0)
            if trigger_time <= now:
                trigger_time = trigger_time + timedelta(days=1)
        else:
            # TIME_RANGE_ALWAYS → assemble at 23:59
            trigger_time = now.replace(hour=23, minute=59, second=0, microsecond=0)
            if trigger_time <= now:
                trigger_time = trigger_time + timedelta(days=1)

        @callback
        def _assembly_cb(t: datetime, cid: str = camera_id) -> None:
            self.hass.async_create_task(self._daily_assembly_and_reschedule(cid))

        self._assembly_unsubs[camera_id] = async_track_point_in_time(
            self.hass,
            _assembly_cb,
            trigger_time,
        )

    async def _daily_assembly_and_reschedule(self, camera_id: str) -> None:
        """Assemble today's timelapse, cleanup old files, then schedule next day."""
        cameras = self.entry.options.get(CONF_CAMERAS, {})
        config = cameras.get(camera_id)
        if config is None:
            return

        target_date = dt_util.now().date()
        await self.async_assemble_daily(camera_id, target_date)
        await self.async_cleanup_camera(camera_id)
        await self._schedule_assembly_trigger(camera_id, config)

    # ------------------------------------------------------------------
    # Time window helpers
    # ------------------------------------------------------------------

    def _is_in_active_window(self, camera_id: str) -> bool:
        """Return True if current time is within the configured capture window."""
        cameras = self.entry.options.get(CONF_CAMERAS, {})
        config = cameras.get(camera_id)
        if config is None:
            return False

        time_range_type = config.get(CONF_TIME_RANGE_TYPE, TIME_RANGE_SUNRISE_SUNSET)
        now = dt_util.now()

        if time_range_type == TIME_RANGE_ALWAYS:
            return True

        if time_range_type == TIME_RANGE_SUNRISE_SUNSET:
            today = now.date()
            sunrise = self._get_astral_event(today, "sunrise")
            sunset = self._get_astral_event(today, "sunset")
            if sunrise is None or sunset is None:
                # Polar day/night fallback: always capture
                return True
            return sunrise <= now <= sunset

        if time_range_type == TIME_RANGE_CUSTOM:
            start_str = config.get(CONF_TIME_START, "07:00:00")
            end_str = config.get(CONF_TIME_END, "19:00:00")
            start = now.replace(
                hour=int(start_str[:2]),
                minute=int(start_str[3:5]),
                second=0,
                microsecond=0,
            )
            end = now.replace(
                hour=int(end_str[:2]),
                minute=int(end_str[3:5]),
                second=0,
                microsecond=0,
            )
            return start <= now <= end

        return True

    def _get_astral_event(self, target_date: date, event: str):
        """Return an aware datetime for a solar event, or None."""
        try:
            from homeassistant.helpers.sun import get_astral_event_date
            result = get_astral_event_date(self.hass, event, target_date)
            return result
        except Exception:
            return None

    def _get_sunset_today(self):
        """Return today's sunset as an aware datetime, or None."""
        return self._get_astral_event(dt_util.now().date(), "sunset")

    # ------------------------------------------------------------------
    # Frame capture
    # ------------------------------------------------------------------

    async def async_capture_frame(self, camera_id: str, bypass_window: bool = False) -> None:
        """Capture one frame from a camera entity."""
        if not bypass_window and not self._is_in_active_window(camera_id):
            return

        cameras = self.entry.options.get(CONF_CAMERAS, {})
        config = cameras.get(camera_id, {})
        n_frames = int(config.get(CONF_HDR_FRAMES, DEFAULT_HDR_FRAMES))

        try:
            from homeassistant.components.camera import async_get_image
            if n_frames > 1:
                raw_frames = []
                for i in range(n_frames):
                    if i > 0:
                        await asyncio.sleep(0.5)
                    img = await async_get_image(self.hass, camera_id, timeout=10)
                    raw_frames.append(img.content)
                content = await self.hass.async_add_executor_job(_average_frames, raw_frames)
            else:
                image = await async_get_image(self.hass, camera_id, timeout=10)
                content = image.content
        except Exception as err:
            _LOGGER.warning("Frame capture failed for %s: %s", camera_id, err)
            await self._update_unavailable_frame(camera_id)
            return

        now = dt_util.now()

        # Update frame counter (reset daily)
        today = now.date()
        if self._frame_count_dates.get(camera_id) != today:
            self._frame_counts[camera_id] = 0
            self._frame_count_dates[camera_id] = today
        self._frame_counts[camera_id] = self._frame_counts.get(camera_id, 0) + 1
        async_dispatcher_send(
            self.hass,
            SIGNAL_SENSOR_UPDATE.format(self.entry.entry_id, camera_id),
        )

        camera_slug = _camera_slug(camera_id)
        date_str = now.strftime("%Y-%m-%d")
        time_str = now.strftime("%H%M%S")
        frame_dir = Path(self.storage_path) / "frames" / camera_slug / date_str
        frame_path = frame_dir / f"{time_str}.jpg"

        def _save() -> None:
            frame_dir.mkdir(parents=True, exist_ok=True)
            frame_path.write_bytes(content)

        await self.hass.async_add_executor_job(_save)
        self._latest_frame_path[camera_id] = frame_path
        _LOGGER.debug("Frame saved: %s", frame_path)

        # Trigger rolling assembly (debounced)
        cameras = self.entry.options.get(CONF_CAMERAS, {})
        config = cameras.get(camera_id, {})
        mode = config.get(CONF_MODE, MODE_DAILY)
        if mode in (MODE_ROLLING, MODE_BOTH):
            last = self._last_rolling.get(camera_id)
            elapsed = (now - last).total_seconds() if last else ROLLING_DEBOUNCE_SECONDS + 1
            if elapsed >= ROLLING_DEBOUNCE_SECONDS:
                self._last_rolling[camera_id] = now
                self.hass.async_create_task(self.async_assemble_rolling(camera_id))

    # ------------------------------------------------------------------
    # Unavailable-frame placeholder
    # ------------------------------------------------------------------

    async def _update_unavailable_frame(self, camera_id: str) -> None:
        """Point the image entity at the placeholder image when the camera is unavailable."""
        cameras = self.entry.options.get(CONF_CAMERAS, {})
        config = cameras.get(camera_id, {})
        custom_bg = config.get(CONF_PLACEHOLDER_IMAGE, DEFAULT_PLACEHOLDER_IMAGE).strip()
        bg_path = Path(custom_bg) if custom_bg else Path(self.storage_path) / "placeholder.jpg"

        if not bg_path.exists():
            _LOGGER.debug("Placeholder image not found at %s, skipping update", bg_path)
            return

        self._latest_frame_path[camera_id] = bg_path
        async_dispatcher_send(
            self.hass,
            SIGNAL_SENSOR_UPDATE.format(self.entry.entry_id, camera_id),
        )

    # ------------------------------------------------------------------
    # Assembly
    # ------------------------------------------------------------------

    async def async_assemble_daily(self, camera_id: str, target_date: date) -> None:
        """Collect frames for a given date and write a timelapse file."""
        cameras = self.entry.options.get(CONF_CAMERAS, {})
        config = cameras.get(camera_id)
        if config is None:
            _LOGGER.warning("async_assemble_daily: camera %s not configured", camera_id)
            return

        camera_slug = _camera_slug(camera_id)
        date_str = target_date.strftime("%Y-%m-%d")
        frame_dir = Path(self.storage_path) / "frames" / camera_slug / date_str

        frames = await self.hass.async_add_executor_job(
            _collect_frames_day, frame_dir
        )

        if len(frames) < 2:
            _LOGGER.debug("Not enough frames for %s on %s (%d frame(s))", camera_id, date_str, len(frames))
            return

        fmt = config.get(CONF_OUTPUT_FORMAT, FORMAT_MP4)
        fps = int(config.get(CONF_FPS, 10))
        output_dir = Path(self.storage_path) / camera_slug
        output_path = output_dir / f"{date_str}.{fmt}"

        # Streaming mode: append new frames to existing file, then discard frame files.
        # Only valid for daily mode — rolling mode needs frames to rebuild from scratch.
        mode = config.get(CONF_MODE, MODE_DAILY)
        keep_frames = bool(config.get(CONF_KEEP_FRAMES, DEFAULT_KEEP_FRAMES))
        streaming = not keep_frames and mode == MODE_DAILY

        # In streaming mode pass the existing timelapse as append target (if it exists)
        append_to = output_path if streaming and output_path.exists() else None

        lock = self._write_locks.setdefault(camera_id, asyncio.Lock())
        async with lock:
            await self.hass.async_add_executor_job(
                self._write_timelapse, frames, output_path, fps, fmt, append_to
            )
        _LOGGER.info("Daily timelapse written: %s", output_path)

        # Delete frame directory after successful assembly in streaming mode
        if streaming:
            await self.hass.async_add_executor_job(
                shutil.rmtree, str(frame_dir), True
            )
            _LOGGER.debug("Streaming mode: deleted frame dir %s", frame_dir)

        # Update sensor state
        self._last_timelapse_info[camera_id] = {
            "path": str(output_path),
            "type": MODE_DAILY,
            "assembled_at": dt_util.now().isoformat(),
        }
        mb = await self.hass.async_add_executor_job(
            self._calc_disk_mb, _camera_slug(camera_id)
        )
        self._disk_usage_mb[camera_id] = mb
        async_dispatcher_send(
            self.hass,
            SIGNAL_SENSOR_UPDATE.format(self.entry.entry_id, camera_id),
        )

        # Fire HA event
        self.hass.bus.async_fire(
            EVENT_TIMELAPSE_READY,
            {
                "camera_entity_id": camera_id,
                "type": MODE_DAILY,
                "path": str(output_path),
                "date": date_str,
            },
        )

    async def async_assemble_rolling(self, camera_id: str) -> None:
        """Build a rolling timelapse from the last N days of frames."""
        cameras = self.entry.options.get(CONF_CAMERAS, {})
        config = cameras.get(camera_id)
        if config is None:
            return

        n_days = int(config.get(CONF_ROLLING_PERIOD_DAYS, 7))
        camera_slug = _camera_slug(camera_id)
        frames_root = Path(self.storage_path) / "frames" / camera_slug
        today = dt_util.now().date()

        frames = await self.hass.async_add_executor_job(
            _collect_frames_rolling, frames_root, today, n_days
        )

        if len(frames) < 2:
            return

        fmt = config.get(CONF_OUTPUT_FORMAT, FORMAT_MP4)
        fps = int(config.get(CONF_FPS, 10))
        output_dir = Path(self.storage_path) / camera_slug
        output_path = output_dir / f"rolling_{n_days}d.{fmt}"

        lock = self._write_locks.setdefault(camera_id, asyncio.Lock())
        async with lock:
            await self.hass.async_add_executor_job(
                self._write_timelapse, frames, output_path, fps, fmt
            )
        _LOGGER.info("Rolling timelapse written: %s", output_path)

        # Update sensor state
        self._last_timelapse_info[camera_id] = {
            "path": str(output_path),
            "type": MODE_ROLLING,
            "assembled_at": dt_util.now().isoformat(),
        }
        mb = await self.hass.async_add_executor_job(
            self._calc_disk_mb, _camera_slug(camera_id)
        )
        self._disk_usage_mb[camera_id] = mb
        async_dispatcher_send(
            self.hass,
            SIGNAL_SENSOR_UPDATE.format(self.entry.entry_id, camera_id),
        )

        # Fire HA event
        self.hass.bus.async_fire(
            EVENT_TIMELAPSE_READY,
            {
                "camera_entity_id": camera_id,
                "type": MODE_ROLLING,
                "path": str(output_path),
            },
        )

        # For rolling-only cameras, run cleanup once per day here
        # (daily/both cameras get cleanup via _daily_assembly_and_reschedule)
        cameras = self.entry.options.get(CONF_CAMERAS, {})
        config = cameras.get(camera_id, {})
        if config.get(CONF_MODE) == MODE_ROLLING:
            today = dt_util.now().date()
            if self._last_cleanup_date.get(camera_id) != today:
                self._last_cleanup_date[camera_id] = today
                await self.async_cleanup_camera(camera_id)

    # ------------------------------------------------------------------
    # Writers (run in executor thread)
    # ------------------------------------------------------------------

    def _write_timelapse(
        self,
        frames: list[Path],
        output: Path,
        fps: int,
        fmt: str,
        append_to: Path | None = None,
    ) -> None:
        """Dispatch to the appropriate format writer.

        If *append_to* is provided (streaming mode), new frames are appended to
        that file in-place rather than creating a fresh timelapse from scratch.
        """
        output.parent.mkdir(parents=True, exist_ok=True)
        if append_to is not None and append_to.exists():
            if fmt == FORMAT_GIF:
                self._append_gif(frames, append_to, fps)
            elif fmt == FORMAT_APNG:
                self._append_apng(frames, append_to, fps)
            else:
                self._append_mp4(frames, append_to, fps)
        else:
            if fmt == FORMAT_GIF:
                self._write_gif(frames, output, fps)
            elif fmt == FORMAT_APNG:
                self._write_apng(frames, output, fps)
            else:
                self._write_mp4(frames, output, fps)

    def _write_gif(self, frames: list[Path], output: Path, fps: int) -> None:
        from PIL import Image  # noqa: PLC0415

        images = [Image.open(f).convert("RGB") for f in frames]
        duration_ms = max(1, int(1000 / fps))
        images[0].save(
            output,
            save_all=True,
            append_images=images[1:],
            duration=duration_ms,
            loop=0,
            optimize=True,
        )

    def _write_apng(self, frames: list[Path], output: Path, fps: int) -> None:
        from PIL import Image  # noqa: PLC0415

        images = [Image.open(f).convert("RGBA") for f in frames]
        duration_ms = max(1, int(1000 / fps))
        images[0].save(
            output,
            save_all=True,
            append_images=images[1:],
            duration=duration_ms,
            loop=0,
        )

    def _write_mp4(self, frames: list[Path], output: Path, fps: int) -> None:
        import subprocess  # noqa: PLC0415
        import tempfile  # noqa: PLC0415

        import imageio_ffmpeg  # noqa: PLC0415

        ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()

        # Build a concat demuxer list file so ffmpeg reads frames in order
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False
        ) as f:
            list_path = f.name
            duration = 1.0 / fps
            for frame in frames:
                f.write(f"file '{frame.absolute()}'\n")
                f.write(f"duration {duration:.6f}\n")

        try:
            subprocess.run(
                [
                    ffmpeg_exe, "-y",
                    "-f", "concat", "-safe", "0",
                    "-i", list_path,
                    # Ensure even dimensions required by yuv420p
                    "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
                    "-c:v", "libx264",
                    "-crf", "23",
                    "-preset", "fast",
                    "-pix_fmt", "yuv420p",
                    str(output),
                ],
                check=True,
                capture_output=True,
            )
        finally:
            os.unlink(list_path)

    # ------------------------------------------------------------------
    # Append writers (streaming mode) — run in executor thread
    # ------------------------------------------------------------------

    def _append_mp4(self, new_frames: list[Path], existing: Path, fps: int) -> None:
        """Append *new_frames* to an existing MP4 without re-encoding.

        Strategy: encode new frames into a temporary MP4, then use ffmpeg
        concat with -c copy to join existing + new into a replacement file.
        """
        import subprocess  # noqa: PLC0415
        import tempfile  # noqa: PLC0415

        import imageio_ffmpeg  # noqa: PLC0415

        ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
        tmp_new = existing.with_name(existing.stem + "._new.mp4")
        tmp_out = existing.with_name(existing.stem + "._out.mp4")
        list_path: str | None = None
        try:
            # Step 1: encode new frames to a temp file (same settings as _write_mp4)
            self._write_mp4(new_frames, tmp_new, fps)

            # Step 2: concat list — existing video followed by new segment
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".txt", delete=False
            ) as f:
                list_path = f.name
                f.write(f"file '{existing.absolute()}'\n")
                f.write(f"file '{tmp_new.absolute()}'\n")

            # Step 3: join with stream copy (no quality loss, fast)
            subprocess.run(
                [
                    ffmpeg_exe, "-y",
                    "-f", "concat", "-safe", "0",
                    "-i", list_path,
                    "-c", "copy",
                    str(tmp_out),
                ],
                check=True,
                capture_output=True,
            )

            # Step 4: atomically replace existing file
            tmp_out.replace(existing)
        finally:
            tmp_new.unlink(missing_ok=True)
            tmp_out.unlink(missing_ok=True)
            if list_path:
                os.unlink(list_path)

    def _append_gif(self, new_frames: list[Path], existing: Path, fps: int) -> None:
        """Append new JPEG frames to an existing GIF animation."""
        from PIL import Image  # noqa: PLC0415

        # Extract all frames from the existing GIF
        existing_images: list[Image.Image] = []
        with Image.open(existing) as gif:
            for i in range(getattr(gif, "n_frames", 1)):
                gif.seek(i)
                existing_images.append(gif.copy().convert("RGB"))

        new_images = [Image.open(f).convert("RGB") for f in new_frames]
        all_images = existing_images + new_images
        duration_ms = max(1, int(1000 / fps))
        all_images[0].save(
            existing,
            save_all=True,
            append_images=all_images[1:],
            duration=duration_ms,
            loop=0,
            optimize=True,
        )

    def _append_apng(self, new_frames: list[Path], existing: Path, fps: int) -> None:
        """Append new JPEG frames to an existing APNG animation."""
        from PIL import Image  # noqa: PLC0415

        existing_images: list[Image.Image] = []
        with Image.open(existing) as apng:
            for i in range(getattr(apng, "n_frames", 1)):
                apng.seek(i)
                existing_images.append(apng.copy().convert("RGBA"))

        new_images = [Image.open(f).convert("RGBA") for f in new_frames]
        all_images = existing_images + new_images
        duration_ms = max(1, int(1000 / fps))
        all_images[0].save(
            existing,
            save_all=True,
            append_images=all_images[1:],
            duration=duration_ms,
            loop=0,
        )

    def _calc_disk_mb(self, camera_slug: str) -> float:
        """Return total disk usage in MB for a camera. Runs in executor."""
        total = 0
        for root in (
            Path(self.storage_path) / "frames" / camera_slug,
            Path(self.storage_path) / camera_slug,
        ):
            if root.exists():
                for dirpath, _, filenames in os.walk(root):
                    for fname in filenames:
                        try:
                            total += os.path.getsize(os.path.join(dirpath, fname))
                        except OSError:
                            pass
        return round(total / (1024 * 1024), 2)

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    async def async_cleanup_camera(self, camera_id: str) -> None:
        """Delete frames and daily timelapse files older than max_retention_days."""
        cameras = self.entry.options.get(CONF_CAMERAS, {})
        config = cameras.get(camera_id, {})
        max_days = int(config.get(CONF_MAX_RETENTION_DAYS, 30))
        if max_days == 0:
            return

        camera_slug = _camera_slug(camera_id)
        cutoff = dt_util.now().date() - timedelta(days=max_days)

        await self.hass.async_add_executor_job(
            self._cleanup_sync, camera_slug, cutoff
        )

    def _cleanup_sync(self, camera_slug: str, cutoff: date) -> None:
        # Remove old frame directories
        frames_root = Path(self.storage_path) / "frames" / camera_slug
        if frames_root.exists():
            for day_dir in frames_root.iterdir():
                if not day_dir.is_dir():
                    continue
                try:
                    dir_date = date.fromisoformat(day_dir.name)
                except ValueError:
                    continue
                if dir_date < cutoff:
                    shutil.rmtree(day_dir, ignore_errors=True)
                    _LOGGER.debug("Removed old frames dir: %s", day_dir)

        # Remove old daily timelapse files (skip rolling_ files)
        output_dir = Path(self.storage_path) / camera_slug
        if not output_dir.exists():
            return
        for tl_file in output_dir.iterdir():
            if not tl_file.is_file():
                continue
            if tl_file.stem.startswith("rolling_"):
                continue
            try:
                file_date = date.fromisoformat(tl_file.stem)
            except ValueError:
                continue
            if file_date < cutoff:
                tl_file.unlink(missing_ok=True)
                _LOGGER.debug("Removed old timelapse: %s", tl_file)

    # ------------------------------------------------------------------
    # Startup recovery
    # ------------------------------------------------------------------

    async def _restore_frame_counts(self) -> None:
        """Restore in-memory sensor state from disk after a restart."""
        cameras: dict = self.entry.options.get(CONF_CAMERAS, {})
        today = dt_util.now().date()
        date_str = today.strftime("%Y-%m-%d")
        for camera_id in cameras:
            camera_slug = _camera_slug(camera_id)

            # frames_today
            frame_dir = Path(self.storage_path) / "frames" / camera_slug / date_str
            count: int = await self.hass.async_add_executor_job(
                lambda d=frame_dir: len(list(d.glob("*.jpg"))) if d.exists() else 0
            )
            if count > 0:
                self._frame_counts[camera_id] = count
                self._frame_count_dates[camera_id] = today
                _LOGGER.debug(
                    "Restored frames_today=%d for %s from disk", count, camera_id
                )

            # disk_usage
            mb: float = await self.hass.async_add_executor_job(
                self._calc_disk_mb, camera_slug
            )
            self._disk_usage_mb[camera_id] = mb

            # last_timelapse — find the most recently modified file in the output dir
            output_dir = Path(self.storage_path) / camera_slug
            info: dict | None = await self.hass.async_add_executor_job(
                _find_latest_timelapse, output_dir
            )
            if info:
                self._last_timelapse_info[camera_id] = info
                _LOGGER.debug(
                    "Restored last_timelapse for %s: %s", camera_id, info["path"]
                )

            # latest_frame — most recent JPEG across all day dirs, or placeholder
            frames_root = Path(self.storage_path) / "frames" / camera_slug
            placeholder_path = Path(self.storage_path) / ".placeholders" / f"{camera_slug}.jpg"
            latest: Path | None = await self.hass.async_add_executor_job(
                _find_latest_frame_any, frames_root, placeholder_path
            )
            if latest:
                self._latest_frame_path[camera_id] = latest

    def get_latest_frame_path(self, camera_id: str) -> Path | None:
        """Return path of the most recently captured frame, or None."""
        return self._latest_frame_path.get(camera_id)

    async def _recover_missing_daily(self) -> None:
        """On startup, assemble any missing daily timelapses from yesterday."""
        cameras: dict = self.entry.options.get(CONF_CAMERAS, {})
        yesterday = dt_util.now().date() - timedelta(days=1)

        for camera_id, config in cameras.items():
            mode = config.get(CONF_MODE, MODE_DAILY)
            if mode not in (MODE_DAILY, MODE_BOTH):
                continue

            camera_slug = _camera_slug(camera_id)
            fmt = config.get(CONF_OUTPUT_FORMAT, FORMAT_MP4)
            output_file = (
                Path(self.storage_path)
                / camera_slug
                / f"{yesterday.strftime('%Y-%m-%d')}.{fmt}"
            )

            if output_file.exists():
                continue

            # Frames exist but file is missing → assemble now
            frames_dir = (
                Path(self.storage_path)
                / "frames"
                / camera_slug
                / yesterday.strftime("%Y-%m-%d")
            )
            has_frames = await self.hass.async_add_executor_job(
                lambda d=frames_dir: d.exists() and bool(list(d.glob("*.jpg"))[:1])
            )
            if has_frames:
                _LOGGER.info(
                    "Recovering missing daily timelapse for %s on %s",
                    camera_id,
                    yesterday,
                )
                await self.async_assemble_daily(camera_id, yesterday)
                await self.async_cleanup_camera(camera_id)


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _create_default_placeholder(storage_path: str) -> None:
    """Create a simple dark-grey placeholder JPEG in *storage_path* if absent.

    The file is used as the background when 'Camera unavailable' is overlaid.
    Users can replace it with any JPEG they prefer.
    """
    target = Path(storage_path) / "placeholder.jpg"
    if target.exists():
        return
    from PIL import Image  # noqa: PLC0415

    target.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (640, 360), color=(30, 30, 30))
    img.save(str(target), format="JPEG", quality=85)



def _find_latest_frame(frame_dir: Path) -> Path | None:
    """Return the most recently modified JPEG in *frame_dir*, or None."""
    if not frame_dir.exists():
        return None
    files = list(frame_dir.glob("*.jpg"))
    return max(files, key=lambda f: f.stat().st_mtime) if files else None


def _find_latest_frame_any(frames_root: Path, placeholder_path: Path) -> Path | None:
    """Return the most recently modified JPEG across all day subdirectories.

    Falls back to the placeholder if no real frames exist on disk.
    """
    best: Path | None = None
    best_mtime = 0.0
    if frames_root.exists():
        for day_dir in frames_root.iterdir():
            if not day_dir.is_dir():
                continue
            for f in day_dir.glob("*.jpg"):
                mtime = f.stat().st_mtime
                if mtime > best_mtime:
                    best_mtime = mtime
                    best = f
    if best is None and placeholder_path.exists():
        best = placeholder_path
    return best


def _find_latest_timelapse(output_dir: Path) -> dict | None:
    """Return info dict for the most recently modified timelapse file, or None."""
    if not output_dir.exists():
        return None
    files = [f for f in output_dir.iterdir() if f.is_file()]
    if not files:
        return None
    latest = max(files, key=lambda f: f.stat().st_mtime)
    timelapse_type = MODE_ROLLING if latest.stem.startswith("rolling_") else MODE_DAILY
    assembled_at = dt_util.as_local(
        dt_util.utc_from_timestamp(latest.stat().st_mtime)
    ).isoformat()
    return {"path": str(latest), "type": timelapse_type, "assembled_at": assembled_at}


def _camera_slug(entity_id: str) -> str:
    """Convert camera entity_id to a filesystem-safe directory name."""
    slug = entity_id.replace(".", "_")
    if slug.startswith("camera_"):
        slug = slug[len("camera_"):]
    return slug


def _collect_frames_day(frame_dir: Path) -> list[Path]:
    """Collect sorted frame paths for one day. Runs in executor."""
    if not frame_dir.exists():
        return []
    return sorted(frame_dir.glob("*.jpg"))


def _collect_frames_rolling(
    frames_root: Path, today: date, n_days: int
) -> list[Path]:
    """Collect sorted frame paths for the last N days. Runs in executor."""
    frames: list[Path] = []
    for i in range(n_days, -1, -1):
        day = today - timedelta(days=i)
        day_dir = frames_root / day.strftime("%Y-%m-%d")
        if day_dir.exists():
            frames.extend(sorted(day_dir.glob("*.jpg")))
    return frames


def _average_frames(raw_list: list[bytes]) -> bytes:
    """Average N JPEG frames pixel-wise using iterative Pillow blending. Runs in executor."""
    import io  # noqa: PLC0415

    from PIL import Image  # noqa: PLC0415

    images = [Image.open(io.BytesIO(b)).convert("RGB") for b in raw_list]
    result = images[0]
    for i, img in enumerate(images[1:], 2):
        result = Image.blend(result, img, 1.0 / i)
    buf = io.BytesIO()
    result.save(buf, format="JPEG")
    return buf.getvalue()
