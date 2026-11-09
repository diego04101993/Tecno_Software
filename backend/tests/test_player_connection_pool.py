"""Real HTTP lifecycle with a deliberately bounded, owned local PostgreSQL pool.

No app startup, worker, migration or external database. The fixture creates
only its own UUID schema in the explicitly verified disposable QA database.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
import os
import threading
from unittest import skipUnless
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from test_tenant_gate_performance import GateFixture
from app.api.deps import get_db
from app.models.entities import CampaignChannelAssignment


@skipUnless(os.environ.get("SECURITY_R5_TEST_DATABASE_URL"), "Owned local PostgreSQL QA required")
class PlayerConnectionPoolTests(GateFixture):
    def make_engine(self):
        url = make_url(os.environ["SECURITY_R5_TEST_DATABASE_URL"])
        marker = os.environ.get("QA_EXPECT_PG_MARKER")
        expected = os.environ.get("SECURITY_R5_PG_SYSTEM_IDENTIFIER")
        if (url.get_backend_name() != "postgresql" or url.host not in {"127.0.0.1", "localhost"}
                or url.database != "security_r5_qa" or url.query.get("options") or not marker or not expected):
            raise RuntimeError("Refusing an unverified/nonlocal PostgreSQL fixture")
        schema = "player_pool_" + uuid4().hex
        # Allow a bounded queue of real HTTP writes to serialize on the sole
        # socket; the old nested acquisition still cannot succeed at any wait.
        engine = create_engine(url, pool_size=1, max_overflow=0, pool_timeout=5,
                               hide_parameters=True, connect_args={"options": "-csearch_path=" + schema})
        with engine.begin() as conn:
            self.assertEqual(conn.scalar(text("SELECT current_database()")), "security_r5_qa")
            self.assertEqual(conn.scalar(text("SELECT current_setting('vwflex.qa_worker', true)")), marker)
            self.assertEqual(conn.scalar(text("SELECT system_identifier::text FROM pg_control_system()")), expected)
            conn.execute(text("CREATE SCHEMA " + schema))
        def cleanup():
            try:
                with engine.begin() as conn:
                    conn.execute(text("DROP SCHEMA " + schema + " CASCADE"))
            finally:
                engine.dispose()
        self.addCleanup(cleanup)
        return engine

    def setUp(self):
        super().setUp()
        a = self.ids["A"]
        self.db.add(CampaignChannelAssignment(channel_id=a["channel"], campaign_id=a["campaign"]))
        self.db.commit()
        # Exercise the actual production get_db generator/finally-close. Only
        # its factory is replaced with this test's owned local engine.
        self.http.app.dependency_overrides.pop(get_db)
        self.enterContext(patch("app.api.deps.SessionLocal", sessionmaker(
            bind=self.engine, autocommit=False, autoflush=False)))
        self.checked_out = 0
        self.peak_checked_out = 0
        self.pool_events = {"checkout": 0, "checkin": 0}
        self.pool_lock = threading.Lock()
        def checkout(*_):
            with self.pool_lock:
                self.checked_out += 1
                self.peak_checked_out = max(self.peak_checked_out, self.checked_out)
                self.pool_events["checkout"] += 1
        def checkin(*_):
            with self.pool_lock:
                self.checked_out -= 1
                self.pool_events["checkin"] += 1
        event.listen(self.engine, "checkout", checkout)
        event.listen(self.engine, "checkin", checkin)
        self.addCleanup(event.remove, self.engine, "checkout", checkout)
        self.addCleanup(event.remove, self.engine, "checkin", checkin)

    def call(self, kind):
        a = self.ids["A"]
        headers = {"Authorization": "Bearer " + self.rows["A"]["token"]}
        if kind == "runtime":
            return self.http.get("/api/player/runtime-config", headers=headers)
        if kind == "heartbeat":
            return self.http.post("/api/player/heartbeat", headers=headers, json={
                "status": "online", "current_content_id": a["content"],
                "current_campaign_id": a["campaign"], "current_layout_id": a["layout"]})
        return self.http.post("/api/player/playback-events", headers=headers, json={
            "content_id": a["content"], "campaign_id": a["campaign"], "layout_id": a["layout"],
            "started_at": datetime.now(UTC).isoformat(), "status": "completed"})

    def assert_pool_returned(self):
        self.assertEqual(self.engine.pool.checkedout(), 0)
        self.assertEqual(self.checked_out, 0)
        self.assertEqual(self.pool_events["checkout"], self.pool_events["checkin"])

    def test_runtime_with_authentication_needs_only_one_checkout_at_a_time(self):
        response = self.call("runtime")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["videowall"], {"enabled": False})
        self.assertEqual(response.json()["runtime_version"], 3)
        self.assert_pool_returned()
        self.assertLessEqual(self.peak_checked_out, 1)

    def test_repeated_runtime_heartbeat_and_events_do_not_accumulate_connections(self):
        for _ in range(12):
            for kind, expected in (("runtime", 200), ("heartbeat", 200), ("event", 201)):
                response = self.call(kind)
                self.assertEqual(response.status_code, expected, response.text)
                self.assert_pool_returned()
        self.assertLessEqual(self.peak_checked_out, 1)
        self.assertEqual(self.engine.pool.size(), 1)

    def test_concurrent_mixed_requests_return_every_connection(self):
        kinds = ("runtime", "heartbeat", "event") * 2
        # Start a bounded wave; the small pool queues requests instead of each
        # holder waiting for a second socket from that same exhausted pool.
        with ThreadPoolExecutor(max_workers=3) as executor:
            results = list(executor.map(self.call, kinds))
        for kind, response in zip(kinds, results):
            self.assertEqual(response.status_code, 201 if kind == "event" else 200, response.text)
        self.assert_pool_returned()

    def test_auth_and_snapshot_errors_release_connections_and_next_request_recovers(self):
        response = self.http.get("/api/player/runtime-config", headers={"Authorization": "Bearer invalid-synthetic"})
        self.assertEqual(response.status_code, 401)
        self.assert_pool_returned()
        with patch("app.services.player_runtime.build_player_runtime_config", side_effect=RuntimeError("synthetic snapshot failure")):
            response = self.call("runtime")
        self.assertEqual(response.status_code, 503, response.text)
        self.assert_pool_returned()
        response = self.call("runtime")
        self.assertEqual(response.status_code, 200, response.text)
        self.assert_pool_returned()
