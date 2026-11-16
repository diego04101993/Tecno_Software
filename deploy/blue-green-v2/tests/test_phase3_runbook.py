"""Validate the Phase 3 operator sequence without Docker or production I/O."""

import os
from pathlib import Path
import re
import shutil
import subprocess
import unittest


RUNBOOK = (Path(__file__).resolve().parents[1] / "PHASE3_RUNBOOK.md").read_text(encoding="utf-8")
BLOCKS = re.findall(r"^```(?:sh|bash)\n(.*?)^```$", RUNBOOK, re.M | re.S)


def bash():
    if os.name == "nt":
        git_bash = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git/bin/bash.exe"
        if git_bash.is_file():
            return str(git_bash)
    return shutil.which("bash")


class Phase3RunbookTest(unittest.TestCase):
    def assert_order(self, body, *steps):
        cursor = 0
        for step in steps:
            position = body.find(step, cursor)
            self.assertGreaterEqual(position, 0, step)
            cursor = position + len(step)

    def test_handoff_and_reverse_preserve_roles_and_scope(self):
        self.assert_order(RUNBOOK, "R3_EXACT_IMAGE_QUALIFICATION", "verify_index(package)",
                          'docker_local start "$FRONTEND_BLUE_NAME"', 'docker_local start "$EDGE_ID"',
                          "routectl_pre preview", "phase3_ready.py", "routectl_pre initialize",
                          "deployment_phases.py backup", "routectl_pre prepare-handoff", "phase3-baseline",
                          "T_FRONTEND_STOP", "T_PORTS_FREE", "T_PROXY_READY", "T_TLS_READY",
                          "T_INGRESS_READY", "T_FIRST_HTTP_200", "phase3-observe", "systemctl enable --now",
                          "phase3-final.json", "T_ROLLBACK_DECISION", "systemctl disable --now",
                          "routectl_live close-ingress", "T_EDGE_RELEASED",
                          'docker_local start "$OLD_FRONTEND_ID"', "T_FIRST_R3_HTTP_200")
        self.assertIn("`ACTIVE_API_SLOT=BLUE` y `ACTIVE_FRONTEND_SLOT=BLUE`", RUNBOOK)
        commands = "\n".join(BLOCKS)
        self.assert_order(commands, "routectl_pre prepare-handoff", "phase3-baseline",
                          "routectl_pre execute-handoff", 'test "$HANDOFF_READY" = HANDOFF_PREPARED_VALIDATED',
                          "mark_phase3 T_FRONTEND_STOP",
                          'docker_local stop --time 3 "$OLD_FRONTEND_ID"',
                          "mark_phase3 T_PORTS_FREE",
                          'docker_local network connect --ip 172.30.250.2 "$PROXY_NET" "$EDGE_ID"',
                          'docker_local network connect --ip "$OLD_INGRESS_IP" "$INGRESS_NET" "$EDGE_ID"',
                          "mark_phase3 T_EDGE_OWNS_PORTS", "mark_phase3 T_TLS_READY",
                          "coproc PHASE3_CAPTURE", 'exec {HANDOFF_IN}>&- {HANDOFF_OUT}<&-',
                          "mark_phase3 T_PLAYER_CAPTURE_READY", "HANDOFF_BUDGET=$(handoff_budget 30)",
                          "TRANSFER_AND_CAPTURE_READY", 'wait "$HANDOFF_ROUTING_CHILD"',
                          'first_public_200 "$PUBLIC_HTTP_BUDGET"', "mark_phase3 T_FIRST_HTTP_200", "phase3-observe",
                          'connect_historical_endpoint "$INGRESS_NET"', 'connect_historical_endpoint "$PROXY_NET"',
                          'docker_local start "$OLD_FRONTEND_ID"', 'test "$(port_bindings "$OLD_FRONTEND_ID")" = "$OLD_PORTS"',
                          '"nc", "-z", "-w", "1", "127.0.0.1", "443"',
                          'first_public_200 "$ROLLBACK_HTTP_BUDGET"', "mark_phase3 T_FIRST_R3_HTTP_200")
        for forbidden in ("docker compose", "switch-api", "switch-frontend", "apply019", "ssh ", "recover-ingress",
                          'docker_local stop "$BACKEND_ID"', 'docker_local stop "$PG_ID"'):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, commands)

    def test_prestage_stops_before_fresh_backup_player_baseline_and_ingress_change(self):
        prestage, handoff = RUNBOOK.split("## 4. Ventana de traspaso con autorización independiente", 1)
        self.assertIn("READY_FOR_DOWNTIME_WINDOW=YES", prestage)
        self.assertIn("INGRESS_READY=NO", prestage)
        for command in ("deployment_phases.py backup", "phase3-baseline", "recover-ingress", "docker_local stop"):
            with self.subTest(command=command):
                self.assertNotIn(command, prestage)
        self.assert_order(handoff, "deployment_phases.py backup", "check_backup(state, payload, verify_bytes=True)",
                          "prepare-handoff", "phase3-baseline", "T_FRONTEND_STOP", "PHASE3_REAL_PLAYER_CONTINUITY_GATE",
                          "ALL_BASELINE_CONTINUOUS")

    def test_phase3_observer_uses_time_namespace_launcher_for_every_action(self):
        prestage, handoff = RUNBOOK.split("## 4. Ventana de traspaso con autorización independiente", 1)
        launcher = '/usr/bin/python3 -B "$TOOL/player_observer_launcher.py"'
        inspection = '''CAPTURE_IMAGE_ID=$(docker_local image inspect --format '{{.Id}}' "$CAPTURE_IMAGE")'''
        identity = '--collector-id "$COLLECTOR_ID" --collector-image "$CAPTURE_IMAGE_ID" --collector-restart-count 0'
        probe_receipt = '"$WORK/phase3-clock-probe.json"'
        preflight = f"--preflight-receipt {probe_receipt}"
        self.assert_order(prestage, inspection, 'COLLECTOR_ID=$(docker_local create',
                          '--entrypoint /bin/sh "$CAPTURE_IMAGE_ID"', 'docker_local start "$COLLECTOR_ID"',
                          'docker_local exec "$COLLECTOR_ID"', launcher, identity,
                          f"--clock-receipt {probe_receipt} probe", "READY_FOR_PHASE3_HANDOFF=YES")
        commands = "\n".join(BLOCKS)
        self.assertEqual(commands.count(launcher), 3)
        self.assertNotIn('"$TOOL/player_observer.py"', commands)
        self.assert_order(handoff, launcher, identity, preflight,
                          '--clock-receipt "$WORK/phase3-clock-baseline.json" baseline --',
                          '--receipt "$WORK/phase3-baseline.json" phase3-baseline', "T_FRONTEND_STOP",
                          '"$EDGE_PROXY_IF" 172.30.250.2 "$BACKEND_PROXY_IP" 165 --phase3-monotonic',
                          "mark_phase3 T_FIRST_HTTP_200", launcher, identity, preflight,
                          '--clock-receipt "$WORK/phase3-clock-observe.json"',
                          'observe --timeline "$WORK/phase3-timeline.jsonl" --', "phase3-observe",
                          '--capture-ready-json "$CAPTURE_READY" <&3', 'wait "$CAPTURE_PID"')
        self.assertNotRegex(commands, r"--(?:opened|started|finished)-monotonic-ns|--(?:started|finished)-at")

    @unittest.skipUnless(bash(), "Bash is required for shell snippet validation")
    def test_shell_blocks_parse(self):
        self.assertGreaterEqual(len(BLOCKS), 8)
        for block in BLOCKS:
            with self.subTest(block=block.splitlines()[0]):
                result = subprocess.run([bash(), "-n"], input=block, text=True, capture_output=True, timeout=15)
                self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(bash(), "Bash is required for command simulation")
    def test_writer_must_authorize_before_t0_and_wait_for_transfer(self):
        commands = "\n".join(BLOCKS)
        start = commands.index("coproc HANDOFF_ROUTING {")
        stop = commands.index('docker_local stop --time 3 "$OLD_FRONTEND_ID"', start)
        validate_before_stop = commands[start:stop]
        for approved in (True, False):
            with self.subTest(approved=approved):
                setup = f"set -euo pipefail\nAPPROVED={int(approved)}\n"
                mocks = r"""
WORK=/unused
HANDOFF_PREPARED_SHA256=pin
HANDOFF_ARGS=(--historical-id exact)
mark_phase3() { printf '%s\n' "$1"; }
routectl_pre() {
  [[ "$*" == 'execute-handoff /unused/handoff-prepared.json pin --historical-id exact' ]]
  [[ $ROUTECTL_HANDOFF_CHILD == 1 ]]
  if [[ $APPROVED == 0 ]]; then exit 1; fi
  printf '%s\n' HANDOFF_PREPARED_VALIDATED
  IFS= read -r ack
  [[ $ack == TRANSFER_AND_CAPTURE_READY ]]
  printf '%s\n' INGRESS_READY
}
"""
                after_stop = r"""
printf '%s\n' TRANSFER_AND_CAPTURE_READY >&"$HANDOFF_IN"
exec {HANDOFF_IN}>&-
IFS= read -r result <&"$HANDOFF_OUT"
exec {HANDOFF_OUT}<&-
wait "$HANDOFF_ROUTING_CHILD"
test "$result" = INGRESS_READY
"""
                result = subprocess.run([bash()], input=setup + mocks + validate_before_stop + after_stop,
                                        text=True, capture_output=True, timeout=15)
                if approved:
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout.strip(), "T_FRONTEND_STOP")
                else:
                    self.assertNotEqual(result.returncode, 0)
                    self.assertNotIn("T_FRONTEND_STOP", result.stdout)

    @unittest.skipUnless(bash(), "Bash is required for command simulation")
    def test_first_public_http_rejects_redirect_and_transport_failure(self):
        command = re.search(r"^first_public_200\(\) \{\n.*?^\}", RUNBOOK, re.M | re.S).group()
        mock = r"""
curl() {
  [[ "$*" == *"--connect-to api.tecnocontrolcloud.com:443:192.0.2.1:443"* ]]
  [[ "$*" == *"--max-time $MOCK_BUDGET"* ]]
  [[ "$*" == *"--fail --output /dev/null --write-out %{http_code}"* ]]
  [[ "$*" == *"https://api.tecnocontrolcloud.com/"* ]]
  [[ "$*" != *"--insecure"* ]]
  printf '%s' "$MOCK_STATUS"
  return "$MOCK_RC"
}
"""
        for status, code, budget, expected in ((200, 0, "", 0), (200, 0, "2.345", 0), (308, 0, "", 1),
                                               (500, 22, "", 22), (0, 28, "2.345", 28)):
            with self.subTest(status=status, code=code):
                script = (f"set -eu\nPUBLIC_ADDRESS=192.0.2.1\nMOCK_STATUS={status}\nMOCK_RC={code}\n"
                          f"MOCK_BUDGET={budget or 10}\n" + mock + "\n" + command + f"\nfirst_public_200 {budget}\n")
                result = subprocess.run([bash()], input=script, text=True, capture_output=True, timeout=15)
                self.assertEqual(result.returncode, expected, result.stderr)


if __name__ == "__main__":
    unittest.main()
