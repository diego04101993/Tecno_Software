"""Legacy final-state test synchronization for durable playback acceptance.

This helper changes no HTTP timeout, payload or product response. Old tests that
inspect final event rows immediately after 201 opt into a bounded settlement
step *after* the response. Acceptance/latency tests must set
``legacy_playback_settlement = False`` and observe the unmodified acceptance.
No global HTTP client or product function is monkeypatched.
"""
from functools import wraps
import time

from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.models.entities import PlayerPlaybackEvent, PlayerPlaybackInbox


def settle_legacy_player_event(testcase, engine, event_id, *, timeout=10.0):
    from app.api.routes.player import _record_player_playback_event
    from app.services.playback_inbox import materialize_one

    testcase.assertIsInstance(event_id, str)
    deadline = time.monotonic() + timeout
    last_status = "UNKNOWN"
    last_error_code = None
    while True:
        # Every probe owns/closes its connection before advancing real work.
        with Session(engine, autoflush=False) as db:
            exists = select(PlayerPlaybackEvent.id).where(PlayerPlaybackEvent.id == event_id).exists()
            status = select(PlayerPlaybackInbox.status).where(PlayerPlaybackInbox.id == event_id).scalar_subquery()
            error = select(PlayerPlaybackInbox.last_error_code).where(PlayerPlaybackInbox.id == event_id).scalar_subquery()
            finished, last_status, last_error_code = db.execute(select(exists, status, error)).one()
            if finished:
                return
            testcase.assertIsNotNone(last_status, "201 has neither its durable receipt nor its final event")
            testcase.assertNotEqual(last_status, "FAILED",
                                    "Accepted playback receipt reached FAILED: " + str(last_error_code))
        if time.monotonic() >= deadline:
            testcase.fail("Playback final-state fixture did not settle within its internal deadline; "
                          "status=" + str(last_status) + "; error_code=" + str(last_error_code))
        # This is the real current processor, with its normal tenant/media
        # guards and transaction. Another worker's advisory lock simply returns
        # False; it is never bypassed and FAILED/retry state is never rewritten.
        progressed = materialize_one(engine, _record_player_playback_event)
        if not progressed:
            time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))


def install_legacy_player_settlement(testcase, http, engine):
    """Install on one entered TestClient; stop its worker before portal teardown."""
    from app.services.playback_inbox import stop_worker

    if getattr(http, "_legacy_playback_fixture_installed", False):
        return
    testcase.assertIsNotNone(http.portal, "Player fixture requires an entered TestClient lifecycle")
    http._legacy_playback_fixture_installed = True
    # unittest cleanups are LIFO. Call after enterContext(TestClient(...)) so the
    # consumer ends while its event loop and owned fixture engine still exist.
    testcase.addCleanup(http.portal.call, stop_worker, http.app, engine)
    if not getattr(testcase, "legacy_playback_settlement", True):
        return
    original_request = http.request

    @wraps(original_request)
    def request(*args, **kwargs):
        response = original_request(*args, **kwargs)
        if (response.status_code == 201 and response.request.method == "POST"
                and response.request.url.path == "/api/player/playback-events"):
            if isinstance(engine.pool, StaticPool):
                # These old in-memory fixtures share one physical SQLite socket
                # with their assertion Session. Finish the product consumer
                # before those final-state reads, then materialize manually if
                # its stop occurred before this particular receipt was claimed.
                http.portal.call(stop_worker, http.app, engine)
            settle_legacy_player_event(testcase, engine, response.json()["event_id"])
        return response

    http.request = request
