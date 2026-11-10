"""Authenticated, isolated regressions for bounded channel playback projection.

Uses only the in-memory tenant harness; PostgreSQL plans/performance are measured
separately on explicitly owned QA infrastructure, never on an application DB.
"""
import json
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import create_engine, event, select, update
from sqlalchemy.dialects import postgresql
from sqlalchemy.pool import QueuePool

from test_tenant_isolation import HistoricalRelationFixture
from app.models.entities import (
    Branch, Channel, DeviceOutput,
    PlayerDevice, PlayerHeartbeat, UserRole,
)
from app.schemas.domain import ChannelRead
from app.services.tenant_responses import read_snapshot, safe_model_read


class ChannelPlaybackBatchTests(HistoricalRelationFixture):
    def make_engine(self):
        # Real checkouts instead of StaticPool's one shared DBAPI record let
        # source/snapshot ownership be measured independently. A third slot
        # accommodates the fixture's independent verification transaction;
        # production PostgreSQL pool_size=1 is tested by its owned QA suite.
        # This uniquely named shared-memory DB is entirely process-local.
        return create_engine('sqlite:///file:channel_batch_' + uuid4().hex
                             + '?mode=memory&cache=shared&uri=true',
                             poolclass=QueuePool, pool_size=3, max_overflow=0, pool_timeout=0.5,
                             connect_args={'check_same_thread': False})

    def setUp(self):
        super().setUp()
        self.a, self.b = self.res['A'], self.res['B']
        self.received = datetime(2026, 1, 1, tzinfo=UTC)
        self.a['channel'].current_playback = 'UNTRUSTED_STORED_FREE_TEXT'
        self.hb = self.heartbeat()
        self.heartbeat('B')
        self.db.commit()

    def heartbeat(self, side='A', **changes):
        r = self.res[side]
        values = dict(player_device_id=r['player'].id, channel_id=r['channel'].id,
                      client_id=self.tenants[side].id, branch_id=r['branch'].id,
                      current_content_id=r['content'].id,
                      current_campaign_id=r['campaign'].id,
                      current_layout_id=r['layout'].id, received_at=self.received)
        values.update(changes)
        return self.add(PlayerHeartbeat, **values)

    def listing(self, *, who='A_client_admin', **params):
        response = self.request('GET', '/api/channels/', who=who, params=params)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def own_channel(self, **kwargs):
        rows = self.listing(**kwargs)
        return next(row for row in rows if row['id'] == self.a['channel'].id)

    def assert_untrusted_label_omitted(self):
        rows = self.listing()
        row = next(row for row in rows if row['id'] == self.a['channel'].id)
        self.assertIsNone(row['current_playback'])
        rendered = json.dumps(rows)
        self.assertNotIn('UNTRUSTED_STORED_FREE_TEXT', rendered)
        for key in ('content', 'campaign', 'layout'):
            self.assertNotIn(self.b[key].name, rendered)

    @contextmanager
    def observed_sql(self):
        statements = []
        def before(conn, cursor, statement, parameters, context, many):
            statements.append((statement, parameters))
        event.listen(self.engine, 'before_cursor_execute', before)
        try:
            yield statements
        finally:
            event.remove(self.engine, 'before_cursor_execute', before)

    @staticmethod
    def heartbeat_reads(statements):
        return [(sql, params) for sql, params in statements
                if 'FROM player_heartbeats' in sql and sql.lstrip().upper().startswith(('SELECT', 'WITH'))]

    def add_channels(self, count):
        added = []
        for number in range(count):
            channel = self.add(Channel, client_id=self.tenants['A'].id,
                               branch_id=self.a['branch'].id, name='Batch channel ' + str(number))
            player = self.add(PlayerDevice, client_id=channel.client_id, branch_id=channel.branch_id,
                              channel_id=channel.id, hardware_id='batch-' + uuid4().hex,
                              device_name='Synthetic batch player', player_token_hash=uuid4().hex)
            self.heartbeat(channel_id=channel.id, player_device_id=player.id)
            added.append(channel)
        self.db.commit()
        return added

    def corrupt_missing_reference(self, field):
        """Persist an orphan only in this in-memory SQLite QA fixture."""
        self.db.commit()
        with self.engine.connect() as connection:
            connection.exec_driver_sql('PRAGMA foreign_keys=OFF')
            connection.execute(update(PlayerHeartbeat).where(PlayerHeartbeat.id == self.hb.id)
                               .values({field: str(uuid4())}))
            connection.commit()
            connection.exec_driver_sql('PRAGMA foreign_keys=ON')
            connection.commit()
        self.db.expire_all()

    def test_one_channel_contract_is_identical_except_trusted_rebuilt_label(self):
        expected = ChannelRead.model_validate(self.a['channel']).model_dump(mode='json')
        expected['current_playback'] = self.a['content'].name
        self.assertEqual(self.listing(), [expected])

    def test_multiple_channels_contract_and_same_content_priority(self):
        expected = {}
        for side in ('A', 'B'):
            row = ChannelRead.model_validate(self.res[side]['channel']).model_dump(mode='json')
            row['current_playback'] = self.res[side]['content'].name
            expected[row['id']] = row
        actual = self.listing(who='A_super_admin')
        self.assertEqual({row['id']: row for row in actual}, expected)

    def test_heartbeat_query_count_is_one_for_one_and_many_channels(self):
        with self.observed_sql() as one:
            self.listing()
        self.add_channels(23)
        with self.observed_sql() as many:
            rows = self.listing()
        self.assertEqual(len(rows), 24)
        self.assertEqual(len(self.heartbeat_reads(one)), 1)
        self.assertEqual(len(self.heartbeat_reads(many)), 1)

    def test_batch_resources_are_loaded_with_bounded_query_count(self):
        from app.services.tenant_responses import playback_labels_for_read
        channels = [self.a['channel']]
        with self.observed_sql() as one:
            first = playback_labels_for_read(channels, self.db)
        channels += self.add_channels(18)
        with self.observed_sql() as many:
            result = playback_labels_for_read(channels, self.db)
        self.assertEqual(first[self.a['channel'].id], self.a['content'].name)
        self.assertEqual(set(result), {row.id for row in channels})
        # readable_query's invariant/ownership preflights are part of the
        # contract too; none may grow with the channel collection.
        self.assertEqual(len(one), len(many))
        self.assertEqual(len(self.heartbeat_reads(many)), 1)

    def test_logical_old_n_plus_one_timeout_budget_no_longer_fails(self):
        channels = [self.a['channel'], *self.add_channels(31)]
        queries = 0
        def query_budget(conn, cursor, statement, parameters, context, many):
            nonlocal queries
            if 'FROM player_heartbeats' in statement:
                queries += 1
                if queries > 1:
                    raise RuntimeError('logical regression: second per-channel history query exceeds budget')
        event.listen(self.engine, 'before_cursor_execute', query_budget)
        try:
            # Reproduce the old per-channel lookup under the same explicit
            # logical history-query budget. This is a query-shape regression,
            # not an invented PostgreSQL statement-timeout measurement.
            with self.assertRaisesRegex(RuntimeError, 'second per-channel history query'):
                for channel in channels:
                    self.db.scalar(select(PlayerHeartbeat).where(PlayerHeartbeat.channel_id == channel.id)
                                   .order_by(PlayerHeartbeat.received_at.desc()).limit(1))
            self.assertEqual(queries, 2)
            self.db.rollback()
            queries = 0
            self.assertEqual(len(self.listing()), 32)
        finally:
            event.remove(self.engine, 'before_cursor_execute', query_budget)
        self.assertEqual(queries, 1)

    def test_no_heartbeat_never_uses_stored_free_text(self):
        self.db.delete(self.hb)
        self.db.commit()
        self.assert_untrusted_label_omitted()

    def test_heartbeat_other_client_is_rejected(self):
        self.hb.client_id = self.tenants['B'].id
        self.db.commit()
        self.assert_untrusted_label_omitted()

    def test_heartbeat_incorrect_branch_is_rejected(self):
        self.hb.branch_id = self.b['branch'].id
        self.db.commit()
        self.assert_untrusted_label_omitted()

    def test_player_other_channel_is_rejected(self):
        self.hb.player_device_id = self.b['player'].id
        self.db.commit()
        self.assert_untrusted_label_omitted()

    def test_player_inconsistent_client_is_rejected(self):
        self.a['player'].client_id = self.tenants['B'].id
        self.db.commit()
        self.assert_untrusted_label_omitted()

    def test_player_inconsistent_branch_is_rejected(self):
        self.a['player'].branch_id = self.b['branch'].id
        self.db.commit()
        self.assert_untrusted_label_omitted()

    def test_cross_tenant_content_campaign_and_layout_each_rejected(self):
        for kind in ('content', 'campaign', 'layout'):
            with self.subTest(resource=kind):
                field = 'current_' + kind + '_id'
                original = getattr(self.hb, field)
                setattr(self.hb, field, self.b[kind].id)
                self.db.commit()
                self.assert_untrusted_label_omitted()
                setattr(self.hb, field, original)
                self.db.commit()

    def test_missing_reference_ids_are_rejected(self):
        fields = ('player_device_id', 'current_content_id', 'current_campaign_id', 'current_layout_id')
        originals = {field: getattr(self.hb, field) for field in fields}
        for field in fields:
            with self.subTest(field=field):
                self.corrupt_missing_reference(field)
                self.assert_untrusted_label_omitted()
                setattr(self.hb, field, originals[field])
                self.db.commit()

    def test_latest_invalid_heartbeat_does_not_fall_back_to_older_valid(self):
        self.heartbeat(received_at=self.received + timedelta(seconds=1), client_id=self.tenants['B'].id)
        self.db.commit()
        self.assert_untrusted_label_omitted()

    def test_received_at_tie_uses_id_desc_deterministically(self):
        tied = self.received + timedelta(seconds=1)
        self.heartbeat(id='00000000-0000-0000-0000-000000000001', received_at=tied)
        self.heartbeat(id='00000000-0000-0000-0000-000000000002', received_at=tied,
                       current_content_id=None)
        self.db.commit()
        for _ in range(3):
            self.assertEqual(self.own_channel()['current_playback'], self.a['campaign'].name)

    def test_missing_optional_content_uses_campaign_then_layout_and_no_refs_none(self):
        self.hb.current_content_id = None
        self.db.commit()
        self.assertEqual(self.own_channel()['current_playback'], self.a['campaign'].name)
        self.hb.current_campaign_id = None
        self.db.commit()
        self.assertEqual(self.own_channel()['current_playback'], self.a['layout'].name)
        self.hb.current_layout_id = None
        self.db.commit()
        self.assert_untrusted_label_omitted()

    def test_inactive_or_corrupt_resource_graph_keeps_existing_runtime_safety(self):
        self.a['campaign'].is_active = False
        self.db.commit()
        self.assert_untrusted_label_omitted()
        self.a['campaign'].is_active = True
        self.a['campaign'].layout_id = self.b['layout'].id
        self.db.commit()
        self.assert_untrusted_label_omitted()

    def test_all_eight_administrative_roles_keep_tenant_and_branch_scopes(self):
        extra_branch = self.add(Branch, client_id=self.tenants['A'].id, name='Other A branch', code='A2')
        other = self.add(Channel, client_id=self.tenants['A'].id, branch_id=extra_branch.id, name='Other A channel')
        self.db.commit()
        for role in UserRole:
            with self.subTest(role=role.value):
                ids = {row['id'] for row in self.listing(who='A_' + role.value)}
                if role in {UserRole.SUPER_ADMIN, UserRole.STAFF_ADMIN, UserRole.STAFF_OPERATOR}:
                    expected = {self.a['channel'].id, self.b['channel'].id, other.id}
                elif role in {UserRole.BRANCH_MANAGER, UserRole.OPERATOR}:
                    expected = {self.a['channel'].id}
                else:
                    expected = {self.a['channel'].id, other.id}
                self.assertEqual(ids, expected)

    def test_queries_materialize_only_visible_channel_heartbeats(self):
        with self.observed_sql() as observed:
            self.listing()
        reads = self.heartbeat_reads(observed)
        self.assertEqual(len(reads), 1)
        self.assertIn(self.a['channel'].id, str(reads[0][1]))
        self.assertNotIn(self.b['channel'].id, str(reads[0][1]))
        # Historical telemetry JSON/free text is neither needed nor trusted.
        for field in ('resolution_json', 'cache_status_json', 'errors_json', 'playback_position'):
            self.assertNotIn(field, reads[0][0])

    def test_read_snapshot_get_does_not_flush_commit_or_repair_persisted_rows(self):
        rows = self.observe_unchanged(lambda: self.listing())
        self.assertEqual(rows[0]['current_playback'], self.a['content'].name)
        self.db.refresh(self.a['channel'])
        self.assertEqual(self.a['channel'].current_playback, 'UNTRUSTED_STORED_FREE_TEXT')

    def test_read_snapshot_still_disables_autoflush_and_blocks_core_writes(self):
        @read_snapshot(response_model=list[ChannelRead])
        def endpoint(db):
            self.assertFalse(db.autoflush)
            db.execute(update(Channel).values(current_playback='FORBIDDEN'))
            return []
        with self.assertRaises(HTTPException) as caught:
            endpoint(db=self.db)
        self.assertEqual(caught.exception.status_code, 409)
        self.db.refresh(self.a['channel'])
        self.assertEqual(self.a['channel'].current_playback, 'UNTRUSTED_STORED_FREE_TEXT')

    def test_pool_checkouts_checkins_balance_after_repeated_http_lists(self):
        self.db.rollback()
        counts = {'out': 0, 'in': 0}
        def checkout(*args): counts['out'] += 1
        def checkin(*args): counts['in'] += 1
        event.listen(self.engine.pool, 'checkout', checkout)
        event.listen(self.engine.pool, 'checkin', checkin)
        try:
            for _ in range(6):
                self.listing()
        finally:
            event.remove(self.engine.pool, 'checkout', checkout)
            event.remove(self.engine.pool, 'checkin', checkin)
        self.assertGreater(counts['out'], 0)
        self.assertEqual(counts['out'], counts['in'])
        self.assertEqual(self.engine.pool.checkedout(), 0)

    def test_device_outputs_contract_and_foreign_output_neutralization_are_preserved(self):
        own = self.add(DeviceOutput, root_channel_id=self.a['channel'].id, name='Own display', display_order=1,
                       channel_id=self.a['channel'].id, width=960, height=1080)
        self.add(DeviceOutput, root_channel_id=self.a['channel'].id, name='Foreign target', display_order=2,
                 channel_id=self.b['channel'].id)
        self.db.commit()
        payload = self.own_channel()
        self.assertEqual(len(payload['device_outputs']), 1)
        self.assertEqual(payload['device_outputs'][0]['id'], own.id)
        self.assertEqual(payload['device_outputs'][0]['width'], 960)
        self.assertEqual(payload['current_playback'], self.a['content'].name)

    def test_individual_channelread_player_config_and_realtime_rebuild_same_trusted_label(self):
        from app.api.routes.realtime import _channel_payload
        individual = safe_model_read(self.a['channel'], ChannelRead, self.db)
        self.assertEqual(individual.current_playback, self.a['content'].name)
        response = self.request('GET', '/api/channels/' + self.a['channel'].id + '/player-config')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['channel']['current_playback'], self.a['content'].name)
        payload = _channel_payload(self.a['channel'].id, db=self.db)
        self.assertEqual(payload['current_playback'], self.a['content'].name)

    def test_administrative_heartbeat_broadcast_is_trusted_one_query_and_not_stale(self):
        from app.api.routes.channels import manager
        channel_id = self.a['channel'].id
        with patch.object(manager, 'broadcast_to_client', new_callable=AsyncMock) as broadcast:
            with self.observed_sql() as first_queries:
                response = self.request('POST', '/api/channels/' + channel_id + '/heartbeat',
                                        body={'current_playback': 'ADMIN_REPORTED_TEXT', 'status': 'online'})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(set(response.json()), set(ChannelRead.model_fields))
            # The existing administrative POST response remains unchanged;
            # broadcasts and subsequent safe reads reconstruct trusted names.
            self.assertEqual(response.json()['current_playback'], 'ADMIN_REPORTED_TEXT')
            # write_guard separately inspects historical relationships before
            # and after the authorized POST. Preserve those security reads;
            # the playback projection itself performs one latest-row query.
            latest = [(sql, params) for sql, params in self.heartbeat_reads(first_queries)
                      if 'received_at DESC' in sql]
            self.assertEqual(len(latest), 1)
            self.assertEqual(broadcast.await_count, 1)
            first = broadcast.await_args.args
            self.assertEqual(first[0], self.tenants['A'].id)
            self.assertEqual(first[1]['current_playback'], self.a['content'].name)

            self.a['content'].name = 'RENAMED_TRUSTED_CONTENT'
            self.db.commit()
            with self.observed_sql() as second_queries:
                response = self.request('POST', '/api/channels/' + channel_id + '/heartbeat',
                                        body={'current_playback': 'SECOND_ADMIN_TEXT', 'status': 'online'})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()['current_playback'], 'SECOND_ADMIN_TEXT')
            latest = [(sql, params) for sql, params in self.heartbeat_reads(second_queries)
                      if 'received_at DESC' in sql]
            self.assertEqual(len(latest), 1)
            self.assertEqual(broadcast.await_count, 2)
            self.assertEqual(broadcast.await_args.args[1]['current_playback'], 'RENAMED_TRUSTED_CONTENT')

    def test_precomputed_projection_does_not_query_history_again(self):
        from app.services.tenant_responses import playback_labels_for_read
        projection = playback_labels_for_read([self.a['channel']], self.db)
        with self.observed_sql() as queries:
            result = safe_model_read(self.a['channel'], ChannelRead, self.db, channel_playback=projection)
        self.assertEqual(result.current_playback, self.a['content'].name)
        self.assertEqual(len(self.heartbeat_reads(queries)), 0)

    def test_postgres_query_has_bounded_lateral_latest_and_deterministic_order(self):
        from app.services.tenant_responses import latest_heartbeats_query
        query = latest_heartbeats_query([self.a['channel'].id, self.b['channel'].id], dialect_name='postgresql')
        sql = str(query.compile(dialect=postgresql.dialect()))
        self.assertIn('LATERAL', sql.upper())
        self.assertIn('received_at DESC', sql)
        self.assertIn('id DESC', sql)
        self.assertIn('LIMIT', sql.upper())
        self.assertNotIn('errors_json', sql)

    def test_empty_visible_collection_has_no_heartbeat_query(self):
        from app.services.tenant_responses import playback_labels_for_read
        with self.observed_sql() as queries:
            self.assertEqual(playback_labels_for_read([], self.db), {})
        self.assertEqual(queries, [])
