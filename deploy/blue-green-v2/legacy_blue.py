"""Prepare read-only evidence for the initial legacy BLUE backend adoption."""

import argparse
from pathlib import Path
import sys

from edge_config import seal_new
from gates import DockerProbe
from release_state import Stop, canonical, decode, read_private, require_private


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bindings", required=True, type=Path)
    parser.add_argument("--docker-context", default="default")
    parser.add_argument("--trusted-proxy-name", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--manifest", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        bindings = decode(read_private(args.bindings))
        require_private(args.manifest.parent, directory=True)
        probe = DockerProbe(bindings, context=args.docker_context, edge_name=args.trusted_proxy_name)
        manifest = probe.capture_legacy_blue(expected_project=args.project, expected_service="backend")
        seal_new(args.manifest, canonical(manifest) + b"\n")
    except (Stop, OSError, ValueError, KeyError, TypeError) as error:
        print(str(error) if isinstance(error, Stop) else "LEGACY_BLUE_CAPTURE_FAILED", file=sys.stderr)
        return 1
    print(f"LEGACY_BLUE_MANIFEST_SHA256={manifest['sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
