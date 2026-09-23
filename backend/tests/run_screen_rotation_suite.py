"""Run rotation and retention tests without an application server or external DB."""
from pathlib import Path
import os
import sys
from tempfile import TemporaryDirectory
import unittest


def main() -> int:
    backend = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(backend))
    with TemporaryDirectory(prefix="tecno-screen-rotation-") as media_root:
        # Override inherited configuration BEFORE importing any application module.
        os.environ["DATABASE_URL"] = "sqlite://"
        os.environ["TEST_DATABASE_URL"] = ""
        os.environ["AUTO_SEED"] = "false"
        os.environ["MEDIA_ROOT"] = media_root
        os.environ["PUBLIC_BASE_URL"] = "http://testserver"
        os.environ["SECRET_KEY"] = "isolated-screen-rotation-test-only"
        from app.core.database import engine

        if engine.url.get_backend_name() != "sqlite" or engine.url.database not in (None, "", ":memory:"):
            raise RuntimeError("Refusing to test against a non-memory database")
        print("Verified test configuration: sqlite:// (memory), AUTO_SEED=false; no app.main/startup", flush=True)
        suite = unittest.defaultTestLoader.discover(str(backend / "tests"), pattern="test_*.py")
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        engine.dispose()
        return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
