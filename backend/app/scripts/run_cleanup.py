from __future__ import annotations

import argparse
import json
import logging

from app.core.database import SessionLocal
from app.services.cleanup_retention import (
    DEFAULT_CAMPAIGN_RETENTION_DAYS,
    DEFAULT_CONTENT_RETENTION_DAYS,
    DEFAULT_FOLDER_RETENTION_DAYS,
    run_retention_cleanup,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run conservative retention cleanup for unused campaigns, contents and folders.")
    parser.add_argument("--content-days", type=int, default=DEFAULT_CONTENT_RETENTION_DAYS, help="Age in days for unused contents.")
    parser.add_argument("--campaign-days", type=int, default=DEFAULT_CAMPAIGN_RETENTION_DAYS, help="Age in days for unused campaigns.")
    parser.add_argument("--folder-days", type=int, default=DEFAULT_FOLDER_RETENTION_DAYS, help="Age in days for unused folders.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    with SessionLocal() as db:
        summary = run_retention_cleanup(
            db,
            content_days=args.content_days,
            campaign_days=args.campaign_days,
            folder_days=args.folder_days,
        )

    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
