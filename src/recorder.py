import logging
import math
import os
import time
from array import array
from collections import deque
from datetime import datetime
from enum import Enum
from typing import Callable, Optional

from src.audio_format import AudioFormat
from src.config import SUPPORTED_EXPORT_FORMATS
from src.events import Event, SegmentClosed, SegmentOpened
from src.exporter import ExportManager

log = logging.getLogger("sessiontape")


def db_to_rms(db: float) -> float:
    # 0 dBFS = 32768 for 16-bit audio
    return 32768.0 * (10.0 ** (db / 20.0))


class RecorderState(Enum):
    PASSIVE = "PASSIVE"
    ACTIVE = "ACTIVE"


class PCMStreamRecorder:
    """Processes PCM chunks into segmented recording sessions."""

    def __init__(
        self,
        output_dir: str,
        on_tap_broken: Optional[Callable[[], None]] = None,
        sample_rate: int = 48000,
        silence_threshold_db: float = -50.0,
        min_silence_duration: float = 15.0,
        min_segment_duration: float = 10.0,
        decay_tail: float = 5.0,
        export_format: str = "wav",
        export_manager: Optional[ExportManager] = None,
        event_sink: Optional[Callable[[Event], None]] = None,
    ):
        self.output_dir = os.path.abspath(os.path.expanduser(output_dir))
        self.on_tap_broken = on_tap_broken
        self.sample_rate = sample_rate
        self.min_segment_duration = min_segment_duration
        if not isinstance(export_format, str):
            raise ValueError("export_format must be a string")
        self.export_format = export_format.lower()
        if self.export_format not in SUPPORTED_EXPORT_FORMATS:
            supported = ", ".join(sorted(SUPPORTED_EXPORT_FORMATS))
            raise ValueError(f"export_format must be one of: {supported}")
        self.event_sink = event_sink

        self.chunk_duration = 0.1  # 100ms chunks
        self.audio_fmt = AudioFormat(sample_rate, 2, 2)  # stereo s16le fixed
        self.chunk_size = self.audio_fmt.chunk_size
        self.channels = self.audio_fmt.channels
        self.bytes_per_sample = self.audio_fmt.bytes_per_sample

        self.rms_threshold = db_to_rms(silence_threshold_db)
        self.silence_chunks_threshold = int(min_silence_duration / self.chunk_duration)
        self.buffer_maxlen = int(decay_tail / self.chunk_duration)

        self._owns_export_manager = export_manager is None
        self.export_manager = export_manager or ExportManager(
            sample_rate=self.sample_rate,
            channels=self.channels,
            bytes_per_sample=self.bytes_per_sample,
            export_format=self.export_format,
        )

        self.state: RecorderState = RecorderState.PASSIVE
        self.ring_buffer = deque(maxlen=self.buffer_maxlen)
        self.silence_count = 0
        self._chunk_count = 0
        self.last_active_at: float = 0.0  # epoch time of last real audio chunk in ACTIVE state
        self._last_rms: float = 0.0  # for drop detection
        self._consecutive_zero_chunks: int = 0  # perfect zeros = tap broken signal

        self._tap_broken_fired: bool = False

        self._raw_path: Optional[str] = None
        self._output_path: Optional[str] = None
        self._raw_file = None

    def reset(self) -> None:
        self.state = RecorderState.PASSIVE
        self.ring_buffer.clear()
        self.silence_count = 0
        self._chunk_count = 0
        self.last_active_at = 0.0
        self._last_rms = 0.0
        self._consecutive_zero_chunks = 0
        self._tap_broken_fired = False

    def _emit(self, event: Event) -> None:
        if self.event_sink:
            self.event_sink(event)

    def process_chunk(self, chunk: bytes) -> None:
        rms = self._calculate_rms(chunk)
        is_silent = rms < self.rms_threshold

        self._chunk_count += 1
        if self._chunk_count % 100 == 0:  # log every ~10s
            db = 20 * math.log10(rms / 32768.0) if rms > 0 else -math.inf
            log.debug(f"[{self.state}] chunk #{self._chunk_count} RMS={rms:.0f} ({db:.1f} dB), threshold={self.rms_threshold:.0f}")

        # Detect sudden drop to near-zero while actively recording — tap may have broken.
        # Only meaningful in ACTIVE state; in PASSIVE, zeros are expected (music not playing).
        if self.state == RecorderState.ACTIVE:
            if self._last_rms > self.rms_threshold * 10 and rms == 0:
                log.warning(f"[DIAG] RMS dropped to zero at chunk #{self._chunk_count} (was {self._last_rms:.0f}) — possible tap failure")
            elif self._last_rms > self.rms_threshold * 100 and rms < self.rms_threshold:
                db_before = 20 * math.log10(self._last_rms / 32768.0)
                db_now = 20 * math.log10(rms / 32768.0) if rms > 0 else -math.inf
                log.warning(f"[DIAG] RMS dropped from {db_before:.1f} dB to {db_now:.1f} dB at chunk #{self._chunk_count}")
        self._last_rms = rms

        # Perfect zeros (rms == 0.0 exactly) are a strong broken-tap signal.
        # Real silence has noise floor; a tap delivering all-zero bytes is pathological.
        if rms == 0.0:
            self._consecutive_zero_chunks += 1
            # 900 chunks = 90s @ 100ms/chunk. This replaces the old watchdog timer.
            if self._consecutive_zero_chunks == 900 and not self._tap_broken_fired:
                log.warning(
                    f"[DIAG] 900 consecutive all-zero chunks (90s) — "
                    f"tap definitely broken. Pushing TapBroken event."
                )
                self._tap_broken_fired = True
                if self.on_tap_broken:
                    self.on_tap_broken()
            elif self.state == RecorderState.ACTIVE and self._consecutive_zero_chunks in (10, 50, 150):
                log.warning(
                    f"[DIAG] {self._consecutive_zero_chunks} consecutive all-zero chunks "
                    f"({self._consecutive_zero_chunks * self.chunk_duration:.0f}s) — "
                    f"tap likely broken"
                )
        else:
            self._consecutive_zero_chunks = 0

        if self.state == RecorderState.PASSIVE:
            self.ring_buffer.append(chunk)
            if not is_silent:
                buffered_chunks = list(self.ring_buffer)
                log.info(
                    "Audio detected! Transitioning to ACTIVE. "
                    f"RMS={rms:.1f} > {self.rms_threshold:.1f}"
                )
                self.state = RecorderState.ACTIVE
                self.last_active_at = time.time()
                self._open_new_file()
                for buffered_chunk in buffered_chunks:
                    self._raw_file.write(buffered_chunk)
                if not buffered_chunks and self._raw_file:
                    self._raw_file.write(chunk)
                self.ring_buffer.clear()
                self.silence_count = 0
            return

        if self._raw_file:
            self._raw_file.write(chunk)

        if is_silent:
            self.silence_count += 1
            if self.silence_count >= self.silence_chunks_threshold:
                if self._consecutive_zero_chunks > 10:
                    # Tap is delivering perfect zeros — broken tap, not real silence.
                    # Don't split the recording; let the watchdog restart the tap instead.
                    # Throttle this warning to avoid audio glitches due to logging overhead.
                    if self.silence_count % 100 == 0:
                        log.warning(
                            f"[DIAG] Suppressing session close — tap broken "
                            f"({self._consecutive_zero_chunks} zero chunks). Watchdog will restart."
                        )
                else:
                    log.info("Continuous silence detected. Transitioning to PASSIVE.")
                    self._close_current_file()
                    self.state = RecorderState.PASSIVE
                    self.last_active_at = time.time()
        else:
            self.silence_count = 0
            self.last_active_at = time.time()  # refresh on every real audio chunk

    def finalize(self) -> None:
        if self.state == RecorderState.ACTIVE:
            self._close_current_file()
            self.state = RecorderState.PASSIVE
        if self._owns_export_manager:
            self.export_manager.shutdown()

    def _open_new_file(self) -> None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        self._raw_path = os.path.join(self.output_dir, f".rb_session_{timestamp}.raw")
        self._output_path = os.path.join(
            self.output_dir,
            f"rb_session_{timestamp}.{self.export_format}",
        )
        self._raw_file = open(self._raw_path, "wb")
        log.info(f"Opened new recording session: {self._raw_path}")
        self._emit(
            SegmentOpened(raw_path=self._raw_path, output_path=self._output_path)
        )

    def _close_current_file(self) -> None:
        if self._raw_file:
            self._raw_file.close()
            self._raw_file = None

        if not self._raw_path or not os.path.exists(self._raw_path):
            self._raw_path = None
            self._output_path = None
            return

        raw_size = os.path.getsize(self._raw_path)
        duration = raw_size / (
            self.sample_rate * self.channels * self.bytes_per_sample
        )
        log.info(f"Raw capture finished: {raw_size} bytes ({duration:.1f}s)")
        output_path = self._output_path

        if raw_size > 0:
            discarded = duration < self.min_segment_duration
            self._emit(
                SegmentClosed(
                    raw_path=self._raw_path,
                    output_path=output_path,
                    duration_seconds=duration,
                    discarded=discarded,
                )
            )
            if discarded:
                log.info(
                    "Discarding short segment %.1fs < %.1fs: %s",
                    duration,
                    self.min_segment_duration,
                    self._raw_path,
                )
                os.unlink(self._raw_path)
            else:
                self.export_manager.enqueue(self._raw_path, output_path)
        else:
            self._emit(
                SegmentClosed(
                    raw_path=self._raw_path,
                    output_path=output_path,
                    duration_seconds=duration,
                    discarded=True,
                )
            )
            os.unlink(self._raw_path)

        self._raw_path = None
        self._output_path = None

    def _calculate_rms(self, chunk: bytes) -> float:
        audio_samples = array("h", chunk)
        if not audio_samples:
            return 0.0
        sum_squares = sum(sample * sample for sample in audio_samples)
        return math.sqrt(sum_squares / len(audio_samples))
