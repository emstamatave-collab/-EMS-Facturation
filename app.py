import sqlite3, os, re, hmac, uuid, shutil
from urllib.parse import quote
from pathlib import Path
from datetime import date, timedelta
from flask import Flask, render_template_string, request, redirect, url_for, send_file, flash, Response, jsonify, session
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader

BASE=Path(__file__).parent
DATA_DIR=Path(os.environ.get('EMS_DATA_DIR', str(BASE/'data')))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB=DATA_DIR/'ems.db'
LOGO=BASE/'logo_ems.png'
UPLOADS=DATA_DIR/'uploads'; UPLOADS.mkdir(parents=True, exist_ok=True)
app=Flask(__name__)
app.secret_key=os.environ.get('EMS_SECRET_KEY','change-this-secret-before-production')
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax', SESSION_COOKIE_SECURE=os.environ.get('EMS_HTTPS','0')=='1')
ADMIN_USER=os.environ.get('EMS_ADMIN_USER','admin')
ADMIN_PASSWORD=os.environ.get('EMS_ADMIN_PASSWORD','change-me')

COMPANY={
 'name':'EMS TMM','address':'Lot K4 107 LD Ivato\nAmbohidratrimo 105 - MADAGASCAR',
 'nif':'5019396150','stat':'45101 11 2025 0 11108','email':'emstamatave@gmail.com','phone':'+261 37 61 700 42',
 'bank':'XBred','bank_code':'00008','branch_code':'00021','account':'05003025816','key':'21'
}

def db():
    c=sqlite3.connect(DB); c.row_factory=sqlite3.Row; return c

def init_db():
    c=db(); c.executescript('''
    CREATE TABLE IF NOT EXISTS clients(id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT NOT NULL,address TEXT,nif TEXT,stat TEXT,email TEXT,phone TEXT);
    CREATE TABLE IF NOT EXISTS docs(id INTEGER PRIMARY KEY AUTOINCREMENT,kind TEXT NOT NULL,number TEXT NOT NULL,doc_date TEXT,due_date TEXT,client_id INTEGER,reference TEXT,po_number TEXT,payment_terms TEXT,delivery TEXT,status TEXT DEFAULT 'Brouillon',notes TEXT DEFAULT '',created_at TEXT DEFAULT CURRENT_TIMESTAMP,FOREIGN KEY(client_id) REFERENCES clients(id));
    CREATE TABLE IF NOT EXISTS lines(id INTEGER PRIMARY KEY AUTOINCREMENT,doc_id INTEGER,description TEXT,qty REAL DEFAULT 1,unit_price REAL DEFAULT 0,FOREIGN KEY(doc_id) REFERENCES docs(id));
    CREATE TABLE IF NOT EXISTS payments(id INTEGER PRIMARY KEY AUTOINCREMENT,doc_id INTEGER,payment_date TEXT,amount REAL DEFAULT 0,method TEXT,note TEXT,FOREIGN KEY(doc_id) REFERENCES docs(id));
    CREATE TABLE IF NOT EXISTS products(id INTEGER PRIMARY KEY AUTOINCREMENT,reference TEXT UNIQUE,name TEXT NOT NULL,description TEXT,price REAL DEFAULT 0,stock REAL DEFAULT 0,min_stock REAL DEFAULT 0,photo TEXT,active INTEGER DEFAULT 1,created_at TEXT DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE IF NOT EXISTS stock_moves(id INTEGER PRIMARY KEY AUTOINCREMENT,product_id INTEGER,move_date TEXT,qty REAL,move_type TEXT,note TEXT,FOREIGN KEY(product_id) REFERENCES products(id));
    CREATE TABLE IF NOT EXISTS signatures(id INTEGER PRIMARY KEY AUTOINCREMENT,doc_id INTEGER UNIQUE,signer_name TEXT,signature_data TEXT,signed_at TEXT DEFAULT CURRENT_TIMESTAMP,FOREIGN KEY(doc_id) REFERENCES docs(id));
    CREATE TABLE IF NOT EXISTS client_scans(id INTEGER PRIMARY KEY AUTOINCREMENT,client_id INTEGER,image_path TEXT,extracted_text TEXT,created_at TEXT DEFAULT CURRENT_TIMESTAMP,FOREIGN KEY(client_id) REFERENCES clients(id));
    ''');
    cols={r['name'] for r in c.execute('pragma table_info(docs)').fetchall()}
    for name,typ in [('source_doc_id','INTEGER'),('doc_subtype',"TEXT DEFAULT 'Standard'"),('deposit_percent','REAL DEFAULT 0'),('currency',"TEXT DEFAULT 'MGA'"),('vat_percent','REAL DEFAULT 0'),('discount_percent','REAL DEFAULT 0')]:
        if name not in cols: c.execute(f'alter table docs add column {name} {typ}')
    client_cols={r['name'] for r in c.execute('pragma table_info(clients)').fetchall()}
    if 'photo' not in client_cols: c.execute("alter table clients add column photo TEXT DEFAULT ''")
    c.commit(); c.close()

# Initialise automatiquement la base, y compris avec Gunicorn/Render.
init_db()

def automatic_backup():
    if not DB.exists(): return
    bdir=DATA_DIR/'backups'; bdir.mkdir(parents=True,exist_ok=True)
    target=bdir/f"ems_{date.today().isoformat()}.db"
    if not target.exists(): shutil.copy2(DB,target)

automatic_backup()

def money(v, currency='MGA'):
    v=float(v or 0)
    if currency=='EUR': return f"{v:,.2f}".replace(',', ' ').replace('.', ',')
    return f"{v:,.0f}".replace(',', ' ')

def currency_label(currency): return '€' if currency=='EUR' else 'Ar'

def subtotal_for(con, doc_id):
    return float(con.execute('select coalesce(sum(qty*unit_price),0) t from lines where doc_id=?',(doc_id,)).fetchone()['t'] or 0)

def totals_for(con, doc_id):
    d=con.execute('select currency,vat_percent,discount_percent from docs where id=?',(doc_id,)).fetchone()
    subtotal=subtotal_for(con,doc_id)
    discount_percent=float((d['discount_percent'] if d else 0) or 0)
    vat_percent=float((d['vat_percent'] if d else 0) or 0)
    discount=subtotal*discount_percent/100
    net_ht=subtotal-discount
    vat=net_ht*vat_percent/100
    total=net_ht+vat
    return {'subtotal':subtotal,'discount_percent':discount_percent,'discount':discount,'net_ht':net_ht,'vat_percent':vat_percent,'vat':vat,'total':total,'currency':(d['currency'] if d and d['currency'] else 'MGA')}

