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
