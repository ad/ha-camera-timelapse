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

# JPEG quality for saved frames (1–95). Lower = smaller files, higher = better quality.
CONF_FRAME_QUALITY = "frame_quality"
DEFAULT_FRAME_QUALITY = 85

# Target frame dimensions in pixels. 0 = use original size.
# If only one is set, the other is derived from the original aspect ratio.
CONF_FRAME_WIDTH = "frame_width"
CONF_FRAME_HEIGHT = "frame_height"
DEFAULT_FRAME_WIDTH = 0
DEFAULT_FRAME_HEIGHT = 0

# Streaming mode: discard frame files after each assembly instead of keeping them.
# Incompatible with rolling mode (rolling needs historical frames on disk).
CONF_KEEP_FRAMES = "keep_frames"
DEFAULT_KEEP_FRAMES = True

# Placeholder image shown when the camera fails to provide a frame.
# The file at this path is used as a background; "Camera unavailable" text
# is overlaid on top at runtime. Empty string = use the auto-generated default
# at {storage_path}/placeholder.jpg.
CONF_PLACEHOLDER_IMAGE = "placeholder_image"
DEFAULT_PLACEHOLDER_IMAGE = ""

# Frame stabilization: compensate for camera shake via numpy phase correlation.
# Applied at timelapse assembly time (translation-only correction).
CONF_STABILIZATION = "stabilization"
DEFAULT_STABILIZATION = False

# Sensor overlay: draw live sensor values on each captured frame.
CONF_OVERLAY_SENSORS = "overlay_sensors"   # list[str] of entity_ids
DEFAULT_OVERLAY_SENSORS: list = []

CONF_OVERLAY_POSITION = "overlay_position"  # top_left | top_right | bottom_left | bottom_right
DEFAULT_OVERLAY_POSITION = "top_left"

CONF_OVERLAY_FONT_SIZE = "overlay_font_size"  # int, pixels
DEFAULT_OVERLAY_FONT_SIZE = 16

CONF_OVERLAY_TEXT_COLOR = "overlay_text_color"   # [r, g, b]
DEFAULT_OVERLAY_TEXT_COLOR: list = [255, 255, 255]  # white

CONF_OVERLAY_STROKE_WIDTH = "overlay_stroke_width"  # int, pixels (0 = off)
DEFAULT_OVERLAY_STROKE_WIDTH = 1

CONF_OVERLAY_STROKE_COLOR = "overlay_stroke_color"  # [r, g, b]
DEFAULT_OVERLAY_STROKE_COLOR: list = [0, 0, 0]  # black

CONF_OVERLAY_MARGIN = "overlay_margin"  # int, pixels from edge
DEFAULT_OVERLAY_MARGIN = 10

CONF_OVERLAY_LINE_SPACING = "overlay_line_spacing"  # int, extra pixels between lines
DEFAULT_OVERLAY_LINE_SPACING = 4
