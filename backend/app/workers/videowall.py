"""Durable PostgreSQL publication worker. Run explicitly, never via app.main."""
import argparse
import hashlib
import logging
import os
from pathlib import Path
import socket
from tempfile import TemporaryDirectory
import time
from uuid import uuid4

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.services.videowall_derivatives import DerivativeError, PROFILE, prepare_node_slices
from app.services.videowall_integrity import valid_geometry


logger = logging.getLogger(__name__)


def process_next_publication(*, session_factory=SessionLocal, worker_id: str,
                             work_root: Path, lease_seconds: int = 120) -> bool:
    # The authority owns every DB transition/commit. Work snapshots never grant
    # media ownership; it resolves the current master afresh before every read.
    from app.services.videowall_publications import (
        claim_publication, complete_publication, fail_publication,
        renew_publication_lease, resolve_publication_master,
    )
    with session_factory() as db:
        work = claim_publication(db, worker_id, lease_seconds=lease_seconds)
    if work is None:
        return False
    last_renew = float("-inf")
    lease_lost = False
    interval = min(20, max(1, lease_seconds // 3))

    def heartbeat(force=False):
        nonlocal last_renew, lease_lost
        if lease_lost:
            return False
        now = time.monotonic()
        if not force and now - last_renew < interval:
            return True
        with session_factory() as db:
            current = renew_publication_lease(db, work.publication_id, work.lease_token,
                                             lease_seconds=lease_seconds)
        last_renew = now
        lease_lost = not current
        return bool(current)

    try:
        root = Path(work_root).resolve()
        media = get_settings().MEDIA_ROOT.resolve()
        if root == media or root.is_relative_to(media):
            raise DerivativeError("PRIVATE_WORK_ROOT_REQUIRED", "Worker staging must be outside the public media root")
        root.mkdir(parents=True, exist_ok=True)
        if work.encoding_profile != PROFILE:
            raise DerivativeError("UNSUPPORTED_ENCODING_PROFILE", "Publication encoding profile is unsupported")
        geometry = work.geometry_snapshot_json
        if not valid_geometry(geometry):
            raise DerivativeError("UNSUPPORTED_WALL_GEOMETRY", "Publication group geometry is invalid")
        prepared = {}
        prefix = "publication-" + hashlib.sha256(work.publication_id.encode()).hexdigest()[:16] + "-"
        with TemporaryDirectory(prefix=prefix, dir=root) as directory:
            staging = Path(directory).resolve()
            if staging.is_relative_to(media) or not staging.is_relative_to(root):
                raise DerivativeError("PRIVATE_WORK_ROOT_REQUIRED", "Worker staging must be private")
            for item in work.program_snapshot_json["items"]:
                identity = item["id"]
                if not isinstance(identity, str) or not identity or identity in prepared:
                    raise DerivativeError("INVALID_PROGRAM_SNAPSHOT", "Publication logical item identities are invalid")
                if not heartbeat(force=True):
                    raise DerivativeError("LEASE_LOST", "Publication processing lease was lost")
                with session_factory() as db:
                    source = resolve_publication_master(db, work, item)
                output = staging / ("item-" + hashlib.sha256(identity.encode()).hexdigest())
                prepared[identity] = prepare_node_slices(
                    source, output, canvas_width=geometry["total_width"],
                    canvas_height=geometry["total_height"], content_type=item["type"],
                    rows=geometry["rows"], columns=geometry["columns"],
                    image_duration_ms=item.get("duration_ms"), heartbeat=heartbeat,
                    ffmpeg_bin=os.getenv("FFMPEG_BINARY", "ffmpeg"),
                    ffprobe_bin=os.getenv("FFPROBE_BINARY", "ffprobe"))
            if not prepared:
                raise DerivativeError("EMPTY_PUBLICATION", "Publication has no playable items")
            if not heartbeat(force=True):
                raise DerivativeError("LEASE_LOST", "Publication processing lease was lost")
            with session_factory() as db:
                complete_publication(db, work.publication_id, work.lease_token, prepared)
        return True
    except Exception as exc:
        code = exc.code if isinstance(exc, DerivativeError) else "MEDIA_PREPARATION_FAILED"
        message = exc.safe_message if isinstance(exc, DerivativeError) else "Media preparation failed"
        logger.warning("VW1 preparation rejected publication=%s code=%s", work.publication_id, code)
        with session_factory() as db:
            # A stale token cannot overwrite a replacement worker's generation.
            fail_publication(db, work.publication_id, work.lease_token, code, message)
        return True


def main(argv=None):
    parser = argparse.ArgumentParser(description="Process durable VW1 publication leases")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--worker-id", default=socket.gethostname() + "-" + uuid4().hex[:12])
    parser.add_argument("--work-root", type=Path,
                        default=Path(os.getenv("VW1_WORK_ROOT", "/app/storage/processing/videowall")))
    parser.add_argument("--lease-seconds", type=int, default=int(os.getenv("VW1_LEASE_SECONDS", "120")))
    parser.add_argument("--poll-seconds", type=float, default=float(os.getenv("VW1_POLL_SECONDS", "2")))
    arguments = parser.parse_args(argv)
    if arguments.lease_seconds < 30 or arguments.poll_seconds <= 0:
        parser.error("Lease must be at least 30 seconds and polling must be positive")
    from app.core.security_logging import install_security_log_redaction
    install_security_log_redaction()
    while True:
        processed = process_next_publication(worker_id=arguments.worker_id,
            work_root=arguments.work_root, lease_seconds=arguments.lease_seconds)
        if arguments.once:
            return 0
        if not processed:
            time.sleep(arguments.poll_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
