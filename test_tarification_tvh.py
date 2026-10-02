import unittest
from decimal import Decimal
from tarification_tvh import coefficient_tvh, prix_vente_conseille


class TestTarificationTVH(unittest.TestCase):
    def test_paliers(self):
        for achat, coefficient in [
            ("0.01", "3.5"), ("50", "3.5"), ("50.01", "3"),
            ("100", "3"), ("100.01", "2.5"), ("150", "2.5"),
            ("150.01", "2"), ("350", "2"), ("350.01", "1.5"),
        ]:
            self.assertEqual(coefficient_tvh(achat), Decimal(coefficient))

    def test_prix(self):
        self.assertEqual(prix_vente_conseille("10"), Decimal("35.00"))
        self.assertEqual(prix_vente_conseille("200"), Decimal("400.00"))

    def test_negative(self):
        with self.assertRaises(ValueError):
            prix_vente_conseille("-1")


if __name__ == "__main__":
    unittest.main()
