"""The disposable handoff treats an unready collector as a bounded STOP."""

from io import BytesIO
from threading import Event
import unittest

from qa_player_observer import read_capture_ready


class CaptureReadyTests(unittest.TestCase):
    def test_preserves_ready_line_without_consuming_observation(self):
        ready = b'{"type":"ready","started_ns":100}\n'
        wire = BytesIO(ready + b'{"type":"done"}\n')
        self.assertEqual(read_capture_ready(wire), ready)
        self.assertEqual(wire.readline(), b'{"type":"done"}\n')

    def test_timeout_and_invalid_signal_only_expose_safe_error(self):
        blocked = Event()

        class QuietStream:
            def readline(self, _limit):
                blocked.wait(1)
                return b""

        try:
            with self.assertRaisesRegex(RuntimeError, "^PASSIVE_CAPTURE_READINESS_TIMEOUT$"):
                read_capture_ready(QuietStream(), timeout=0.01)
        finally:
            blocked.set()
        with self.assertRaisesRegex(RuntimeError, "^PASSIVE_CAPTURE_FAILED_TO_START$"):
            read_capture_ready(BytesIO(b'{"token":"disposable-test-token"}\n'))
