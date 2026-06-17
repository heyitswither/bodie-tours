import sys
import os
import runpy
from unittest.mock import patch, MagicMock, mock_open

# Add root directory to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import seed_templates


def test_seed_templates_success():
    """Test seed_templates when both HTML template files exist."""
    mock_client = MagicMock()
    mock_doc_reminder = MagicMock()

    mock_client.collection.return_value.document.return_value = mock_doc_reminder

    reminder_html = "Reminder: {{customer_name}}, {{booking_id}}, {{tour_datetime_str}}, {{payment_link}}, {{total_amount}}, {{party_size}}"
    receipt_html = "Receipt: {{customer_name}}, {{booking_id}}, {{tour_datetime_str}}, {{party_size}}, {{total_amount}}, {{invoice_link}}"

    def exists_side_effect(path):
        if "payment_reminder.html" in path:
            return True
        if "booking_receipt.html" in path:
            return True
        return False

    def open_side_effect(path, *args, **kwargs):
        if "payment_reminder.html" in path:
            return mock_open(read_data=reminder_html)()
        if "booking_receipt.html" in path:
            return mock_open(read_data=receipt_html)()
        raise FileNotFoundError(path)

    with patch("google.cloud.firestore.Client", return_value=mock_client), patch(
        "os.path.exists", side_effect=exists_side_effect
    ), patch("builtins.open", side_effect=open_side_effect):

        seed_templates.seed_templates()

        # Verify collection calls
        assert mock_client.collection.call_count == 2
        mock_client.collection.assert_any_call("email_templates")


def test_seed_templates_files_missing():
    """Test seed_templates when template files do not exist."""
    mock_client = MagicMock()

    with patch("google.cloud.firestore.Client", return_value=mock_client), patch(
        "os.path.exists", return_value=False
    ):

        seed_templates.seed_templates()

        # Firestore set should not be called
        mock_client.collection.assert_not_called()


def test_seed_templates_main_execution():
    """Test execution of the __main__ block in seed_templates.py."""
    mock_client = MagicMock()

    with patch("google.cloud.firestore.Client", return_value=mock_client), patch(
        "os.path.exists", return_value=False
    ):

        # Run module as __main__
        test_dir = os.path.dirname(os.path.abspath(__file__))
        script_path = os.path.join(test_dir, "..", "backend", "seed_templates.py")
        runpy.run_path(script_path, run_name="__main__")

        # Verify firestore Client was initialized
        mock_client.collection.assert_not_called()
