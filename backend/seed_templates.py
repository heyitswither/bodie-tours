import os
from google.cloud import firestore


def seed_templates():
    print("Initializing Firestore Client...")
    db = firestore.Client(database="bodie-tours")

    base_dir = os.path.dirname(os.path.abspath(__file__))
    # Read payment reminder template
    reminder_path = os.path.join(base_dir, "templates", "payment_reminder.html")
    if os.path.exists(reminder_path):
        with open(reminder_path, "r", encoding="utf-8") as f:
            reminder_html = f.read()

        # Replace template placeholders with code-compatible tokens
        # The python function send_outlook_reminder expects:
        # {customer_name}, {booking_id}, {tour_datetime_str}, {payment_link}
        # We can map the double curly braces {{var}} to python format placeholders {var}
        reminder_formatted = (
            reminder_html.replace("{{customer_name}}", "{customer_name}")
            .replace("{{booking_id}}", "{booking_id}")
            .replace("{{tour_datetime_str}}", "{tour_datetime_str}")
            .replace("{{payment_link}}", "{payment_link}")
            .replace("{{total_amount}}", "{total_amount}")
            .replace("{{party_size}}", "{party_size}")
            .replace("{{cancellation_link}}", "{cancellation_link}")
        )

        print("Seeding email_templates/prune_reminder...")
        doc_ref = db.collection("email_templates").document("prune_reminder")
        doc_ref.set(
            {
                "subject": "Reminder: Your Bodie State Park Tour Booking Is Pending Payment",
                "body": reminder_formatted,
            }
        )
        print("Successfully seeded prune_reminder!")
    else:
        print(f"Error: {reminder_path} not found.")

    # Read booking receipt template
    receipt_path = os.path.join(base_dir, "templates", "booking_receipt.html")
    if os.path.exists(receipt_path):
        with open(receipt_path, "r", encoding="utf-8") as f:
            receipt_html = f.read()

        receipt_formatted = (
            receipt_html.replace("{{customer_name}}", "{customer_name}")
            .replace("{{booking_id}}", "{booking_id}")
            .replace("{{tour_datetime_str}}", "{tour_datetime_str}")
            .replace("{{party_size}}", "{party_size}")
            .replace("{{total_amount}}", "{total_amount}")
            .replace("{{invoice_link}}", "{invoice_link}")
            .replace("{{cancellation_link}}", "{cancellation_link}")
        )

        print("Seeding email_templates/booking_receipt...")
        doc_ref = db.collection("email_templates").document("booking_receipt")
        doc_ref.set(
            {
                "subject": "Receipt: Your Bodie State Park Tour Booking Is Confirmed",
                "body": receipt_formatted,
            }
        )
        print("Successfully seeded booking_receipt!")
    else:
        print(f"Error: {receipt_path} not found.")


if __name__ == "__main__":
    seed_templates()
