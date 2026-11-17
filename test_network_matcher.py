import unittest

from app import build_network_matches


class NetworkMatchTests(unittest.TestCase):
    def test_build_network_matches_prefers_shared_batch_and_department(self):
        alumni = [
            {
                "id": 1,
                "name": "Aarav",
                "batch_year": 2020,
                "department": "Computer Science",
                "current_company": "Google",
                "city": "Bengaluru",
            },
            {
                "id": 2,
                "name": "Bhavna",
                "batch_year": 2020,
                "department": "Computer Science",
                "current_company": "Microsoft",
                "city": "Bengaluru",
            },
            {
                "id": 3,
                "name": "Chetan",
                "batch_year": 2019,
                "department": "Electronics",
                "current_company": "Google",
                "city": "Delhi",
            },
            {
                "id": 4,
                "name": "Diya",
                "batch_year": 2021,
                "department": "Computer Science",
                "current_company": "Amazon",
                "city": "Hyderabad",
            },
        ]

        matches = build_network_matches(alumni[0], alumni)

        self.assertEqual(matches[0]["name"], "Bhavna")
        self.assertGreaterEqual(matches[0]["score"], matches[1]["score"])
        self.assertTrue(matches[0]["reason"])


if __name__ == "__main__":
    unittest.main()
