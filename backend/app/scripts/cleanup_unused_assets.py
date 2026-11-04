from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.core.database import SessionLocal
from app.models.entities import AudioPlaylistItem, Campaign, CampaignChannelAssignment, CampaignPlaylistItem, CampaignSequenceItem, ContentItem, Schedule
from app.services.content_cleanup import cleanup_files, delete_content_items, preflight_content_files
from app.services.media_references import lock_media_paths, media_reference_operation, approve_media_cleanup
from app.services.tenant_mutations import delete_guard
from app.services.tenant_relations import conflict


def list_unused_campaigns(db, cutoff: datetime) -> list[Campaign]:
    candidates = list(db.scalars(select(Campaign).where(Campaign.created_at <= cutoff).order_by(Campaign.created_at.asc())))
    return [
        campaign
        for campaign in candidates
        if not db.scalar(select(CampaignChannelAssignment.id).where(CampaignChannelAssignment.campaign_id == campaign.id).limit(1))
        and not db.scalar(select(Schedule.id).where(Schedule.campaign_id == campaign.id).limit(1))
    ]


def list_unused_contents(db, cutoff: datetime, *, removing_campaign_ids=()) -> list[ContentItem]:
    candidates = list(db.scalars(select(ContentItem).where(ContentItem.created_at <= cutoff).order_by(ContentItem.created_at.asc())))
    return [
        content
        for content in candidates
        if not db.scalar(select(CampaignSequenceItem.id).where(CampaignSequenceItem.content_id == content.id,
            CampaignSequenceItem.campaign_id.not_in(removing_campaign_ids)).limit(1))
        and not db.scalar(select(CampaignPlaylistItem.id).where(CampaignPlaylistItem.content_id == content.id,
            CampaignPlaylistItem.campaign_id.not_in(removing_campaign_ids)).limit(1))
        and not db.scalar(select(AudioPlaylistItem.id).where(AudioPlaylistItem.content_id == content.id).limit(1))
    ]


def print_report(title: str, items: list[object], render_line) -> None:
    print(f"{title}: {len(items)}")
    for item in items:
        print(f" - {render_line(item)}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Dry-run safe cleanup for unused campaigns and contents.")
    parser.add_argument("--days", type=int, default=5, help="Minimum age in days before an item can be considered unused.")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be removed without deleting anything.")
    parser.add_argument("--apply", action="store_true", help="Apply the cleanup.")
    args = parser.parse_args()
    if args.apply and args.dry_run:
        parser.error("Use either --dry-run or --apply, not both together.")
    return args


def main() -> None:
    args = parse_args()
    apply_changes = bool(args.apply)
    cutoff = datetime.now(UTC) - timedelta(days=max(1, args.days))

    with SessionLocal() as db:
        unused_campaigns = list_unused_campaigns(db, cutoff)
        print_report(
            "Unused campaigns",
            unused_campaigns,
            lambda campaign: f'{campaign.name} ({campaign.id}) client={campaign.client_id} created_at={campaign.created_at.isoformat() if campaign.created_at else "n/a"}',
        )

        if not apply_changes:
            unused_contents = list_unused_contents(db, cutoff)
            print_report(
                "Unused contents",
                unused_contents,
                lambda content: f'{content.name} ({content.id}) type={content.type.value} client={content.client_id} created_at={content.created_at.isoformat() if content.created_at else "n/a"}',
            )
            print("\nDry-run complete. No records were deleted.")
            return

        # Simulate only the existing campaign deletions when discovering contents.
        # No delete/flush is allowed to discover the rest of this batch.
        unused_contents = list_unused_contents(db, cutoff, removing_campaign_ids=tuple(c.id for c in unused_campaigns))
        campaign_ids = tuple(c.id for c in unused_campaigns)
        content_ids = tuple(c.id for c in unused_contents)
        with delete_guard(db, (*unused_campaigns, *unused_contents)), media_reference_operation(db):
            if (tuple(c.id for c in list_unused_campaigns(db, cutoff)) != campaign_ids
                    or tuple(c.id for c in list_unused_contents(db, cutoff, removing_campaign_ids=campaign_ids)) != content_ids):
                raise conflict()
            # Validate before deleting campaigns; this CLI is not executed by SECURITY-1A.
            all_paths = preflight_content_files(list(db.scalars(select(ContentItem).where(ContentItem.created_at <= cutoff))))
            lock_media_paths(db, all_paths)
            approve_media_cleanup(db, unused_contents)
            for campaign in unused_campaigns:
                db.delete(campaign)
            db.flush()

            print_report(
                "Unused contents after campaign cleanup",
                unused_contents,
                lambda content: f'{content.name} ({content.id}) type={content.type.value} client={content.client_id} created_at={content.created_at.isoformat() if content.created_at else "n/a"}',
            )

            file_paths = delete_content_items(unused_contents, db)
            db.commit()
            deleted_files = cleanup_files(file_paths, db)

            print(
                f"\nCleanup applied. Deleted {len(unused_campaigns)} campaign(s), {len(unused_contents)} content item(s) and {deleted_files} file(s)."
            )


if __name__ == "__main__":
    main()
