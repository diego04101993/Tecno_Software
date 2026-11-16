"""Opt-in real PG16 QA, never app startup, migrations or an existing schema."""
import json
import os
import re
import time
import unittest
from collections import Counter
from contextlib import nullcontext
from pathlib import Path
from statistics import median
from uuid import uuid4
from unittest.mock import patch

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool

from test_tenant_isolation import TenantFixture
from app.models.base import Base
from app.models.entities import Branch, Channel, DeviceOutput, PlayerDevice
from app.services import tenant_responses


@unittest.skipUnless(os.environ.get('CHANNEL_PLAYBACK_QA_DATABASE_URL'), 'Explicit owned local PostgreSQL QA required')
class ChannelPlaybackPostgresTests(TenantFixture):
    def make_engine(self):
        url = make_url(os.environ['CHANNEL_PLAYBACK_QA_DATABASE_URL'])
        marker = os.environ.get('QA_EXPECT_PG_MARKER')
        system_id = os.environ.get('SECURITY_R5_PG_SYSTEM_IDENTIFIER')
        if (url.get_backend_name() != 'postgresql' or url.host not in {'127.0.0.1', 'localhost'}
                or url.database != 'tecno_control_retention_test' or url.query.get('options')
                or not marker or not system_id):
            raise RuntimeError('Refusing unverified/nonlocal channel playback QA')
        control = create_engine(url, poolclass=NullPool, hide_parameters=True)
        schema = 'channel_batch_' + uuid4().hex
        with control.begin() as connection:
            if (connection.scalar(text("SELECT current_setting('vwflex.qa_worker',true)")) != marker
                    or connection.scalar(text('SELECT system_identifier::text FROM pg_control_system()')) != system_id
                    or int(connection.scalar(text("SELECT current_setting('server_version_num')"))) // 10000 != 16):
                raise RuntimeError('PostgreSQL channel QA provenance mismatch')
            connection.exec_driver_sql('CREATE SCHEMA ' + schema)
        engine = create_engine(url, pool_size=1, max_overflow=0, pool_timeout=1,
                               hide_parameters=True,
                               connect_args={'options': '-csearch_path=' + schema + ' -cstatement_timeout=3000'})
        def cleanup():
            engine.dispose()
            try:
                with control.begin() as connection:
                    connection.exec_driver_sql('DROP SCHEMA ' + schema + ' CASCADE')
            finally:
                control.dispose()
        self.addCleanup(cleanup)
        return engine

    def test_channel_collection_preserves_output_scope_and_batches_database_reads(self):
        side = self.res["A"]
        client_id, branch_id = side["channel"].client_id, side["branch"].id
        other_branch = self.add(Branch, client_id=client_id, name="Other branch", code="other")
        target = self.add(Channel, client_id=client_id, branch_id=other_branch.id, name="Linked outside filter")
        roots = [side["channel"]]
        for index in range(5):
            roots.append(self.add(Channel, client_id=client_id, branch_id=branch_id, name=f"QA root {index}"))
        expected = {}
        for root in roots:
            null_target = self.add(DeviceOutput, root_channel_id=root.id, name="Unassigned",
                                   display_order=2, channel_id=None)
            self.add(DeviceOutput, root_channel_id=root.id, name="Foreign", display_order=1,
                     channel_id=self.res["B"]["channel"].id)
            same_owner = self.add(DeviceOutput, root_channel_id=root.id, name="Disabled",
                                  display_order=0, channel_id=target.id, enabled=False)
            expected[root.id] = [same_owner.id, null_target.id]
        self.db.commit()
        self.db.rollback()
        sql = []

        def collect(_conn, _cursor, statement, _params, _context, _many):
            sql.append(statement)

        # Without the snapshot witness the existing per-row projection remains
        # available. Compare the complete response for the same persisted data.
        with patch.object(tenant_responses, "_projection_graph_scope", return_value=nullcontext()):
            expected_response = self.request("GET", "/api/channels/", params={"branch_id": branch_id})
        event.listen(self.engine, "before_cursor_execute", collect)
        try:
            actual_response = self.request("GET", "/api/channels/", params={"branch_id": branch_id})
        finally:
            event.remove(self.engine, "before_cursor_execute", collect)
        self.assertEqual(expected_response.status_code, 200, expected_response.text)
        self.assertEqual(actual_response.status_code, 200, actual_response.text)
        self.assertEqual(actual_response.json(), expected_response.json())
        self.assertEqual({row["id"] for row in actual_response.json()}, expected.keys())
        for row in actual_response.json():
            self.assertEqual([item["id"] for item in row["device_outputs"]], expected[row["id"]])
            self.assertFalse(row["device_outputs"][0]["enabled"])
            self.assertEqual(row["device_outputs"][0]["channel_id"], target.id)
            self.assertIsNone(row["device_outputs"][1]["channel_id"])
        output_reads = [statement for statement in sql
                        if re.search(r"^FROM device_outputs\b", statement, re.MULTILINE)]
        self.assertEqual(len(output_reads), 2, "One eligible collection query and one graph preload for all roots")
        self.assertTrue(any("ORDER BY device_outputs.root_channel_id, device_outputs.display_order" in query
                            for query in output_reads))
        self.assertEqual(self.engine.pool.checkedout(), 0)

    @unittest.skipUnless(int(os.environ.get('CHANNEL_PLAYBACK_QA_ROWS', '0')) >= 1_000_000,
                         'Explicit million-row synthetic QA required')
    def test_large_full_http_route_is_readonly_constant_and_returns_every_connection(self):
        rows = int(os.environ['CHANNEL_PLAYBACK_QA_ROWS'])
        self.assertLessEqual(rows, 4_000_000)
        self.assertEqual(len(Base.metadata.tables), 40)
        self.assertEqual(sum(len(table.columns) for table in Base.metadata.tables.values()), 505)
        side = self.res['A']
        channels = [side['channel']]
        players = [side['player']]
        for i in range(18):
            channel = self.add(Channel, client_id=self.tenants['A'].id, branch_id=side['branch'].id,
                               name='Large QA ' + str(i))
            player = self.add(PlayerDevice, client_id=channel.client_id, branch_id=channel.branch_id,
                              channel_id=channel.id, hardware_id='qa-' + str(i), device_name='QA',
                              player_token_hash='synthetic-only-' + uuid4().hex)
            channels.append(channel)
            players.append(player)
        self.db.commit()
        mapping = [{'ordinal': i, 'channel': channel.id, 'player': player.id}
                   for i, (channel, player) in enumerate(zip(channels, players))]
        expected_ids = {channel.id for channel in channels}
        expected_label = side['content'].name
        parameters = {'mapping': json.dumps(mapping), 'rows': rows, 'client': self.tenants['A'].id,
                      'branch': side['branch'].id, 'content': side['content'].id,
                      'campaign': side['campaign'].id, 'layout': side['layout'].id}
        self.db.rollback()
        # Independent synthetic fixture/index DDL, NOT execution of migration019.
        # Bounded inserts also bound deferred FK-trigger memory on the QA host.
        for start in range(1, rows + 1, 100_000):
            with self.engine.begin() as connection:
                connection.exec_driver_sql("SET LOCAL statement_timeout='0'")
                connection.execute(text('''
                INSERT INTO player_heartbeats
                  (id,player_device_id,channel_id,client_id,branch_id,status,current_content_id,
                   current_campaign_id,current_layout_id,received_at,
                   resolution_json,cache_status_json,errors_json,playback_position)
                SELECT md5('channel-playback-qa-' || g::text), m.player, m.channel,
                       :client, :branch, 'online', :content, :campaign, :layout,
                       timestamptz '2026-01-01 00:00:00+00' + g * interval '1 second',
                       '{}'::json, '{}'::json, '[]'::json, '{}'::json
                FROM generate_series(:start, :stop) g
                JOIN jsonb_to_recordset(CAST(:mapping AS jsonb))
                  AS m(ordinal int,channel text,player text) ON (g % 19) = m.ordinal
                '''), dict(parameters, start=start, stop=min(rows, start + 99_999)))
        with self.engine.begin() as connection:
            connection.exec_driver_sql("SET LOCAL statement_timeout='0'")
            connection.exec_driver_sql('CREATE INDEX ix_player_heartbeats_channel_received_at_id '
                                       'ON player_heartbeats (channel_id,received_at DESC,id DESC)')
            connection.exec_driver_sql('ANALYZE player_heartbeats')
        pool = Counter()
        sql_reads = []
        readonly = []
        writes = []
        def checkout(*_): pool['checkout'] += 1
        def checkin(*_): pool['checkin'] += 1
        def observe(connection, _cursor, statement, _parameters, _context, _many):
            head = statement.lstrip().split(None, 1)[0].upper()
            if head in {'INSERT', 'UPDATE', 'DELETE', 'MERGE', 'CREATE', 'DROP', 'ALTER', 'TRUNCATE'}:
                writes.append(head)
            if 'latest_heartbeat' in statement and 'FROM player_heartbeats' in statement:
                sql_reads.append(statement)
                readonly.append(connection.exec_driver_sql('SHOW transaction_read_only').scalar_one())
        for name, fn in [('checkout', checkout), ('checkin', checkin), ('before_cursor_execute', observe)]:
            event.listen(self.engine, name, fn)
            self.addCleanup(event.remove, self.engine, name, fn)
        samples = []
        for _ in range(3):
            start = time.perf_counter()
            response = self.request('GET', '/api/channels/')
            samples.append(time.perf_counter() - start)
            self.assertEqual(response.status_code, 200, response.text)
            payload = response.json()
            self.assertEqual({item['id'] for item in payload}, expected_ids)
            self.assertTrue(all(item['current_playback'] == expected_label for item in payload))
            self.assertTrue(all(isinstance(item['device_outputs'], list) for item in payload))
            self.assertEqual(self.engine.pool.checkedout(), 0)
            self.assertEqual(pool['checkout'], pool['checkin'])
        self.assertEqual(len(sql_reads), 3, 'Exactly one heartbeat statement per full HTTP list')
        self.assertEqual(readonly, ['on'] * 3)
        self.assertEqual(writes, [])
        self.assertLess(max(samples), 3.0, 'Full route budget; SQL timeout remains 3000ms')
        report = {'rows': rows, 'visible_channels': 19, 'tables': 40, 'columns': 505,
                  'sql_statement_timeout_ms': 3000, 'route_seconds': samples,
                  'median_seconds': median(samples), 'worst_seconds': max(samples),
                  'heartbeat_queries_per_request': 1, 'transaction_read_only': readonly,
                  'write_statements': writes, 'pool_events': dict(pool), 'pool_checkedout_final': 0}
        destination = os.environ.get('CHANNEL_PLAYBACK_QA_HTTP_RESULT')
        if destination:
            root = Path(__file__).resolve().parents[2]
            path = Path(destination).resolve()
            if not path.is_relative_to(root / '.tmp' / 'CHANNELS_BATCH_QA'):
                raise RuntimeError('QA results must stay inside the repository QA directory')
            path.write_text(json.dumps(report, indent=2), encoding='utf8')


if __name__ == '__main__':
    unittest.main()
