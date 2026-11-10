"""Opt-in PostgreSQL QA for the channel projection's transaction-local memo."""

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import event, update

import test_channel_playback_postgres as postgres_qa
from test_tenant_isolation import TenantFixture
from app.models.entities import Branch, Channel, Client, DeviceOutput, PlayerDevice, PlayerHeartbeat
from app.services import tenant_responses
from app.services.db_snapshots import readonly_snapshot_session
from app.services.tenant_ownership import OwnerState, RowRef, TenantGraph


@unittest.skipUnless(os.environ.get('CHANNEL_PLAYBACK_QA_DATABASE_URL'),
                     'Explicit owned local PostgreSQL QA required')
class TenantGraphSnapshotMemoTests(TenantFixture):
    # Reuse the verified local URL, PG16, worker marker, system ID, private
    # schema, and bounded-pool setup. Each test receives its own QA schema.
    make_engine = postgres_qa.ChannelPlaybackPostgresTests.make_engine

    def setUp(self):
        super().setUp()
        a, b = self.res['A'], self.res['B']
        extra_branch = self.add(Branch, client_id=a['channel'].client_id, name='A second branch', code='A2')
        extra_channel = self.add(Channel, client_id=a['channel'].client_id, branch_id=extra_branch.id,
                                 name='A second channel')
        extra_player = self.add(PlayerDevice, client_id=extra_channel.client_id, branch_id=extra_branch.id,
                                channel_id=extra_channel.id, hardware_id='memo-qa-player',
                                device_name='Memo QA player', player_token_hash='qa-' + uuid4().hex)
        at = datetime(2026, 1, 1, tzinfo=UTC)
        self.channel_ids = {}
        self.branch_ids = {}
        self.client_ids = {side: self.tenants[side].id for side in ('A', 'B')}
        self.labels = {}
        self.output_ids = {}
        for order, (key, channel, player, resources) in enumerate((
            ('A', a['channel'], a['player'], a),
            ('A2', extra_channel, extra_player, a),
            ('B', b['channel'], b['player'], b),
        )):
            channel.created_at = at + timedelta(seconds=order)
            channel.current_playback = 'untrusted stored label'
            self.add(PlayerHeartbeat, channel_id=channel.id, player_device_id=player.id,
                     client_id=channel.client_id, branch_id=channel.branch_id, received_at=at,
                     current_content_id=resources['content'].id,
                     current_campaign_id=resources['campaign'].id, current_layout_id=resources['layout'].id)
            output = self.add(DeviceOutput, root_channel_id=channel.id, channel_id=channel.id,
                              name='Memo output ' + key, display_order=0)
            self.channel_ids[key] = channel.id
            self.branch_ids[key] = channel.branch_id
            self.labels[key] = resources['content'].name
            self.output_ids[key] = output.id
        self.db.commit()

    def assert_pool_returned(self):
        self.assertEqual(self.engine.pool.checkedout(), 0)

    @contextmanager
    def without_memo(self):
        def baseline(session, **_options):
            return TenantGraph(session)
        with patch.object(tenant_responses, 'TenantGraph', side_effect=baseline):
            yield

    def list_channels(self, who='A_client_admin', **params):
        response = self.request('GET', '/api/channels/', who=who, params=params)
        self.assert_pool_returned()
        return response.status_code, response.json()

    def equivalent(self, who='A_client_admin', **params):
        with self.without_memo():
            baseline = self.list_channels(who, **params)
        actual = self.list_channels(who, **params)
        self.assertEqual(actual, baseline)
        return actual

    def assert_channels(self, response, keys):
        status, rows = response
        self.assertEqual(status, 200, rows)
        self.assertEqual([row['id'] for row in rows], [self.channel_ids[key] for key in keys])
        return rows

    def corrupt_channel(self, field, identity, *, orphan=False):
        self.db.rollback()
        with self.engine.begin() as connection:
            if orphan:
                # Simulate a historical dangling reference in the test's
                # disposable schema. This setting dies with the transaction.
                connection.exec_driver_sql('SET LOCAL session_replication_role = replica')
            connection.execute(update(Channel).where(Channel.id == self.channel_ids['A'])
                               .values({field: identity}))

    @contextmanager
    def memo_graph(self):
        with readonly_snapshot_session(self.db) as snapshot:
            connection = snapshot.connection()
            self.assertEqual(connection.get_isolation_level(), 'REPEATABLE READ')
            self.assertEqual(connection.exec_driver_sql('SHOW transaction_read_only').scalar_one(), 'on')
            yield TenantGraph(snapshot, snapshot_transaction=connection.get_transaction())

    @contextmanager
    def capture_projection_graphs(self):
        graphs = []
        def capture(session, **options):
            graph = TenantGraph(session, **options)
            if options.get('snapshot_transaction') is not None:
                graphs.append(graph)
            return graph
        with patch.object(tenant_responses, 'TenantGraph', side_effect=capture):
            yield graphs

    def test_cached_http_contract_keeps_order_playback_and_outputs(self):
        keys = ('B', 'A2', 'A')
        rows = self.assert_channels(self.equivalent('A_super_admin'), keys)
        for row, key in zip(rows, keys):
            self.assertEqual(row['current_playback'], self.labels[key])
            self.assertEqual([output['id'] for output in row['device_outputs']], [self.output_ids[key]])

    def test_cached_http_scopes_keep_clients_and_branches_separate(self):
        self.assert_channels(self.equivalent(), ('A2', 'A'))
        self.assert_channels(self.equivalent('A_operator'), ('A',))
        self.assert_channels(self.equivalent('B_client_admin'), ('B',))
        self.assert_channels(self.equivalent(branch_id=self.branch_ids['A2']), ('A2',))
        status, _ = self.equivalent(branch_id=self.branch_ids['B'])
        self.assertIn(status, (403, 404))

    def test_cached_http_omits_mismatched_and_missing_owners(self):
        for field, identity, orphan in (
            ('branch_id', self.branch_ids['B'], False),
            ('branch_id', str(uuid4()), True),
            ('client_id', str(uuid4()), True),
        ):
            with self.subTest(field=field, orphan=orphan):
                self.corrupt_channel(field, identity, orphan=orphan)
                self.assert_channels(self.equivalent('A_super_admin'), ('B', 'A2'))
                original = self.branch_ids['A'] if field == 'branch_id' else self.client_ids['A']
                self.corrupt_channel(field, original)
        self.assert_channels(self.equivalent('A_super_admin'), ('B', 'A2', 'A'))

    def test_cached_http_keeps_inconsistent_output_omission_and_inactive_client_rules(self):
        with self.engine.begin() as connection:
            connection.execute(update(DeviceOutput).where(DeviceOutput.id == self.output_ids['A'])
                               .values(channel_id=self.channel_ids['B']))
        rows = self.assert_channels(self.equivalent(), ('A2', 'A'))
        self.assertEqual(rows[1]['device_outputs'], [])
        with self.engine.begin() as connection:
            connection.execute(update(Client).where(Client.id == self.client_ids['A'])
                               .values(status='suspended'))
        self.assertEqual(self.equivalent()[0], 403)
        rows = self.assert_channels(self.equivalent('A_super_admin'), ('B', 'A2', 'A'))
        self.assertEqual(rows[0]['current_playback'], self.labels['B'])
        self.assertIsNone(rows[1]['current_playback'])
        self.assertIsNone(rows[2]['current_playback'])

    def test_only_row_facts_are_reused_within_the_snapshot(self):
        with self.engine.begin() as connection:
            connection.execute(update(Branch).where(Branch.id == self.branch_ids['A'])
                               .values(is_active=False))
        # IDs can coincide across tables. A memo keyed by ID alone would
        # substitute the Client facts when the Branch is requested.
        self.add(Branch, id=self.client_ids['A'], client_id=self.client_ids['A'],
                 name='Colliding branch ID', code='COLLIDE')
        self.db.commit()
        channel = RowRef('Channel', self.channel_ids['A'])
        branch = RowRef('Branch', self.branch_ids['A'])
        client = RowRef('Client', self.client_ids['A'])
        collision = RowRef('Branch', self.client_ids['A'])
        missing = RowRef('Branch', str(uuid4()))
        refs = (channel, branch, client, collision, missing)
        statements = []
        def observe(_conn, _cursor, statement, _parameters, _context, _many):
            statements.append(statement)
        with self.memo_graph() as graph:
            event.listen(self.engine, 'before_cursor_execute', observe)
            try:
                for _ in range(3):
                    rows = tuple(graph.snapshot(ref) for ref in refs)
                    self.assertTrue(all(row is not None for row in rows[:-1]))
                    self.assertEqual(rows[3]['name'], 'Colliding branch ID')
                    self.assertIsNone(rows[-1])
                self.assertEqual(len(statements), len(refs))
            finally:
                event.remove(self.engine, 'before_cursor_execute', observe)
            self.assertEqual(graph.resolve_owner(channel, self.client_ids['A']).state, OwnerState.OWNED)
            self.assertEqual(graph.resolve_owner(channel, self.client_ids['B']).state, OwnerState.FOREIGN)
            own_context = SimpleNamespace(branch_id=self.branch_ids['A'])
            other_context = SimpleNamespace(branch_id=self.branch_ids['A2'])
            self.assertTrue(graph.relation_valid(channel, 'Channel.branch_id', profile='READ', context=own_context))
            self.assertFalse(graph.relation_valid(channel, 'Channel.branch_id', profile='READ', context=other_context))
            self.assertFalse(graph.relation_valid(channel, 'Channel.branch_id', profile='RUNTIME', context=own_context))
        self.assert_pool_returned()

    def test_negative_row_cache_cannot_survive_into_a_new_transaction(self):
        identity = str(uuid4())
        ref = RowRef('Branch', identity)
        with self.memo_graph() as first:
            self.assertIsNone(first.snapshot(ref))
        self.assertFalse(first._snapshot_rows)
        self.add(Branch, id=identity, client_id=self.client_ids['A'], name='New branch', code='NEW')
        self.db.commit()
        with self.memo_graph() as second:
            self.assertEqual(second.snapshot(ref)['name'], 'New branch')
        self.assert_pool_returned()

    def test_http_get_does_not_share_or_retain_the_memo_between_requests(self):
        pool = {'checkout': 0, 'checkin': 0}
        def checkout(*_args):
            pool['checkout'] += 1
        def checkin(*_args):
            pool['checkin'] += 1
        event.listen(self.engine, 'checkout', checkout)
        event.listen(self.engine, 'checkin', checkin)
        try:
            with self.capture_projection_graphs() as graphs:
                self.assert_channels(self.list_channels(), ('A2', 'A'))
                self.assertEqual(len(graphs), 1)
                self.assertFalse(graphs[0]._snapshot_rows)
                self.assert_channels(self.list_channels('B_client_admin'), ('B',))
                self.assertEqual(len(graphs), 2)
                self.assertIsNot(graphs[0], graphs[1])
                self.assertIsNot(graphs[0].session, graphs[1].session)
                self.assertFalse(graphs[1]._snapshot_rows)
        finally:
            event.remove(self.engine, 'checkout', checkout)
            event.remove(self.engine, 'checkin', checkin)
        self.assertGreater(pool['checkout'], 0)
        self.assertEqual(pool['checkout'], pool['checkin'])

    def test_failed_projection_discards_the_memo_and_returns_the_connection(self):
        project = tenant_responses.safe_model_read
        def fail_after_read(*args, **kwargs):
            project(*args, **kwargs)
            raise RuntimeError('synthetic projection failure')
        with self.capture_projection_graphs() as graphs:
            with patch.object(tenant_responses, 'safe_model_read', side_effect=fail_after_read):
                with self.assertRaisesRegex(RuntimeError, 'synthetic projection failure'):
                    self.list_channels()
        self.assertEqual(len(graphs), 1)
        self.assertFalse(graphs[0]._snapshot_rows)
        self.assert_pool_returned()

    def test_write_session_graph_sees_changes_and_rollback_without_caching(self):
        graph = TenantGraph(self.db)
        ref = RowRef('Client', self.client_ids['A'])
        original = graph.snapshot(ref)['name']
        self.db.execute(update(Client).where(Client.id == ref.identity).values(name='Uncommitted QA name'))
        self.assertEqual(graph.snapshot(ref)['name'], 'Uncommitted QA name')
        self.db.rollback()
        self.assertEqual(graph.snapshot(ref)['name'], original)
        self.db.rollback()
        self.assert_pool_returned()
