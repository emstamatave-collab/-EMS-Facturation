"""Check homepage uses paid status and does not count existing payments twice."""
import os
import tempfile
import unittest
from unittest.mock import patch

_tmp = tempfile.TemporaryDirectory(prefix="ems_dashboard_")
os.environ["EMS_DATA_DIR"] = _tmp.name
os.environ.pop("DATABASE_URL", None)
import app_v15 as v15

class DashboardTest(unittest.TestCase):
    def test_paid_invoice_is_subtracted_once(self):
        class Row(dict):
            pass
        class Conn:
            def __init__(self, status, paid):
                self.status=status; self.paid=paid
            def execute(self, sql, *args):
                if "count(*) n from clients" in sql: return self.Result([Row(n=1)])
                if "count(*) n from docs" in sql: return self.Result([Row(n=1)])
                if "where d.kind='Facture'" in sql:
                    return self.Result([Row(id=1,currency="MGA",fx_rate=1,total=1000,paid=self.paid,status=self.status)])
                return self.Result([])
            def close(self): pass
            class Result:
                def __init__(self, rows): self.rows=rows
                def fetchone(self): return self.rows[0]
                def fetchall(self): return self.rows
        for paid in (0, 300, 1000):
            with self.subTest(paid=paid), patch.object(v15.legacy, "db", return_value=Conn("Payé",paid)), patch.object(v15.legacy, "page", side_effect=lambda html:html):
                page=v15.home_v15()
                self.assertIn("Reste à encaisser",page)
                self.assertIn('class="kpi">0 Ar',page)
        with patch.object(v15.legacy, "db", return_value=Conn("Partiellement payé",300)), patch.object(v15.legacy, "page", side_effect=lambda html:html):
            self.assertIn('class="kpi">700 Ar',v15.home_v15())

if __name__=="__main__":
    unittest.main()
