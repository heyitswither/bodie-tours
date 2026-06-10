import pytest
from unittest.mock import MagicMock, patch
import sys

sys.path.insert(0, ".")


# Mock functions_framework and firestore before importing main
class DummyFunctionsFramework:
    @staticmethod
    def http(func):
        return func


if "functions_framework" not in sys.modules:
    sys.modules["functions_framework"] = DummyFunctionsFramework

mock_firestore_module = MagicMock()
# Make the @firestore.transactional decorator a no-op passthrough
mock_firestore_module.transactional = lambda f: f
mock_firestore_module.SERVER_TIMESTAMP = "SERVER_TIMESTAMP"
mock_firestore_module.FieldFilter = MagicMock

if "google.cloud.firestore" not in sys.modules:
    sys.modules["google.cloud"] = MagicMock()
    sys.modules["google.cloud.firestore"] = mock_firestore_module

import main


def test_process_booking_negative_party_size():
    transaction = MagicMock()
    inventory_ref = MagicMock()
    # Snapshot does not exist so it won't try to read capacity
    snapshot = MagicMock()
    snapshot.exists = False
    inventory_ref.get.return_value = snapshot

    with pytest.raises(ValueError, match="Party size must be greater than 0."):
        main.process_booking_transaction(
            transaction,
            inventory_ref,
            "2026-06-15",
            "10:00",
            -1,  # Negative party size
            {"name": "Alice"},
        )


def test_process_booking_zero_party_size():
    transaction = MagicMock()
    inventory_ref = MagicMock()
    snapshot = MagicMock()
    snapshot.exists = False
    inventory_ref.get.return_value = snapshot

    with pytest.raises(ValueError, match="Party size must be greater than 0."):
        main.process_booking_transaction(
            transaction,
            inventory_ref,
            "2026-06-15",
            "10:00",
            0,  # Zero party size
            {"name": "Alice"},
        )
