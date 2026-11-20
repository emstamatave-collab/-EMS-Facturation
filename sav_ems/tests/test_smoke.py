import os
import unittest
from unittest.mock import patch

os.environ.setdefault("SECRET_KEY", "unit-test-only-secret-key")
os.environ.setdefault("DATABASE_URL", "postgresql://unused:unused@localhost/unused")
os.environ.setdefault("SETUP_KEY", "unit-test-only-setup-code")

import app as ems


class SavSmokeTests(unittest.TestCase):
    def setUp(self):
        ems.app.config["TESTING"] = True
        self.client=ems.app.test_client()

    def test_dashboard_requires_login(self):
        r=self.client.get("/",follow_redirects=False)
        self.assertEqual(r.status_code,302)
        self.assertIn("/login",r.location)

    def test_health_checks_live_database(self):
        with patch.object(ems,"execute",return_value={"ok":1}):
            self.assertEqual(self.client.get("/health").json["ok"], True)
        with patch.object(ems,"execute",side_effect=RuntimeError("db unavailable")):
            self.assertEqual(self.client.get("/health").status_code,503)

    def test_first_user_setup(self):
        with patch.object(ems,"count",return_value=0):
            r=self.client.get("/setup")
            self.assertEqual(r.status_code,200)
            self.assertIn(b"Activer EMS SAV",r.data)

    def test_login_form_when_user_exists(self):
        with patch.object(ems,"count",return_value=1):
            r=self.client.get("/login")
            self.assertEqual(r.status_code,200)
            self.assertIn(b"Connexion EMS SAV",r.data)

    def test_invalid_decimal_rejected(self):
        with ems.app.test_request_context(method="POST",data={"ref":"EMS-TEST-01","stock_type":"Vente","status":"Disponible","price_sale":"NaN"}):
            with self.assertRaises(ValueError):
                ems.validate_form("machines")

    def test_negative_prices_rejected(self):
        with ems.app.test_request_context(method="POST",data={"ref":"EMS-TEST-01","stock_type":"Vente","status":"Disponible","price_sale":"-1"}):
            with self.assertRaises(ValueError):
                ems.validate_form("machines")

    def test_machine_creation_requires_admin(self):
        with self.client.session_transaction() as sess:
            sess["user"]={"id":2,"name":"Test Tech","role":"technicien","email":"tech@example.com"}
            sess["_csrf"]="unitcsrf"
        r=self.client.get("/r/machines/new")
        self.assertEqual(r.status_code,403)

    def test_admin_can_open_machine_creation_form(self):
        with self.client.session_transaction() as sess:
            sess["user"]={"id":1,"name":"Test Admin","role":"admin","email":"admin@example.com"}
            sess["_csrf"]="unitcsrf"
        with patch.object(ems,"execute",return_value=[]):
            r=self.client.get("/r/machines/new")
            self.assertEqual(r.status_code,200)
            self.assertIn(b"de machine",r.data)

    def test_vgp_dashboard_render(self):
        with self.client.session_transaction() as sess:
            sess["user"]={"id":1,"name":"Test Admin","role":"admin","email":"admin@example.com"}
            sess["_csrf"]="unitcsrf"
        with patch.object(ems,"count",return_value=0),patch.object(ems,"execute",return_value=[]):
            r=self.client.get("/")
            self.assertEqual(r.status_code,200)
            self.assertIn(b"VGP",r.data)
            self.assertIn(b"EMS",r.data)

    def test_csrf_blocks_write(self):
        with self.client.session_transaction() as sess:
            sess["user"]={"id":1,"name":"Test Admin","role":"admin","email":"admin@example.com"}
            sess["_csrf"]="unitcsrf"
        with patch.object(ems,"execute",return_value=[]):
            self.assertEqual(self.client.post("/r/clients/new",data={"name":"Test Client"}).status_code,403)


if __name__=="__main__":
    unittest.main()
