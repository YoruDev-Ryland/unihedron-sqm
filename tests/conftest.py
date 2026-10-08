from __future__ import annotations

import os
import tempfile

import pytest
from fastapi.testclient import TestClient

# sqm_service.main builds its configuration and database at import time, so
# web tests need a private data directory before anything imports it.
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="sqm-test-"))
os.environ.setdefault("IMPORT_DIR", os.path.join(os.environ["DATA_DIR"], "imports"))
os.environ.setdefault("ADMIN_PASSWORD", "Web-Test-Password-1")
os.environ.setdefault("API_KEY", "web-test-api-key")



@pytest.fixture(scope="session")
def app_client():
    # The app closes its module-level database on shutdown, so it is started
    # once for the whole session.
    from sqm_service import main

    with TestClient(main.app) as test_client:
        yield test_client
