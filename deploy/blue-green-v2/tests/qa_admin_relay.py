"""Run the production Unix socket and fsync code in Linux from Windows QA."""

import base64
import json
import sys

from routectl import CaddyEdge


def run():
    request = json.load(sys.stdin)
    edge = CaddyEdge("/run/bgv2/admin.sock", "/config/caddy/autosave.json")
    if request["method"] in ("AUTOSAVE", "AUTOSAVE_READ_ONLY"):
        try:
            raw = edge.read_autosave(synchronize=request["method"] == "AUTOSAVE")
        except FileNotFoundError:
            print(json.dumps({"missing": True}))
        else:
            print(json.dumps({"body": base64.b64encode(raw).decode()}))
    elif request["method"] == "INGRESS_CLOSE":
        edge.close_ingress()
        print("{}")
    elif request["method"] == "INGRESS_OPEN":
        edge.open_ingress(request["config"])
        print("{}")
    elif request["method"] == "INGRESS_STATUS":
        print(json.dumps({"open": edge.ingress_open()}))
    elif request["method"] == "INGRESS_REQUIRE_CLOSED":
        edge.require_closed()
        print("{}")
    else:
        status, etag, raw = edge.request(request["method"], request["etag"], request["config"])
        print(json.dumps({"status": status, "etag": etag, "body": base64.b64encode(raw).decode()}))


if __name__ == "__main__":
    run()
