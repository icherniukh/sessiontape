import queue
from dataclasses import dataclass
from typing import Optional


@dataclass
class Event:
    """Base class for all supervisor events."""
    pass


@dataclass
class ProcessStarted(Event):
    """The target process (e.g. Rekordbox) has started and stabilized."""
    pid: int


@dataclass
class ProcessStopped(Event):
    """The target process has exited."""
    pid: int


@dataclass
class ProcessReplaced(Event):
    """The target process family stayed alive, but the active PID changed."""
    old_pid: int
    new_pid: int


@dataclass
class CaptureDied(Event):
    """The capture backend process (e.g. audiotee) died unexpectedly."""
    exit_code: Optional[int]


@dataclass
class TapBroken(Event):
    """The capture backend is still running but delivering all zeros (pathological silence)."""
    pass


@dataclass
class ShutdownRequested(Event):
    """The daemon received a shutdown signal (SIGTERM/SIGINT)."""
    pass


@dataclass
class SegmentOpened(Event):
    """A new raw segment file has been opened for writing."""
    raw_path: str
    output_path: str


@dataclass
class SegmentClosed(Event):
    """A raw segment file has been closed."""
    raw_path: str
    output_path: str
    duration_seconds: float
    discarded: bool = False


@dataclass
class ExportStarted(Event):
    """An export job has started converting a raw segment."""
    raw_path: str
    output_path: str
    recovery: bool = False


@dataclass
class ExportFinished(Event):
    """An export job completed successfully."""
    raw_path: str
    output_path: str
    recovery: bool = False


@dataclass
class ExportFailed(Event):
    """An export job failed."""
    raw_path: str
    output_path: str
    error: str
    recovery: bool = False


# Type alias for the central event queue
EventQueue = queue.Queue[Event]
