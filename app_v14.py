"""
EMS Facturation V14
- Conserve toutes les fonctions de app_v13.py
- Utilise PostgreSQL/Supabase via DATABASE_URL pour les données
- Sauvegarde aussi les photos/pièces jointes dans PostgreSQL
- Restaure automatiquement les fichiers locaux au démarrage
"""
import os
import re
import sqlite3
import tempfile
import zipfile
import json
import shutil
from pathlib import Path
from datetime import date

import psycopg
from psycopg import errors as pg_errors
import app_v13 as legacy
from flask import jsonify, send_file, request, redirect, url_for, flash

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()

if not DATABASE_URL:
    # Sécurité : si DATABASE_URL n'est pas présent, on garde la V13 telle quelle.
    app = legacy.app
else:
    class CompatRow:
        def __init__(self, columns, values):
            self._columns = list(columns)
            self._values = tuple(values)
            self._map = dict(zip(self._columns, self._values))

        def __getitem__(self, key):
            if isinstance(key, int):
                return self._values[key]
            return self._map[key]

        def get(self, key, default=None):
            return self._map.get(key, default)

        def keys(self):
            return self._map.keys()

        def __iter__(self):
            return iter(self._values)

        def __repr__(self):
            return repr(self._map)


    def _translate_sql(sql):
        s = str(sql)
        # SQLite utilise ? ; psycopg utilise %s.
        s = s.replace("?", "%s")
        # Quelques requêtes V13 utilisent "" pour chaîne vide (SQLite).
        s = s.replace('""', "''")   
        s = s.replace("group by d.id order by d.id desc limit 6", "group by d.id, c.name order by d.id desc limit 6")
        s = s.replace("group by d.id order by d.id desc", "group by d.id, c.name order by d.id desc")
        return s


    class CompatCursor:
        def __init__(self, conn, cursor, sql=""):
            self.conn = conn
            self.cursor = cursor
            self.sql = sql
            self.lastrowid = None
            self._prefetched = None

        @property
        def rowcount(self):
            return self.cursor.rowcount

        def _columns(self):
            if not self.cursor.description:
                return []
            return [d.name if hasattr(d, "name") else d[0] for d in self.cursor.description]

        def _convert(self, row):
            if row is None:
                return None
            if isinstance(row, CompatRow):
                return row
            return CompatRow(self._columns(), row)

        def fetchone(self):
            if self._prefetched is not None:
                row = self._prefetched
                self._prefetched = None
                return row
            return self._convert(self.cursor.fetchone())

        def fetchall(self):
            rows = []
            if self._prefetched is not None:
                rows.append(self._prefetched)
                self._prefetched = None
            rows.extend(self._convert(r) for r in self.cursor.fetchall())
            return rows

        def __iter__(self):
            while True:
                row = self.fetchone()
                if row is None:
                    break
                yield row

        def close(self):
            self.cursor.close()


    class CompatConnection:
        def __init__(self):
            self.raw = psycopg.connect(DATABASE_URL)

        def execute(self, sql, params=()):
            translated = _translate_sql(sql)
            stripped = translated.lstrip().lower()
            is_insert = stripped.startswith("insert into")
            has_returning = " returning " in stripped

            # La V13 attend cur.lastrowid après certaines insertions.
            if is_insert and not has_returning:
                translated = translated.rstrip().rstrip(";") + " RETURNING id"

            cur = self.raw.cursor()
            try:
                cur.execute(translated, tuple(params or ()))
            except pg_errors.IntegrityError as e:
                self.raw.rollback()
                cur.close()
                # La V13 intercepte sqlite3.IntegrityError.
                raise sqlite3.IntegrityError(str(e)) from e

            wrapper = CompatCursor(self, cur, translated)
            if is_insert:
                try:
                    row = cur.fetchone()
                    if row:
                        wrapper.lastrowid = row[0]
                except Exception:
                    pass
            return wrapper

        def commit(self):
            self.raw.commit()

        def rollback(self):
            self.raw.rollback()

        def close(self):
            self.raw.close()


    def pg_db():
        return CompatConnection()


    def init_pg():
        statements = [
            """CREATE TABLE IF NOT EXISTS clients(
                id BIGSERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                address TEXT, nif TEXT, stat TEXT, email TEXT, phone TEXT
            )""",
            """CREATE TABLE IF NOT EXISTS docs(
                id BIGSERIAL PRIMARY KEY,
                kind TEXT NOT NULL,
                number TEXT NOT NULL,
                doc_date TEXT, due_date TEXT,
                client_id BIGINT REFERENCES clients(id),
                reference TEXT, po_number TEXT, payment_terms TEXT, delivery TEXT,
                status TEXT DEFAULT 'Brouillon',
                notes TEXT DEFAULT '',
                internal_note TEXT DEFAULT '',
                created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
            )""",
            """CREATE TABLE IF NOT EXISTS lines(
                id BIGSERIAL PRIMARY KEY,
                doc_id BIGINT REFERENCES docs(id),
                description TEXT,
                qty DOUBLE PRECISION DEFAULT 1,
                unit_price DOUBLE PRECISION DEFAULT 0,
                discount_pct DOUBLE PRECISION DEFAULT 0
            )""",
            """CREATE TABLE IF NOT EXISTS payments(
                id BIGSERIAL PRIMARY KEY,
                doc_id BIGINT REFERENCES docs(id),
                payment_date TEXT,
                amount DOUBLE PRECISION DEFAULT 0,
                method TEXT, note TEXT
            )""",
            """CREATE TABLE IF NOT EXISTS client_attachments(
                id BIGSERIAL PRIMARY KEY,
                client_id BIGINT REFERENCES clients(id),
                original_name TEXT, stored_name TEXT, mime TEXT,
                created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
            )""",
            """CREATE TABLE IF NOT EXISTS client_machine_photos(
                id BIGSERIAL PRIMARY KEY,
                client_id BIGINT REFERENCES clients(id),
                original_name TEXT, stored_name TEXT, mime TEXT,
                created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
            )""",
            """CREATE TABLE IF NOT EXISTS doc_images(
                id BIGSERIAL PRIMARY KEY,
                doc_id BIGINT REFERENCES docs(id),
                original_name TEXT, stored_name TEXT, mime TEXT,
                created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
            )""",
            """CREATE TABLE IF NOT EXISTS stock_items(
                id BIGSERIAL PRIMARY KEY,
                reference TEXT UNIQUE,
                designation TEXT NOT NULL,
                qty DOUBLE PRECISION DEFAULT 0,
                purchase_price DOUBLE PRECISION DEFAULT 0,
                sale_price DOUBLE PRECISION DEFAULT 0,
                min_qty DOUBLE PRECISION DEFAULT 0,
                original_name TEXT, stored_name TEXT, mime TEXT,
                notes TEXT DEFAULT '',
                created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
            )""",
            """CREATE TABLE IF NOT EXISTS stock_moves(
                id BIGSERIAL PRIMARY KEY,
                item_id BIGINT NOT NULL REFERENCES stock_items(id),
                move_date TEXT,
                move_type TEXT NOT NULL,
                qty DOUBLE PRECISION NOT NULL,
                note TEXT,
                created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
            )""",
            """CREATE TABLE IF NOT EXISTS ems_files(
                stored_name TEXT PRIMARY KEY,
                original_name TEXT,
                mime TEXT,
                data BYTEA NOT NULL,
                created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
            )""",
        ]
        with psycopg.connect(DATABASE_URL) as con:
            for sql in statements:
                con.execute(sql)


    def restore_files_from_pg():
        legacy.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        with psycopg.connect(DATABASE_URL) as con:
            rows = con.execute("SELECT stored_name, data FROM ems_files").fetchall()
        for stored_name, data in rows:
            safe_name = Path(stored_name).name
            if safe_name:
                (legacy.UPLOAD_DIR / safe_name).write_bytes(bytes(data))


    _legacy_save_upload = legacy.save_upload

    def persistent_save_upload(file, prefix):
        result = _legacy_save_upload(file, prefix)
        if not result:
            return result
        original, stored, mime = result
        path = legacy.UPLOAD_DIR / stored
        if path.exists():
            raw = path.read_bytes()
            with psycopg.connect(DATABASE_URL) as con:
                con.execute(
                    """INSERT INTO ems_files(stored_name, original_name, mime, data)
                       VALUES(%s,%s,%s,%s)
                       ON CONFLICT(stored_name) DO UPDATE SET
                         original_name=EXCLUDED.original_name,
                         mime=EXCLUDED.mime,
                         data=EXCLUDED.data""",
                    (stored, original, mime, raw),
                )
        return result


    def _all_tables():
        return [
            "clients", "docs", "lines", "payments", "client_attachments",
            "client_machine_photos", "doc_images", "stock_items", "stock_moves"
        ]


    def backup_v14():
        work = Path(tempfile.mkdtemp(prefix="ems_v14_backup_"))
        try:
            payload = {}
            with psycopg.connect(DATABASE_URL) as con:
                for table in _all_tables():
                    cur = con.execute(f"SELECT * FROM {table} ORDER BY id")
                    cols = [d.name for d in cur.description]
                    payload[table] = [dict(zip(cols, row)) for row in cur.fetchall()]

                files = con.execute(
                    "SELECT stored_name, original_name, mime, data FROM ems_files"
                ).fetchall()

            # Convertit dates/timestamps en texte JSON.
            def default_json(obj):
                if hasattr(obj, "isoformat"):
                    return obj.isoformat()
                raise TypeError

            data_file = work / "ems_v14.json"
            data_file.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, default=default_json),
                encoding="utf-8"
            )

            zip_path = work / f"EMS_V14_sauvegarde_{date.today().isoformat()}.zip"
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
                z.write(data_file, "ems_v14.json")
                for stored_name, original_name, mime, data in files:
                    safe = Path(stored_name).name
                    if safe:
                        z.writestr(f"uploads/{safe}", bytes(data))
                z.writestr(
                    "SAUVEGARDE_EMS.txt",
                    f"EMS Facturation V14\nDate: {date.today().isoformat()}\n"
                    f"Base: PostgreSQL/Supabase\nFichiers: {len(files)}\n"
                )
            return send_file(
                zip_path,
                as_attachment=True,
                download_name=zip_path.name,
                max_age=0
            )
        except Exception as e:
            shutil.rmtree(work, ignore_errors=True)
            return f"Sauvegarde impossible : {e}", 500


    def restore_v14():
        if request.method == "GET":
            return legacy.page(
                """<div class="card"><h2>Restaurer une sauvegarde V14</h2>
                <p>Choisis un fichier <b>EMS_V14_sauvegarde_....zip</b>.</p>
                <form method="post" enctype="multipart/form-data">
                <p><input type="file" name="backup_file" accept=".zip,application/zip" required></p>
                <p><button type="submit">Restaurer la sauvegarde</button></p>
                </form></div>"""
            )

        f = request.files.get("backup_file")
        if not f or not f.filename:
            flash("Choisis une sauvegarde EMS V14.")
            return redirect(url_for("restore_backup"))

        work = Path(tempfile.mkdtemp(prefix="ems_v14_restore_"))
        try:
            archive = work / "backup.zip"
            f.save(archive)
            extract = work / "extract"
            extract.mkdir()

            with zipfile.ZipFile(archive, "r") as z:
                for member in z.infolist():
                    target = (extract / member.filename).resolve()
                    if extract.resolve() not in target.parents and target != extract.resolve():
                        raise ValueError("Archive invalide")
                z.extractall(extract)

            data_file = extract / "ems_v14.json"
            if not data_file.exists():
                raise ValueError("Cette sauvegarde n'est pas au format V14")

            payload = json.loads(data_file.read_text(encoding="utf-8"))

            # Ordre de suppression inverse des dépendances.
            delete_order = [
                "stock_moves", "doc_images", "client_machine_photos",
                "client_attachments", "payments", "lines", "docs",
                "stock_items", "clients"
            ]
            insert_order = [
                "clients", "docs", "lines", "payments", "client_attachments",
                "client_machine_photos", "doc_images", "stock_items", "stock_moves"
            ]

            with psycopg.connect(DATABASE_URL) as con:
                for table in delete_order:
                    con.execute(f"DELETE FROM {table}")
                con.execute("DELETE FROM ems_files")

                for table in insert_order:
                    for row in payload.get(table, []):
                        if not row:
                            continue
                        cols = list(row.keys())
                        vals = [row[c] for c in cols]
                        placeholders = ",".join(["%s"] * len(cols))
                        con.execute(
                            f"INSERT INTO {table} ({','.join(cols)}) VALUES ({placeholders})",
                            vals
                        )

                uploads = extract / "uploads"
                if uploads.exists():
                    for p in uploads.iterdir():
                        if p.is_file():
                            con.execute(
                                """INSERT INTO ems_files(stored_name, original_name, mime, data)
                                   VALUES(%s,%s,%s,%s)
                                   ON CONFLICT(stored_name) DO UPDATE SET data=EXCLUDED.data""",
                                (p.name, p.name, "application/octet-stream", p.read_bytes())
                            )

                # Recale les séquences SERIAL après restauration d'IDs.
                for table in insert_order:
                    con.execute(
                        f"""SELECT setval(
                            pg_get_serial_sequence('{table}','id'),
                            COALESCE((SELECT MAX(id) FROM {table}), 1),
                            true
                        )"""
                    )

            restore_files_from_pg()
            flash("Sauvegarde V14 restaurée avec succès.")
            return redirect(url_for("home"))
        except Exception as e:
            flash(f"Restauration impossible : {e}")
            return redirect(url_for("restore_backup"))
        finally:
            shutil.rmtree(work, ignore_errors=True)


    def health_v14():
        try:
            with psycopg.connect(DATABASE_URL) as con:
                con.execute("SELECT 1").fetchone()
                counts = {}
                for table in ("clients", "docs", "stock_items", "ems_files"):
                    counts[table] = con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            return jsonify({
                "ok": True,
                "app": "EMS Facturation V14",
                "database": "PostgreSQL/Supabase",
                "files": "PostgreSQL/Supabase",
                "counts": counts,
            })
        except Exception as e:
            return jsonify({
                "ok": False,
                "app": "EMS Facturation V14",
                "database": "PostgreSQL/Supabase",
                "error": str(e)[:250],
            }), 500


    # Initialisation PostgreSQL avant d'activer le wrapper.
    init_pg()
    restore_files_from_pg()

    # Les fonctions de la V13 continueront à fonctionner, mais sur PostgreSQL.
    legacy.db = pg_db
    legacy.save_upload = persistent_save_upload

    # L'ancien système de sauvegarde Supabase Storage n'est plus nécessaire :
    # les données et fichiers sont déjà persistants dans PostgreSQL.
    legacy.backup_to_supabase = lambda: None
    legacy.restore_from_supabase = lambda: None

    # Remplace les écrans Sauvegarde / Restaurer par les versions V14.
    if "backup_db" in legacy.app.view_functions:
        legacy.app.view_functions["backup_db"] = backup_v14
    if "restore_backup" in legacy.app.view_functions:
        legacy.app.view_functions["restore_backup"] = restore_v14
    if "health" in legacy.app.view_functions:
        legacy.app.view_functions["health"] = health_v14

    app = legacy.app


