"""Replay the approved pre-reconnect R3 frontend against freshly measured evidence."""

import argparse
import json
from pathlib import Path
import sys

from qa_docker import DEPLOY_DIRECTORY

sys.path.insert(0, str(DEPLOY_DIRECTORY))
from docker_networks import VOLATILE_ENDPOINT_FIELDS
from legacy_frontend_blue import LEGACY_FRONTEND_IMAGE_EXACT, verify_historical_stable_projection
from qa_r3_rollback_archive import ARCHIVE_SHA256, PAYLOAD_SHA256, approved
from release_state import canonical, decode, digest, require


BEFORE_TREE_FILE_SHA256 = "b74394a9b5f1d0a2c55fef198df106de8e5a65c037bcc96d4dc7f01bbb5d41f6"
BEFORE_TREE_SHA256 = "e7cadd1e11557627fca3123983608bfcc723020b370a7019f447b20eb930fb2a"
RETRY_RECEIPT_SHA256 = "cc7148b366c14671588fdd1a1949a5beed101ef3ac790479861b3521f648643d"


def replay(manifest_path, before_tree_path, retry_receipt_path, current_evidence_path):
    manifest = approved(manifest_path, ARCHIVE_SHA256)
    before = {key: manifest["payload"][key] for key in ("historical", "image", "content")}
    before_tree = digest(approved(before_tree_path, BEFORE_TREE_FILE_SHA256))
    require(before_tree == BEFORE_TREE_SHA256, "STABLE_FRONTEND_REPLAY_BEFORE_SRV_CHANGED")

    receipt = approved(retry_receipt_path, RETRY_RECEIPT_SHA256)
    require(receipt["approved_initial_manifest"]["sha256"] == PAYLOAD_SHA256
            and receipt["stable_network"]["status"] == "PASS",
            "STABLE_FRONTEND_REPLAY_PREVIOUS_RECONNECT_NOT_ATTESTED")
    current = decode(current_evidence_path.read_bytes())
    keys = {"historical_frontend_projection", "historical_classifier_evidence"} & set(current)
    require(len(keys) == 1, "STABLE_FRONTEND_REPLAY_CURRENT_PROJECTION_INVALID")
    after = current[keys.pop()]
    after_tree = digest(current["srv_inventory"])
    require(after_tree == current["srv_snapshot_a_sha256"] == current["srv_snapshot_b_sha256"],
            "STABLE_FRONTEND_REPLAY_CURRENT_SRV_UNSTABLE")

    compose = receipt["historical_compose_proof"]
    old = verify_historical_stable_projection(manifest, PAYLOAD_SHA256, before,
                                               compose_proof=compose, observed_srv_tree_sha256=before_tree)
    new = verify_historical_stable_projection(manifest, PAYLOAD_SHA256, after,
                                               compose_proof=compose, observed_srv_tree_sha256=after_tree)
    require(old["classification"] == new["classification"] == LEGACY_FRONTEND_IMAGE_EXACT,
            "STABLE_FRONTEND_REPLAY_CLASSIFICATION_CHANGED")
    evidence_key = "legacy_frontend_evidence_sha256"
    identity_key = "legacy_frontend_stable_pin"
    require(old[evidence_key] != new[evidence_key], "STABLE_FRONTEND_REPLAY_RAW_EVIDENCE_DID_NOT_CHANGE")
    require(old[identity_key] == new[identity_key], "STABLE_FRONTEND_REPLAY_PIN_CHANGED")

    changes = {name: sorted(field for field in VOLATILE_ENDPOINT_FIELDS
                            if canonical(rows["approved"].get(field)) != canonical(rows["observed"].get(field)))
               for name, rows in new["volatile_endpoint_evidence"].items()}
    require(any(changes.values()), "STABLE_FRONTEND_REPLAY_VOLATILE_CHANGE_NOT_OBSERVED")
    return {"result": "PASS", "old_raw_evidence_sha256": old[evidence_key], "current_raw_evidence_sha256": new[evidence_key],
            "old_stable_pin": old[identity_key], "current_stable_pin": new[identity_key],
            "legacy_frontend_replay_before_reconnect": old["classification"],
            "legacy_frontend_replay_after_reconnect": new["classification"],
            "old_srv_tree_sha256": before_tree, "current_srv_tree_sha256": after_tree,
            "observed_volatile_fields_changed": changes}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("manifest", "before-tree", "retry-receipt", "current-evidence"):
        parser.add_argument("--" + name, required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(replay(args.manifest, args.before_tree, args.retry_receipt, args.current_evidence), sort_keys=True))
