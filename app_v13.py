import sqlite3, os, re, hmac, uuid, mimetypes, json, base64, urllib.request, urllib.error, zipfile, tempfile, shutil
from pathlib import Path
from datetime import date, timedelta
from flask import Flask, render_template_string, request, redirect, url_for, send_file, send_from_directory, flash, Response, jsonify, session
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from werkzeug.utils import secure_filename

BASE=Path(__file__).parent
DATA_DIR=Path(os.environ.get('EMS_DATA_DIR', str(BASE/'data')))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB=DATA_DIR/'ems.db'
LOGO=BASE/'logo_ems.png'
UPLOAD_DIR=DATA_DIR/'uploads'
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
app=Flask(__name__)
app.secret_key=os.environ.get('EMS_SECRET_KEY','change-this-secret-before-production')
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax', SESSION_COOKIE_SECURE=os.environ.get('EMS_HTTPS','0')=='1', MAX_CONTENT_LENGTH=40*1024*1024)
ADMIN_USER=os.environ.get('EMS_ADMIN_USER','admin')
ADMIN_PASSWORD=os.environ.get('EMS_ADMIN_PASSWORD','change-me')
# ===== PERSISTANCE SUPABASE EMS =====
SUPABASE_URL = "https://pseugydjgchwymuoghst.supabase.co"
SUPABASE_BUCKET = "ems-backups"

def supabase_headers():
    key = os.environ.get("SUPABASE_SECRET_KEY", "").strip()
    return {
        "Authorization": f"Bearer {key}",
        "apikey": key,
    }

def restore_from_supabase():
    key = os.environ.get("SUPABASE_SECRET_KEY", "").strip()
    if not key:
        return

    url = f"{SUPABASE_URL}/storage/v1/object/{SUPABASE_BUCKET}/latest.zip"
    req = urllib.request.Request(url, headers=supabase_headers(), method="GET")

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return
        print("Supabase restore HTTP error:", e)
        return
    except Exception as e:
        print("Supabase restore error:", e)
        return

    temp_dir = Path(tempfile.mkdtemp(prefix="ems_supabase_restore_"))
    try:
        zip_path = temp_dir / "latest.zip"
        zip_path.write_bytes(data)

        extract_dir = temp_dir / "extract"
        with zipfile.ZipFile(zip_path, "r") as z:
            z.extractall(extract_dir)

        new_db = extract_dir / "ems.db"
        if new_db.exists():
            test = sqlite3.connect(str(new_db))
            try:
                result = test.execute("PRAGMA integrity_check").fetchone()
                if not result or result[0] != "ok":
                    print("Supabase restore: DB invalide")
                    return
            finally:
                test.close()

            shutil.copy2(new_db, DB)

        new_uploads = extract_dir / "uploads"
        if new_uploads.exists():
            if UPLOAD_DIR.exists():
                shutil.rmtree(UPLOAD_DIR)
            shutil.copytree(new_uploads, UPLOAD_DIR)

        print("EMS restaure depuis Supabase")

    except Exception as e:
        print("Supabase restore extraction error:", e)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def backup_to_supabase():
    key = os.environ.get("SUPABASE_SECRET_KEY", "").strip()
    if not key or not DB.exists():
        return

    temp_dir = Path(tempfile.mkdtemp(prefix="ems_supabase_backup_"))
    try:
        db_copy = temp_dir / "ems.db"

        source = sqlite3.connect(str(DB))
        destination = sqlite3.connect(str(db_copy))
        try:
            source.backup(destination)
        finally:
            destination.close()
            source.close()

        zip_path = temp_dir / "latest.zip"

        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.write(db_copy, "ems.db")

            if UPLOAD_DIR.exists():
                for f in UPLOAD_DIR.rglob("*"):
                    if f.is_file():
                        archive.write(
                            f,
                            str(Path("uploads") / f.relative_to(UPLOAD_DIR))
                        )

        url = f"{SUPABASE_URL}/storage/v1/object/{SUPABASE_BUCKET}/latest.zip"
        headers = supabase_headers()
        headers["Content-Type"] = "application/zip"
        headers["x-upsert"] = "true"

        req = urllib.request.Request(
            url,
            data=zip_path.read_bytes(),
            headers=headers,
            method="POST"
        )

        with urllib.request.urlopen(req, timeout=45) as resp:
            resp.read()

        print("EMS sauvegarde dans Supabase")

    except Exception as e:
        print("Supabase backup error:", e)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


@app.after_request
def sauvegarde_supabase_apres_modification(response):
    if request.method in ("POST", "PUT", "PATCH", "DELETE") and response.status_code < 500:
        try:
            backup_to_supabase()
        except Exception as e:
            print("Erreur sauvegarde automatique:", e)
    return response

# ===== FIN PERSISTANCE SUPABASE EMS =====
COMPANY={
 'name':'EMS TMM','address':'Lot K4 107 LD Ivato\nAmbohidratrimo 105 - MADAGASCAR',
 'nif':'5019396150','stat':'45101 11 2025 0 11108','email':'emstamatave@gmail.com','phone':'+261 37 61 700 42',
 'bank':'XBred','bank_code':'00008','branch_code':'00021','account':'05003025816','key':'21'
}

def db():
    c=sqlite3.connect(DB); c.row_factory=sqlite3.Row; return c

def ensure_column(c, table, column, definition):
    cols={r['name'] for r in c.execute(f'PRAGMA table_info({table})').fetchall()}
    if column not in cols:
        c.execute(f'ALTER TABLE {table} ADD COLUMN {column} {definition}')

def init_db():
    c=db(); c.executescript('''
    CREATE TABLE IF NOT EXISTS clients(id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT NOT NULL,address TEXT,nif TEXT,stat TEXT,email TEXT,phone TEXT);
    CREATE TABLE IF NOT EXISTS docs(id INTEGER PRIMARY KEY AUTOINCREMENT,kind TEXT NOT NULL,number TEXT NOT NULL,doc_date TEXT,due_date TEXT,client_id INTEGER,reference TEXT,po_number TEXT,payment_terms TEXT,delivery TEXT,status TEXT DEFAULT 'Brouillon',notes TEXT DEFAULT '',created_at TEXT DEFAULT CURRENT_TIMESTAMP,FOREIGN KEY(client_id) REFERENCES clients(id));
    CREATE TABLE IF NOT EXISTS lines(id INTEGER PRIMARY KEY AUTOINCREMENT,doc_id INTEGER,description TEXT,qty REAL DEFAULT 1,unit_price REAL DEFAULT 0,FOREIGN KEY(doc_id) REFERENCES docs(id));
    CREATE TABLE IF NOT EXISTS payments(id INTEGER PRIMARY KEY AUTOINCREMENT,doc_id INTEGER,payment_date TEXT,amount REAL DEFAULT 0,method TEXT,note TEXT,FOREIGN KEY(doc_id) REFERENCES docs(id));
    CREATE TABLE IF NOT EXISTS client_attachments(id INTEGER PRIMARY KEY AUTOINCREMENT,client_id INTEGER,original_name TEXT,stored_name TEXT,mime TEXT,created_at TEXT DEFAULT CURRENT_TIMESTAMP,FOREIGN KEY(client_id) REFERENCES clients(id));
    CREATE TABLE IF NOT EXISTS client_machine_photos(id INTEGER PRIMARY KEY AUTOINCREMENT,client_id INTEGER,original_name TEXT,stored_name TEXT,mime TEXT,created_at TEXT DEFAULT CURRENT_TIMESTAMP,FOREIGN KEY(client_id) REFERENCES clients(id));
    CREATE TABLE IF NOT EXISTS doc_images(id INTEGER PRIMARY KEY AUTOINCREMENT,doc_id INTEGER,original_name TEXT,stored_name TEXT,mime TEXT,created_at TEXT DEFAULT CURRENT_TIMESTAMP,FOREIGN KEY(doc_id) REFERENCES docs(id));
    CREATE TABLE IF NOT EXISTS stock_items(id INTEGER PRIMARY KEY AUTOINCREMENT,reference TEXT UNIQUE,designation TEXT NOT NULL,qty REAL DEFAULT 0,purchase_price REAL DEFAULT 0,sale_price REAL DEFAULT 0,min_qty REAL DEFAULT 0,original_name TEXT,stored_name TEXT,mime TEXT,notes TEXT DEFAULT '',created_at TEXT DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE IF NOT EXISTS stock_moves(id INTEGER PRIMARY KEY AUTOINCREMENT,item_id INTEGER NOT NULL,move_date TEXT,move_type TEXT NOT NULL,qty REAL NOT NULL,note TEXT,created_at TEXT DEFAULT CURRENT_TIMESTAMP,FOREIGN KEY(item_id) REFERENCES stock_items(id));
    ''')
    ensure_column(c,'docs','internal_note',"TEXT DEFAULT ''")
    ensure_column(c,'lines','discount_pct','REAL DEFAULT 0')
    c.commit(); c.close()

def save_upload(file, prefix):
    if not file or not file.filename: return None
    original=secure_filename(file.filename) or 'fichier'
    ext=Path(original).suffix.lower()
    stored=f"{prefix}_{uuid.uuid4().hex}{ext}"
    file.save(UPLOAD_DIR/stored)
    return original, stored, (file.mimetype or mimetypes.guess_type(original)[0] or 'application/octet-stream')

# Initialise automatiquement la base, y compris avec Gunicorn/Render.
init_db()

def money(v): return f"{float(v or 0):,.0f}".replace(',', ' ')

def parse_decimal(v, default=0.0):
    """Accepte les saisies mobiles FR/MG : 12 500, 12500, 12,50 ou 12.50."""
    if v is None: return default
    t=str(v).strip().replace('\u00a0','').replace(' ','').replace(',', '.')
    if not t: return default
    try: return float(t)
    except (TypeError, ValueError): return default



