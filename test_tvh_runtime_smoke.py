"""End-to-end isolated SQLite smoke test; no production database access."""
import os
import tempfile
import unittest
from pathlib import Path

_tmp = tempfile.TemporaryDirectory(prefix="ems_tvh_smoke_")
os.environ["EMS_DATA_DIR"] = _tmp.name
os.environ.pop("DATABASE_URL", None)
os.environ.pop("SUPABASE_SECRET_KEY", None)
os.environ["EMS_HTTPS"] = "0"

import app_v15 as v15
from app_v15 import legacy


class Smoke(unittest.TestCase):
    def test_tvh_round_trip_and_conversion(self):
        con = legacy.db()
        cols = [r[1] for r in con.execute("PRAGMA table_info(lines)").fetchall()]
        self.assertIn("tvh_purchase_eur", cols)
        con.execute("INSERT INTO clients(name) VALUES(?)", ("Client test",))
        client_id = con.execute("SELECT id FROM clients WHERE name=?", ("Client test",)).fetchone()[0]
        doc_cols = [r[1] for r in con.execute("PRAGMA table_info(docs)").fetchall()]
        vals = {"kind":"Devis", "number":"TEST-TVH-001", "doc_date":"2026-10-02", "client_id":client_id, "status":"Brouillon"}
        cols = [c for c in vals if c in doc_cols]
        cur = con.execute("INSERT INTO docs("+",".join(cols)+") VALUES("+",".join("?" for _ in cols)+")", tuple(vals[c] for c in cols))
        doc_id = cur.lastrowid
        con.commit()
        with v15.app.test_request_context("/", method="POST", data={
            "description":"Test TVH", "qty":"2", "unit_price":"175", "discount_pct":"0",
            "purchase_price":"50", "tvh_purchase_eur":"50", "mms_ref":"MMS-TEST"
        }):
            v15._save_lines(con, doc_id)
        con.commit()
        row = con.execute("SELECT * FROM lines WHERE doc_id=?", (doc_id,)).fetchone()
        self.assertEqual(row["tvh_purchase_eur"], 50)
        self.assertEqual(row["purchase_price"], 50)
        self.assertIn('value="50"', v15._line_row(row))
        con.close()
        with v15.app.test_request_context("/"):
            response = v15.convert_v15(doc_id)
            self.assertEqual(response.status_code, 302)
        con = legacy.db()
        invoice = con.execute("SELECT id FROM docs WHERE kind='Facture' ORDER BY id DESC LIMIT 1").fetchone()
        self.assertIsNotNone(invoice)
        copied = con.execute("SELECT * FROM lines WHERE doc_id=?", (invoice["id"],)).fetchone()
        self.assertEqual(copied["tvh_purchase_eur"], 50)
        self.assertEqual(copied["purchase_price"], 50)
        con.close()


if __name__ == "__main__":
    unittest.main()
