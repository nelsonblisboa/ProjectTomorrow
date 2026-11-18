import os
import tempfile

# Isolate integration-test data from a developer's real local database.
os.environ.setdefault("MAG_DEMO", "1")
os.environ.setdefault("MAG_SECRET", "test-only-session-secret-at-least-32-chars")
os.environ.setdefault("MAG_DATA_DIR", tempfile.mkdtemp(prefix="mag-tracker-tests-"))