def extract_client_from_image(file_storage):
    """Extrait les coordonnées d'un client depuis une photo avec OpenAI Vision.
    Nécessite OPENAI_API_KEY sur le serveur.
    """
    api_key=os.environ.get('OPENAI_API_KEY','').strip()
    if not api_key:
        raise RuntimeError("La reconnaissance photo n'est pas configurée sur le serveur (OPENAI_API_KEY manquante).")
    raw=file_storage.read()
    file_storage.stream.seek(0)
    if not raw:
        raise RuntimeError("Image vide.")
    mime=file_storage.mimetype or 'image/jpeg'
    if not mime.startswith('image/'):
        raise RuntimeError("Choisis une photo (JPG, PNG, HEIC converti par le téléphone).")
    data_url=f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"
    prompt=(
        "Lis cette carte de visite, facture, devis ou document client. "
        "Retourne UNIQUEMENT un objet JSON avec exactement ces clés: "
        "name, phone, email, nif, stat, address. "
        "name = nom de société si présent, sinon nom de la personne. "
        "N'invente rien; mets une chaîne vide si une information n'est pas visible."
    )
    payload={
        'model': os.environ.get('EMS_OCR_MODEL','gpt-4.1-mini'),
        'input': [{
            'role':'user',
            'content':[
                {'type':'input_text','text':prompt},
                {'type':'input_image','image_url':data_url}
            ]
        }]
    }
    req=urllib.request.Request(
        'https://api.openai.com/v1/responses',
        data=json.dumps(payload).encode('utf-8'),
        headers={'Authorization':f'Bearer {api_key}','Content-Type':'application/json'},
        method='POST'
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            result=json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        detail=e.read().decode('utf-8','ignore')[:500]
        raise RuntimeError(f"Erreur de reconnaissance ({e.code}). {detail}")
    except Exception as e:
        raise RuntimeError(f"Impossible de lire la photo: {e}")

    text=result.get('output_text','')
    if not text:
        # Compatibilité avec réponses où le texte est imbriqué dans output/content.
        for item in result.get('output',[]):
            for content in item.get('content',[]):
                if content.get('type') in ('output_text','text') and content.get('text'):
                    text=content.get('text'); break
            if text: break
    try:
        obj=json.loads(text) if isinstance(text,str) else {}
    except Exception:
        m=re.search(r'\{.*\}', text or '', re.S)
        obj=json.loads(m.group(0)) if m else {}
    return {k:str(obj.get(k,'') or '').strip() for k in ('name','phone','email','nif','stat','address')}

def total_for(con, doc_id):
    return con.execute('select coalesce(sum(qty*unit_price*(1-coalesce(discount_pct,0)/100.0)),0) t from lines where doc_id=?',(doc_id,)).fetchone()['t']

def paid_for(con, doc_id):
    return con.execute('select coalesce(sum(amount),0) t from payments where doc_id=?',(doc_id,)).fetchone()['t']

ONES=['zéro','un','deux','trois','quatre','cinq','six','sept','huit','neuf','dix','onze','douze','treize','quatorze','quinze','seize']
def under100(n):
    if n<17:return ONES[n]
    if n<20:return 'dix-'+ONES[n-10]
    tens={20:'vingt',30:'trente',40:'quarante',50:'cinquante',60:'soixante'}
    if n<70:
        t=(n//10)*10;r=n%10;return tens[t]+((' et un') if r==1 else ('-'+ONES[r] if r else ''))
    if n<80:
        r=n-60;return 'soixante-'+('et-onze' if r==11 else under100(r))
    r=n-80; base='quatre-vingt' + ('s' if r==0 else '')
    return base if r==0 else base+'-'+under100(r)
def under1000(n):
    if n<100:return under100(n)
    h=n//100;r=n%100
    p='cent' if h==1 else ONES[h]+' cent'
    if r==0 and h>1:p+='s'
    return p if r==0 else p+' '+under100(r)
def number_words(n):
    n=int(round(n))
    if n==0:return 'zéro Ariary'
    parts=[]
    for value,name in [(1_000_000_000,'milliard'),(1_000_000,'million'),(1000,'mille')]:
        if n>=value:
            q,n=divmod(n,value)
            if value==1000 and q==1: parts.append('mille')
            else: parts.append(under1000(q)+' '+name+('s' if q>1 and value!=1000 else ''))
    if n: parts.append(under1000(n))
    s=' '.join(parts)
    return s[:1].upper()+s[1:]+' Ariary'

def next_number(kind):
    y=str(date.today().year)[-2:]; prefix='DEV' if kind=='Devis' else 'FAC'
    con=db(); rows=con.execute('select number from docs where kind=? and number like ?',(kind,f'%/{y}')).fetchall(); con.close()
    nums=[]
    for r in rows:
        m=re.search(r'(\d+)(?=/'+re.escape(y)+r'$)',r['number'] or '')
        if m: nums.append(int(m.group(1)))
    return f"{prefix} {max(nums,default=0)+1:03d}/{y}"

STYLE='''
:root{--ink:#171717;--muted:#6b7280;--line:#e5e7eb;--bg:#f6f7f9;--card:#fff;--accent:#f59e0b;--green:#166534;--red:#b91c1c}
*{box-sizing:border-box}body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif;margin:0;background:var(--bg);color:var(--ink)}
.top{background:#111;color:white;padding:14px 20px;display:flex;align-items:center;gap:14px;position:sticky;top:0;z-index:5}.top img{height:42px;border-radius:5px}.brand{font-weight:800}.sub{font-size:12px;color:#bbb}
.wrap{max-width:1180px;margin:22px auto;padding:0 14px}.nav{display:flex;gap:8px;flex-wrap:wrap}.nav a,.btn2{background:white;border:1px solid var(--line);padding:9px 12px;border-radius:9px;color:#111;text-decoration:none}.nav a:hover,.btn2:hover{border-color:#aaa}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:18px;margin-top:16px;box-shadow:0 2px 10px rgba(0,0,0,.025)}
.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.grid3{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}.grid4{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px}
input,select,textarea{width:100%;padding:11px;border:1px solid #cfd4dc;border-radius:8px;background:white;font-size:16px;min-height:44px}textarea{min-height:70px}label{font-size:13px;font-weight:650;display:block;margin-bottom:5px}
button{padding:10px 14px;border:0;border-radius:8px;background:#111;color:#fff;cursor:pointer;font-weight:700}.danger{background:var(--red)}.success{background:var(--green)}
table{width:100%;border-collapse:collapse}th,td{padding:10px;border-bottom:1px solid #eee;text-align:left;vertical-align:top}th{font-size:12px;color:#555;text-transform:uppercase}.right{text-align:right}.muted{color:var(--muted)}.total{font-size:22px;font-weight:800}.kpi{font-size:28px;font-weight:800}.badge{display:inline-block;padding:4px 8px;border-radius:999px;background:#eee;font-size:12px}.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}.flash{background:#fff6d8;border:1px solid #f5d98c;padding:10px;border-radius:8px;margin:12px 0}.search{max-width:320px}.small{font-size:12px}.nowrap{white-space:nowrap}.linefield{display:block;min-height:52px!important;font-size:18px!important;padding:12px!important;-webkit-appearance:none;appearance:none;pointer-events:auto!important;user-select:text!important;-webkit-user-select:text!important}.qtyfield,.pricefield{width:100%!important;min-width:0!important}
@media(max-width:760px){.grid,.grid3,.grid4{grid-template-columns:1fr}.top{position:static}.tablewrap{overflow:visible}.wrap{margin-top:12px}.nav a{flex:1;text-align:center}.card{padding:14px}
.line-table,.line-table tbody,.line-table tr,.line-table td{display:block;width:100%}.line-table thead{display:none}.line-table tr{border:1px solid #e5e7eb;border-radius:12px;padding:10px;margin:0 0 12px}.line-table td{border:0;padding:5px}.line-table td:before{display:block;font-size:12px;font-weight:700;color:#555;margin-bottom:4px}.line-table td:nth-child(1):before{content:'Désignation'}.line-table td:nth-child(2):before{content:'Quantité'}.line-table td:nth-child(3):before{content:'P.U. Ar'}.line-table td:nth-child(4):before{content:'Remise %'}.line-table textarea{min-width:0!important;width:100%}
}
'''
TPL='''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>EMS Facturation</title><meta name="theme-color" content="#111111"><meta name="apple-mobile-web-app-capable" content="yes"><meta name="apple-mobile-web-app-status-bar-style" content="black-translucent"><meta name="apple-mobile-web-app-title" content="EMS"><link rel="manifest" href="/manifest.webmanifest"><link rel="apple-touch-icon" href="/icon-180.png"><link rel="icon" href="/icon-192.png"><style>'''+STYLE+'''</style></head><body>
<div class="top">{% if logo %}<img src="{{url_for('logo')}}">{% endif %}<div><div class="brand">EMS — Devis & Facturation</div><div class="sub">Gestion commerciale • Ariary (MGA)</div></div></div>
<div class="wrap"><div class="nav"><a href="{{url_for('home')}}">Tableau de bord</a><a href="{{url_for('clients')}}">Clients</a><a href="{{url_for('documents')}}">Devis / Factures</a><a href="{{url_for('stock')}}">Stock</a><a href="{{url_for('new_document')}}">+ Nouveau</a><a href="{{url_for('backup_db')}}">Sauvegarde complète</a><a href="{{url_for('restore_backup')}}">Restaurer</a><a href="{{url_for('logout')}}">Déconnexion</a></div>
{% with messages=get_flashed_messages() %}{% for m in messages %}<div class="flash">{{m}}</div>{% endfor %}{% endwith %}{{body|safe}}</div><script>if('serviceWorker' in navigator){navigator.serviceWorker.register('/service-worker.js').catch(()=>{});}</script></body></html>'''

def page(body): return render_template_string(TPL,body=body,logo=LOGO.exists())


LOGIN_TPL='''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Connexion EMS</title><style>body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif;background:#f6f7f9;margin:0;display:grid;place-items:center;min-height:100vh}.box{background:white;border:1px solid #e5e7eb;border-radius:16px;padding:24px;width:min(92vw,390px);box-shadow:0 8px 30px rgba(0,0,0,.06)}input{width:100%;padding:11px;border:1px solid #cfd4dc;border-radius:8px;box-sizing:border-box;margin:5px 0 14px}button{width:100%;padding:11px;border:0;border-radius:8px;background:#111;color:#fff;font-weight:700}.err{background:#fee2e2;padding:9px;border-radius:8px;margin-bottom:12px}</style></head><body><form class="box" method="post"><h2>EMS — Connexion</h2><p>Accès sécurisé au logiciel de devis & facturation.</p>{% if error %}<div class="err">{{error}}</div>{% endif %}<label>Identifiant</label><input name="username" autocomplete="username" required><label>Mot de passe</label><input type="password" name="password" autocomplete="current-password" required><button>Se connecter</button></form></body></html>'''

@app.before_request
def require_login():
    allowed={'login','health','manifest','service_worker','icon180','icon192','icon512','logo'}
    if request.endpoint in allowed or request.path.startswith('/static/'):
        return None
    if not session.get('logged_in'):
        return redirect(url_for('login', next=request.path))

@app.route('/login', methods=['GET','POST'])
def login():
    error=None
    if request.method=='POST':
        u=request.form.get('username','')
        pw=request.form.get('password','')
        if hmac.compare_digest(u, ADMIN_USER) and hmac.compare_digest(pw, ADMIN_PASSWORD):
            session['logged_in']=True
            return redirect(request.args.get('next') or url_for('home'))
        error='Identifiant ou mot de passe incorrect.'
    return render_template_string(LOGIN_TPL,error=error)

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/backup')
def backup_db():
    if not DB.exists():
        return 'Aucune base de données',404
    tmp = Path(tempfile.gettempdir()) / f"ems_backup_{date.today().isoformat()}_{uuid.uuid4().hex[:8]}.zip"
    with zipfile.ZipFile(tmp, 'w', zipfile.ZIP_DEFLATED) as z:
        z.write(DB, 'ems.db')
        file_count=0
        if UPLOAD_DIR.exists():
            for p in UPLOAD_DIR.rglob('*'):
                if p.is_file():
                    z.write(p, f"uploads/{p.relative_to(UPLOAD_DIR)}")
                    file_count += 1
        z.writestr('SAUVEGARDE_EMS.txt', f"EMS Facturation V13\\nDate: {date.today().isoformat()}\\nBase: ems.db\\nFichiers/photos: {file_count}\\n")
    return send_file(tmp, as_attachment=True, download_name=f"EMS_sauvegarde_complete_{date.today().isoformat()}.zip")

@app.route('/restore', methods=['GET','POST'])
def restore_backup():
    if request.method == 'POST':
        f = request.files.get('backup_file')
        if not f or not f.filename:
            flash('Choisis un fichier de sauvegarde EMS.')
            return redirect(url_for('restore_backup'))
        if not f.filename.lower().endswith('.zip'):
            flash('Le fichier doit être une sauvegarde EMS au format ZIP.')
            return redirect(url_for('restore_backup'))
        work = Path(tempfile.mkdtemp(prefix='ems_restore_'))
        try:
            archive = work / 'backup.zip'
            f.save(archive)
            extract = work / 'extract'
            extract.mkdir()
            with zipfile.ZipFile(archive, 'r') as z:
                # Protection contre les chemins ZIP malveillants
                for member in z.infolist():
                    target = (extract / member.filename).resolve()
                    if extract.resolve() not in target.parents and target != extract.resolve():
                        raise ValueError('Archive invalide')
                z.extractall(extract)
            restored_db = extract / 'ems.db'
            if not restored_db.exists():
                raise ValueError('Base ems.db absente de la sauvegarde')
            # Vérifie que la base SQLite est lisible avant remplacement
            test = sqlite3.connect(restored_db)
            test.execute('PRAGMA schema_version').fetchone()
            test.close()
            shutil.copy2(restored_db, DB)
            restored_uploads = extract / 'uploads'
            if UPLOAD_DIR.exists():
                shutil.rmtree(UPLOAD_DIR)
            UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
            if restored_uploads.exists():
                for p in restored_uploads.rglob('*'):
                    if p.is_file():
                        dest = UPLOAD_DIR / p.relative_to(restored_uploads)
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(p, dest)
            init_db()
            flash('Sauvegarde restaurée avec succès.')
            return redirect(url_for('home'))
        except Exception as e:
            flash(f"Restauration impossible : {e}")
            return redirect(url_for('restore_backup'))
        finally:
            shutil.rmtree(work, ignore_errors=True)
    return page('''<div class="card"><h2>Restaurer une sauvegarde</h2>
    <p>Choisis un fichier <b>EMS_sauvegarde_complete_....zip</b>. La restauration remplacera les données actuelles et les photos par celles de la sauvegarde.</p>
    <form method="post" enctype="multipart/form-data">
      <p><input type="file" name="backup_file" accept=".zip,application/zip" required></p>
      <p><button type="submit" onclick="return confirm('Restaurer cette sauvegarde ? Les données actuelles seront remplacées.')">Restaurer</button></p>
    </form></div>''')

@app.route('/logo.png')
def logo(): return send_file(LOGO)

@app.route('/icon-180.png')
def icon180(): return send_file(BASE/'icon-180.png', mimetype='image/png')

@app.route('/icon-192.png')
def icon192(): return send_file(BASE/'icon-192.png', mimetype='image/png')

@app.route('/icon-512.png')
def icon512(): return send_file(BASE/'icon-512.png', mimetype='image/png')

@app.route('/manifest.webmanifest')
def manifest():
    return jsonify({
        'name':'EMS — Devis & Facturation',
        'short_name':'EMS',
        'start_url':'/',
        'scope':'/',
        'display':'standalone',
        'background_color':'#f6f7f9',
        'theme_color':'#111111',
        'icons':[
            {'src':'/icon-192.png','sizes':'192x192','type':'image/png'},
            {'src':'/icon-512.png','sizes':'512x512','type':'image/png'}
        ]
    })

@app.route('/service-worker.js')
def service_worker():
    js = '''const CACHE='ems-v10';
const CORE=['/','/manifest.webmanifest','/icon-192.png','/icon-512.png'];
self.addEventListener('install',e=>e.waitUntil(caches.open(CACHE).then(c=>c.addAll(CORE))));
self.addEventListener('activate',e=>e.waitUntil(caches.keys().then(ks=>Promise.all(ks.filter(k=>k!==CACHE).map(k=>caches.delete(k))))));
self.addEventListener('fetch',e=>{if(e.request.method!=='GET')return;e.respondWith(fetch(e.request).then(r=>{const cp=r.clone();caches.open(CACHE).then(c=>c.put(e.request,cp));return r;}).catch(()=>caches.match(e.request).then(r=>r||caches.match('/'))));});'''
    return Response(js, mimetype='application/javascript', headers={'Cache-Control':'no-cache'})

@app.route('/health')
def health(): return {'ok': True, 'app':'EMS Facturation V13'}

@app.route('/')
def home():
    con=db(); clients=con.execute('select count(*) n from clients').fetchone()['n']; devis=con.execute("select count(*) n from docs where kind='Devis'").fetchone()['n']; fact=con.execute("select count(*) n from docs where kind='Facture'").fetchone()['n']
    billed=con.execute("select coalesce(sum(l.qty*l.unit_price*(1-coalesce(l.discount_pct,0)/100.0)),0) t from lines l join docs d on d.id=l.doc_id where d.kind='Facture'").fetchone()['t']; paid=con.execute("select coalesce(sum(p.amount),0) t from payments p join docs d on d.id=p.doc_id where d.kind='Facture'").fetchone()['t']; recent=con.execute('''select d.*,c.name client,coalesce(sum(l.qty*l.unit_price*(1-coalesce(l.discount_pct,0)/100.0)),0) total from docs d left join clients c on c.id=d.client_id left join lines l on l.doc_id=d.id group by d.id order by d.id desc limit 6''').fetchall(); con.close()
    trs=''.join(f"<tr><td>{r['kind']}</td><td><a href='/document/{r['id']}'>{r['number']}</a></td><td>{r['client'] or ''}</td><td class='right'>{money(r['total'])} Ar</td><td><span class='badge'>{r['status']}</span></td></tr>" for r in recent)
    return page(f'''<div class="grid4"><div class="card"><div class="muted">Clients</div><div class="kpi">{clients}</div></div><div class="card"><div class="muted">Devis</div><div class="kpi">{devis}</div></div><div class="card"><div class="muted">Factures</div><div class="kpi">{fact}</div></div><div class="card"><div class="muted">Reste à encaisser</div><div class="kpi">{money(max(0,billed-paid))} Ar</div></div></div><div class="card"><h2>Activité</h2><div class="grid"><div><div class="muted">Total facturé</div><div class="total">{money(billed)} Ar</div></div><div><div class="muted">Total encaissé</div><div class="total">{money(paid)} Ar</div></div></div></div><div class="card"><h2>Documents récents</h2><div class="tablewrap"><table><tr><th>Type</th><th>N°</th><th>Client</th><th class="right">Total</th><th>Statut</th></tr>{trs}</table></div></div>''')

@app.route('/clients',methods=['GET','POST'])
def clients():
    con=db()
    if request.method=='POST':
        cur=con.execute('insert into clients(name,address,nif,stat,email,phone) values(?,?,?,?,?,?)',(request.form['name'],request.form.get('address'),request.form.get('nif'),request.form.get('stat'),request.form.get('email'),request.form.get('phone'))); cid=cur.lastrowid
        for f in request.files.getlist('attachments'):
            saved=save_upload(f,f'client_{cid}')
            if saved: con.execute('insert into client_attachments(client_id,original_name,stored_name,mime) values(?,?,?,?)',(cid,*saved))
        con.commit(); con.close(); flash('Client ajouté.'); return redirect(url_for('clients'))
    q=request.args.get('q','').strip(); rows=con.execute('select * from clients where name like ? or nif like ? order by name',(f'%{q}%',f'%{q}%')).fetchall() if q else con.execute('select * from clients order by name').fetchall(); con.close()
    trs=''.join(f"<tr><td><a href='/client/{r['id']}/edit'>{r['name']}</a></td><td>{r['nif'] or ''}</td><td>{r['stat'] or ''}</td><td>{r['phone'] or ''}</td><td>{r['email'] or ''}</td></tr>" for r in rows)
    return page(f'''<div class="card"><h2>Nouveau client</h2><form method="post" enctype="multipart/form-data" id="client-form"><div class="grid3"><div><label>Nom / société</label><input id="client-name" name="name" required></div><div><label>Téléphone</label><input id="client-phone" name="phone" inputmode="tel"></div><div><label>Email</label><input id="client-email" name="email" inputmode="email"></div><div><label>NIF</label><input id="client-nif" name="nif"></div><div><label>STAT</label><input id="client-stat" name="stat"></div><div><label>Adresse</label><textarea id="client-address" name="address"></textarea></div></div><p><label>Photo / scan du client + pièces jointes</label><input id="client-attachments" type="file" name="attachments" multiple accept="image/*,application/pdf"><span class="small muted">Choisis une photo puis touche « Lire la photo » pour préremplir la fiche.</span></p><p class="row"><button type="button" class="success" id="scan-client">📷 Lire la photo et préremplir</button><span id="scan-status" class="small muted"></span></p><p><button type="submit">Ajouter le client</button></p></form></div><div class="card"><div class="row"><h2 style="flex:1">Clients</h2><form><input class="search" name="q" value="{q}" placeholder="Rechercher nom ou NIF"></form></div><div class="tablewrap"><table><tr><th>Nom</th><th>NIF</th><th>STAT</th><th>Téléphone</th><th>Email</th></tr>{trs}</table></div></div><script>
const scanBtn=document.getElementById('scan-client');
if(scanBtn) scanBtn.addEventListener('click',async()=>{{
  const input=document.getElementById('client-attachments'); const status=document.getElementById('scan-status');
  if(!input.files.length){{status.textContent='Choisis d’abord une photo.';return;}}
  const file=[...input.files].find(f=>f.type.startsWith('image/'));
  if(!file){{status.textContent='La reconnaissance nécessite une photo.';return;}}
  const fd=new FormData(); fd.append('image',file); scanBtn.disabled=true; status.textContent='Lecture de la photo…';
  try{{const r=await fetch('/client/scan',{{method:'POST',body:fd}});const j=await r.json();if(!r.ok||!j.ok)throw new Error(j.error||'Erreur');
    const map={{name:'client-name',phone:'client-phone',email:'client-email',nif:'client-nif',stat:'client-stat',address:'client-address'}};
    for(const [k,id] of Object.entries(map)) if(j.data[k]) document.getElementById(id).value=j.data[k];
    status.textContent='Fiche préremplie. Vérifie puis enregistre.';
  }}catch(e){{status.textContent=e.message;}}finally{{scanBtn.disabled=false;}}
}});
</script>''')


@app.post('/client/scan')
def scan_client():
    f=request.files.get('image')
    if not f or not f.filename:
        return jsonify({'ok':False,'error':'Aucune photo reçue.'}),400
    try:
        data=extract_client_from_image(f)
        return jsonify({'ok':True,'data':data})
    except Exception as e:
        return jsonify({'ok':False,'error':str(e)}),400

@app.route('/client/<int:cid>/edit',methods=['GET','POST'])
def edit_client(cid):
    con=db(); r=con.execute('select * from clients where id=?',(cid,)).fetchone()
    if not r: con.close(); return 'Client introuvable',404
    if request.method=='POST':
        con.execute('update clients set name=?,address=?,nif=?,stat=?,email=?,phone=? where id=?',
                    (request.form['name'],request.form.get('address'),request.form.get('nif'),request.form.get('stat'),request.form.get('email'),request.form.get('phone'),cid))
        con.commit(); con.close(); flash('Client mis à jour.')
        return redirect(url_for('edit_client',cid=cid))
    photos=con.execute('select * from client_machine_photos where client_id=? order by id desc',(cid,)).fetchall()
    history=con.execute('select id,kind,number,doc_date,status from docs where client_id=? order by id desc',(cid,)).fetchall()
    con.close()
    count=len(photos)
    history_html=''.join(f"<tr><td><a href='/document/{h['id']}'>{h['kind']} {h['number']}</a></td><td>{h['doc_date'] or ''}</td><td><span class='badge'>{h['status'] or ''}</span></td></tr>" for h in history) or "<tr><td colspan='3' class='muted'>Aucun devis ou facture.</td></tr>"
    gallery=''.join(
        f"<div style='display:inline-block;margin:6px'><a href='/client/{cid}/machine-photo/{p['id']}' target='_blank'><img src='/client/{cid}/machine-photo/{p['id']}' style='width:125px;height:95px;object-fit:cover;border-radius:9px;border:1px solid #ddd'></a><form method='post' action='/client/{cid}/machine-photo/{p['id']}/delete'><button type='submit' class='btn2' style='margin-top:4px;padding:5px 8px'>Supprimer</button></form></div>"
        for p in photos
    ) or '<span class="muted">Aucune photo de machine.</span>'
    return page(f'''<div class="card"><h2>Modifier le client</h2><form method="post"><div class="grid"><div><label>Nom</label><input name="name" value="{r['name'] or ''}" required></div><div><label>Téléphone</label><input name="phone" value="{r['phone'] or ''}"></div><div><label>Email</label><input name="email" value="{r['email'] or ''}"></div><div><label>NIF</label><input name="nif" value="{r['nif'] or ''}"></div><div><label>STAT</label><input name="stat" value="{r['stat'] or ''}"></div><div><label>Adresse</label><textarea name="address">{r['address'] or ''}</textarea></div></div><p><button>Enregistrer</button></p></form></div>
    <div class="card"><div class="row"><h2 style="flex:1">Machines du client</h2><span class="badge">{count}/10 photos</span></div>
    <form method="post" action="/client/{cid}/machines/add" enctype="multipart/form-data">
    <p><input type="file" name="machine_photos" multiple accept="image/*"></p>
    <p class="small muted">Sélectionne jusqu'à 10 photos au total pour ce client.</p>
    <p><button type="submit" class="success">📷 Ajouter une machine</button></p></form>
    <div>{gallery}</div></div>
    <div class="card"><h2>Historique du client</h2><div class="tablewrap"><table><tr><th>Document</th><th>Date</th><th>Statut</th></tr>{history_html}</table></div></div>''')

@app.post('/client/<int:cid>/machines/add')
def add_client_machine_photos(cid):
    con=db()
    if not con.execute('select id from clients where id=?',(cid,)).fetchone():
        con.close(); return 'Client introuvable',404
    existing=con.execute('select count(*) n from client_machine_photos where client_id=?',(cid,)).fetchone()['n']
    files=[f for f in request.files.getlist('machine_photos') if f and f.filename]
    available=max(0,10-existing); added=0
    for f in files[:available]:
        if not (f.mimetype or '').startswith('image/'): continue
        saved=save_upload(f,f'machine_{cid}')
        if saved:
            con.execute('insert into client_machine_photos(client_id,original_name,stored_name,mime) values(?,?,?,?)',(cid,*saved)); added+=1
    con.commit(); con.close()
    if not files: flash('Choisis au moins une photo.')
    elif available==0: flash('Ce client a déjà 10 photos de machines.')
    elif len(files)>available: flash(f'{added} photo(s) ajoutée(s). Limite : 10 photos par client.')
    else: flash(f'{added} photo(s) ajoutée(s).')
    return redirect(url_for('edit_client',cid=cid))

@app.route('/client/<int:cid>/machine-photo/<int:pid>')
def client_machine_photo(cid,pid):
    con=db(); p=con.execute('select * from client_machine_photos where id=? and client_id=?',(pid,cid)).fetchone(); con.close()
    if not p:return 'Photo introuvable',404
    return send_from_directory(UPLOAD_DIR,p['stored_name'],mimetype=p['mime'])

@app.post('/client/<int:cid>/machine-photo/<int:pid>/delete')
def delete_client_machine_photo(cid,pid):
    con=db(); p=con.execute('select * from client_machine_photos where id=? and client_id=?',(pid,cid)).fetchone()
    if p:
        con.execute('delete from client_machine_photos where id=?',(pid,)); con.commit()
        try:(UPLOAD_DIR/p['stored_name']).unlink(missing_ok=True)
        except Exception:pass
    con.close(); flash('Photo supprimée.')
    return redirect(url_for('edit_client',cid=cid))

@app.route('/client/<int:cid>/attachment/<int:aid>')
def client_attachment(cid,aid):
    con=db(); a=con.execute('select * from client_attachments where id=? and client_id=?',(aid,cid)).fetchone(); con.close()
    if not a:return 'Pièce jointe introuvable',404
    return send_from_directory(UPLOAD_DIR,a['stored_name'],download_name=a['original_name'])

@app.route('/stock', methods=['GET','POST'])
def stock():
    con=db()
    if request.method=='POST':
        reference=(request.form.get('reference') or '').strip()
        designation=(request.form.get('designation') or '').strip()
        if not designation:
            con.close(); flash('La désignation est obligatoire.'); return redirect(url_for('stock'))
        qty=parse_decimal(request.form.get('qty'))
        purchase_price=max(0,parse_decimal(request.form.get('purchase_price')))
        sale_price=max(0,parse_decimal(request.form.get('sale_price')))
        min_qty=max(0,parse_decimal(request.form.get('min_qty')))
        notes=request.form.get('notes') or ''
        photo=request.files.get('photo')
        saved=save_upload(photo,'stock') if photo and photo.filename else None
        try:
            cur=con.execute("insert into stock_items(reference,designation,qty,purchase_price,sale_price,min_qty,notes,original_name,stored_name,mime) values(?,?,?,?,?,?,?,?,?,?)",(reference or None,designation,qty,purchase_price,sale_price,min_qty,notes,saved[0] if saved else None,saved[1] if saved else None,saved[2] if saved else None))
            item_id=cur.lastrowid
            if qty:
                con.execute('insert into stock_moves(item_id,move_date,move_type,qty,note) values(?,?,?,?,?)',(item_id,date.today().isoformat(),'Entrée initiale',qty,'Stock initial'))
            con.commit(); flash('Article ajouté au stock.')
        except sqlite3.IntegrityError:
            con.rollback(); flash('Cette référence existe déjà.')
        con.close(); return redirect(url_for('stock'))
    q=(request.args.get('q') or '').strip(); only_low=request.args.get('low')=='1'
    sql='select * from stock_items where 1=1'; params=[]
    if q:
        sql+=' and (coalesce(reference,"") like ? or designation like ?)'; params += [f'%{q}%',f'%{q}%']
    if only_low: sql+=' and qty<=min_qty'
    sql+=' order by designation'
    rows=con.execute(sql,params).fetchall()
    total_items=con.execute('select count(*) n from stock_items').fetchone()['n']
    low_count=con.execute('select count(*) n from stock_items where qty<=min_qty').fetchone()['n']
    stock_value=con.execute('select coalesce(sum(qty*purchase_price),0) t from stock_items').fetchone()['t']; con.close()
    trs=''
    for r in rows:
        alert=" <span class='badge' style='background:#fee2e2;color:#991b1b'>Stock faible</span>" if float(r['qty'] or 0)<=float(r['min_qty'] or 0) else ''
        photo=f"<a class='btn2' href='/stock/{r['id']}/photo' target='_blank'>Photo</a>" if r['stored_name'] else ''
        trs += f"<tr><td><a href='/stock/{r['id']}/edit'>{r['reference'] or ''}</a></td><td><a href='/stock/{r['id']}/edit'>{r['designation']}</a>{alert}</td><td class='right'><b>{float(r['qty'] or 0):g}</b></td><td class='right'>{money(r['purchase_price'])} Ar</td><td class='right'>{money(r['sale_price'])} Ar</td><td class='right'>{float(r['min_qty'] or 0):g}</td><td>{photo}</td></tr>"
    return page(f"""<div class='grid3'><div class='card'><div class='muted'>Références en stock</div><div class='kpi'>{total_items}</div></div><div class='card'><div class='muted'>Alertes stock faible</div><div class='kpi'>{low_count}</div></div><div class='card'><div class='muted'>Valeur du stock (achat)</div><div class='kpi'>{money(stock_value)} Ar</div></div></div><div class='card'><h2>Ajouter une pièce / un matériel</h2><form method='post' enctype='multipart/form-data'><div class='grid3'><div><label>Référence</label><input name='reference'></div><div><label>Désignation</label><input name='designation' required></div><div><label>Quantité initiale</label><input type='text' inputmode='decimal' name='qty' value='0'></div><div><label>Prix d'achat (Ar)</label><input type='text' inputmode='decimal' name='purchase_price' value='0'></div><div><label>Prix de vente (Ar)</label><input type='text' inputmode='decimal' name='sale_price' value='0'></div><div><label>Seuil d'alerte</label><input type='text' inputmode='decimal' name='min_qty' value='0'></div></div><p><label>Photo</label><input type='file' name='photo' accept='image/*'></p><p><label>Notes internes</label><textarea name='notes'></textarea></p><button>Ajouter au stock</button></form></div><div class='card'><div class='row'><h2 style='flex:1'>Stock</h2><form class='row'><input class='search' name='q' value='{q}' placeholder='Référence ou désignation'><label style='display:flex;gap:6px;align-items:center;font-weight:500'><input style='width:auto;min-height:auto' type='checkbox' name='low' value='1' {'checked' if only_low else ''}> Stock faible uniquement</label><button>Rechercher</button></form></div><div class='tablewrap'><table><tr><th>Réf.</th><th>Désignation</th><th class='right'>Qté</th><th class='right'>Achat</th><th class='right'>Vente</th><th class='right'>Alerte</th><th>Photo</th></tr>{trs}</table></div></div>""")

@app.route('/stock/<int:item_id>/edit', methods=['GET','POST'])
def edit_stock(item_id):
    con=db(); r=con.execute('select * from stock_items where id=?',(item_id,)).fetchone()
    if not r: con.close(); return 'Article introuvable',404
    if request.method=='POST':
        reference=(request.form.get('reference') or '').strip() or None; designation=(request.form.get('designation') or '').strip()
        photo=request.files.get('photo'); saved=save_upload(photo,f'stock_{item_id}') if photo and photo.filename else None
        vals=[reference,designation,max(0,parse_decimal(request.form.get('purchase_price'))),max(0,parse_decimal(request.form.get('sale_price'))),max(0,parse_decimal(request.form.get('min_qty'))),request.form.get('notes') or '']
        try:
            if saved: con.execute('update stock_items set reference=?,designation=?,purchase_price=?,sale_price=?,min_qty=?,notes=?,original_name=?,stored_name=?,mime=? where id=?',(*vals,*saved,item_id))
            else: con.execute('update stock_items set reference=?,designation=?,purchase_price=?,sale_price=?,min_qty=?,notes=? where id=?',(*vals,item_id))
            con.commit(); flash('Article mis à jour.')
        except sqlite3.IntegrityError: con.rollback(); flash('Cette référence existe déjà.')
        con.close(); return redirect(url_for('edit_stock',item_id=item_id))
    moves=con.execute('select * from stock_moves where item_id=? order by id desc limit 100',(item_id,)).fetchall(); con.close()
    photo=f"<p><a class='btn2' href='/stock/{item_id}/photo' target='_blank'>Voir la photo actuelle</a></p>" if r['stored_name'] else ''
    mtrs=''.join(f"<tr><td>{m['move_date'] or ''}</td><td>{m['move_type']}</td><td class='right'>{float(m['qty'] or 0):g}</td><td>{m['note'] or ''}</td></tr>" for m in moves)
    return page(f"""<div class='card'><div class='row'><h2 style='flex:1'>Stock — {r['designation']}</h2><a class='btn2' href='/stock'>← Retour au stock</a></div><form method='post' enctype='multipart/form-data'><div class='grid3'><div><label>Référence</label><input name='reference' value='{r['reference'] or ''}'></div><div><label>Désignation</label><input name='designation' value='{r['designation']}' required></div><div><label>Quantité actuelle</label><input value='{float(r['qty'] or 0):g}' disabled></div><div><label>Prix d'achat (Ar)</label><input type='text' inputmode='decimal' name='purchase_price' value='{float(r['purchase_price'] or 0):g}'></div><div><label>Prix de vente (Ar)</label><input type='text' inputmode='decimal' name='sale_price' value='{float(r['sale_price'] or 0):g}'></div><div><label>Seuil d'alerte</label><input type='text' inputmode='decimal' name='min_qty' value='{float(r['min_qty'] or 0):g}'></div></div><p><label>Remplacer / ajouter une photo</label><input type='file' name='photo' accept='image/*'></p>{photo}<p><label>Notes internes</label><textarea name='notes'>{r['notes'] or ''}</textarea></p><button>Enregistrer</button></form></div><div class='card'><h3>Entrée / sortie de stock</h3><form method='post' action='/stock/{item_id}/move'><div class='grid3'><div><label>Type</label><select name='move_type'><option>Entrée</option><option>Sortie</option><option>Correction +</option><option>Correction -</option></select></div><div><label>Quantité</label><input type='text' inputmode='decimal' name='qty' required></div><div><label>Date</label><input type='date' name='move_date' value='{date.today().isoformat()}'></div></div><p><label>Motif / note</label><input name='note'></p><button class='success'>Valider le mouvement</button></form></div><div class='card'><h3>Historique des mouvements</h3><div class='tablewrap'><table><tr><th>Date</th><th>Type</th><th class='right'>Quantité</th><th>Note</th></tr>{mtrs}</table></div></div>""")

@app.post('/stock/<int:item_id>/move')
def stock_move(item_id):
    qty=max(0,parse_decimal(request.form.get('qty'))); move_type=request.form.get('move_type') or 'Entrée'
    if qty<=0: flash('Quantité invalide.'); return redirect(url_for('edit_stock',item_id=item_id))
    delta=qty if move_type in ('Entrée','Correction +') else -qty
    con=db(); r=con.execute('select * from stock_items where id=?',(item_id,)).fetchone()
    if not r: con.close(); return 'Article introuvable',404
    new_qty=float(r['qty'] or 0)+delta
    con.execute('update stock_items set qty=? where id=?',(new_qty,item_id))
    con.execute('insert into stock_moves(item_id,move_date,move_type,qty,note) values(?,?,?,?,?)',(item_id,request.form.get('move_date') or date.today().isoformat(),move_type,delta,request.form.get('note') or ''))
    con.commit(); con.close(); flash(f'Mouvement enregistré. Nouveau stock : {new_qty:g}.'); return redirect(url_for('edit_stock',item_id=item_id))

@app.route('/stock/<int:item_id>/photo')
def stock_photo(item_id):
    con=db(); r=con.execute('select * from stock_items where id=?',(item_id,)).fetchone(); con.close()
    if not r or not r['stored_name']: return 'Photo introuvable',404
    return send_from_directory(UPLOAD_DIR,r['stored_name'],mimetype=r['mime'],download_name=r['original_name'])

@app.route('/documents')
def documents():
    con=db(); q=request.args.get('q','').strip(); kind=request.args.get('kind','').strip(); status=request.args.get('status','').strip(); sql='''select d.*,c.name client,coalesce(sum(l.qty*l.unit_price*(1-coalesce(l.discount_pct,0)/100.0)),0) total,(select coalesce(sum(amount),0) from payments p where p.doc_id=d.id) paid from docs d left join clients c on c.id=d.client_id left join lines l on l.doc_id=d.id where 1=1'''; params=[]
    if q: sql+=' and (d.number like ? or c.name like ? or d.reference like ?)'; params += [f'%{q}%']*3
    if kind: sql+=' and d.kind=?'; params.append(kind)
    if status: sql+=' and d.status=?'; params.append(status)
    sql+=' group by d.id order by d.id desc'; rows=con.execute(sql,params).fetchall(); con.close()
    trs=''.join(f"<tr><td>{r['kind']}</td><td><a href='/document/{r['id']}'>{r['number']}</a></td><td>{r['client'] or ''}</td><td>{r['doc_date'] or ''}</td><td class='right'>{money(r['total'])} Ar</td><td class='right'>{money(max(0,r['total']-r['paid']))} Ar</td><td><span class='badge'>{r['status']}</span></td></tr>" for r in rows)
    return page(f'''<div class="card"><div class="row"><h2 style="flex:1">Devis / Factures</h2><a class="btn2" href="{url_for('new_document')}">+ Nouveau document</a></div><form class="row" style="margin:12px 0"><input class="search" name="q" value="{q}" placeholder="N°, client, référence"><select name="kind" style="width:auto"><option value="">Tous types</option><option {'selected' if kind=='Devis' else ''}>Devis</option><option {'selected' if kind=='Facture' else ''}>Facture</option></select><select name="status" style="width:auto"><option value="">Tous statuts</option>{''.join(f'<option {"selected" if status==s else ""}>{s}</option>' for s in ['Brouillon','Envoyé','Accepté','Refusé','Facturé','Partiellement payé','Payé','Annulé'])}</select><button>Filtrer</button></form><div class="tablewrap"><table><tr><th>Type</th><th>N°</th><th>Client</th><th>Date</th><th class="right">Total</th><th class="right">Solde</th><th>Statut</th></tr>{trs}</table></div></div>''')

@app.route('/document/new',methods=['GET','POST'])
def new_document():
    con=db(); cls=con.execute('select * from clients order by name').fetchall()
    if request.method=='POST':
        kind=request.form['kind']; number=request.form.get('number') or next_number(kind)
        cur=con.execute('insert into docs(kind,number,doc_date,due_date,client_id,reference,po_number,payment_terms,delivery,status,notes,internal_note) values(?,?,?,?,?,?,?,?,?,?,?,?)',(kind,number,request.form.get('doc_date'),request.form.get('due_date'),request.form.get('client_id') or None,request.form.get('reference'),request.form.get('po_number'),request.form.get('payment_terms'),request.form.get('delivery'),'Brouillon',request.form.get('notes'),request.form.get('internal_note'))); did=cur.lastrowid
        for d,q,p,disc in zip(request.form.getlist('description'),request.form.getlist('qty'),request.form.getlist('unit_price'),request.form.getlist('discount_pct')):
            if d.strip(): con.execute('insert into lines(doc_id,description,qty,unit_price,discount_pct) values(?,?,?,?,?)',(did,d,parse_decimal(q),parse_decimal(p),max(0,min(100,parse_decimal(disc)))))
        for f in request.files.getlist('photos'):
            saved=save_upload(f,f'doc_{did}')
            if saved and saved[2].startswith('image/'): con.execute('insert into doc_images(doc_id,original_name,stored_name,mime) values(?,?,?,?)',(did,*saved))
        con.commit(); con.close(); return redirect(url_for('document',doc_id=did))
    con.close(); today=date.today(); due=today+timedelta(days=30); opts=''.join(f"<option value='{c['id']}'>{c['name']}</option>" for c in cls)
    rows=''.join("<tr><td><textarea name='description' style='min-width:260px'></textarea></td><td><input class='linefield qtyfield' name='qty' type='text' inputmode='decimal' autocomplete='off' value='1' placeholder='1'></td><td><input class='linefield pricefield' name='unit_price' type='text' inputmode='decimal' autocomplete='off' value='' placeholder='Ex. 125000'></td><td><input name='discount_pct' type='text' inputmode='decimal' autocomplete='off' value='0' placeholder='0'></td></tr>" for _ in range(8))
    return page(f'''<div class="card"><h2>Nouveau devis / facture</h2><form method="post" enctype="multipart/form-data"><div class="grid3"><div><label>Type</label><select name="kind"><option>Devis</option><option>Facture</option></select></div><div><label>Numéro</label><input name="number" placeholder="Automatique si vide"></div><div><label>Client</label><select name="client_id"><option value="">-- Choisir --</option>{opts}</select></div><div><label>Date</label><input type="date" name="doc_date" value="{today.isoformat()}"></div><div><label>Échéance</label><input type="date" name="due_date" value="{due.isoformat()}"></div><div><label>Bon de commande</label><input name="po_number"></div></div><p><label>Référence / objet</label><input name="reference" placeholder="Ex. TRAVAUX EFFECTUÉS SUR LE CHARIOT ÉLÉVATEUR 4A : Entretien"></p><div class="grid"><div><label>Modalité de règlement</label><input name="payment_terms" value="Virement sous 30 jours"></div><div><label>Livraison / travaux</label><input name="delivery"></div></div><p><label>Notes visibles / générales</label><textarea name="notes"></textarea></p><p><label>Note interne EMS — jamais affichée sur le PDF client</label><textarea name="internal_note" placeholder="Références internes, fournisseur, marge, n° série..."></textarea></p><p><label>Photos à insérer dans le devis</label><input type="file" name="photos" multiple accept="image/*"><span class="small muted">Tu peux choisir plusieurs photos depuis la galerie.</span></p><h3>Lignes</h3><div class="tablewrap"><table class="line-table"><thead><tr><th>Désignation</th><th>Qté</th><th>P.U. Ar</th><th>Remise %</th></tr></thead><tbody>{rows}</tbody></table></div><p><button>Créer le document</button></p></form></div>''')

@app.route('/document/<int:doc_id>')
def document(doc_id):
    con=db(); d=con.execute('''select d.*,c.name client,c.address,c.nif,c.stat,c.email,c.phone from docs d left join clients c on c.id=d.client_id where d.id=?''',(doc_id,)).fetchone(); lines=con.execute('select * from lines where doc_id=?',(doc_id,)).fetchall(); images=con.execute('select * from doc_images where doc_id=? order by id',(doc_id,)).fetchall(); images=con.execute('select * from doc_images where doc_id=? order by id',(doc_id,)).fetchall(); pays=con.execute('select * from payments where doc_id=? order by payment_date desc,id desc',(doc_id,)).fetchall();
    if not d: con.close(); return 'Document introuvable',404
    total=total_for(con,doc_id); paid=paid_for(con,doc_id); con.close(); balance=max(0,total-paid)
    trs=''.join(f"<tr><td>{l['description']}</td><td>{l['qty']:g}</td><td class='right'>{money(l['unit_price'])}</td><td class='right'>{float(l['discount_pct'] or 0):g}%</td><td class='right'>{money(l['qty']*l['unit_price']*(1-float(l['discount_pct'] or 0)/100))}</td></tr>" for l in lines)
    imgs=''.join(f"<a href='/document/{doc_id}/image/{im['id']}' target='_blank'><img src='/document/{doc_id}/image/{im['id']}' style='width:150px;height:110px;object-fit:cover;border-radius:8px;margin:5px;border:1px solid #ddd'></a>" for im in images) or '<span class="muted">Aucune photo.</span>'
    ptrs=''.join(f"<tr><td>{p['payment_date'] or ''}</td><td>{p['method'] or ''}</td><td>{p['note'] or ''}</td><td class='right'>{money(p['amount'])} Ar</td></tr>" for p in pays) or '<tr><td colspan="4" class="muted">Aucun règlement enregistré.</td></tr>'
    conv=f"<form method='post' action='/document/{doc_id}/convert'><button>Transformer en facture</button></form>" if d['kind']=='Devis' else ''
    payform=f'''<div class="card"><h3>Enregistrer un règlement</h3><form method="post" action="/document/{doc_id}/payment"><div class="grid3"><div><label>Date</label><input type="date" name="payment_date" value="{date.today().isoformat()}"></div><div><label>Montant (Ar)</label><input type="text" inputmode="decimal" autocomplete="off" name="amount" value="{int(balance)}" placeholder="Montant"></div><div><label>Mode</label><select name="method"><option>Virement</option><option>Espèces</option><option>Chèque</option><option>Mobile Money</option><option>Autre</option></select></div></div><p><label>Note</label><input name="note"></p><button class="success">Ajouter le règlement</button></form></div>''' if d['kind']=='Facture' else ''
    return page(f'''<div class="card"><div class="row"><div style="flex:1"><h2>{d['kind']} {d['number']}</h2><div class="muted">{d['client'] or 'Sans client'} • {d['doc_date'] or ''}</div></div><a class="btn2" href="/document/{doc_id}/edit">Modifier</a><a class="btn2" href="/document/{doc_id}/pdf">Télécharger PDF</a>{conv}</div><hr><div class="grid3"><div><div class="muted">Total</div><div class="total">{money(total)} Ar</div></div><div><div class="muted">Encaissé</div><div class="total">{money(paid)} Ar</div></div><div><div class="muted">Solde</div><div class="total">{money(balance)} Ar</div></div></div><p><b>Statut :</b> <span class="badge">{d['status']}</span> &nbsp; <b>Référence :</b> {d['reference'] or ''}</p><h3>Client</h3><div>{d['client'] or ''}<br>{(d['address'] or '').replace(chr(10),'<br>')}<br>NIF : {d['nif'] or ''}<br>STAT : {d['stat'] or ''}</div><h3>Détail</h3><div class="tablewrap"><table><tr><th>Désignation</th><th>Qté</th><th class="right">P.U.</th><th class="right">Remise</th><th class="right">Total</th></tr>{trs}<tr><td colspan="4" class="right"><b>TOTAL</b></td><td class="right total">{money(total)} Ar</td></tr></table></div><h3>Photos du devis</h3><div>{imgs}</div><div class="card" style="background:#fff8e8"><b>Note interne EMS (non visible client/PDF)</b><br>{(d['internal_note'] or '').replace(chr(10),'<br>') or '<span class=muted>Aucune note interne.</span>'}</div><p><b>Arrêté à la somme de :</b> {number_words(total)}.</p><p><b>Règlement :</b> {d['payment_terms'] or ''}<br><b>Bon de commande :</b> {d['po_number'] or ''}<br><b>Livraison :</b> {d['delivery'] or ''}</p></div>{payform}<div class="card"><h3>Règlements</h3><div class="tablewrap"><table><tr><th>Date</th><th>Mode</th><th>Note</th><th class="right">Montant</th></tr>{ptrs}</table></div></div>''')

@app.route('/document/<int:doc_id>/edit',methods=['GET','POST'])
def edit_document(doc_id):
    con=db(); d=con.execute('select * from docs where id=?',(doc_id,)).fetchone(); cls=con.execute('select * from clients order by name').fetchall(); lines=con.execute('select * from lines where doc_id=?',(doc_id,)).fetchall()
    if not d: con.close(); return 'Document introuvable',404
    if request.method=='POST':
        con.execute('update docs set kind=?,number=?,doc_date=?,due_date=?,client_id=?,reference=?,po_number=?,payment_terms=?,delivery=?,status=?,notes=?,internal_note=? where id=?',(request.form['kind'],request.form['number'],request.form.get('doc_date'),request.form.get('due_date'),request.form.get('client_id') or None,request.form.get('reference'),request.form.get('po_number'),request.form.get('payment_terms'),request.form.get('delivery'),request.form.get('status'),request.form.get('notes'),request.form.get('internal_note'),doc_id)); con.execute('delete from lines where doc_id=?',(doc_id,))
        for de,q,p,disc in zip(request.form.getlist('description'),request.form.getlist('qty'),request.form.getlist('unit_price'),request.form.getlist('discount_pct')):
            if de.strip(): con.execute('insert into lines(doc_id,description,qty,unit_price,discount_pct) values(?,?,?,?,?)',(doc_id,de,parse_decimal(q),parse_decimal(p),max(0,min(100,parse_decimal(disc)))))
        for f in request.files.getlist('photos'):
            saved=save_upload(f,f'doc_{doc_id}')
            if saved and saved[2].startswith('image/'): con.execute('insert into doc_images(doc_id,original_name,stored_name,mime) values(?,?,?,?)',(doc_id,*saved))
        con.commit(); con.close(); flash('Document mis à jour.'); return redirect(url_for('document',doc_id=doc_id))
    con.close(); opts=''.join(f"<option value='{c['id']}' {'selected' if c['id']==d['client_id'] else ''}>{c['name']}</option>" for c in cls); statuses=['Brouillon','Envoyé','Accepté','Refusé','Facturé','Partiellement payé','Payé','Annulé']; sopts=''.join(f"<option {'selected' if s==d['status'] else ''}>{s}</option>" for s in statuses)
    all_lines=list(lines)+[{'description':'','qty':1,'unit_price':0,'discount_pct':0} for _ in range(max(3,8-len(lines)))]; rows=''.join(f"<tr><td><textarea name='description' style='min-width:260px'>{str(l['description'] or '')}</textarea></td><td><input class='linefield qtyfield' name='qty' type='text' inputmode='decimal' autocomplete='off' value='{l['qty']:g}'></td><td><input class='linefield pricefield' name='unit_price' type='text' inputmode='decimal' autocomplete='off' value='{l['unit_price']:g}' placeholder='Ex. 125000'></td><td><input name='discount_pct' type='text' inputmode='decimal' autocomplete='off' value='{float(l['discount_pct'] or 0):g}'></td></tr>" for l in all_lines)
    return page(f'''<div class="card"><h2>Modifier {d['kind']} {d['number']}</h2><form method="post" enctype="multipart/form-data"><div class="grid3"><div><label>Type</label><select name="kind"><option {'selected' if d['kind']=='Devis' else ''}>Devis</option><option {'selected' if d['kind']=='Facture' else ''}>Facture</option></select></div><div><label>Numéro</label><input name="number" value="{d['number']}"></div><div><label>Statut</label><select name="status">{sopts}</select></div><div><label>Client</label><select name="client_id"><option value="">--</option>{opts}</select></div><div><label>Date</label><input type="date" name="doc_date" value="{d['doc_date'] or ''}"></div><div><label>Échéance</label><input type="date" name="due_date" value="{d['due_date'] or ''}"></div><div><label>Bon de commande</label><input name="po_number" value="{d['po_number'] or ''}"></div></div><p><label>Référence</label><input name="reference" value="{d['reference'] or ''}"></p><div class="grid"><div><label>Modalité de règlement</label><input name="payment_terms" value="{d['payment_terms'] or ''}"></div><div><label>Livraison / travaux</label><input name="delivery" value="{d['delivery'] or ''}"></div></div><p><label>Notes visibles / générales</label><textarea name="notes">{d['notes'] or ''}</textarea></p><p><label>Note interne EMS — jamais affichée sur le PDF client</label><textarea name="internal_note">{d['internal_note'] or ''}</textarea></p><p><label>Ajouter des photos au devis</label><input type="file" name="photos" multiple accept="image/*"></p><div class="tablewrap"><table class="line-table"><thead><tr><th>Désignation</th><th>Qté</th><th>P.U. Ar</th><th>Remise %</th></tr></thead><tbody>{rows}</tbody></table></div><p><button>Enregistrer</button></p></form></div>''')

@app.route('/document/<int:doc_id>/image/<int:iid>')
def document_image(doc_id,iid):
    con=db(); im=con.execute('select * from doc_images where id=? and doc_id=?',(iid,doc_id)).fetchone(); con.close()
    if not im:return 'Image introuvable',404
    return send_from_directory(UPLOAD_DIR,im['stored_name'],mimetype=im['mime'])

@app.post('/document/<int:doc_id>/convert')
def convert(doc_id):
    con=db(); d=con.execute('select * from docs where id=?',(doc_id,)).fetchone()
    if not d or d['kind']!='Devis':
        con.close(); flash('Ce document ne peut pas être transformé en facture.'); return redirect(url_for('document',doc_id=doc_id))
    new_number=next_number('Facture')
    cur=con.execute('''insert into docs(kind,number,doc_date,due_date,client_id,reference,po_number,payment_terms,delivery,status,notes,internal_note)
                       values(?,?,?,?,?,?,?,?,?,?,?,?)''',
                    ('Facture',new_number,date.today().isoformat(),d['due_date'],d['client_id'],d['reference'],d['po_number'],
                     d['payment_terms'],d['delivery'],'Brouillon',d['notes'],d['internal_note']))
    new_id=cur.lastrowid
    for l in con.execute('select * from lines where doc_id=?',(doc_id,)).fetchall():
        con.execute('insert into lines(doc_id,description,qty,unit_price,discount_pct) values(?,?,?,?,?)',
                    (new_id,l['description'],l['qty'],l['unit_price'],l['discount_pct']))
    for im in con.execute('select * from doc_images where doc_id=?',(doc_id,)).fetchall():
        con.execute('insert into doc_images(doc_id,original_name,stored_name,mime) values(?,?,?,?)',
                    (new_id,im['original_name'],im['stored_name'],im['mime']))
    con.execute("update docs set status='Facturé' where id=?",(doc_id,))
    con.commit(); con.close()
    flash(f'Facture {new_number} créée. Le devis original est conservé.')
    return redirect(url_for('document',doc_id=new_id))

@app.post('/document/<int:doc_id>/payment')
def add_payment(doc_id):
    amount=max(0,parse_decimal(request.form.get('amount'))); con=db(); con.execute('insert into payments(doc_id,payment_date,amount,method,note) values(?,?,?,?,?)',(doc_id,request.form.get('payment_date'),amount,request.form.get('method'),request.form.get('note'))); total=total_for(con,doc_id); paid=paid_for(con,doc_id); status='Payé' if paid>=total and total>0 else 'Partiellement payé'; con.execute('update docs set status=? where id=?',(status,doc_id)); con.commit(); con.close(); flash('Règlement enregistré.'); return redirect(url_for('document',doc_id=doc_id))

@app.post('/document/<int:doc_id>/deposit')
def add_deposit(doc_id):
    amount=max(0,parse_decimal(request.form.get('amount')))
    if amount<=0:
        flash('Indique un montant d’acompte.'); return redirect(url_for('document',doc_id=doc_id))
    con=db()
    con.execute('insert into payments(doc_id,payment_date,amount,method,note) values(?,?,?,?,?)',
                (doc_id,request.form.get('payment_date') or date.today().isoformat(),amount,request.form.get('method') or 'Acompte','Acompte'))
    total=total_for(con,doc_id); paid=paid_for(con,doc_id)
    status='Payé' if paid>=total and total>0 else 'Partiellement payé'
    con.execute('update docs set status=? where id=?',(status,doc_id)); con.commit(); con.close()
    flash('Acompte enregistré.')
    return redirect(url_for('document',doc_id=doc_id))

def draw_wrapped(c,text,x,y,maxw,font='Helvetica',size=9,leading=4.3*mm,max_lines=3):
    c.setFont(font,size); words=(text or '').split(); line=''; lines=[]
    for w in words:
        t=(line+' '+w).strip()
        if c.stringWidth(t,font,size)<=maxw: line=t
        else:
            if line: lines.append(line)
            line=w
    if line: lines.append(line)
    for ln in lines[:max_lines]: c.drawString(x,y,ln); y-=leading
    return y

@app.route('/document/<int:doc_id>/pdf')
def pdf(doc_id):
    con=db(); d=con.execute('''select d.*,c.name client,c.address,c.nif,c.stat,c.email,c.phone from docs d left join clients c on c.id=d.client_id where d.id=?''',(doc_id,)).fetchone(); lines=con.execute('select * from lines where doc_id=?',(doc_id,)).fetchall(); images=con.execute('select * from doc_images where doc_id=? order by id',(doc_id,)).fetchall(); total=total_for(con,doc_id); con.close()
    if not d:return 'Document introuvable',404
    safe=re.sub(r'[^A-Za-z0-9_-]+','-',d['number']); out=DATA_DIR/f"{d['kind']}_{safe}.pdf"; c=canvas.Canvas(str(out),pagesize=A4); W,H=A4; L=14*mm; R=W-14*mm; y=H-12*mm
    c.setStrokeColorRGB(.15,.15,.15); c.rect(10*mm,10*mm,W-20*mm,H-20*mm)
    if LOGO.exists(): c.drawImage(ImageReader(str(LOGO)),L,y-20*mm,width=62*mm,height=18.5*mm,preserveAspectRatio=True,mask='auto')
    c.setFont('Helvetica-Bold',12); c.drawString(116*mm,y-5*mm,f"{COMPANY['name']} {d['kind'].upper()} {d['number']}")
    c.setFont('Helvetica-Bold',9); c.drawString(116*mm,y-11*mm,f"Date : {d['doc_date'] or ''}"); c.drawString(116*mm,y-16*mm,f"Échéance : {d['due_date'] or ''}")
    y-=27*mm; c.setFillColorRGB(.68,.66,.66); c.rect(L,y, R-L,6*mm,fill=1,stroke=0); c.setFillColorRGB(0,0,0); c.setFont('Helvetica-Bold',9.5); c.drawString(L+2*mm,y+1.7*mm,'Émetteur :'); c.drawString(100*mm,y+1.7*mm,'Adresse de facturation :'); y-=2*mm
    boxh=38*mm; c.rect(L,y-boxh,R-L,boxh,fill=0,stroke=1); c.line(98*mm,y,98*mm,y-boxh)
    c.setFont('Helvetica-Bold',10); c.drawString(L+2*mm,y-5*mm,COMPANY['name']); c.setFont('Helvetica',8.5); yy=y-10*mm
    for ln in COMPANY['address'].split('\n'): c.drawString(L+2*mm,yy,ln); yy-=4.5*mm
    c.drawString(L+2*mm,yy-2*mm,f"NIF : {COMPANY['nif']}"); yy-=6*mm; c.drawString(L+2*mm,yy,f"STAT : {COMPANY['stat']}"); yy-=4.5*mm; c.drawString(L+2*mm,yy,f"Mail : {COMPANY['email']}"); yy-=4.5*mm; c.drawString(L+2*mm,yy,f"Tél : {COMPANY['phone']}")
    xx=100*mm; yy=y-5*mm; c.setFont('Helvetica-Bold',10); c.drawString(xx,yy,d['client'] or ''); yy-=5*mm; c.setFont('Helvetica',8.5)
    for ln in (d['address'] or '').split('\n'): c.drawString(xx,yy,ln); yy-=4.5*mm
    yy-=2*mm; c.drawString(xx,yy,f"NIF : {d['nif'] or ''}"); yy-=4.5*mm; c.drawString(xx,yy,f"STAT : {d['stat'] or ''}")
    y-=boxh+9*mm; c.setFont('Helvetica-Bold',10); y=draw_wrapped(c,'REF : '+(d['reference'] or ''),L,y,R-L,'Helvetica-Bold',10,5*mm,2); y-=4*mm
    # table header
    x1=L; x2=88*mm; x3=112*mm; x4=140*mm; x5=158*mm; x6=R; c.setFillColorRGB(.68,.66,.66); c.rect(x1,y-8*mm,x6-x1,8*mm,fill=1,stroke=1); c.setFillColorRGB(0,0,0); [c.line(x,y,x,y-8*mm) for x in [x2,x3,x4,x5]]; c.setFont('Helvetica-Bold',8.2); c.drawCentredString((x1+x2)/2,y-5.5*mm,'DÉSIGNATION'); c.drawCentredString((x2+x3)/2,y-5.5*mm,'QTS'); c.drawCentredString((x3+x4)/2,y-5.5*mm,'P.U'); c.drawCentredString((x4+x5)/2,y-5.5*mm,'REM.'); c.drawCentredString((x5+x6)/2,y-5.5*mm,'TOTAL Ar'); y-=8*mm
    c.setFont('Helvetica',8.2)
    for l in lines:
        h=8*mm; c.rect(x1,y-h,x6-x1,h,fill=0,stroke=1); [c.line(x,y,x,y-h) for x in [x2,x3,x4,x5]]; c.drawString(x1+2*mm,y-5.2*mm,(l['description'] or '')[:45]); c.drawRightString(x3-2*mm,y-5.2*mm,f"{l['qty']:g}"); c.drawRightString(x4-2*mm,y-5.2*mm,money(l['unit_price'])); c.drawRightString(x5-2*mm,y-5.2*mm,f"{float(l['discount_pct'] or 0):g}%"); net=l['qty']*l['unit_price']*(1-float(l['discount_pct'] or 0)/100); c.drawRightString(x6-2*mm,y-5.2*mm,money(net)); y-=h
    y-=1*mm; c.setFillColorRGB(.68,.66,.66); c.rect(100*mm,y-8*mm,R-100*mm,8*mm,fill=1,stroke=1); c.setFillColorRGB(0,0,0); c.setFont('Helvetica-Bold',9.5); c.drawString(103*mm,y-5.5*mm,'TOTAL en Ariary'); c.drawRightString(R-2*mm,y-5.5*mm,money(total)); y-=25*mm
    c.setFont('Helvetica-Bold',9); c.drawString(L+5*mm,y,'Arrêté à la somme de :'); c.setFont('Helvetica',9); c.drawString(L+44*mm,y,number_words(total)+'.')
    # bottom reference block fixed near bottom
    by=50*mm; c.setFillColorRGB(.7,.69,.69); c.rect(L,by,R-L,40*mm,fill=1,stroke=0); c.setFillColorRGB(.45,.43,.43); c.rect(L+2*mm,by+32*mm,R-L-4*mm,7*mm,fill=1,stroke=0); c.setFillColorRGB(0,0,0); c.setFont('Helvetica-Bold',9); c.drawString(L+4*mm,by+34*mm,'RÉFÉRENCE :'); c.setFont('Helvetica-Bold',8.5); c.drawString(L+4*mm,by+25*mm,f"Devis : {'-' if d['kind']=='Devis' else ''}"); c.drawString(L+4*mm,by+19*mm,f"Bon de commande : {d['po_number'] or ''}"); c.drawString(L+4*mm,by+13*mm,f"Modalité du règlement : {d['payment_terms'] or ''}"); c.drawString(L+4*mm,by+7*mm,f"Date et lieu de livraison : {d['delivery'] or ''}")
    c.setFont('Helvetica-Bold',9); c.drawString(L+10*mm,41*mm,'Coordonnées bancaires de la société :'); c.setFillColorRGB(.78,.77,.77); c.rect(L,19*mm,R-L,18*mm,fill=1,stroke=1); c.setFillColorRGB(0,0,0); c.setFont('Helvetica',8.5); c.drawString(L+2*mm,32*mm,f"Banque : {COMPANY['bank']}"); c.setFont('Helvetica-Bold',8); heads=['Code banque','Code guichet','N° de compte','Clé']; vals=[COMPANY['bank_code'],COMPANY['branch_code'],COMPANY['account'],COMPANY['key']]; xs=[L+30*mm,L+72*mm,L+120*mm,L+164*mm]
    for i,h in enumerate(heads): c.drawCentredString(xs[i],26*mm,h); c.setFont('Helvetica',8); c.drawCentredString(xs[i],21.5*mm,vals[i]); c.setFont('Helvetica-Bold',8)
    if images:
        c.showPage(); c.setFont('Helvetica-Bold',14); c.drawString(L,H-18*mm,f"Photos — {d['kind']} {d['number']}")
        px=L; py=H-32*mm; cellw=84*mm; cellh=62*mm; col=0
        for im in images:
            path=UPLOAD_DIR/im['stored_name']
            try:
                c.drawImage(ImageReader(str(path)),px,py-cellh,width=cellw,height=cellh,preserveAspectRatio=True,anchor='c',mask='auto')
                c.setFont('Helvetica',7); c.drawString(px,py-cellh-4*mm,(im['original_name'] or '')[:45])
            except Exception:
                c.setFont('Helvetica',8); c.drawString(px,py-8*mm,'Photo non compatible avec le PDF : '+(im['original_name'] or ''))
            col+=1
            if col%2==0: px=L; py-=78*mm
            else: px=L+94*mm
            if py<70*mm and col%2==0:
                c.showPage(); c.setFont('Helvetica-Bold',14); c.drawString(L,H-18*mm,'Photos (suite)'); px=L; py=H-32*mm
    c.showPage(); c.save(); return send_file(out,as_attachment=True,download_name=out.name)

if __name__=='__main__':
    init_db(); app.run(host='0.0.0.0',port=5000,debug=False)


# ===== SAUVEGARDE COMPLETE EMS =====
@app.route('/backup-complet')
def backup_complet():
    if not session.get('logged_in'):
        return redirect(url_for('login'))

    backup_dir = DATA_DIR / 'backups'
    backup_dir.mkdir(parents=True, exist_ok=True)

    backup_name = f"EMS_sauvegarde_{date.today().isoformat()}.zip"
    backup_path = backup_dir / backup_name

    # Copie SQLite cohérente même si EMS est utilisé
    db_copy = backup_dir / 'ems.db'
    source = sqlite3.connect(str(DB))
    destination = sqlite3.connect(str(db_copy))
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()

    try:
        with zipfile.ZipFile(
            backup_path, 'w', zipfile.ZIP_DEFLATED
        ) as archive:
            archive.write(db_copy, 'ems.db')

            if UPLOAD_DIR.exists():
                for fichier in UPLOAD_DIR.rglob('*'):
                    if fichier.is_file():
                        archive.write(
                            fichier,
                            str(Path('uploads') / fichier.relative_to(UPLOAD_DIR))
                        )
    finally:
        if db_copy.exists():
            db_copy.unlink()

    return send_file(
        backup_path,
        as_attachment=True,
        download_name=backup_name,
        mimetype='application/zip'
    )
# ===== FIN SAUVEGARDECOMPLETE EMS =====
# ===== RESTAURATION COMPLETE EMS =====
@app.route('/restauration', methods=['GET', 'POST'])
def restauration():
    if not session.get('logged_in'):
        return redirect(url_for('login'))

    if request.method == 'GET':
        return '''
        <!doctype html>
        <html lang="fr">
        <head>
            <meta name="viewport" content="width=device-width,initial-scale=1">
            <title>Restauration EMS</title>
        </head>
        <body style="font-family:Arial;padding:25px;max-width:600px;margin:auto">
            <h2>Restauration EMS</h2>
            <p><b>Attention :</b> cette opération remplace les données actuelles.</p>
            <form method="post" enctype="multipart/form-data">
                <input type="file" name="backup" accept=".zip" required>
                <br><br>
                <button type="submit"
                    style="padding:14px 20px;font-size:16px">
                    Restaurer la sauvegarde
                </button>
            </form>
        </body>
        </html>
        '''

    fichier = request.files.get('backup')
    if not fichier or not fichier.filename.lower().endswith('.zip'):
        return 'Fichier ZIP invalide', 400

    temp_dir = Path(tempfile.mkdtemp(prefix='ems_restore_'))

    try:
        zip_path = temp_dir / 'backup.zip'
        fichier.save(zip_path)

        with zipfile.ZipFile(zip_path, 'r') as archive:
            noms = archive.namelist()

            if 'ems.db' not in noms:
                return 'Sauvegarde invalide : ems.db absent', 400

            # Protection contre les chemins dangereux dans le ZIP
            for nom in noms:
                p = Path(nom)
                if p.is_absolute() or '..' in p.parts:
                    return 'Sauvegarde ZIP non autorisée', 400

            archive.extractall(temp_dir / 'contenu')

        contenu = temp_dir / 'contenu'
        nouvelle_db = contenu / 'ems.db'

        # Vérification de la base avant remplacement
        test_db = sqlite3.connect(str(nouvelle_db))
        try:
            resultat = test_db.execute('PRAGMA integrity_check').fetchone()
            if not resultat or resultat[0] != 'ok':
                return 'Base de données de sauvegarde endommagée', 400
        finally:
            test_db.close()

        # Sauvegarde automatique de sécurité avant restauration
        secours = DATA_DIR / 'avant_restauration'
        secours.mkdir(parents=True, exist_ok=True)

        if DB.exists():
            shutil.copy2(DB, secours / 'ems_avant_restauration.db')

        # Remplacement de la base
        shutil.copy2(nouvelle_db, DB)

        # Restauration des photos et pièces jointes
        nouvelles_uploads = contenu / 'uploads'
        if nouvelles_uploads.exists():
            if UPLOAD_DIR.exists():
                shutil.rmtree(UPLOAD_DIR)
            shutil.copytree(nouvelles_uploads, UPLOAD_DIR)

        return '''
        <h2>Restauration terminée avec succès</h2>
        <p>La base EMS, les photos et les pièces jointes ont été restaurées.</p>
        <p><a href="/">Retour à EMS</a></p>
        '''

    except zipfile.BadZipFile:
        return 'Le fichier de sauvegarde est invalide ou endommagé', 400
    except Exception as e:
        return 'Erreur pendant la restauration : ' + str(e), 500
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

# ===== FIN RESTAURATION COMPLETE EMS =====
