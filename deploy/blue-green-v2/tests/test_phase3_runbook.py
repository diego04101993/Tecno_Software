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
                          "deployment_phases.py backup", 'docker_local start "$FRONTEND_BLUE_NAME"',
                          'docker_local start "$EDGE_ID"', "routectl_pre preview", "phase3_ready.py",
                          "routectl_pre initialize", "phase3-baseline", "T_R3_STOP_COMMAND",
                          "T_80_443_RELEASED", "T_PROXY_IDENTITY_READY", "T_EDGE_OWNS_PORTS",
                          "T_TLS_WRITER_READY", "T_PLAYER_CAPTURE_READY", "routectl_live recover-ingress",
                          "T_FIRST_PUBLIC_HTTP_200", "phase3-observe", "systemctl enable --now",
                          "phase3-final.json", "T_ROLLBACK_DECISION", "systemctl disable --now",
                          "routectl_live close-ingress", "T_EDGE_RELEASED",
                          'docker_local start "$OLD_FRONTEND_ID"', "T_FIRST_R3_HTTP_200")
        self.assertIn("`ACTIVE_API_SLOT=BLUE` y `ACTIVE_FRONTEND_SLOT=BLUE`", RUNBOOK)
        commands = "\n".join(BLOCKS)
        self.assert_order(commands, "mark_phase3 T_R3_STOP_COMMAND",
                          'docker_local stop --time 3 "$OLD_FRONTEND_ID"',
                          "mark_phase3 T_80_443_RELEASED",
                          'docker_local network connect --ip 172.30.250.2 "$PROXY_NET" "$EDGE_ID"',
                          'docker_local network connect --ip "$OLD_INGRESS_IP" "$INGRESS_NET" "$EDGE_ID"',
                          "mark_phase3 T_EDGE_OWNS_PORTS", "mark_phase3 T_TLS_WRITER_READY",
                          "coproc PHASE3_CAPTURE", "mark_phase3 T_PLAYER_CAPTURE_READY",
                          "routectl_live recover-ingress", "mark_phase3 T_INGRESS_READY",
                          "\nfirst_public_200\n", "mark_phase3 T_FIRST_PUBLIC_HTTP_200", "phase3-observe",
                          'connect_historical_endpoint "$INGRESS_NET"', 'connect_historical_endpoint "$PROXY_NET"',
                          'docker_local start "$OLD_FRONTEND_ID"', 'test "$(port_bindings "$OLD_FRONTEND_ID")" = "$OLD_PORTS"',
                          '"nc", "-z", "-w", "1", "127.0.0.1", "443"',
                          'first_public_200 "$ROLLBACK_HTTP_BUDGET"', "mark_phase3 T_FIRST_R3_HTTP_200")
        for forbidden in ("docker compose", "switch-api", "switch-frontend", "apply019", "ssh ",
                          'docker_local stop "$BACKEND_ID"', 'docker_local stop "$PG_ID"'):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, commands)

    @unittest.skipUnless(bash(), "Bash is required for shell snippet validation")
    def test_shell_blocks_parse(self):
        self.assertGreaterEqual(len(BLOCKS), 8)
        for block in BLOCKS:
            with self.subTest(block=block.splitlines()[0]):
                result = subprocess.run([bash(), "-n"], input=block, text=True, capture_output=True, timeout=15)
                self.assertEqual(result.returncode, 0, result.stderr)

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
