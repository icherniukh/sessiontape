import enum


class AppState(enum.Enum):
    IDLE = "idle"
    MONITORING = "monitoring"
    RECORDING = "recording"
    ERROR = "error"
