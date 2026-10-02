import unittest
from decimal import Decimal
from tarification_tvh_devis import proposition_tvh

class TestDevisTVH(unittest.TestCase):
    def test_eur(self):
        self.assertEqual(proposition_tvh(40), Decimal("140.00"))
        self.assertEqual(proposition_tvh(75), Decimal("225.00"))
        self.assertEqual(proposition_tvh(120), Decimal("300.00"))
        self.assertEqual(proposition_tvh(250), Decimal("500.00"))
        self.assertEqual(proposition_tvh(400), Decimal("600.00"))
    def test_mga(self):
        self.assertEqual(proposition_tvh(40, "MGA", 5000), Decimal("700000"))
    def test_invalid_rate(self):
        with self.assertRaises(ValueError):
            proposition_tvh(40, "MGA", 0)
    def test_invalid_currency(self):
        with self.assertRaises(ValueError):
            proposition_tvh(40, "USD", 5000)

if __name__ == "__main__":
    unittest.main()
