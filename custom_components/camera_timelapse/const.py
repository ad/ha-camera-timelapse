"""Constants for Camera Timelapse integration."""

DOMAIN = "camera_timelapse"

# Config entry data keys
CONF_STORAGE_PATH = "storage_path"
CONF_CAMERAS = "cameras"

# Per-camera config keys
CONF_CAMERA_ENTITY_ID = "camera_entity_id"
CONF_INTERVAL_MINUTES = "interval_minutes"
CONF_TIME_RANGE_TYPE = "time_range_type"
CONF_TIME_START = "time_start"
CONF_TIME_END = "time_end"
CONF_MODE = "mode"
CONF_ROLLING_PERIOD_DAYS = "rolling_period_days"
CONF_OUTPUT_FORMAT = "output_format"
CONF_FPS = "fps"
CONF_MAX_RETENTION_DAYS = "max_retention_days"
CONF_ASSEMBLY_INTERVAL_MINUTES = "assembly_interval_minutes"

# Time range types
TIME_RANGE_SUNRISE_SUNSET = "sunrise_sunset"
TIME_RANGE_CUSTOM = "custom"
TIME_RANGE_ALWAYS = "always"

# Timelapse modes
MODE_DAILY = "daily"
MODE_ROLLING = "rolling"
MODE_BOTH = "both"

# Output formats
FORMAT_GIF = "gif"
FORMAT_APNG = "apng"
FORMAT_MP4 = "mp4"

# Service names
SERVICE_CAPTURE_NOW = "capture_now"
SERVICE_GENERATE_TIMELAPSE = "generate_timelapse"

# OptionsFlow actions
ACTION_ADD = "add"
ACTION_EDIT = "edit"
ACTION_REMOVE = "remove"

# Defaults
DEFAULT_STORAGE_PATH = "/media/timelapses"
DEFAULT_INTERVAL_MINUTES = 5
DEFAULT_TIME_RANGE_TYPE = TIME_RANGE_SUNRISE_SUNSET
DEFAULT_MODE = MODE_DAILY
DEFAULT_ROLLING_PERIOD_DAYS = 7
DEFAULT_OUTPUT_FORMAT = FORMAT_MP4
DEFAULT_FPS = 10
DEFAULT_MAX_RETENTION_DAYS = 30
# 0 = only at end of active window; >0 = also assemble periodically every N minutes
DEFAULT_ASSEMBLY_INTERVAL_MINUTES = 0

# Rolling assembly debounce interval in seconds
ROLLING_DEBOUNCE_SECONDS = 300

# Events
EVENT_TIMELAPSE_READY = "camera_timelapse_timelapse_ready"

# Dispatcher signals
SIGNAL_CAMERAS_UPDATED = "camera_timelapse_cameras_updated_{}"  # .format(entry_id)
SIGNAL_SENSOR_UPDATE = "camera_timelapse_sensor_update_{}_{}"   # .format(entry_id, camera_id)

# Frame averaging (HDR-like noise reduction)
CONF_HDR_FRAMES = "hdr_frames"
DEFAULT_HDR_FRAMES = 0  # 0 = disabled, 2-5 = number of frames to average

# Streaming mode: discard frame files after each assembly instead of keeping them.
# Incompatible with rolling mode (rolling needs historical frames on disk).
CONF_KEEP_FRAMES = "keep_frames"
DEFAULT_KEEP_FRAMES = True
