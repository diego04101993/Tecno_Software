"""A6 fixtures: synthetic rows, ephemeral SQLite; no application startup."""
import os
from contextlib import contextmanager
from dataclasses import dataclass
from unittest import TestCase

if os.environ.get("DATABASE_URL") != "sqlite://" or os.environ.get("AUTO_SEED") != "false":
    raise RuntimeError("Use the isolated backend test harness")

from sqlalchemy import create_engine, event, select, update
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.models.base import Base
from app.models.entities import (
    Branch, Campaign, CampaignSequenceItem, Channel, Client, ContentFolder,
    ContentItem, ContentType, DataSource, DataSourceType, Dataset, DatasetImport,
    DatasetRow, DeviceOutput, Layout, LayoutRevision, User, UserRole,
)
from app.services.tenant_ownership import RowRef, TenantGraph, context_for_actor
from app.services.tenant_relations import OPERATIONS


def operation(function):
    return next(o for o in OPERATIONS if o.function == function)


class GraphFixture(TestCase):
    def make_engine(self):
        return create_engine("sqlite://", poolclass=StaticPool)

    def setUp(self):
        self.engine = self.make_engine()
        @event.listens_for(self.engine, "connect")
        def configure(connection, _):
            if self.engine.dialect.name == "sqlite":
                connection.execute("PRAGMA foreign_keys=ON")
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine, expire_on_commit=False)
        self.addCleanup(self.engine.dispose)
        self.addCleanup(self.db.close)
        self.rows = {}
        for side in ("A", "B"):
            client = self.add(Client, name=side, slug=side)
            branch = self.add(Branch, client_id=client.id, name=side, code=side)
            channel = self.add(Channel, client_id=client.id, branch_id=branch.id, name=side)
            layout = self.add(Layout, client_id=client.id, name="layout-" + side)
            campaign = self.add(Campaign, client_id=client.id, layout_id=layout.id, name=side)
            folder = self.add(ContentFolder, client_id=client.id, name=side)
            content = self.add(ContentItem, client_id=client.id, folder_id=folder.id,
                               name="content-" + side, type=ContentType.IMAGE)
            item = self.add(CampaignSequenceItem, campaign_id=campaign.id,
                            content_id=content.id, item_type="content")
            output = self.add(DeviceOutput, root_channel_id=channel.id, channel_id=channel.id,
                              name=side, display_order=1)
            source = self.add(DataSource, client_id=client.id, name=side, source_type=DataSourceType.FILE_UPLOAD)
            dataset = self.add(Dataset, client_id=client.id, data_source_id=source.id, name=side, slug=side)
            imp = self.add(DatasetImport, dataset_id=dataset.id, source_filename="synthetic.csv", storage_path="")
            row = self.add(DatasetRow, dataset_id=dataset.id, import_id=imp.id, row_index=0)
            user = self.add(User, client_id=client.id, role=UserRole.CLIENT_ADMIN,
                            email=side.lower() + "@example.com", full_name=side, password_hash="synthetic")
            operator = self.add(User, client_id=client.id, branch_id=branch.id, role=UserRole.OPERATOR,
                                email=side.lower() + "-operator@example.com", full_name=side, password_hash="synthetic")
            self.rows[side] = dict(client=client, branch=branch, channel=channel, layout=layout,
                                   campaign=campaign, folder=folder, content=content, item=item,
                                   output=output, source=source, dataset=dataset, imp=imp, row=row,
                                   user=user, operator=operator)
        self.admin = self.add(User, role=UserRole.SUPER_ADMIN, email="admin@example.com",
                              full_name="Admin", password_hash="synthetic")
        self.db.commit()
        self.graph = TenantGraph(self.db)
        self.contexts = {s: context_for_actor(self.db, r["user"]) for s, r in self.rows.items()}
        self.global_context = context_for_actor(self.db, self.admin)

    def add(self, cls, **values):
        obj = cls(**values)
        self.db.add(obj)
        self.db.flush()
        return obj

    def ref(self, obj):
        return self.graph.reference(obj)

    def corrupt(self, obj, **values):
        self.db.execute(update(type(obj)).where(type(obj).id == obj.id).values(**values))
        self.db.commit()
        self.db.expire_all()

    @contextmanager
    def no_effects(self):
        effects = []
        def sql(conn, cursor, statement, parameters, context, executemany):
            verb = statement.lstrip().split(None, 1)[0].upper()
            if verb in {"INSERT", "UPDATE", "DELETE", "REPLACE"}:
                effects.append(verb)
        def flush(*args): effects.append("FLUSH")
        def commit(*args): effects.append("COMMIT")
        def assignment(*args): effects.append("ORM_ASSIGNMENT")
        listeners = []
        event.listen(self.engine, "before_cursor_execute", sql)
        event.listen(self.db, "before_flush", flush)
        event.listen(self.db, "after_commit", commit)
        for mapper in Base.registry.mappers:
            for attr in mapper.column_attrs:
                target = getattr(mapper.class_, attr.key)
                event.listen(target, "set", assignment)
                listeners.append(target)
        try:
            yield effects
        finally:
            event.remove(self.engine, "before_cursor_execute", sql)
            event.remove(self.db, "before_flush", flush)
            event.remove(self.db, "after_commit", commit)
            for target in listeners:
                event.remove(target, "set", assignment)
            self.assertEqual(effects, [])
            self.assertFalse(self.db.new or self.db.dirty or self.db.deleted)
