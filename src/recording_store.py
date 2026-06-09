import glob
import logging
import os
import time
from typing import Optional

from src.exporter import ExportManager

log = logging.getLogger("sessiontape")


def recover_orphaned_raw_files(
    output_dir: str,
    sample_rate: int,
    export_format: str,
    export_manager: Optional[ExportManager] = None,
) -> None:
    if not os.path.exists(output_dir):
        return

    # Both patterns are load-bearing: glob("*.raw") does NOT match ".rb_session_*.raw" on any
    # platform — Python fnmatch treats a leading dot as opaque when the pattern starts with "*".
    patterns = [os.path.join(output_dir, ".rb_session_*.raw"), os.path.join(output_dir, "*.raw")]
    raw_files = set()
    for p in patterns:
        raw_files.update(glob.glob(p))

    if not raw_files:
        return

    log.info(f"Found {len(raw_files)} orphaned raw files. Starting recovery...")
    exporter = export_manager or ExportManager(
        sample_rate=sample_rate,
        channels=2,
        bytes_per_sample=2,
        export_format=export_format,
    )

    for raw_path in raw_files:
        # Skip files touched in the last 30s — previous process may still be converting them
        age = time.time() - os.path.getmtime(raw_path)
        if age < 30:
            log.info(f"Skipping recent raw file (age={age:.0f}s, likely in-flight): {raw_path}")
            continue

        size = os.path.getsize(raw_path)
        if size == 0:
            log.info(f"Deleting empty orphaned file: {raw_path}")
            os.unlink(raw_path)
            continue

        base = os.path.splitext(os.path.basename(raw_path))[0]
        if base.startswith('.'):
            base = base[1:]

        output_path = os.path.join(output_dir, f"{base}.{export_format}")
        log.info(f"Recovering {raw_path} to {output_path}")
        exporter.enqueue(raw_path, output_path, recovery=True)

    if export_manager is None:
        exporter.shutdown()
