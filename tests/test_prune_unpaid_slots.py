import unittest
from datetime import datetime, timezone, timedelta

# Import the function from the module
from prune_unpaid_slots import calculate_ttl


class TestCalculateTTL(unittest.TestCase):
    def test_ttl_7_days_or_more(self):
        created = datetime(2023, 1, 1, tzinfo=timezone.utc)
        tour = created + timedelta(days=10)
        ttl = calculate_ttl(created, tour)
        self.assertEqual(ttl, timedelta(hours=48))

    def test_ttl_2_to_7_days(self):
        created = datetime(2023, 1, 1, tzinfo=timezone.utc)
        tour = created + timedelta(days=5)
        ttl = calculate_ttl(created, tour)
        self.assertEqual(ttl, timedelta(hours=24))

    def test_ttl_1_to_2_days(self):
        created = datetime(2023, 1, 1, tzinfo=timezone.utc)
        tour = created + timedelta(days=1, hours=12)
        ttl = calculate_ttl(created, tour)
        self.assertEqual(ttl, timedelta(hours=3))

    def test_ttl_less_than_1_day(self):
        created = datetime(2023, 1, 1, tzinfo=timezone.utc)
        tour = created + timedelta(hours=12)
        ttl = calculate_ttl(created, tour)
        self.assertEqual(ttl, timedelta(hours=1))


if __name__ == "__main__":
    unittest.main()
