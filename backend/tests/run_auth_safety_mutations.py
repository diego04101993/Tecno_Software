"""Run SECURITY-1H-R1 mutations only in disposable copies outside the repo."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory


BACKEND = Path(__file__).resolve().parents[1]
AUTH = "test_auth_revocation_safety.AuthRevocationSafetyTests."
LOGS = "test_auth_log_redaction.AuthLogRedactionTests."
MUTATIONS = (
    ("jwt_parse_exception_unhandled", "app/core/security.py",
     "except (ValueError, RecursionError):", "except RecursionError:",
     AUTH + "test_invalid_signature_encoding_returns_401"),
    ("websocket_stale_claims_only", "app/api/routes/realtime.py",
     'user = get_current_user(authorization=f"Bearer {token}", db=db)',
     'from types import SimpleNamespace\n            from app.core.security import decode_access_token\n'
     '            payload = decode_access_token(token)\n'
     '            user = SimpleNamespace(role=UserRole(payload["role"]), client_id=payload.get("client_id"), branch_id=payload.get("branch_id"))',
     AUTH + "test_ws_disabled_user_with_old_token_is_rejected"),
    ("public_activation_reenables_disabled_player", "app/api/routes/player.py",
     "if player_device is not None and not player_device.is_active:", "if False:",
     AUTH + "test_disabled_player_public_reactivation_rejected_and_unchanged"),
    ("remove_log_redaction_installation", "app/main.py",
     "install_security_log_redaction()\nsettings", "# Mutation: logging redaction disabled\nsettings",
     LOGS + "test_uvicorn_websocket_accept_and_reject_logs_hide_query_token"),
    ("bypass_activation_utf8_validation", "app/schemas/domain.py",
     'value.encode("utf-8")', 'pass  # Mutation: invalid Unicode reaches the route/DB',
     AUTH + "test_activation_device_name_invalid_unicode_rejected_before_sql"),
)

CHILD = """
import json, os, sys, unittest
from pathlib import Path
backend = Path.cwd()
os.environ.update(APP_ENV='test', DATABASE_URL='sqlite://', TEST_DATABASE_URL='', AUTO_SEED='false',
    MEDIA_ROOT=str(backend/'temporary-media'), PUBLIC_BASE_URL='http://testserver',
    SECRET_KEY='isolated-mutation-fixture-not-a-deployment-secret')
sys.path[:0] = [str(backend), str(backend/'tests')]
suite = unittest.defaultTestLoader.loadTestsFromNames(sys.argv[1:])
result = unittest.TextTestRunner(verbosity=1).run(suite)
print(json.dumps(dict(run=result.testsRun, failures=len(result.failures), errors=len(result.errors), skipped=len(result.skipped))))
sys.exit(0 if result.wasSuccessful() else 1)
"""


def check_copy(root: Path, name: str, targets: list[str], mutation=None) -> dict:
    destination = root / name
    assert destination.resolve().is_relative_to(root.resolve())
    destination.mkdir()
    for folder in ("app", "tests"):
        shutil.copytree(BACKEND / folder, destination / folder, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    if mutation is not None:
        _, relative, old, new, _ = mutation
        target = destination / relative
        source = target.read_text(encoding="utf-8")
        if source.count(old) != 1:
            raise RuntimeError(f"Mutation target must match once: {name}")
        target.write_text(source.replace(old, new, 1), encoding="utf-8")
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    completed = subprocess.run([sys.executable, "-B", "-c", CHILD, *targets], cwd=destination,
                               env=env, capture_output=True, text=True, timeout=90)
    # Keep logs/sentinels only in memory. Syntax errors, skips and harness errors
    # must not count as killed mutations.
    summary = json.loads(completed.stdout.strip().splitlines()[-1])
    expected_failure = mutation is not None
    passed = (summary["run"] == len(targets) and summary["errors"] == 0 and summary["skipped"] == 0
              and ((completed.returncode == 1 and summary["failures"] > 0) if expected_failure
                   else (completed.returncode == 0 and summary["failures"] == 0)))
    return {"case": name, "result": "PASS" if passed else "FAIL", "expected_test_failure": expected_failure, **summary}


def main() -> int:
    with TemporaryDirectory(prefix="tecno-auth-mutations-") as temporary:
        root = Path(temporary).resolve()
        if root == BACKEND or root.is_relative_to(BACKEND):
            raise RuntimeError("Mutation fixtures must be outside the repository")
        results = [check_copy(root, "baseline", [mutation[4] for mutation in MUTATIONS])]
        if results[0]["result"] == "PASS":
            results.extend(check_copy(root, mutation[0], [mutation[4]], mutation) for mutation in MUTATIONS)
    print(json.dumps({"results": results, "temporary_fixtures_removed": not root.exists()}, indent=2))
    return 0 if len(results) == len(MUTATIONS) + 1 and all(result["result"] == "PASS" for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
