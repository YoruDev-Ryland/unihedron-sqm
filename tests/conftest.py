from __future__ import annotations

import os
import tempfile

# sqm_service.main builds its configuration and database at import time, so
# web tests need a private data directory before anything imports it.
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="sqm-test-"))
os.environ.setdefault("IMPORT_DIR", os.path.join(os.environ["DATA_DIR"], "imports"))
os.environ.setdefault("ADMIN_PASSWORD", "Web-Test-Password-1")
os.environ.setdefault("API_KEY", "web-test-api-key")
