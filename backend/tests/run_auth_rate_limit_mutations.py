"""R2B mutations in disposable copies only; errors/skips never count as kills."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory


BACKEND = Path(__file__).resolve().parents[1]
SERVICE = "app/services/auth_rate_limit.py"
BUDGET = "test_auth_rate_limit.BudgetTests."
ROUTE = "test_auth_rate_limit.RouteTests."
MIDDLEWARE = "test_auth_rate_limit.MiddlewareTests."

MUTATIONS = [
    ("limiter_disabled", "    def _admit(self, specs, *, failure: bool):\n",
     "    def _admit(self, specs, *, failure: bool):\n        return Reservation(self, ()) if failure else None\n",
     BUDGET + "test_login_pair"),
]
for scope, target in (
    ("login:failure:pair", "test_login_pair"),
    ("login:failure:identifier", "test_login_identifier"),
    ("login:failure:ip", "test_login_ip"),
    ("activation:failure:pair", "test_activation_pair"),
    ("activation:failure:code", "test_activation_code"),
    ("activation:failure:ip", "test_activation_ip"),
):
    MUTATIONS.append((scope.replace(":", "_") + "_removed",
                      "        return self._admit(specs, failure=True)",
                      f'        specs = [spec for spec in specs if spec[0][0] != "{scope}"]\n'
                      "        return self._admit(specs, failure=True)", BUDGET + target))
MUTATIONS.extend([
    ("success_clears_debt", "                bucket.pending -= 1",
     "                bucket.pending -= 1\n                if not failure:\n                    bucket.debt = 0.0",
     BUDGET + "test_success_does_not_clear_previous_debt"),
    ("429_adds_debt", "                raise RateLimitExceeded(retry, key[0], key[1])",
     "                self._buckets[key].debt += 1\n                raise RateLimitExceeded(retry, key[0], key[1])",
     BUDGET + "test_rejection_adds_no_debt_and_reserves_nothing"),
    ("volume_omitted", "                limiter.take_volume(route, client_ip(request))",
     "                pass  # Mutation: no pre-body protection", MIDDLEWARE + "test_prebody_guard_slash_variants_and_unrelated_routes"),
    ("memory_ceiling_omitted", "if len(self._buckets) + missing > self._max_entries:", "if False:",
     BUDGET + "test_memory_ceiling_fail_closed_no_partial_reservation"),
    ("pending_reservation_removed", "                    bucket.pending += 1", "                    pass",
     BUDGET + "test_concurrent_login"),
    ("retry_after_wrong", "self.retry_after = max(1, math.ceil(retry))", "self.retry_after = 1",
     BUDGET + "test_retry_after_ceil_and_maximum_of_blocked_budgets"),
    ("xff_read_directly", "return normalize_ip(request.client.host if request.client else None)",
     'return normalize_ip(request.headers.get("x-forwarded-for", request.client.host if request.client else None))',
     ROUTE + "test_forwarded_headers_cannot_change_key"),
    ("raw_identifier_in_log", "digest = hmac.new(self._secret, encoded, hashlib.sha256).hexdigest()",
     'digest = "|".join(reversed(values))', BUDGET + "test_logs_are_hmac_only_and_frequency_bounded"),
    ("admission_lock_removed", "        with self._lock:\n            now = self._clock()\n            if now - self._last_cleanup",
     '        with __import__("contextlib").nullcontext():\n            now = self._clock()\n            if now - self._last_cleanup',
     BUDGET + "test_state_clock_runs_inside_lock_and_hmac_logging_outside"),
    ("pending_reservation_removed_activation", "                    bucket.pending += 1", "                    pass",
     BUDGET + "test_concurrent_activation"),
])

CHILD = """
import json, os, sys, unittest
from pathlib import Path
backend = Path.cwd()
os.environ.update(APP_ENV='test', DATABASE_URL='sqlite://', TEST_DATABASE_URL='', AUTO_SEED='false',
    MEDIA_ROOT=str(backend/'temporary-media'), PUBLIC_BASE_URL='http://testserver',
    SECRET_KEY='isolated-rate-limit-mutation-not-a-deployment-secret')
sys.path[:0] = [str(backend), str(backend/'tests')]
suite = unittest.defaultTestLoader.loadTestsFromNames(sys.argv[1:])
result = unittest.TextTestRunner(verbosity=1).run(suite)
print(json.dumps(dict(run=result.testsRun, failures=len(result.failures), errors=len(result.errors), skipped=len(result.skipped))))
sys.exit(0 if result.wasSuccessful() else 1)
"""


def check_copy(root, name, targets, mutation=None):
    destination = root / name
    if not destination.resolve().is_relative_to(root.resolve()):
        raise RuntimeError("Mutation copy must remain within disposable directory")
    destination.mkdir()
    for folder in ("app", "tests"):
        shutil.copytree(BACKEND / folder, destination / folder, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    if mutation is not None:
        _, old, new, _ = mutation
        target = destination / SERVICE
        source = target.read_text(encoding="utf-8")
        expected = 2 if name == "memory_ceiling_omitted" else 1
        if source.count(old) != expected:
            raise RuntimeError(f"Mutation anchor mismatch: {name}")
        target.write_text(source.replace(old, new), encoding="utf-8")
    completed = subprocess.run([sys.executable, "-B", "-c", CHILD, *targets], cwd=destination,
                               env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
                               capture_output=True, text=True, timeout=90)
    lines = completed.stdout.strip().splitlines()
    try:
        summary = json.loads(lines[-1])
    except (IndexError, ValueError):
        return {"case": name, "result": "FAIL", "reason": "Child did not produce a test summary"}
    killed = (completed.returncode == 1 and summary["failures"] > 0)
    baseline_pass = (completed.returncode == 0 and summary["failures"] == 0)
    passed = (summary["run"] == len(targets) and summary["errors"] == 0 and summary["skipped"] == 0
              and (killed if mutation else baseline_pass))
    # Child logs stay in memory; no identifiers/credential sentinels are printed.
    return {"case": name, "result": "PASS" if passed else "FAIL", **summary}


def main():
    results = []
    with TemporaryDirectory(prefix="tecno-r2b-mutations-") as temporary:
        root = Path(temporary).resolve()
        if root == BACKEND or root.is_relative_to(BACKEND):
            raise RuntimeError("Mutations must run outside the repository")
        baseline = check_copy(root, "baseline", list(dict.fromkeys(m[3] for m in MUTATIONS)))
        results.append(baseline)
        print(json.dumps(baseline), flush=True)
        if baseline["result"] == "PASS":
            for mutation in MUTATIONS:
                result = check_copy(root, mutation[0], [mutation[3]], mutation)
                results.append(result)
                print(json.dumps(result), flush=True)
    passed = len(results) == len(MUTATIONS) + 1 and all(r["result"] == "PASS" for r in results)
    print(json.dumps({"mutation_tests": f"{sum(r['result'] == 'PASS' for r in results[1:])}/{len(MUTATIONS)}",
                      "temporary_fixtures_removed": not root.exists(), "result": "PASS" if passed else "FAIL"}), flush=True)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
