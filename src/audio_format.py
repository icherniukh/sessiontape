from dataclasses import dataclass


@dataclass(frozen=True)
class AudioFormat:
    sample_rate: int
    channels: int
    bytes_per_sample: int  # bytes per sample per channel (s16le = 2)

    @property
    def chunk_size(self) -> int:
        return int(self.sample_rate * 0.1 * self.channels * self.bytes_per_sample)  # 100ms
