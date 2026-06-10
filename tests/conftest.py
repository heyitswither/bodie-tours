import sys
from unittest.mock import MagicMock

# Globally mock google.cloud.firestore for unit tests
mock_firestore_module = MagicMock()
mock_firestore_module.transactional = lambda f: f
mock_firestore_module.SERVER_TIMESTAMP = "SERVER_TIMESTAMP"
mock_firestore_module.FieldFilter = MagicMock

import types

sys.modules["google.cloud.firestore"] = mock_firestore_module
if "google.cloud" in sys.modules:
    sys.modules["google.cloud"].firestore = mock_firestore_module
else:
    pkg = types.ModuleType("google.cloud")
    pkg.__path__ = []
    pkg.firestore = mock_firestore_module
    sys.modules["google.cloud"] = pkg
