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


# Type alias for the central event queue
EventQueue = queue.Queue[Event]
