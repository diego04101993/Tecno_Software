"""Seal Caddy's generation-zero configuration before the Edge starts."""

import argparse
import os
import stat
import sys
from pathlib import Path

from edge_boot import gated_config
from release_state import Stop, canonical, decode, require, require_private, sync_dir, write_private_atomic
from routectl import BLUE_PAIR, MAX_CONFIG_BYTES, RouteConfig


def bootstrap_config(adapted):
    config = decode(adapted)
    try:
        public = config["apps"]["http"]["servers"]["public"]
        require(isinstance(public, dict) and "@id" not in public, "UNEXPECTED_BOOT_GENERATION")
        public["@id"] = "bgv2-generation-0"
        config["apps"]["http"]["servers"]["redirect"].setdefault("routes", [])
    except (KeyError, TypeError) as exc:
        raise Stop("INVALID_ADAPTED_CONFIG") from exc
    result = RouteConfig(gated_config(config))
    require(result.active == BLUE_PAIR, "BOOTSTRAP_REQUIRES_BLUE")
    return result


def seal_new(path, contents):
    """Publish complete bytes without replacing an existing artifact."""
    write_private_atomic(path, contents, replace=False)


def seal_bootstrap(config, config_dir, baseline):
    require_private(config_dir, directory=True)
    require_private(baseline.parent, directory=True)
    if os.name == "posix":
        require(stat.S_IMODE(config_dir.stat().st_mode) == 0o700, "CONFIG_DIRECTORY_MODE")
    autosave_dir = config_dir / "caddy"
    autosave_dir.mkdir(mode=0o700, exist_ok=True)
    require_private(autosave_dir, directory=True)
    if os.name == "posix":
        require(stat.S_IMODE(autosave_dir.stat().st_mode) == 0o700, "AUTOSAVE_DIRECTORY_MODE")
    sync_dir(config_dir)
    autosave = autosave_dir / "autosave.json"
    require(baseline.resolve() != autosave.resolve(), "BOOTSTRAP_PATHS_COLLIDE")
    for path in (baseline, autosave):
        require(not path.exists() and not path.is_symlink(), "BOOTSTRAP_ARTIFACT_EXISTS")
    contents = canonical(config.config)
    seal_new(baseline, contents)
    seal_new(autosave, contents)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-dir", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        adapted = sys.stdin.buffer.read(MAX_CONFIG_BYTES + 1)
        require(len(adapted) <= MAX_CONFIG_BYTES, "ADAPTED_CONFIG_TOO_LARGE")
        config = bootstrap_config(adapted)
        seal_bootstrap(config, args.config_dir, args.baseline)
    except Stop as exc:
        print(f"STOP: {exc}", file=sys.stderr)
        return 1
    except OSError:
        print("STOP: BOOTSTRAP_ARTIFACT_IO_ERROR", file=sys.stderr)
        return 1
    print(f"EDGE_BASELINE_SHA256={config.hash}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
