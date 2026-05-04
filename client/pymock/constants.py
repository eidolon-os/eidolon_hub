"""Audio and server constants for the GUI client."""

# Audio configuration
SAMPLE_RATE: int = 16000
CHANNELS: int = 1
AUDIO_FRAME_SIZE: int = 320  # samples per frame (20ms at 16kHz, protocol standard)
DTYPE: str = "int16"  # 16-bit PCM

# PCM byte length per frame: channels * bytes_per_sample * frame_size
# = 1 * 2 * 960 = 1920 bytes
PCM_FRAME_BYTES: int = CHANNELS * 2 * AUDIO_FRAME_SIZE

# Opus encoding
OPUS_APPLICATION: int = 2048  # opuslib.APPLICATION_VOIP

# Audio format options
FORMAT_PCM = "pcm"
FORMAT_OPUS = "opus"
AUDIO_FORMATS = [FORMAT_PCM, FORMAT_OPUS]

# WebSocket server defaults
DEFAULT_WS_HOST: str = "127.0.0.1"
DEFAULT_WS_PORT: int = 9981
DEFAULT_WS_PATH: str = "/auto"

# UI dimensions
WINDOW_WIDTH: int = 600
WINDOW_HEIGHT: int = 500
LOG_LINES: int = 200

# Log panel height (approx rows)
LOG_TEXT_HEIGHT: int = 15

# Status colors
STATUS_COLOR_IDLE = "#888888"
STATUS_COLOR_CONNECTING = "#f0ad4e"
STATUS_COLOR_CONNECTED = "#5cb85c"
STATUS_COLOR_ERROR = "#d9534f"
STATUS_COLOR_LISTENING = "#5bc0de"
STATUS_COLOR_SPEAKING = "#9b59b6"