# ===== INTEGRATION SITE EMS -> DEVIS BROUILLON =====
def _site_api_authorized():
    expected = os.environ.get("EMS_SITE_API_KEY", "").strip()
    provided = request.headers.get("X-EMS-Site-Key", "").strip()
    auth = request.headers.get("Authorization", "").strip()
    if not provided and auth.lower().startswith("bearer "):
        provided = auth[7:].strip()
    return bool(expected) and bool(provided) and legacy.hmac.compare_digest(provided, expected)


def _clean_site_text(value, max_len=500):
    return str(value or "").strip()[:max_len]


@app.post("/api/site/part-request")
def site_request_api():
    """Crée un devis brouillon EMS depuis une demande de pièce du site."""
    if not _site_api_authorized():
        return jsonify({"ok": False, "error": "Non autorisé"}), 401

    payload = request.get_json(silent=True) or {}
    client = payload.get("client") or {}
    items = payload.get("items") or []

    if not items and any(payload.get(k) for k in ("reference", "designation", "qty", "quantity")):
        items = [{
            "reference": payload.get("reference"),
            "designation": payload.get("designation"),
            "qty": payload.get("qty", payload.get("quantity", 1)),
        }]

    if not isinstance(items, list) or not items:
        return jsonify({"ok": False, "error": "Aucune pièce dans la demande."}), 400

    normalized_items = []
    for item in items[:100]:
        if not isinstance(item, dict):
            continue
        ref = _clean_site_text(item.get("reference"), 120)
        designation = _clean_site_text(item.get("designation"), 1200)
        qty = max(0.01, legacy.parse_decimal(item.get("qty", item.get("quantity", 1)), 1))
        supplier_name = _clean_site_text(item.get("supplier_name"), 120)
        supplier_ref = _clean_site_text(item.get("supplier_ref"), 250)
        if supplier_ref and not supplier_name:
            supplier_name = "TVH"
        if not ref and not designation:
            continue
        description = ""
        if ref:
            description += f"Réf. MMS : {ref}"
        if designation:
            description += ("\n" if description else "") + designation
        normalized_items.append((ref, designation, description, qty, supplier_name, supplier_ref))

    if not normalized_items:
        return jsonify({"ok": False, "error": "Aucune pièce exploitable dans la demande."}), 400

    name = _clean_site_text(client.get("name") or client.get("company") or payload.get("company"), 250)
    email = _clean_site_text(client.get("email") or payload.get("email"), 250)
    phone = _clean_site_text(client.get("phone") or payload.get("phone"), 120)
    address = _clean_site_text(client.get("address") or payload.get("address"), 1000)
    nif = _clean_site_text(client.get("nif") or payload.get("nif"), 120)
    stat = _clean_site_text(client.get("stat") or payload.get("stat"), 120)

    if not name:
        name = email or phone or "Demande site EMS"

    request_id = re.sub(r"[^A-Za-z0-9._:-]", "", _clean_site_text(payload.get("request_id"), 120))
    source = _clean_site_text(payload.get("source") or "emstamatave.mg", 250)
    customer_message = _clean_site_text(payload.get("message"), 3000)
    delivery_mode = _clean_site_text(payload.get("delivery_mode"), 500)

    con = legacy.db()
    try:
        if request_id:
            marker = f"[EMS_SITE_REQUEST:{request_id}]"
            existing = con.execute(
                "select id, number from docs where internal_note like ? order by id desc limit 1",
                (f"%{marker}%",)
            ).fetchone()
            if existing:
                did = existing["id"]
                base = request.host_url.rstrip("/")
                return jsonify({
                    "ok": True,
                    "duplicate": True,
                    "document_id": did,
                    "number": existing["number"],
                    "document_url": f"{base}/document/{did}",
                    "edit_url": f"{base}/document/{did}/edit",
                })

        existing_client = None
        if email:
            existing_client = con.execute(
                "select * from clients where lower(coalesce(email,''))=lower(?) order by id limit 1",
                (email,)
            ).fetchone()
        if not existing_client and phone:
            existing_client = con.execute(
                "select * from clients where coalesce(phone,'')=? order by id limit 1",
                (phone,)
            ).fetchone()
        if not existing_client and name:
            existing_client = con.execute(
                "select * from clients where lower(name)=lower(?) order by id limit 1",
                (name,)
            ).fetchone()

        if existing_client:
            cid = existing_client["id"]
            con.execute(
                """update clients set
                   address=case when coalesce(address,'')='' then ? else address end,
                   nif=case when coalesce(nif,'')='' then ? else nif end,
                   stat=case when coalesce(stat,'')='' then ? else stat end,
                   email=case when coalesce(email,'')='' then ? else email end,
                   phone=case when coalesce(phone,'')='' then ? else phone end
                   where id=?""",
                (address, nif, stat, email, phone, cid)
            )
        else:
            cur = con.execute(
                "insert into clients(name,address,nif,stat,email,phone) values(?,?,?,?,?,?)",
                (name, address, nif, stat, email, phone)
            )
            cid = cur.lastrowid

        number = legacy.next_number("Devis")
        today = date.today()
        first_ref = normalized_items[0][0]
        reference = _clean_site_text(
            payload.get("document_reference")
            or (f"Demande site EMS — {first_ref}" if first_ref else "Demande de pièces — site EMS"),
            500
        )
        marker = f"[EMS_SITE_REQUEST:{request_id}]" if request_id else "[EMS_SITE_REQUEST]"
        internal_note = (
            f"{marker}\n"
            f"Créé automatiquement depuis {source}.\n"
            f"Demande client reçue via le site EMS."
        )
        if customer_message:
            internal_note += f"\nMessage client : {customer_message}"

        cur = con.execute(
            """insert into docs(
               kind,number,doc_date,due_date,client_id,reference,po_number,
               payment_terms,delivery,status,notes,internal_note
               ) values(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "Devis", number, today.isoformat(), (today + legacy.timedelta(days=30)).isoformat(),
                cid, reference, "", "À définir", delivery_mode, "Brouillon", "", internal_note
            )
        )
        did = cur.lastrowid

        for _ref, _designation, description, qty, supplier_name, supplier_ref in normalized_items:
            con.execute(
                """insert into lines(
                   doc_id,description,qty,unit_price,discount_pct,mms_ref,supplier_name,supplier_ref
                   ) values(?,?,?,?,?,?,?,?)""",
                (did, description, qty, 0, 0, _ref, supplier_name, supplier_ref)
            )

        con.commit()
        print(f"EMS_NOTIFY_EMAIL document_id={did} number={number}", flush=True)
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()

    base = request.host_url.rstrip("/")
    return jsonify({
        "ok": True,
        "duplicate": False,
        "document_id": did,
        "number": number,
        "client_id": cid,
        "document_url": f"{base}/document/{did}",
        "edit_url": f"{base}/document/{did}/edit",
        "status": "Brouillon",
    }), 201
# ===== FIN INTEGRATION SITE EMS -> DEVIS BROUILLON =====


# ===== FORMULAIRE PUBLIC EMS SANS WORDPRESS =====
_SITE_FORM_TEMPLATE = r"""
<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Demande de pièce EMS</title>
<style>
:root{--green:#07552d;--green2:#0b6f3d;--ink:#18221c;--muted:#667085;--line:#d9e2dc;--bg:#f5f8f6}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif;color:var(--ink)}
.wrap{max-width:860px;margin:0 auto;padding:18px}
.card{background:#fff;border:1px solid var(--line);border-radius:18px;padding:22px;box-shadow:0 12px 35px rgba(0,0,0,.06)}
.brand{font-weight:800;color:var(--green);font-size:14px;letter-spacing:.06em;text-transform:uppercase}
h1{font-size:30px;line-height:1.15;margin:8px 0 8px}
.intro{margin:0 0 22px;color:var(--muted);line-height:1.5}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}
.field{display:flex;flex-direction:column;gap:7px}
.field.full{grid-column:1/-1}
label{font-weight:700;font-size:14px}
input,textarea,select{width:100%;border:1px solid #cfd8d3;border-radius:11px;padding:13px 14px;font:inherit;background:#fff}
textarea{min-height:120px;resize:vertical}
button{width:100%;margin-top:18px;border:0;border-radius:12px;background:var(--green);color:#fff;padding:14px 18px;font-weight:800;font-size:16px}
.notice{border-radius:12px;padding:13px 14px;margin:0 0 18px;font-weight:650;line-height:1.4}
.notice.ok{background:#eaf7ef;color:#07552d;border:1px solid #bfe2cb}
.notice.err{background:#fff0f0;color:#8d1f1f;border:1px solid #efc7c7}
.back{display:inline-block;margin-top:18px;color:var(--green);font-weight:700;text-decoration:none}
.hp{position:absolute;left:-9999px;width:1px;height:1px;overflow:hidden}
@media(max-width:650px){.wrap{padding:10px}.card{padding:18px;border-radius:14px}.grid{grid-template-columns:1fr}.field.full{grid-column:auto}h1{font-size:26px}}
</style>
</head>
<body>
<div class="wrap">
  <div class="card">
    <div class="brand">Europe Manutention Service — Tamatave</div>
    <h1>Demande de pièce</h1>
    <p class="intro">Envoyez votre référence ou la désignation de la pièce. Votre dossier est préparé automatiquement pour accélérer le chiffrage.</p>
    {% if success %}<div class="notice ok">{{ success }}</div>{% endif %}
    {% if error %}<div class="notice err">{{ error }}</div>{% endif %}
    <form method="post" action="/demande-piece">
      <input type="hidden" name="token" value="{{ token }}">
      <div class="hp" aria-hidden="true"><label>Site web<input name="website" tabindex="-1" autocomplete="off"></label></div>
      <div class="grid">
        <div class="field"><label>Société</label><input name="company" value="{{ values.company }}" autocomplete="organization"></div>
        <div class="field"><label>Nom / prénom</label><input name="name" value="{{ values.name }}" autocomplete="name"></div>
        <div class="field"><label>E-mail *</label><input name="email" type="email" value="{{ values.email }}" autocomplete="email" required></div>
        <div class="field"><label>Téléphone / WhatsApp</label><input name="phone" type="tel" value="{{ values.phone }}" autocomplete="tel"></div>
        <div class="field"><label>Pays</label><input name="country" value="{{ values.country }}" autocomplete="country-name"></div>
        <div class="field"><label>Quantité *</label><input name="qty" type="number" min="0.01" step="0.01" value="{{ values.qty }}" required></div>
        <div class="field"><label>Référence MMS</label><input name="reference" value="{{ values.reference }}"></div>
        <div class="field"><label>Désignation</label><input name="designation" value="{{ values.designation }}"></div>
        <div class="field full">
          <label>Livraison / retrait</label>
          <select name="delivery_mode">
            <option value="Expédition Madagascar" {% if values.delivery_mode == 'Expédition Madagascar' %}selected{% endif %}>Expédition à Madagascar</option>
            <option value="Retrait France par transitaire" {% if values.delivery_mode == 'Retrait France par transitaire' %}selected{% endif %}>Retrait en France par mon transitaire</option>
            <option value="À définir" {% if values.delivery_mode == 'À définir' %}selected{% endif %}>À définir</option>
          </select>
        </div>
        <div class="field full"><label>Message</label><textarea name="message" placeholder="Précisions sur la pièce, le matériel, le numéro de série…">{{ values.message }}</textarea></div>
      </div>
      <button type="submit">Envoyer ma demande de pièce</button>
    </form>
    <a class="back" href="https://emstamatave.mg/">← Retour au site EMS</a>
  </div>
</div>
</body>
</html>
"""


def _public_form_values(source=None):
    source = source or {}
    return {
        "company": _clean_site_text(source.get("company"), 250),
        "name": _clean_site_text(source.get("name"), 250),
        "email": _clean_site_text(source.get("email"), 250),
        "phone": _clean_site_text(source.get("phone"), 120),
        "country": _clean_site_text(source.get("country") or "Madagascar", 120),
        "qty": _clean_site_text(source.get("qty") or "1", 30),
        "reference": _clean_site_text(source.get("reference") or source.get("ref"), 120),
        "designation": _clean_site_text(source.get("designation"), 1200),
        "delivery_mode": _clean_site_text(source.get("delivery_mode") or "Expédition Madagascar", 500),
        "message": _clean_site_text(source.get("message"), 3000),
    }


def _create_quote_from_public_form(values):
    email = values["email"]
    reference = values["reference"]
    designation = values["designation"]
    if not email or "@" not in email or (not reference and not designation):
        raise ValueError("Merci de renseigner un e-mail valide et la référence ou la désignation de la pièce.")

    qty = max(0.01, legacy.parse_decimal(values["qty"], 1))
    company = values["company"]
    person = values["name"]
    client_name = company or person or email
    if company and person:
        client_name = f"{company} — {person}"

    con = legacy.db()
    try:
        existing_client = con.execute(
            "select * from clients where lower(coalesce(email,''))=lower(?) order by id limit 1",
            (email,)
        ).fetchone()
        if not existing_client and values["phone"]:
            existing_client = con.execute(
                "select * from clients where coalesce(phone,'')=? order by id limit 1",
                (values["phone"],)
            ).fetchone()
        if not existing_client and client_name:
            existing_client = con.execute(
                "select * from clients where lower(name)=lower(?) order by id limit 1",
                (client_name,)
            ).fetchone()

        if existing_client:
            cid = existing_client["id"]
            con.execute(
                """update clients set
                   address=case when coalesce(address,'')='' then ? else address end,
                   email=case when coalesce(email,'')='' then ? else email end,
                   phone=case when coalesce(phone,'')='' then ? else phone end
                   where id=?""",
                (values["country"], email, values["phone"], cid)
            )
        else:
            cur = con.execute(
                "insert into clients(name,address,nif,stat,email,phone) values(?,?,?,?,?,?)",
                (client_name, values["country"], "", "", email, values["phone"])
            )
            cid = cur.lastrowid

        number = legacy.next_number("Devis")
        today = date.today()
        request_id = f"WEBFORM-{today.strftime('%Y%m%d')}-{legacy.uuid.uuid4().hex[:8]}"
        doc_reference = f"Demande site EMS — {reference}" if reference else "Demande de pièces — site EMS"
        internal_note = (
            f"[EMS_SITE_REQUEST:{request_id}]\n"
            f"Créé automatiquement depuis le formulaire public EMS.\n"
            f"Demande client reçue via le formulaire EMS."
        )
        if values["message"]:
            internal_note += f"\nMessage client : {values['message']}"

        cur = con.execute(
            """insert into docs(
               kind,number,doc_date,due_date,client_id,reference,po_number,
               payment_terms,delivery,status,notes,internal_note
               ) values(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "Devis", number, today.isoformat(), (today + legacy.timedelta(days=30)).isoformat(),
                cid, doc_reference, "", "À définir", values["delivery_mode"], "Brouillon", "", internal_note
            )
        )
        did = cur.lastrowid

        description = ""
        if reference:
            description += f"Réf. MMS : {reference}"
        if designation:
            description += ("\n" if description else "") + designation
        con.execute(
            "insert into lines(doc_id,description,qty,unit_price,discount_pct) values(?,?,?,?,?)",
            (did, description, qty, 0, 0)
        )
        con.commit()
        print(f"EMS_NOTIFY_EMAIL document_id={did} number={number}", flush=True)
        return did, number
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


@app.route("/demande-piece", methods=["GET", "POST"])
def site_part_form():
    if request.method == "GET":
        values = _public_form_values(request.args)
        token = legacy.uuid.uuid4().hex
        legacy.session["site_part_form_token"] = token
        return legacy.render_template_string(
            _SITE_FORM_TEMPLATE, values=values, token=token, success="", error=""
        )

    values = _public_form_values(request.form)

    if _clean_site_text(request.form.get("website"), 250):
        token = legacy.uuid.uuid4().hex
        legacy.session["site_part_form_token"] = token
        return legacy.render_template_string(
            _SITE_FORM_TEMPLATE,
            values=_public_form_values({}),
            token=token,
            success="Votre demande a bien été reçue.",
            error=""
        )

    expected = str(legacy.session.get("site_part_form_token") or "")
    provided = _clean_site_text(request.form.get("token"), 200)
    if not expected or not provided or not legacy.hmac.compare_digest(expected, provided):
        token = legacy.uuid.uuid4().hex
        legacy.session["site_part_form_token"] = token
        return legacy.render_template_string(
            _SITE_FORM_TEMPLATE,
            values=values,
            token=token,
            success="",
            error="La session du formulaire a expiré. Merci de réessayer."
        ), 403

    try:
        did, number = _create_quote_from_public_form(values)
    except ValueError as e:
        token = legacy.uuid.uuid4().hex
        legacy.session["site_part_form_token"] = token
        return legacy.render_template_string(
            _SITE_FORM_TEMPLATE, values=values, token=token, success="", error=str(e)
        ), 400
    except Exception:
        token = legacy.uuid.uuid4().hex
        legacy.session["site_part_form_token"] = token
        return legacy.render_template_string(
            _SITE_FORM_TEMPLATE,
            values=values,
            token=token,
            success="",
            error="La demande n’a pas pu être enregistrée pour le moment. Merci de réessayer."
        ), 500

    legacy.session.pop("site_part_form_token", None)
    token = legacy.uuid.uuid4().hex
    legacy.session["site_part_form_token"] = token
    return legacy.render_template_string(
        _SITE_FORM_TEMPLATE,
        values=_public_form_values({}),
        token=token,
        success=f"Votre demande est bien enregistrée. Dossier {number}. EMS revient vers vous rapidement.",
        error=""
    )
# ===== FIN FORMULAIRE PUBLIC EMS SANS WORDPRESS =====


# ===== FLUX NOTIFICATIONS EMAIL EMS =====
@app.get("/api/site/recent-notifications")
def site_recent_notifications():
    """Expose seulement les numéros/liens des dernières demandes site, sans données client."""
    con = legacy.db()
    try:
        rows = con.execute(
            """select id, number, reference, doc_date
               from docs
               where kind='Devis'
                 and internal_note like '%[EMS_SITE_REQUEST:%'
               order by id desc
               limit 20"""
        ).fetchall()
    finally:
        con.close()

    base = request.host_url.rstrip("/")
    items = []
    for row in rows:
        items.append({
            "document_id": row["id"],
            "number": row["number"],
            "reference": row["reference"] or "",
            "date": row["doc_date"] or "",
            "document_url": f"{base}/document/{row['id']}",
        })
    resp = jsonify({"ok": True, "items": items})
    resp.headers["Cache-Control"] = "no-store, max-age=0"
    resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    return resp
# ===== FIN FLUX NOTIFICATIONS EMAIL EMS =====


