"""Regression checks for the isolated TVH quote form integration."""
import pathlib
import unittest
from decimal import Decimal
from tarification_tvh_devis import proposition_tvh

SOURCE = pathlib.Path(__file__).with_name("app_v15.py").read_text(encoding="utf-8")

class TVHIntegrationContract(unittest.TestCase):
    def test_existing_persistent_fields_unchanged(self):
        self.assertIn("units = request.form.getlist('unit_price')", SOURCE)
        self.assertIn("purchases = request.form.getlist('purchase_price')", SOURCE)
        self.assertIn("insert into lines(doc_id,description,qty,unit_price,discount_pct,purchase_price", SOURCE)

    def test_tvh_input_only_triggers_automatic_suggestion(self):
        self.assertIn('name="tvh_purchase_eur"', SOURCE)
        self.assertIn("e.target.name==='tvh_purchase_eur')suggestTVH(row)", SOURCE)
        self.assertIn("sale.value=", SOURCE)
        self.assertIn("cost.value=", SOURCE)

    def test_eur_and_mga_boundaries(self):
        for price, expected in [(50,175),(50.01,Decimal("150.03")),(100,300),(150,375),(350,700),(400,600)]:
            self.assertEqual(proposition_tvh(price), Decimal(str(expected)).quantize(Decimal("0.01")))
        self.assertEqual(proposition_tvh(40,"MGA",5000),Decimal("700000"))

if __name__ == "__main__":
    unittest.main()
