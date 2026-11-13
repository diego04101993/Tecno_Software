"""Seed an empty, named QA-only database from a sealed local application image."""

import hashlib
import json
import os
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import inspect, text

from app.core.database import SessionLocal, engine, init_db
from app.core.security import hash_password
from app.models.entities import (
    Branch,
    Campaign,
    CampaignChannelAssignment,
    CampaignSequenceItem,
    Channel,
    Client,
    ContentItem,
    ContentType,
    PlayerDevice,
    User,
    UserRole,
)
from app.services.device_outputs import sync_channel_device_outputs


def seed():
    run = os.environ["BGV2_QA_NONCE"]
    if engine.url.database != f"qa_bgv2_{run}" or engine.url.host != f"bgv2-qa-{run}-postgres":
        raise RuntimeError("Fixture DSN must target the current disposable PostgreSQL")
    with engine.connect() as conn:
        marker = conn.scalar(text("SELECT current_setting('bgv2.qa_run', true)"))
        if marker != run or inspect(conn).get_table_names():
            raise RuntimeError("Fixture PostgreSQL is not empty or is not owned by this run")
    init_db()
    with SessionLocal.begin() as session:
        tenant = Client(name="Blue Green QA", slug="blue-green-qa")
        admin = User(email="operator@bgv2.example.com", full_name="Disposable QA Operator",
                     role=UserRole.SUPER_ADMIN, password_hash=hash_password(os.environ["BGV2_QA_PASSWORD"]))
        session.add_all([tenant, admin])
        session.flush()
        branch = Branch(client_id=tenant.id, name="QA Branch", code="BGQA")
        campaign = Campaign(client_id=tenant.id, name="QA Campaign")
        item = ContentItem(client_id=tenant.id, name="QA text", type=ContentType.TEXT,
                           text_content="Blue-green cross-release QA", duration_seconds=12)
        session.add_all([branch, campaign, item])
        session.flush()
        session.add(CampaignSequenceItem(campaign_id=campaign.id, content_id=item.id))
        result = {"client": tenant.id, "channels": [], "devices": []}
        for ordinal in range(4):
            channel = Channel(client_id=tenant.id, branch_id=branch.id, name=f"QA channel {ordinal + 1}")
            session.add(channel)
            session.flush()
            sync_channel_device_outputs(channel)
            session.add(CampaignChannelAssignment(channel_id=channel.id, campaign_id=campaign.id))
            token = os.environ[f"BGV2_QA_PLAYER_{ordinal}"]
            player = PlayerDevice(id=str(uuid4()), channel_id=channel.id, client_id=tenant.id,
                                  branch_id=branch.id, hardware_id=f"bgv2-{run}-{ordinal}",
                                  device_name=f"QA Player {ordinal + 1}", last_seen_at=datetime.now(UTC),
                                  player_token_hash=hashlib.sha256(token.encode()).hexdigest())
            session.add(player)
            result["channels"].append(channel.id)
            result["devices"].append(player.id)
    print(json.dumps(result))


if __name__ == "__main__":
    seed()
