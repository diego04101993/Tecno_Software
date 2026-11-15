"""Check passive BLUE continuity on an isolated initial Edge handoff."""

import json
from pathlib import Path
import tempfile
from types import SimpleNamespace

from qa_docker import LocalDocker, PROJECT_DIRECTORY
from qa_http import utc_now
from qa_initial_handoff import HANDOFF_LIMIT_SECONDS, Handoff, Timeline, handoff, prepare, prepare_trial, reverse
from qa_player_observer import Capture, heartbeat, logs, playback

from gates import device_ref, utc
from player_observer import capture_phase3_baseline, decide_phase3, from_ns, phase3_context, read_only_query


class BaselinePlayerMissing(Exception):
    """The passive gate rejected the disposable handoff."""


def verify_prepared(fixture, *, epoch=False, checkpoint=None, missing_one=False):
    prepare_trial(fixture)
    db = fixture.db
    for token in db.player_tokens:
        heartbeat(fixture.http, token)
    state = fixture.store.load()
    context = phase3_context(state, db.pg)
    baseline = capture_phase3_baseline(db.readonly, state, fixture.key, db.pg)
    baseline_refs = [device_ref(device, fixture.key) for device in db.fixture["devices"]]
    assert len(baseline_refs) == len(db.player_tokens) > 1
    assert set(baseline["payload"]["baseline_refs"]) == set(baseline_refs)
    reported_count = len(baseline_refs) - int(missing_one)
    expected = set(baseline_refs[:reported_count])
    result = {}
    topology = SimpleNamespace(docker=fixture.docker, edge=fixture.edge, bindings={"api:BLUE": fixture.binding("api")})
    capture = Capture(topology, 155, slot="BLUE", defer=True)

    def start_capture(live, controller):
        assert not live.wire.ingress_open() and controller.store.load() == state
        capture.start()

    def observe(live, controller, timeline):
        opened_at = timeline.receipt()["T_INGRESS_READY"]["utc"]
        started = utc_now()
        assert live.wire.ingress_open()
        # The emulated Players belong only to the disposable fixture. The
        # observer itself initiates no HTTP requests or database writes.
        for token in db.player_tokens[:reported_count]:
            heartbeat(live.edge_http, token)
        playback(live.edge_http, db.player_tokens[0])
        print("PHASE3_PASSIVE_BLUE_WINDOW_STARTED", flush=True)
        wire = capture.collect()
        finished = from_ns(json.loads(wire[-1])["ended_ns"]).isoformat()
        receipt, accepted = decide_phase3(wire, logs(live), lambda pairs: read_only_query(db.readonly, pairs),
                                         baseline, context, fixture.key, started, finished, opened_at)
        payload = receipt["payload"]
        assert accepted is (not missing_one)
        assert payload["result"] == ("FAIL" if missing_one else "PASS")
        assert payload["playback_evidence"] == "PASS"
        assert {row["device_ref"] for row in payload["heartbeats"]} == expected
        assert payload["slot"] == "BLUE" and payload["generation"] == context["generation"]
        assert payload["upstream"] == context["upstream"]
        assert controller.store.load() == state and live.wire.ingress_open()
        result.update(result="PASS", continuity="ALL_BASELINE_CONTINUOUS" if accepted else "FAIL",
                      generation=context["generation"], slot=context["slot"],
                      active_baseline=len(baseline_refs), matched=len(expected), playback=payload["playback_evidence"],
                      seconds=(utc(finished) - utc(started)).total_seconds(), observer_generated_requests=0,
                      capture_ready_to_ingress_seconds=(utc(opened_at) - from_ns(json.loads(wire[0])["started_ns"])).total_seconds())
        if not accepted:
            raise BaselinePlayerMissing

    try:
        handoff(fixture, Timeline(), before_open=start_capture, after_open=observe)
    except BaselinePlayerMissing:
        assert missing_one
        rollback = Timeline()
        reverse(fixture, rollback)
        result["rollback_seconds"] = rollback.elapsed("T_ROLLBACK_DECISION", "T_FIRST_R3_HTTP_200")
        assert result["rollback_seconds"] <= HANDOFF_LIMIT_SECONDS, "ROLLBACK_DOWNTIME_LIMIT_EXCEEDED"
    else:
        if checkpoint is not None:
            checkpoint(result)
        if epoch:
            from qa_legacy_epoch import verify_admitted

            result["legacy_epoch"] = verify_admitted(fixture)
        reverse(fixture, Timeline())
    finally:
        capture.close()
        fixture.docker.run("stop", "--time", "1", capture.container)
    result["historical_frontend_restored"] = True
    return result


def main():
    if not __debug__:
        raise RuntimeError("Disposable QA requires assertions enabled")
    docker = LocalDocker()
    with tempfile.TemporaryDirectory(prefix="bgv22-phase3-player-", dir=PROJECT_DIRECTORY / ".tmp") as root:
        try:
            fixture = Handoff(docker, Path(root))
            prepare(fixture)
            missing = verify_prepared(fixture, missing_one=True)
            result = {"continuous": verify_prepared(fixture, epoch=True), "missing_one": missing}
        finally:
            docker.cleanup()
    print(json.dumps(result, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