def total_for(con, doc_id): return totals_for(con,doc_id)['total']

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
def number_words(n, currency='MGA'):
    n=int(round(n)); original_n=n
    if n==0:return 'zéro euro' if currency=='EUR' else 'zéro Ariary'
    parts=[]
    for value,name in [(1_000_000_000,'milliard'),(1_000_000,'million'),(1000,'mille')]:
        if n>=value:
            q,n=divmod(n,value)
            if value==1000 and q==1: parts.append('mille')
            else: parts.append(under1000(q)+' '+name+('s' if q>1 and value!=1000 else ''))
    if n: parts.append(under1000(n))
    s=' '.join(parts)
    suffix=(' euro' if original_n==1 else ' euros') if currency=='EUR' else ' Ariary'
    return s[:1].upper()+s[1:]+suffix

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
input,select,textarea{width:100%;padding:10px;border:1px solid #cfd4dc;border-radius:8px;background:white}textarea{min-height:70px}.designation{min-height:110px;resize:vertical;min-width:330px}label{font-size:13px;font-weight:650;display:block;margin-bottom:5px}
button{padding:10px 14px;border:0;border-radius:8px;background:#111;color:#fff;cursor:pointer;font-weight:700}.danger{background:var(--red)}.success{background:var(--green)}
table{width:100%;border-collapse:collapse}th,td{padding:10px;border-bottom:1px solid #eee;text-align:left;vertical-align:top}th{font-size:12px;color:#555;text-transform:uppercase}.right{text-align:right}.muted{color:var(--muted)}.total{font-size:22px;font-weight:800}.kpi{font-size:28px;font-weight:800}.badge{display:inline-block;padding:4px 8px;border-radius:999px;background:#eee;font-size:12px}.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}.flash{background:#fff6d8;border:1px solid #f5d98c;padding:10px;border-radius:8px;margin:12px 0}.search{max-width:320px}.small{font-size:12px}.nowrap{white-space:nowrap}.low{color:#b91c1c;font-weight:800}.ok{color:#166534;font-weight:800}.actions{display:flex;gap:8px;flex-wrap:wrap}.actions form{margin:0}.btn{display:inline-block;padding:10px 14px;border-radius:8px;background:#111;color:#fff;text-decoration:none;font-weight:700}.btn.alt{background:#fff;color:#111;border:1px solid var(--line)}.btn.green{background:#166534}.sigcanvas{border:1px solid #bbb;border-radius:8px;width:100%;max-width:520px;height:180px;touch-action:none;background:#fff}
@media(max-width:760px){.grid,.grid3,.grid4{grid-template-columns:1fr}.top{position:static}.tablewrap{overflow-x:auto}.wrap{margin-top:12px}.nav{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}.nav a{width:100%;text-align:center;padding:12px 8px}.card{padding:14px}.designation{min-width:260px;min-height:120px}}
'''
TPL='''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>EMS Facturation</title><meta name="theme-color" content="#111111"><meta name="apple-mobile-web-app-capable" content="yes"><meta name="apple-mobile-web-app-status-bar-style" content="black-translucent"><meta name="apple-mobile-web-app-title" content="EMS"><link rel="manifest" href="/manifest.webmanifest"><link rel="apple-touch-icon" href="/icon-180.png"><link rel="icon" href="/icon-192.png"><style>'''+STYLE+'''</style></head><body>
<div class="top">{% if logo %}<img src="{{url_for('logo')}}">{% endif %}<div><div class="brand">EMS — Devis & Facturation</div><div class="sub">Gestion commerciale • Ariary (MGA) & Euro (EUR)</div></div></div>
<div class="wrap"><div class="nav"><a href="{{url_for('home')}}">Tableau de bord</a><a href="{{url_for('clients')}}">Clients</a><a href="{{url_for('documents')}}">Devis / Factures</a><a href="{{url_for('products')}}">Catalogue / Stock</a><a href="{{url_for('global_search')}}">Recherche</a><a href="{{url_for('reminders')}}">Relances</a><a href="{{url_for('new_document')}}">+ Nouveau</a><a href="{{url_for('backup_db')}}">Sauvegarde</a><a href="{{url_for('logout')}}">Déconnexion</a></div>
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
    return send_file(DB, as_attachment=True, download_name='ems_backup.db')

@app.route('/logo.png')
def logo(): return send_file(LOGO)

@app.route('/uploads/<path:name>')
def uploaded_file(name):
    safe=Path(name).name
    path=UPLOADS/safe
    if not path.exists(): return 'Image introuvable',404
    return send_file(path)

def save_image(file_storage, prefix='img'):
    if not file_storage or not getattr(file_storage,'filename',''):
        return ''
    ext=(Path(file_storage.filename).suffix or '.jpg').lower()
    if ext not in ['.jpg','.jpeg','.png','.webp','.heic','.heif']:
        ext='.jpg'
    name=f"{prefix}_{uuid.uuid4().hex[:12]}{ext}"
    file_storage.save(UPLOADS/name)
    return name

def extract_contact_fields(text):
    text=(text or '').replace('\r','\n')
    lines=[re.sub(r'\s+',' ',x).strip(' •|;') for x in text.split('\n') if re.sub(r'\s+',' ',x).strip()]
    email_m=re.search(r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}',text)
    phone_m=re.search(r'(?:\+?261|0)[\d\s().-]{7,}',text)
    nif_m=re.search(r'(?i)\bNIF\b\s*[:\-]?\s*([0-9 ]{6,})',text)
    stat_m=re.search(r'(?i)\bSTAT\b\s*[:\-]?\s*([0-9 ]{6,})',text)
    skip=re.compile(r'(?i)^(tel|tél|phone|mobile|mail|email|e-mail|nif|stat|www|http|adresse|address)\b')
    company=''
    for ln in lines[:8]:
        if '@' in ln or skip.search(ln) or sum(ch.isdigit() for ch in ln)>5: continue
        if len(ln)>=3:
            company=ln; break
    address_candidates=[]
    for ln in lines:
        low=ln.lower()
        if ln==company or '@' in ln or 'nif' in low or 'stat' in low or 'tel' in low or 'phone' in low or 'mobile' in low or 'www.' in low or 'http' in low: continue
        if re.search(r'\b(rue|route|avenue|av\.|lot|quartier|bp|boulevard|bd\.|immeuble|zone|madagascar|tamatave|toamasina|antananarivo|ivato)\b',low) or re.search(r'\d',ln):
            address_candidates.append(ln)
    return {'name':company,'email':email_m.group(0) if email_m else '','phone':phone_m.group(0).strip() if phone_m else '','nif':nif_m.group(1).strip() if nif_m else '','stat':stat_m.group(1).strip() if stat_m else '','address':'\n'.join(address_candidates[:4])}

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
    js = '''const CACHE='ems-v3';
const CORE=['/','/manifest.webmanifest','/icon-192.png','/icon-512.png'];
self.addEventListener('install',e=>e.waitUntil(caches.open(CACHE).then(c=>c.addAll(CORE))));
self.addEventListener('activate',e=>e.waitUntil(caches.keys().then(ks=>Promise.all(ks.filter(k=>k!==CACHE).map(k=>caches.delete(k))))));
self.addEventListener('fetch',e=>{if(e.request.method!=='GET')return;e.respondWith(fetch(e.request).then(r=>{const cp=r.clone();caches.open(CACHE).then(c=>c.put(e.request,cp));return r;}).catch(()=>caches.match(e.request).then(r=>r||caches.match('/'))));});'''
    return Response(js, mimetype='application/javascript', headers={'Cache-Control':'no-cache'})

@app.route('/health')
def health(): return {'ok': True, 'app':'EMS Facturation V5 Complete'}

@app.route('/')
def home():
    con=db(); clients=con.execute('select count(*) n from clients').fetchone()['n']; devis=con.execute("select count(*) n from docs where kind='Devis'").fetchone()['n']; fact=con.execute("select count(*) n from docs where kind='Facture'").fetchone()['n']
    billed=con.execute("select coalesce(sum(l.qty*l.unit_price),0) t from lines l join docs d on d.id=l.doc_id where d.kind='Facture'").fetchone()['t']; paid=con.execute("select coalesce(sum(p.amount),0) t from payments p join docs d on d.id=p.doc_id where d.kind='Facture'").fetchone()['t']; recent=con.execute('''select d.*,c.name client,coalesce(sum(l.qty*l.unit_price),0) total from docs d left join clients c on c.id=d.client_id left join lines l on l.doc_id=d.id group by d.id order by d.id desc limit 6''').fetchall(); con.close()
    trs=''.join(f"<tr><td>{r['kind']}</td><td><a href='/document/{r['id']}'>{r['number']}</a></td><td>{r['client'] or ''}</td><td class='right'>{money(r['total'])} Ar</td><td><span class='badge'>{r['status']}</span></td></tr>" for r in recent)
    return page(f'''<div class="grid4"><div class="card"><div class="muted">Clients</div><div class="kpi">{clients}</div></div><div class="card"><div class="muted">Devis</div><div class="kpi">{devis}</div></div><div class="card"><div class="muted">Factures</div><div class="kpi">{fact}</div></div><div class="card"><div class="muted">Reste à encaisser</div><div class="kpi">{money(max(0,billed-paid))} Ar</div></div></div><div class="card"><h2>Activité</h2><div class="grid"><div><div class="muted">Total facturé</div><div class="total">{money(billed)} Ar</div></div><div><div class="muted">Total encaissé</div><div class="total">{money(paid)} Ar</div></div></div></div><div class="card"><h2>Documents récents</h2><div class="tablewrap"><table><tr><th>Type</th><th>N°</th><th>Client</th><th class="right">Total</th><th>Statut</th></tr>{trs}</table></div></div>''')

@app.route('/clients',methods=['GET','POST'])
def clients():
    con=db()
    if request.method=='POST':
        photo_name=request.form.get('scan_image','')
        uploaded=request.files.get('photo')
        if uploaded and uploaded.filename: photo_name=save_image(uploaded,'client')
        cur=con.execute('insert into clients(name,address,nif,stat,email,phone,photo) values(?,?,?,?,?,?,?)',(request.form['name'],request.form.get('address'),request.form.get('nif'),request.form.get('stat'),request.form.get('email'),request.form.get('phone'),photo_name)); cid=cur.lastrowid
        scan_text=request.form.get('scan_text','')
        if scan_text or request.form.get('scan_image'):
            con.execute('insert into client_scans(client_id,image_path,extracted_text) values(?,?,?)',(cid,request.form.get('scan_image',''),scan_text))
        con.commit(); con.close(); flash('Client ajouté.'); return redirect(url_for('client_history',cid=cid))
    q=request.args.get('q','').strip(); rows=con.execute('select * from clients where name like ? or nif like ? order by name',(f'%{q}%',f'%{q}%')).fetchall() if q else con.execute('select * from clients order by name').fetchall(); con.close()
    trs=''.join(f"<tr><td>{('<img src=/uploads/'+r['photo']+' style=\"width:42px;height:42px;object-fit:cover;border-radius:8px\">') if r['photo'] else ''}</td><td><a href='/client/{r['id']}'>{r['name']}</a></td><td>{r['nif'] or ''}</td><td>{r['stat'] or ''}</td><td>{r['phone'] or ''}</td><td>{r['email'] or ''}</td></tr>" for r in rows)
    return page(f'''<div class="card"><div class="row"><h2 style="flex:1">Nouveau client</h2><a class="btn" href="/client/scan-new">📷 Scanner un contact</a></div><form method="post" enctype="multipart/form-data"><div class="grid3"><div><label>Nom / société</label><input name="name" required></div><div><label>Téléphone</label><input name="phone"></div><div><label>Email</label><input name="email"></div><div><label>NIF</label><input name="nif"></div><div><label>STAT</label><input name="stat"></div><div><label>Photo / logo du client</label><input type="file" name="photo" accept="image/*" capture="environment"></div></div><p><label>Adresse</label><textarea name="address"></textarea></p><p><button>Ajouter le client</button></p></form></div><div class="card"><div class="row"><h2 style="flex:1">Clients</h2><form><input class="search" name="q" value="{q}" placeholder="Rechercher nom ou NIF"></form></div><div class="tablewrap"><table><tr><th>Photo</th><th>Nom</th><th>NIF</th><th>STAT</th><th>Téléphone</th><th>Email</th></tr>{trs}</table></div></div>''')

@app.route('/client/scan-new',methods=['GET','POST'])
def scan_new_client():
    if request.method=='POST':
        f=request.files.get('scan')
        if not f: flash('Choisis ou prends une photo.'); return redirect(url_for('scan_new_client'))
        image_name=save_image(f,'scan_contact')
        text=(request.form.get('ocr_text') or '').strip()
        if not text:
            try:
                import pytesseract
                from PIL import Image
                text=pytesseract.image_to_string(Image.open(UPLOADS/image_name),lang='fra+eng')
            except Exception:
                text=''
        fields=extract_contact_fields(text)
        esc=lambda x:(x or '').replace('&','&amp;').replace('"','&quot;').replace('<','&lt;').replace('>','&gt;')
        return page(f'''<div class="card"><h2>Vérifier le contact reconnu</h2><p class="muted">La photo a été analysée. Corrige les champs si nécessaire avant d'enregistrer.</p><div class="row"><img class="clientphoto" src="/uploads/{image_name}"><div><b>Photo scannée</b><div class="small muted">Elle sera conservée dans la fiche client.</div></div></div><form method="post" action="/clients" enctype="multipart/form-data"><input type="hidden" name="scan_image" value="{image_name}"><textarea name="scan_text" style="display:none">{esc(text)}</textarea><div class="grid3"><div><label>Nom / société</label><input name="name" value="{esc(fields['name'])}" required></div><div><label>Téléphone</label><input name="phone" value="{esc(fields['phone'])}"></div><div><label>Email</label><input name="email" value="{esc(fields['email'])}"></div><div><label>NIF</label><input name="nif" value="{esc(fields['nif'])}"></div><div><label>STAT</label><input name="stat" value="{esc(fields['stat'])}"></div><div><label>Autre photo / logo (facultatif)</label><input type="file" name="photo" accept="image/*"></div></div><p><label>Adresse</label><textarea name="address">{esc(fields['address'])}</textarea></p><p><button>Créer le client</button> <a class="btn alt" href="/client/scan-new">Recommencer</a></p></form></div><div class="card"><details><summary>Texte reconnu</summary><pre style="white-space:pre-wrap">{esc(text) if text else 'Aucun texte reconnu automatiquement. Tu peux remplir les champs manuellement.'}</pre></details></div>''')
    return page('''<div class="card"><h2>📷 Scanner un contact</h2><p>Prends en photo une carte de visite, un en-tête de facture, un document société ou sélectionne une image. EMS essaiera de reconnaître automatiquement le nom, téléphone, e-mail, adresse, NIF et STAT.</p><form method="post" enctype="multipart/form-data" id="scanForm"><div class="scanbox"><label>Photo du contact</label><input id="scanFile" type="file" name="scan" accept="image/*" capture="environment" required><textarea id="ocrText" name="ocr_text" style="display:none"></textarea><div id="ocrProgress" class="progress">Après avoir choisi la photo, appuie sur « Analyser ».</div></div><p><button type="button" id="analyzeBtn">Analyser la photo</button></p></form></div><script src="https://cdn.jsdelivr.net/npm/tesseract.js@5/dist/tesseract.min.js"></script><script>const b=document.getElementById('analyzeBtn'),f=document.getElementById('scanFile'),p=document.getElementById('ocrProgress'),t=document.getElementById('ocrText'),form=document.getElementById('scanForm');b.onclick=async()=>{if(!f.files.length){alert('Choisis une photo.');return}b.disabled=true;p.textContent='Reconnaissance en cours…';try{const r=await Tesseract.recognize(f.files[0],'fra+eng',{logger:m=>{if(m.status==='recognizing text')p.textContent='Reconnaissance : '+Math.round((m.progress||0)*100)+' %';}});t.value=r.data.text||'';p.textContent='Analyse terminée. Ouverture des coordonnées reconnues…';}catch(e){p.textContent='Reconnaissance locale indisponible. EMS va essayer côté serveur.';}form.submit();};</script>''')

@app.route('/client/<int:cid>/edit',methods=['GET','POST'])
def edit_client(cid):
    con=db(); r=con.execute('select * from clients where id=?',(cid,)).fetchone()
    if not r: con.close(); return 'Client introuvable',404
    if request.method=='POST':
        photo_name=r['photo'] or ''
        f=request.files.get('photo')
        if f and f.filename: photo_name=save_image(f,f'client_{cid}')
        con.execute('update clients set name=?,address=?,nif=?,stat=?,email=?,phone=?,photo=? where id=?',(request.form['name'],request.form.get('address'),request.form.get('nif'),request.form.get('stat'),request.form.get('email'),request.form.get('phone'),photo_name,cid)); con.commit(); con.close(); flash('Client mis à jour.'); return redirect(url_for('client_history',cid=cid))
    con.close(); photo_html=f'<img class="clientphoto" src="/uploads/{r["photo"]}">' if r['photo'] else '<div class="clientphoto" style="display:grid;place-items:center">Aucune photo</div>'
    return page(f'''<div class="card"><h2>Modifier le client</h2><div class="row">{photo_html}<div class="muted">Tu peux remplacer la photo ou le logo ci-dessous.</div></div><form method="post" enctype="multipart/form-data"><div class="grid"><div><label>Nom</label><input name="name" value="{r['name'] or ''}" required></div><div><label>Téléphone</label><input name="phone" value="{r['phone'] or ''}"></div><div><label>Email</label><input name="email" value="{r['email'] or ''}"></div><div><label>NIF</label><input name="nif" value="{r['nif'] or ''}"></div><div><label>STAT</label><input name="stat" value="{r['stat'] or ''}"></div><div><label>Photo / logo</label><input type="file" name="photo" accept="image/*" capture="environment"></div><div><label>Adresse</label><textarea name="address">{r['address'] or ''}</textarea></div></div><p><button>Enregistrer</button></p></form></div>''')

@app.route('/documents')
def documents():
    con=db(); q=request.args.get('q','').strip(); kind=request.args.get('kind','').strip(); status=request.args.get('status','').strip(); sql='''select d.*,c.name client,coalesce(sum(l.qty*l.unit_price),0) total,(select coalesce(sum(amount),0) from payments p where p.doc_id=d.id) paid from docs d left join clients c on c.id=d.client_id left join lines l on l.doc_id=d.id where 1=1'''; params=[]
    if q: sql+=' and (d.number like ? or c.name like ? or d.reference like ?)'; params += [f'%{q}%']*3
    if kind: sql+=' and d.kind=?'; params.append(kind)
    if status: sql+=' and d.status=?'; params.append(status)
    sql+=' group by d.id order by d.id desc'; rows=con.execute(sql,params).fetchall(); con.close()
    trs=''.join(f"<tr><td>{r['kind']}</td><td><a href='/document/{r['id']}'>{r['number']}</a></td><td>{r['client'] or ''}</td><td>{r['doc_date'] or ''}</td><td class='right'>{money(r['total'],r['currency'] or 'MGA')} {currency_label(r['currency'] or 'MGA')}</td><td class='right'>{money(max(0,r['total']-r['paid']),r['currency'] or 'MGA')} {currency_label(r['currency'] or 'MGA')}</td><td><span class='badge'>{r['status']}</span></td></tr>" for r in rows)
    return page(f'''<div class="card"><div class="row"><h2 style="flex:1">Devis / Factures</h2><a class="btn2" href="{url_for('new_document')}">+ Nouveau document</a></div><form class="row" style="margin:12px 0"><input class="search" name="q" value="{q}" placeholder="N°, client, référence"><select name="kind" style="width:auto"><option value="">Tous types</option><option {'selected' if kind=='Devis' else ''}>Devis</option><option {'selected' if kind=='Facture' else ''}>Facture</option></select><select name="status" style="width:auto"><option value="">Tous statuts</option>{''.join(f'<option {"selected" if status==s else ""}>{s}</option>' for s in ['Brouillon','Envoyé','Accepté','Refusé','Facturé','Partiellement payé','Payé','Annulé'])}</select><button>Filtrer</button></form><div class="tablewrap"><table><tr><th>Type</th><th>N°</th><th>Client</th><th>Date</th><th class="right">Total</th><th class="right">Solde</th><th>Statut</th></tr>{trs}</table></div></div>''')

@app.route('/document/new',methods=['GET','POST'])
def new_document():
    con=db(); cls=con.execute('select * from clients order by name').fetchall()
    if request.method=='POST':
        kind=request.form['kind']; number=request.form.get('number') or next_number(kind)
        cur=con.execute('insert into docs(kind,number,doc_date,due_date,client_id,reference,po_number,payment_terms,delivery,status,notes,currency,vat_percent,discount_percent) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(kind,number,request.form.get('doc_date'),request.form.get('due_date'),request.form.get('client_id') or None,request.form.get('reference'),request.form.get('po_number'),request.form.get('payment_terms'),request.form.get('delivery'),'Brouillon',request.form.get('notes'),request.form.get('currency') or 'MGA',float(request.form.get('vat_percent') or 0),float(request.form.get('discount_percent') or 0))); did=cur.lastrowid
        for d,q,p in zip(request.form.getlist('description'),request.form.getlist('qty'),request.form.getlist('unit_price')):
            if d.strip(): con.execute('insert into lines(doc_id,description,qty,unit_price) values(?,?,?,?)',(did,d,float(q or 0),float(p or 0)))
        con.commit(); con.close(); return redirect(url_for('document',doc_id=did))
    con.close(); today=date.today(); due=today+timedelta(days=30); pre=request.args.get('client_id',''); opts=''.join(f"<option value='{c['id']}' {'selected' if str(c['id'])==str(pre) else ''}>{c['name']}</option>" for c in cls)
    rows=''.join("<tr><td><textarea class='designation' name='description' placeholder='Décrivez le matériel : marque, modèle, capacité, mât, hauteur, année, heures, accessoires, état...'></textarea></td><td><input name='qty' type='number' step='0.01' value='1'></td><td><input name='unit_price' type='number' step='1' value='0'></td></tr>" for _ in range(8))
    return page(f'''<div class="card"><h2>Nouveau devis / facture</h2><form method="post"><div class="grid3"><div><label>Type</label><select name="kind"><option>Devis</option><option>Facture</option></select></div><div><label>Numéro</label><input name="number" placeholder="Automatique si vide"></div><div><label>Client</label><select name="client_id"><option value="">-- Choisir --</option>{opts}</select></div><div><label>Date</label><input type="date" name="doc_date" value="{today.isoformat()}"></div><div><label>Échéance</label><input type="date" name="due_date" value="{due.isoformat()}"></div><div><label>Bon de commande</label><input name="po_number"></div><div><label>Devise</label><select name="currency"><option value="MGA">Ariary (Ar)</option><option value="EUR">Euro (€)</option></select></div><div><label>TVA (%)</label><input type="number" name="vat_percent" min="0" step="0.01" value="0"></div><div><label>Remise globale (%)</label><input type="number" name="discount_percent" min="0" max="100" step="0.01" value="0"></div></div><p><label>Référence / objet</label><input name="reference" placeholder="Ex. TRAVAUX EFFECTUÉS SUR LE CHARIOT ÉLÉVATEUR 4A : Entretien"></p><div class="grid"><div><label>Modalité de règlement</label><input name="payment_terms" value="Virement sous 30 jours"></div><div><label>Livraison / travaux</label><input name="delivery"></div></div><p><label>Notes</label><textarea name="notes"></textarea></p><h3>Lignes</h3><div class="tablewrap"><table><tr><th>Désignation</th><th>Qté</th><th>P.U. (devise choisie)</th></tr>{rows}</table></div><p><button>Créer le document</button></p></form></div>''')

@app.route('/document/<int:doc_id>')
def document(doc_id):
    con=db(); d=con.execute('''select d.*,c.name client,c.address,c.nif,c.stat,c.email,c.phone from docs d left join clients c on c.id=d.client_id where d.id=?''',(doc_id,)).fetchone(); lines=con.execute('select * from lines where doc_id=?',(doc_id,)).fetchall(); pays=con.execute('select * from payments where doc_id=? order by payment_date desc,id desc',(doc_id,)).fetchall();
    if not d: con.close(); return 'Document introuvable',404
    calc=totals_for(con,doc_id); total=calc['total']; currency=calc['currency']; symbol=currency_label(currency); paid=paid_for(con,doc_id); con.close(); balance=max(0,total-paid)
    trs=''.join(f"<tr><td>{l['description']}</td><td>{l['qty']:g}</td><td class='right'>{money(l['unit_price'],currency)}</td><td class='right'>{money(l['qty']*l['unit_price'],currency)}</td></tr>" for l in lines)
    ptrs=''.join(f"<tr><td>{p['payment_date'] or ''}</td><td>{p['method'] or ''}</td><td>{p['note'] or ''}</td><td class='right'>{money(p['amount'],currency)} {symbol}</td></tr>" for p in pays) or '<tr><td colspan="4" class="muted">Aucun règlement enregistré.</td></tr>'
    conv=(f"<form method='post' action='/document/{doc_id}/convert'><button>Transformer en facture</button></form><form method='post' action='/document/{doc_id}/deposit'><input name='percent' type='number' value='30' min='1' max='100' style='width:80px'><button>Créer acompte %</button></form><a class='btn green' href='/document/{doc_id}/signature'>Signer</a>" if d['kind']=='Devis' else '')
    payform=f'''<div class="card"><h3>Enregistrer un règlement</h3><form method="post" action="/document/{doc_id}/payment"><div class="grid3"><div><label>Date</label><input type="date" name="payment_date" value="{date.today().isoformat()}"></div><div><label>Montant ({symbol})</label><input type="number" name="amount" value="{balance:.2f}" min="0"></div><div><label>Mode</label><select name="method"><option>Virement</option><option>Espèces</option><option>Chèque</option><option>Mobile Money</option><option>Autre</option></select></div></div><p><label>Note</label><input name="note"></p><button class="success">Ajouter le règlement</button></form></div>''' if d['kind']=='Facture' else ''
    return page(f'''<div class="card"><div class="row"><div style="flex:1"><h2>{d['kind']} {d['number']}</h2><div class="muted">{d['client'] or 'Sans client'} • {d['doc_date'] or ''}</div></div><a class="btn2" href="/document/{doc_id}/edit">Modifier</a><a class="btn2" href="/document/{doc_id}/pdf">Télécharger PDF</a><a class="btn2" href="/document/{doc_id}/share">Partager</a>{conv}</div><hr><div class="grid3"><div><div class="muted">Total TTC</div><div class="total">{money(total,currency)} {symbol}</div></div><div><div class="muted">Encaissé</div><div class="total">{money(paid,currency)} {symbol}</div></div><div><div class="muted">Solde</div><div class="total">{money(balance,currency)} {symbol}</div></div></div><div class="card" style="margin-top:12px"><div class="grid3"><div><span class="muted">Sous-total HT</span><br><b>{money(calc['subtotal'],currency)} {symbol}</b></div><div><span class="muted">Remise {calc['discount_percent']:g}%</span><br><b>- {money(calc['discount'],currency)} {symbol}</b></div><div><span class="muted">TVA {calc['vat_percent']:g}%</span><br><b>{money(calc['vat'],currency)} {symbol}</b></div></div></div><p><b>Statut :</b> <span class="badge">{d['status']}</span> &nbsp; <b>Référence :</b> {d['reference'] or ''}</p><form method='post' action='/document/{doc_id}/status' class='row'><label style='margin:0'>Changer le statut</label><select name='status' style='width:auto'><option>{d['status']}</option><option>Brouillon</option><option>Envoyé</option><option>Accepté</option><option>Commandé</option><option>Livré</option><option>Facturé</option><option>Partiellement payé</option><option>Payé</option></select><button>Mettre à jour</button></form><h3>Client</h3><div>{d['client'] or ''}<br>{(d['address'] or '').replace(chr(10),'<br>')}<br>NIF : {d['nif'] or ''}<br>STAT : {d['stat'] or ''}</div><h3>Détail</h3><div class="tablewrap"><table><tr><th>Désignation</th><th>Qté</th><th class="right">P.U.</th><th class="right">Total</th></tr>{trs}<tr><td colspan="3" class="right"><b>TOTAL TTC</b></td><td class="right total">{money(total,currency)} {symbol}</td></tr></table></div><p><b>Arrêté à la somme de :</b> {number_words(total,currency)}.</p><p><b>Règlement :</b> {d['payment_terms'] or ''}<br><b>Bon de commande :</b> {d['po_number'] or ''}<br><b>Livraison :</b> {d['delivery'] or ''}</p></div>{payform}<div class="card"><h3>Règlements</h3><div class="tablewrap"><table><tr><th>Date</th><th>Mode</th><th>Note</th><th class="right">Montant</th></tr>{ptrs}</table></div></div>''')

@app.route('/document/<int:doc_id>/edit',methods=['GET','POST'])
def edit_document(doc_id):
    con=db(); d=con.execute('select * from docs where id=?',(doc_id,)).fetchone(); cls=con.execute('select * from clients order by name').fetchall(); lines=con.execute('select * from lines where doc_id=?',(doc_id,)).fetchall()
    if not d: con.close(); return 'Document introuvable',404
    if request.method=='POST':
        con.execute('update docs set kind=?,number=?,doc_date=?,due_date=?,client_id=?,reference=?,po_number=?,payment_terms=?,delivery=?,status=?,notes=?,currency=?,vat_percent=?,discount_percent=? where id=?',(request.form['kind'],request.form['number'],request.form.get('doc_date'),request.form.get('due_date'),request.form.get('client_id') or None,request.form.get('reference'),request.form.get('po_number'),request.form.get('payment_terms'),request.form.get('delivery'),request.form.get('status'),request.form.get('notes'),request.form.get('currency') or 'MGA',float(request.form.get('vat_percent') or 0),float(request.form.get('discount_percent') or 0),doc_id)); con.execute('delete from lines where doc_id=?',(doc_id,))
        for de,q,p in zip(request.form.getlist('description'),request.form.getlist('qty'),request.form.getlist('unit_price')):
            if de.strip(): con.execute('insert into lines(doc_id,description,qty,unit_price) values(?,?,?,?)',(doc_id,de,float(q or 0),float(p or 0)))
        con.commit(); con.close(); flash('Document mis à jour.'); return redirect(url_for('document',doc_id=doc_id))
    con.close(); opts=''.join(f"<option value='{c['id']}' {'selected' if c['id']==d['client_id'] else ''}>{c['name']}</option>" for c in cls); statuses=['Brouillon','Envoyé','Accepté','Commandé','Livré','Facturé','Partiellement payé','Payé','Refusé','Annulé']; sopts=''.join(f"<option {'selected' if s==d['status'] else ''}>{s}</option>" for s in statuses)
    all_lines=list(lines)+[{'description':'','qty':1,'unit_price':0} for _ in range(max(3,8-len(lines)))]; rows=''.join(f"<tr><td><textarea class='designation' name='description'>{str(l['description'] or '').replace('&','&amp;').replace('<','&lt;').replace('>','&gt;')}</textarea></td><td><input name='qty' type='number' step='0.01' value='{l['qty']}'></td><td><input name='unit_price' type='number' step='1' value='{l['unit_price']}'></td></tr>" for l in all_lines)
    return page(f'''<div class="card"><h2>Modifier {d['kind']} {d['number']}</h2><form method="post"><div class="grid3"><div><label>Type</label><select name="kind"><option {'selected' if d['kind']=='Devis' else ''}>Devis</option><option {'selected' if d['kind']=='Facture' else ''}>Facture</option></select></div><div><label>Numéro</label><input name="number" value="{d['number']}"></div><div><label>Statut</label><select name="status">{sopts}</select></div><div><label>Client</label><select name="client_id"><option value="">--</option>{opts}</select></div><div><label>Date</label><input type="date" name="doc_date" value="{d['doc_date'] or ''}"></div><div><label>Échéance</label><input type="date" name="due_date" value="{d['due_date'] or ''}"></div><div><label>Bon de commande</label><input name="po_number" value="{d['po_number'] or ''}"></div><div><label>Devise</label><select name="currency"><option value="MGA" {'selected' if (d['currency'] or 'MGA')=='MGA' else ''}>Ariary (Ar)</option><option value="EUR" {'selected' if d['currency']=='EUR' else ''}>Euro (€)</option></select></div><div><label>TVA (%)</label><input type="number" name="vat_percent" min="0" step="0.01" value="{d['vat_percent'] or 0}"></div><div><label>Remise globale (%)</label><input type="number" name="discount_percent" min="0" max="100" step="0.01" value="{d['discount_percent'] or 0}"></div></div><p><label>Référence</label><input name="reference" value="{d['reference'] or ''}"></p><div class="grid"><div><label>Modalité de règlement</label><input name="payment_terms" value="{d['payment_terms'] or ''}"></div><div><label>Livraison / travaux</label><input name="delivery" value="{d['delivery'] or ''}"></div></div><p><label>Notes</label><textarea name="notes">{d['notes'] or ''}</textarea></p><div class="tablewrap"><table><tr><th>Désignation</th><th>Qté</th><th>P.U. (devise choisie)</th></tr>{rows}</table></div><p><button>Enregistrer</button></p></form></div>''')

@app.post('/document/<int:doc_id>/convert')
def convert(doc_id):
    con=db(); d=con.execute('select * from docs where id=?',(doc_id,)).fetchone()
    if not d or d['kind']!='Devis': con.close(); return redirect(url_for('document',doc_id=doc_id))
    cur=con.execute('insert into docs(kind,number,doc_date,due_date,client_id,reference,po_number,payment_terms,delivery,status,notes,source_doc_id,doc_subtype,deposit_percent,currency,vat_percent,discount_percent) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',('Facture',next_number('Facture'),date.today().isoformat(),(date.today()+timedelta(days=30)).isoformat(),d['client_id'],d['reference'],d['po_number'],d['payment_terms'],d['delivery'],'Facturé',d['notes'],doc_id,'Finale',0,d['currency'] or 'MGA',d['vat_percent'] or 0,d['discount_percent'] or 0))
    nid=cur.lastrowid
    con.execute('insert into lines(doc_id,description,qty,unit_price) select ?,description,qty,unit_price from lines where doc_id=?',(nid,doc_id))
    deposits=con.execute("""select coalesce(sum(l.qty*l.unit_price),0) t from docs a join lines l on l.doc_id=a.id where a.source_doc_id=? and a.kind='Facture' and a.doc_subtype='Acompte' and a.status not in ('Annulé')""",(doc_id,)).fetchone()['t']
    if deposits>0: con.execute('insert into lines(doc_id,description,qty,unit_price) values(?,?,?,?)',(nid,'Déduction des acomptes déjà facturés',1,-deposits))
    con.execute("update docs set status='Facturé' where id=?",(doc_id,)); con.commit(); con.close(); flash('Facture finale créée, acomptes déduits.'); return redirect(url_for('document',doc_id=nid))

@app.post('/document/<int:doc_id>/deposit')
def create_deposit(doc_id):
    pct=max(1,min(100,float(request.form.get('percent') or 30))); con=db(); d=con.execute('select * from docs where id=?',(doc_id,)).fetchone()
    if not d: con.close(); return redirect(url_for('documents'))
    total=total_for(con,doc_id); amount=round(total*pct/100)
    cur=con.execute('insert into docs(kind,number,doc_date,due_date,client_id,reference,po_number,payment_terms,delivery,status,notes,source_doc_id,doc_subtype,deposit_percent,currency,vat_percent,discount_percent) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',('Facture',next_number('Facture'),date.today().isoformat(),(date.today()+timedelta(days=7)).isoformat(),d['client_id'],f'Acompte {pct:g}% - {d["reference"] or d["number"]}',d['po_number'],d['payment_terms'],d['delivery'],'Facturé',d['notes'],doc_id,'Acompte',pct,d['currency'] or 'MGA',0,0))
    nid=cur.lastrowid; con.execute('insert into lines(doc_id,description,qty,unit_price) values(?,?,?,?)',(nid,f'Acompte {pct:g}% sur {d["number"]}',1,amount)); con.commit(); con.close(); flash('Facture d’acompte créée.'); return redirect(url_for('document',doc_id=nid))

@app.post('/document/<int:doc_id>/payment')
def add_payment(doc_id):
    amount=float(request.form.get('amount') or 0); con=db(); con.execute('insert into payments(doc_id,payment_date,amount,method,note) values(?,?,?,?,?)',(doc_id,request.form.get('payment_date'),amount,request.form.get('method'),request.form.get('note'))); total=total_for(con,doc_id); paid=paid_for(con,doc_id); status='Payé' if paid>=total and total>0 else 'Partiellement payé'; con.execute('update docs set status=? where id=?',(status,doc_id)); con.commit(); con.close(); flash('Règlement enregistré.'); return redirect(url_for('document',doc_id=doc_id))


@app.route('/client/<int:cid>')
def client_history(cid):
    con=db(); c=con.execute('select * from clients where id=?',(cid,)).fetchone(); docs=con.execute('select d.*,coalesce(sum(l.qty*l.unit_price),0) total from docs d left join lines l on l.doc_id=d.id where d.client_id=? group by d.id order by d.id desc',(cid,)).fetchall(); scans=con.execute('select * from client_scans where client_id=? order by id desc limit 5',(cid,)).fetchall(); con.close()
    if not c:return 'Client introuvable',404
    trs=''.join(f"<tr><td>{d['doc_date'] or ''}</td><td>{d['kind']}</td><td><a href='/document/{d['id']}'>{d['number']}</a></td><td>{d['reference'] or ''}</td><td>{d['status']}</td><td class='right'>{money(d['total'],d['currency'] or 'MGA')} {currency_label(d['currency'] or 'MGA')}</td></tr>" for d in docs) or '<tr><td colspan="6">Aucun document.</td></tr>'
    photo_html=f'<img class="clientphoto" src="/uploads/{c["photo"]}">' if c['photo'] else '<div class="clientphoto" style="display:grid;place-items:center">Aucune photo</div>'
    scan_html=''.join(f'<a href="/uploads/{x["image_path"]}">📷 Scan du {str(x["created_at"] or "")[:10]}</a><br>' for x in scans if x['image_path']) or '<span class="muted">Aucun scan enregistré.</span>'
    return page(f'''<div class="card"><div class="row">{photo_html}<div style="flex:1"><h2>{c['name']}</h2><div>{c['phone'] or ''} • {c['email'] or ''}</div><div>{(c['address'] or '').replace(chr(10),'<br>')}</div><div class="muted">NIF {c['nif'] or ''} — STAT {c['stat'] or ''}</div></div><a class="btn alt" href="/client/{cid}/edit">Modifier</a><a class="btn" href="/document/new?client_id={cid}">Nouveau document</a></div></div><div class="card"><h2>Historique complet</h2><div class="tablewrap"><table><tr><th>Date</th><th>Type</th><th>N°</th><th>Objet</th><th>Statut</th><th>Total</th></tr>{trs}</table></div></div><div class="card"><h3>📷 Photo / scan client</h3><p>Tu peux ajouter une carte de visite ou un document : EMS essaiera de compléter les coordonnées manquantes.</p><form method="post" enctype="multipart/form-data" action="/client/{cid}/scan"><input type="file" name="scan" accept="image/*" capture="environment" required><p><button>Importer et analyser</button></p></form><div>{scan_html}</div></div>''')

@app.post('/client/<int:cid>/scan')
def client_scan(cid):
    f=request.files.get('scan')
    if not f: flash('Aucune photo reçue.'); return redirect(url_for('client_history',cid=cid))
    name=save_image(f,f'client_{cid}_scan'); path=UPLOADS/name; text=''
    try:
        import pytesseract
        from PIL import Image
        text=pytesseract.image_to_string(Image.open(path),lang='fra+eng')
    except Exception:
        text=''
    fields=extract_contact_fields(text)
    con=db(); con.execute('insert into client_scans(client_id,image_path,extracted_text) values(?,?,?)',(cid,name,text)); cur=con.execute('select * from clients where id=?',(cid,)).fetchone()
    if cur:
        con.execute('update clients set name=?,address=?,email=?,phone=?,nif=?,stat=?,photo=? where id=?',(
            cur['name'] or fields['name'],cur['address'] or fields['address'],cur['email'] or fields['email'],cur['phone'] or fields['phone'],cur['nif'] or fields['nif'],cur['stat'] or fields['stat'],cur['photo'] or name,cid))
    con.commit(); con.close(); flash('Photo enregistrée. Les coordonnées reconnues ont été ajoutées lorsqu’elles étaient vides.'); return redirect(url_for('client_history',cid=cid))

@app.route('/products',methods=['GET','POST'])
def products():
    con=db()
    if request.method=='POST':
        photo=request.files.get('photo'); pname=''
        if photo and photo.filename: pname=f'prod_{uuid.uuid4().hex[:10]}{Path(photo.filename).suffix.lower()}'; photo.save(UPLOADS/pname)
        try: con.execute('insert into products(reference,name,description,price,stock,min_stock,photo) values(?,?,?,?,?,?,?)',(request.form.get('reference'),request.form['name'],request.form.get('description'),float(request.form.get('price') or 0),float(request.form.get('stock') or 0),float(request.form.get('min_stock') or 0),pname)); con.commit(); flash('Article ajouté au catalogue.')
        except sqlite3.IntegrityError: flash('Cette référence existe déjà.')
        con.close(); return redirect(url_for('products'))
    q=request.args.get('q',''); rows=con.execute('select * from products where active=1 and (reference like ? or name like ? or description like ?) order by name',(f'%{q}%',f'%{q}%',f'%{q}%')).fetchall(); con.close()
    trs=''.join(f"<tr><td>{r['reference'] or ''}</td><td><b>{r['name']}</b><br><span class='small muted'>{r['description'] or ''}</span></td><td class='right'>{money(r['price'])} Ar</td><td class='right'>{r['stock']:g}</td><td>{r['min_stock']:g}</td><td><a href='/product/{r['id']}'>Gérer</a></td></tr>" for r in rows)
    return page(f'''<div class="card"><h2>Catalogue pièces / matériels</h2><form method="post" enctype="multipart/form-data"><div class="grid3"><div><label>Référence</label><input name="reference"></div><div><label>Nom</label><input name="name" required></div><div><label>Prix de vente Ar</label><input type="number" name="price"></div><div><label>Stock initial</label><input type="number" step="0.01" name="stock" value="0"></div><div><label>Seuil d’alerte</label><input type="number" step="0.01" name="min_stock" value="0"></div><div><label>Photo</label><input type="file" name="photo" accept="image/*"></div></div><p><label>Description</label><textarea name="description"></textarea></p><button>Ajouter l’article</button></form></div><div class="card"><div class="row"><h2 style="flex:1">Stock</h2><form><input name="q" value="{q}" placeholder="Référence, nom..."></form></div><div class="tablewrap"><table><tr><th>Réf.</th><th>Article</th><th>Prix</th><th>Stock</th><th>Alerte</th><th></th></tr>{trs}</table></div></div>''')

@app.route('/product/<int:pid>',methods=['GET','POST'])
def product(pid):
    con=db(); p=con.execute('select * from products where id=?',(pid,)).fetchone()
    if not p: con.close(); return 'Article introuvable',404
    if request.method=='POST':
        qty=float(request.form.get('qty') or 0); typ=request.form.get('move_type','Entrée'); delta=abs(qty) if typ=='Entrée' else -abs(qty); con.execute('update products set stock=stock+? where id=?',(delta,pid)); con.execute('insert into stock_moves(product_id,move_date,qty,move_type,note) values(?,?,?,?,?)',(pid,date.today().isoformat(),abs(qty),typ,request.form.get('note'))); con.commit(); con.close(); flash('Stock mis à jour.'); return redirect(url_for('product',pid=pid))
    moves=con.execute('select * from stock_moves where product_id=? order by id desc limit 30',(pid,)).fetchall(); con.close(); trs=''.join(f"<tr><td>{m['move_date']}</td><td>{m['move_type']}</td><td>{m['qty']:g}</td><td>{m['note'] or ''}</td></tr>" for m in moves)
    return page(f'''<div class="card"><h2>{p['reference'] or ''} — {p['name']}</h2><div class="total">Stock : {p['stock']:g}</div><div>{p['description'] or ''}</div><div>Prix : {money(p['price'])} Ar • Seuil : {p['min_stock']:g}</div></div><div class="card"><h3>Mouvement de stock</h3><form method="post"><div class="grid3"><div><label>Type</label><select name="move_type"><option>Entrée</option><option>Sortie</option></select></div><div><label>Quantité</label><input type="number" step="0.01" name="qty" required></div><div><label>Note</label><input name="note"></div></div><p><button>Enregistrer</button></p></form></div><div class="card"><h3>Historique stock</h3><table><tr><th>Date</th><th>Type</th><th>Qté</th><th>Note</th></tr>{trs}</table></div>''')

@app.route('/search')
def global_search():
    q=request.args.get('q','').strip(); body='<div class="card"><h2>Recherche globale</h2><form><input name="q" value="'+q+'" placeholder="Client, téléphone, référence, devis, facture..."><p><button>Rechercher</button></p></form></div>'
    if q:
        con=db(); cs=con.execute('select * from clients where name like ? or phone like ? or email like ? or nif like ?',(f'%{q}%',)*4).fetchall(); ds=con.execute('select d.*,c.name client from docs d left join clients c on c.id=d.client_id where d.number like ? or d.reference like ? or d.po_number like ?',(f'%{q}%',)*3).fetchall(); ps=con.execute('select * from products where reference like ? or name like ? or description like ?',(f'%{q}%',)*3).fetchall(); con.close()
        body+='<div class="card"><h3>Résultats</h3>'+''.join(f"<p>👤 <a href='/client/{x['id']}'>{x['name']}</a> — {x['phone'] or ''}</p>" for x in cs)+''.join(f"<p>📄 <a href='/document/{x['id']}'>{x['kind']} {x['number']}</a> — {x['client'] or ''}</p>" for x in ds)+''.join(f"<p>📦 <a href='/product/{x['id']}'>{x['reference'] or ''} {x['name']}</a></p>" for x in ps)+'</div>'
    return page(body)

@app.route('/reminders')
def reminders():
    con=db(); due=con.execute("""select d.*,c.name client,coalesce(sum(l.qty*l.unit_price),0) total,(select coalesce(sum(amount),0) from payments p where p.doc_id=d.id) paid from docs d left join clients c on c.id=d.client_id left join lines l on l.doc_id=d.id where (d.kind='Devis' and d.status in ('Brouillon','Envoyé','Accepté')) or (d.kind='Facture' and d.status not in ('Payé','Annulé')) group by d.id order by d.due_date""").fetchall(); con.close()
    trs=''.join(f"<tr><td>{d['kind']}</td><td><a href='/document/{d['id']}'>{d['number']}</a></td><td>{d['client'] or ''}</td><td>{d['due_date'] or ''}</td><td>{d['status']}</td><td class='right'>{money(max(0,d['total']-d['paid']))} Ar</td></tr>" for d in due)
    return page(f'''<div class="card"><h2>Relances devis & factures</h2><p class="muted">Devis ouverts et factures non soldées.</p><div class="tablewrap"><table><tr><th>Type</th><th>N°</th><th>Client</th><th>Échéance</th><th>Statut</th><th>À suivre</th></tr>{trs}</table></div></div>''')

@app.route('/document/<int:doc_id>/share')
def share_document(doc_id):
    con=db(); d=con.execute('select d.*,c.name client,c.email,c.phone from docs d left join clients c on c.id=d.client_id where d.id=?',(doc_id,)).fetchone(); con.close()
    if not d:return 'Introuvable',404
    subject=f"{d['kind']} {d['number']} - EMS"; text=f"Bonjour {d['client'] or ''}, veuillez trouver votre {d['kind'].lower()} {d['number']} EMS."; mail=f"mailto:{d['email'] or ''}?subject={quote(subject)}&body={quote(text)}"; phone=re.sub(r'\D','',d['phone'] or ''); wa=f"https://wa.me/{phone}?text={quote(text)}" if phone else '#'
    return page(f'''<div class="card"><h2>Partager {d['kind']} {d['number']}</h2><div class="actions"><a class="btn" href="{mail}">📧 E-mail</a><a class="btn green" href="{wa}">💬 WhatsApp</a><a class="btn alt" href="/document/{doc_id}/pdf">📄 Télécharger le PDF</a></div></div>''')

@app.route('/document/<int:doc_id>/signature',methods=['GET','POST'])
def signature(doc_id):
    if request.method=='POST':
        data=request.form.get('signature_data',''); name=request.form.get('signer_name',''); con=db(); con.execute('insert into signatures(doc_id,signer_name,signature_data) values(?,?,?) on conflict(doc_id) do update set signer_name=excluded.signer_name,signature_data=excluded.signature_data,signed_at=CURRENT_TIMESTAMP',(doc_id,name,data)); con.execute("update docs set status='Accepté' where id=? and kind='Devis'",(doc_id,)); con.commit(); con.close(); flash('Signature enregistrée.'); return redirect(url_for('document',doc_id=doc_id))
    return page(f'''<div class="card"><h2>Signature du devis</h2><form method="post" onsubmit="document.getElementById('sigdata').value=document.getElementById('sig').toDataURL()"><p><label>Nom du signataire</label><input name="signer_name" required></p><canvas id="sig" class="sigcanvas" width="520" height="180"></canvas><input type="hidden" id="sigdata" name="signature_data"><p><button>Signer et accepter</button></p></form></div><script>const c=document.getElementById('sig'),ctx=c.getContext('2d');let on=false;function p(e){{const r=c.getBoundingClientRect(),t=e.touches?e.touches[0]:e;return [(t.clientX-r.left)*c.width/r.width,(t.clientY-r.top)*c.height/r.height]}}c.onmousedown=c.ontouchstart=e=>{{on=true;let a=p(e);ctx.beginPath();ctx.moveTo(...a);e.preventDefault()}};c.onmousemove=c.ontouchmove=e=>{{if(!on)return;ctx.lineTo(...p(e));ctx.stroke();e.preventDefault()}};c.onmouseup=c.onmouseleave=c.ontouchend=()=>on=false;</script>''')

@app.post('/document/<int:doc_id>/status')
def set_status(doc_id):
    status=request.form.get('status'); allowed=['Brouillon','Envoyé','Accepté','Commandé','Livré','Facturé','Partiellement payé','Payé','Refusé','Annulé']
    if status in allowed:
        con=db(); con.execute('update docs set status=? where id=?',(status,doc_id)); con.commit(); con.close(); flash('Statut mis à jour.')
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
    con=db(); d=con.execute('''select d.*,c.name client,c.address,c.nif,c.stat,c.email,c.phone from docs d left join clients c on c.id=d.client_id where d.id=?''',(doc_id,)).fetchone(); lines=con.execute('select * from lines where doc_id=?',(doc_id,)).fetchall(); calc=totals_for(con,doc_id); total=calc['total']; currency=calc['currency']; symbol=currency_label(currency); con.close()
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
    x1=L; x2=100*mm; x3=129*mm; x4=160*mm; x5=R; c.setFillColorRGB(.68,.66,.66); c.rect(x1,y-8*mm,x5-x1,8*mm,fill=1,stroke=1); c.setFillColorRGB(0,0,0); [c.line(x,y,x,y-8*mm) for x in [x2,x3,x4]]; c.setFont('Helvetica-Bold',9); c.drawCentredString((x1+x2)/2,y-5.5*mm,'DÉSIGNATION'); c.drawCentredString((x2+x3)/2,y-5.5*mm,'QTS'); c.drawCentredString((x3+x4)/2,y-5.5*mm,'P.U'); c.drawCentredString((x4+x5)/2,y-5.5*mm,f'TOTAL en {symbol}'); y-=8*mm
    c.setFont('Helvetica',8.5)
    for l in lines:
        h=7*mm; c.rect(x1,y-h,x5-x1,h,fill=0,stroke=1); [c.line(x,y,x,y-h) for x in [x2,x3,x4]]; c.drawString(x1+2*mm,y-4.8*mm,(l['description'] or '')[:58]); c.drawRightString(x3-2*mm,y-4.8*mm,f"{l['qty']:g}"); c.drawRightString(x4-2*mm,y-4.8*mm,money(l['unit_price'],currency)); c.drawRightString(x5-2*mm,y-4.8*mm,money(l['qty']*l['unit_price'],currency)); y-=h
    y-=1*mm; c.setFillColorRGB(.68,.66,.66); c.rect(100*mm,y-8*mm,R-100*mm,8*mm,fill=1,stroke=1); c.setFillColorRGB(0,0,0); c.setFont('Helvetica-Bold',9.5); c.drawString(103*mm,y-5.5*mm,f'SOUS-TOTAL HT'); c.drawRightString(R-2*mm,y-5.5*mm,money(calc['subtotal'],currency)+' '+symbol); y-=8*mm; c.setFont('Helvetica',8.5); c.drawString(103*mm,y-4.5*mm,f"Remise {calc['discount_percent']:g}%"); c.drawRightString(R-2*mm,y-4.5*mm,'- '+money(calc['discount'],currency)+' '+symbol); y-=6*mm; c.drawString(103*mm,y-4.5*mm,f"TVA {calc['vat_percent']:g}%"); c.drawRightString(R-2*mm,y-4.5*mm,money(calc['vat'],currency)+' '+symbol); y-=7*mm; c.setFont('Helvetica-Bold',9.5); c.drawString(103*mm,y-5.5*mm,'TOTAL TTC'); c.drawRightString(R-2*mm,y-5.5*mm,money(total,currency)+' '+symbol); y-=20*mm
    c.setFont('Helvetica-Bold',9); c.drawString(L+5*mm,y,'Arrêté à la somme de :'); c.setFont('Helvetica',9); c.drawString(L+44*mm,y,number_words(total,currency)+'.')
    # bottom reference block fixed near bottom
    by=50*mm; c.setFillColorRGB(.7,.69,.69); c.rect(L,by,R-L,40*mm,fill=1,stroke=0); c.setFillColorRGB(.45,.43,.43); c.rect(L+2*mm,by+32*mm,R-L-4*mm,7*mm,fill=1,stroke=0); c.setFillColorRGB(0,0,0); c.setFont('Helvetica-Bold',9); c.drawString(L+4*mm,by+34*mm,'RÉFÉRENCE :'); c.setFont('Helvetica-Bold',8.5); c.drawString(L+4*mm,by+25*mm,f"Devis : {'-' if d['kind']=='Devis' else ''}"); c.drawString(L+4*mm,by+19*mm,f"Bon de commande : {d['po_number'] or ''}"); c.drawString(L+4*mm,by+13*mm,f"Modalité du règlement : {d['payment_terms'] or ''}"); c.drawString(L+4*mm,by+7*mm,f"Date et lieu de livraison : {d['delivery'] or ''}")
    c.setFont('Helvetica-Bold',9); c.drawString(L+10*mm,41*mm,'Coordonnées bancaires de la société :'); c.setFillColorRGB(.78,.77,.77); c.rect(L,19*mm,R-L,18*mm,fill=1,stroke=1); c.setFillColorRGB(0,0,0); c.setFont('Helvetica',8.5); c.drawString(L+2*mm,32*mm,f"Banque : {COMPANY['bank']}"); c.setFont('Helvetica-Bold',8); heads=['Code banque','Code guichet','N° de compte','Clé']; vals=[COMPANY['bank_code'],COMPANY['branch_code'],COMPANY['account'],COMPANY['key']]; xs=[L+30*mm,L+72*mm,L+120*mm,L+164*mm]
    for i,h in enumerate(heads): c.drawCentredString(xs[i],26*mm,h); c.setFont('Helvetica',8); c.drawCentredString(xs[i],21.5*mm,vals[i]); c.setFont('Helvetica-Bold',8)
    c.showPage(); c.save(); return send_file(out,as_attachment=True,download_name=out.name)

if __name__=='__main__':
    init_db(); app.run(host='0.0.0.0',port=5000,debug=False)
