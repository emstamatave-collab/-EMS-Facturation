import sqlite3, os, re, hmac
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
    '''); c.commit(); c.close()

def money(v): return f"{float(v or 0):,.0f}".replace(',', ' ')

def total_for(con, doc_id):
    return con.execute('select coalesce(sum(qty*unit_price),0) t from lines where doc_id=?',(doc_id,)).fetchone()['t']

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
input,select,textarea{width:100%;padding:10px;border:1px solid #cfd4dc;border-radius:8px;background:white}textarea{min-height:70px}label{font-size:13px;font-weight:650;display:block;margin-bottom:5px}
button{padding:10px 14px;border:0;border-radius:8px;background:#111;color:#fff;cursor:pointer;font-weight:700}.danger{background:var(--red)}.success{background:var(--green)}
table{width:100%;border-collapse:collapse}th,td{padding:10px;border-bottom:1px solid #eee;text-align:left;vertical-align:top}th{font-size:12px;color:#555;text-transform:uppercase}.right{text-align:right}.muted{color:var(--muted)}.total{font-size:22px;font-weight:800}.kpi{font-size:28px;font-weight:800}.badge{display:inline-block;padding:4px 8px;border-radius:999px;background:#eee;font-size:12px}.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}.flash{background:#fff6d8;border:1px solid #f5d98c;padding:10px;border-radius:8px;margin:12px 0}.search{max-width:320px}.small{font-size:12px}.nowrap{white-space:nowrap}
@media(max-width:760px){.grid,.grid3,.grid4{grid-template-columns:1fr}.top{position:static}.tablewrap{overflow-x:auto}.wrap{margin-top:12px}.nav a{flex:1;text-align:center}.card{padding:14px}}
'''
TPL='''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>EMS Facturation</title><meta name="theme-color" content="#111111"><meta name="apple-mobile-web-app-capable" content="yes"><meta name="apple-mobile-web-app-status-bar-style" content="black-translucent"><meta name="apple-mobile-web-app-title" content="EMS"><link rel="manifest" href="/manifest.webmanifest"><link rel="apple-touch-icon" href="/icon-180.png"><link rel="icon" href="/icon-192.png"><style>'''+STYLE+'''</style></head><body>
<div class="top">{% if logo %}<img src="{{url_for('logo')}}">{% endif %}<div><div class="brand">EMS — Devis & Facturation</div><div class="sub">Gestion commerciale • Ariary (MGA)</div></div></div>
<div class="wrap"><div class="nav"><a href="{{url_for('home')}}">Tableau de bord</a><a href="{{url_for('clients')}}">Clients</a><a href="{{url_for('documents')}}">Devis / Factures</a><a href="{{url_for('new_document')}}">+ Nouveau</a><a href="{{url_for('backup_db')}}">Sauvegarde</a><a href="{{url_for('logout')}}">Déconnexion</a></div>
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
def health(): return {'ok': True, 'app':'EMS Facturation V3'}

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
        con.execute('insert into clients(name,address,nif,stat,email,phone) values(?,?,?,?,?,?)',(request.form['name'],request.form.get('address'),request.form.get('nif'),request.form.get('stat'),request.form.get('email'),request.form.get('phone'))); con.commit(); con.close(); flash('Client ajouté.'); return redirect(url_for('clients'))
    q=request.args.get('q','').strip(); rows=con.execute('select * from clients where name like ? or nif like ? order by name',(f'%{q}%',f'%{q}%')).fetchall() if q else con.execute('select * from clients order by name').fetchall(); con.close()
    trs=''.join(f"<tr><td><a href='/client/{r['id']}/edit'>{r['name']}</a></td><td>{r['nif'] or ''}</td><td>{r['stat'] or ''}</td><td>{r['phone'] or ''}</td><td>{r['email'] or ''}</td></tr>" for r in rows)
    return page(f'''<div class="card"><h2>Nouveau client</h2><form method="post"><div class="grid3"><div><label>Nom / société</label><input name="name" required></div><div><label>Téléphone</label><input name="phone"></div><div><label>Email</label><input name="email"></div><div><label>NIF</label><input name="nif"></div><div><label>STAT</label><input name="stat"></div><div><label>Adresse</label><textarea name="address"></textarea></div></div><p><button>Ajouter le client</button></p></form></div><div class="card"><div class="row"><h2 style="flex:1">Clients</h2><form><input class="search" name="q" value="{q}" placeholder="Rechercher nom ou NIF"></form></div><div class="tablewrap"><table><tr><th>Nom</th><th>NIF</th><th>STAT</th><th>Téléphone</th><th>Email</th></tr>{trs}</table></div></div>''')

@app.route('/client/<int:cid>/edit',methods=['GET','POST'])
def edit_client(cid):
    con=db(); r=con.execute('select * from clients where id=?',(cid,)).fetchone()
    if not r: con.close(); return 'Client introuvable',404
    if request.method=='POST':
        con.execute('update clients set name=?,address=?,nif=?,stat=?,email=?,phone=? where id=?',(request.form['name'],request.form.get('address'),request.form.get('nif'),request.form.get('stat'),request.form.get('email'),request.form.get('phone'),cid)); con.commit(); con.close(); flash('Client mis à jour.'); return redirect(url_for('clients'))
    con.close();
    return page(f'''<div class="card"><h2>Modifier le client</h2><form method="post"><div class="grid"><div><label>Nom</label><input name="name" value="{r['name'] or ''}" required></div><div><label>Téléphone</label><input name="phone" value="{r['phone'] or ''}"></div><div><label>Email</label><input name="email" value="{r['email'] or ''}"></div><div><label>NIF</label><input name="nif" value="{r['nif'] or ''}"></div><div><label>STAT</label><input name="stat" value="{r['stat'] or ''}"></div><div><label>Adresse</label><textarea name="address">{r['address'] or ''}</textarea></div></div><p><button>Enregistrer</button></p></form></div>''')

@app.route('/documents')
def documents():
    con=db(); q=request.args.get('q','').strip(); kind=request.args.get('kind','').strip(); status=request.args.get('status','').strip(); sql='''select d.*,c.name client,coalesce(sum(l.qty*l.unit_price),0) total,(select coalesce(sum(amount),0) from payments p where p.doc_id=d.id) paid from docs d left join clients c on c.id=d.client_id left join lines l on l.doc_id=d.id where 1=1'''; params=[]
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
        cur=con.execute('insert into docs(kind,number,doc_date,due_date,client_id,reference,po_number,payment_terms,delivery,status,notes) values(?,?,?,?,?,?,?,?,?,?,?)',(kind,number,request.form.get('doc_date'),request.form.get('due_date'),request.form.get('client_id') or None,request.form.get('reference'),request.form.get('po_number'),request.form.get('payment_terms'),request.form.get('delivery'),'Brouillon',request.form.get('notes'))); did=cur.lastrowid
        for d,q,p in zip(request.form.getlist('description'),request.form.getlist('qty'),request.form.getlist('unit_price')):
            if d.strip(): con.execute('insert into lines(doc_id,description,qty,unit_price) values(?,?,?,?)',(did,d,float(q or 0),float(p or 0)))
        con.commit(); con.close(); return redirect(url_for('document',doc_id=did))
    con.close(); today=date.today(); due=today+timedelta(days=30); opts=''.join(f"<option value='{c['id']}'>{c['name']}</option>" for c in cls)
    rows=''.join("<tr><td><input name='description'></td><td><input name='qty' type='number' step='0.01' value='1'></td><td><input name='unit_price' type='number' step='1' value='0'></td></tr>" for _ in range(8))
    return page(f'''<div class="card"><h2>Nouveau devis / facture</h2><form method="post"><div class="grid3"><div><label>Type</label><select name="kind"><option>Devis</option><option>Facture</option></select></div><div><label>Numéro</label><input name="number" placeholder="Automatique si vide"></div><div><label>Client</label><select name="client_id"><option value="">-- Choisir --</option>{opts}</select></div><div><label>Date</label><input type="date" name="doc_date" value="{today.isoformat()}"></div><div><label>Échéance</label><input type="date" name="due_date" value="{due.isoformat()}"></div><div><label>Bon de commande</label><input name="po_number"></div></div><p><label>Référence / objet</label><input name="reference" placeholder="Ex. TRAVAUX EFFECTUÉS SUR LE CHARIOT ÉLÉVATEUR 4A : Entretien"></p><div class="grid"><div><label>Modalité de règlement</label><input name="payment_terms" value="Virement sous 30 jours"></div><div><label>Livraison / travaux</label><input name="delivery"></div></div><p><label>Notes</label><textarea name="notes"></textarea></p><h3>Lignes</h3><div class="tablewrap"><table><tr><th>Désignation</th><th>Qté</th><th>P.U. Ar</th></tr>{rows}</table></div><p><button>Créer le document</button></p></form></div>''')

@app.route('/document/<int:doc_id>')
def document(doc_id):
    con=db(); d=con.execute('''select d.*,c.name client,c.address,c.nif,c.stat,c.email,c.phone from docs d left join clients c on c.id=d.client_id where d.id=?''',(doc_id,)).fetchone(); lines=con.execute('select * from lines where doc_id=?',(doc_id,)).fetchall(); pays=con.execute('select * from payments where doc_id=? order by payment_date desc,id desc',(doc_id,)).fetchall();
    if not d: con.close(); return 'Document introuvable',404
    total=total_for(con,doc_id); paid=paid_for(con,doc_id); con.close(); balance=max(0,total-paid)
    trs=''.join(f"<tr><td>{l['description']}</td><td>{l['qty']:g}</td><td class='right'>{money(l['unit_price'])}</td><td class='right'>{money(l['qty']*l['unit_price'])}</td></tr>" for l in lines)
    ptrs=''.join(f"<tr><td>{p['payment_date'] or ''}</td><td>{p['method'] or ''}</td><td>{p['note'] or ''}</td><td class='right'>{money(p['amount'])} Ar</td></tr>" for p in pays) or '<tr><td colspan="4" class="muted">Aucun règlement enregistré.</td></tr>'
    conv=f"<form method='post' action='/document/{doc_id}/convert'><button>Transformer en facture</button></form>" if d['kind']=='Devis' else ''
    payform=f'''<div class="card"><h3>Enregistrer un règlement</h3><form method="post" action="/document/{doc_id}/payment"><div class="grid3"><div><label>Date</label><input type="date" name="payment_date" value="{date.today().isoformat()}"></div><div><label>Montant (Ar)</label><input type="number" name="amount" value="{int(balance)}" min="0"></div><div><label>Mode</label><select name="method"><option>Virement</option><option>Espèces</option><option>Chèque</option><option>Mobile Money</option><option>Autre</option></select></div></div><p><label>Note</label><input name="note"></p><button class="success">Ajouter le règlement</button></form></div>''' if d['kind']=='Facture' else ''
    return page(f'''<div class="card"><div class="row"><div style="flex:1"><h2>{d['kind']} {d['number']}</h2><div class="muted">{d['client'] or 'Sans client'} • {d['doc_date'] or ''}</div></div><a class="btn2" href="/document/{doc_id}/edit">Modifier</a><a class="btn2" href="/document/{doc_id}/pdf">Télécharger PDF</a>{conv}</div><hr><div class="grid3"><div><div class="muted">Total</div><div class="total">{money(total)} Ar</div></div><div><div class="muted">Encaissé</div><div class="total">{money(paid)} Ar</div></div><div><div class="muted">Solde</div><div class="total">{money(balance)} Ar</div></div></div><p><b>Statut :</b> <span class="badge">{d['status']}</span> &nbsp; <b>Référence :</b> {d['reference'] or ''}</p><h3>Client</h3><div>{d['client'] or ''}<br>{(d['address'] or '').replace(chr(10),'<br>')}<br>NIF : {d['nif'] or ''}<br>STAT : {d['stat'] or ''}</div><h3>Détail</h3><div class="tablewrap"><table><tr><th>Désignation</th><th>Qté</th><th class="right">P.U.</th><th class="right">Total</th></tr>{trs}<tr><td colspan="3" class="right"><b>TOTAL</b></td><td class="right total">{money(total)} Ar</td></tr></table></div><p><b>Arrêté à la somme de :</b> {number_words(total)}.</p><p><b>Règlement :</b> {d['payment_terms'] or ''}<br><b>Bon de commande :</b> {d['po_number'] or ''}<br><b>Livraison :</b> {d['delivery'] or ''}</p></div>{payform}<div class="card"><h3>Règlements</h3><div class="tablewrap"><table><tr><th>Date</th><th>Mode</th><th>Note</th><th class="right">Montant</th></tr>{ptrs}</table></div></div>''')

@app.route('/document/<int:doc_id>/edit',methods=['GET','POST'])
def edit_document(doc_id):
    con=db(); d=con.execute('select * from docs where id=?',(doc_id,)).fetchone(); cls=con.execute('select * from clients order by name').fetchall(); lines=con.execute('select * from lines where doc_id=?',(doc_id,)).fetchall()
    if not d: con.close(); return 'Document introuvable',404
    if request.method=='POST':
        con.execute('update docs set kind=?,number=?,doc_date=?,due_date=?,client_id=?,reference=?,po_number=?,payment_terms=?,delivery=?,status=?,notes=? where id=?',(request.form['kind'],request.form['number'],request.form.get('doc_date'),request.form.get('due_date'),request.form.get('client_id') or None,request.form.get('reference'),request.form.get('po_number'),request.form.get('payment_terms'),request.form.get('delivery'),request.form.get('status'),request.form.get('notes'),doc_id)); con.execute('delete from lines where doc_id=?',(doc_id,))
        for de,q,p in zip(request.form.getlist('description'),request.form.getlist('qty'),request.form.getlist('unit_price')):
            if de.strip(): con.execute('insert into lines(doc_id,description,qty,unit_price) values(?,?,?,?)',(doc_id,de,float(q or 0),float(p or 0)))
        con.commit(); con.close(); flash('Document mis à jour.'); return redirect(url_for('document',doc_id=doc_id))
    con.close(); opts=''.join(f"<option value='{c['id']}' {'selected' if c['id']==d['client_id'] else ''}>{c['name']}</option>" for c in cls); statuses=['Brouillon','Envoyé','Accepté','Refusé','Facturé','Partiellement payé','Payé','Annulé']; sopts=''.join(f"<option {'selected' if s==d['status'] else ''}>{s}</option>" for s in statuses)
    all_lines=list(lines)+[{'description':'','qty':1,'unit_price':0} for _ in range(max(3,8-len(lines)))]; rows=''.join(f"<tr><td><input name='description' value=\"{str(l['description'] or '').replace(chr(34),'&quot;')}\"></td><td><input name='qty' type='number' step='0.01' value='{l['qty']}'></td><td><input name='unit_price' type='number' step='1' value='{l['unit_price']}'></td></tr>" for l in all_lines)
    return page(f'''<div class="card"><h2>Modifier {d['kind']} {d['number']}</h2><form method="post"><div class="grid3"><div><label>Type</label><select name="kind"><option {'selected' if d['kind']=='Devis' else ''}>Devis</option><option {'selected' if d['kind']=='Facture' else ''}>Facture</option></select></div><div><label>Numéro</label><input name="number" value="{d['number']}"></div><div><label>Statut</label><select name="status">{sopts}</select></div><div><label>Client</label><select name="client_id"><option value="">--</option>{opts}</select></div><div><label>Date</label><input type="date" name="doc_date" value="{d['doc_date'] or ''}"></div><div><label>Échéance</label><input type="date" name="due_date" value="{d['due_date'] or ''}"></div><div><label>Bon de commande</label><input name="po_number" value="{d['po_number'] or ''}"></div></div><p><label>Référence</label><input name="reference" value="{d['reference'] or ''}"></p><div class="grid"><div><label>Modalité de règlement</label><input name="payment_terms" value="{d['payment_terms'] or ''}"></div><div><label>Livraison / travaux</label><input name="delivery" value="{d['delivery'] or ''}"></div></div><p><label>Notes</label><textarea name="notes">{d['notes'] or ''}</textarea></p><div class="tablewrap"><table><tr><th>Désignation</th><th>Qté</th><th>P.U. Ar</th></tr>{rows}</table></div><p><button>Enregistrer</button></p></form></div>''')

@app.post('/document/<int:doc_id>/convert')
def convert(doc_id):
    con=db(); d=con.execute('select * from docs where id=?',(doc_id,)).fetchone()
    if d and d['kind']=='Devis': con.execute('update docs set kind=?,number=?,status=? where id=?',('Facture',next_number('Facture'),'Facturé',doc_id)); con.commit(); flash('Devis transformé en facture.')
    con.close(); return redirect(url_for('document',doc_id=doc_id))

@app.post('/document/<int:doc_id>/payment')
def add_payment(doc_id):
    amount=float(request.form.get('amount') or 0); con=db(); con.execute('insert into payments(doc_id,payment_date,amount,method,note) values(?,?,?,?,?)',(doc_id,request.form.get('payment_date'),amount,request.form.get('method'),request.form.get('note'))); total=total_for(con,doc_id); paid=paid_for(con,doc_id); status='Payé' if paid>=total and total>0 else 'Partiellement payé'; con.execute('update docs set status=? where id=?',(status,doc_id)); con.commit(); con.close(); flash('Règlement enregistré.'); return redirect(url_for('document',doc_id=doc_id))

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
    con=db(); d=con.execute('''select d.*,c.name client,c.address,c.nif,c.stat,c.email,c.phone from docs d left join clients c on c.id=d.client_id where d.id=?''',(doc_id,)).fetchone(); lines=con.execute('select * from lines where doc_id=?',(doc_id,)).fetchall(); total=total_for(con,doc_id); con.close()
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
    x1=L; x2=100*mm; x3=129*mm; x4=160*mm; x5=R; c.setFillColorRGB(.68,.66,.66); c.rect(x1,y-8*mm,x5-x1,8*mm,fill=1,stroke=1); c.setFillColorRGB(0,0,0); [c.line(x,y,x,y-8*mm) for x in [x2,x3,x4]]; c.setFont('Helvetica-Bold',9); c.drawCentredString((x1+x2)/2,y-5.5*mm,'DÉSIGNATION'); c.drawCentredString((x2+x3)/2,y-5.5*mm,'QTS'); c.drawCentredString((x3+x4)/2,y-5.5*mm,'P.U'); c.drawCentredString((x4+x5)/2,y-5.5*mm,'TOTAL en Ar'); y-=8*mm
    c.setFont('Helvetica',8.5)
    for l in lines:
        h=7*mm; c.rect(x1,y-h,x5-x1,h,fill=0,stroke=1); [c.line(x,y,x,y-h) for x in [x2,x3,x4]]; c.drawString(x1+2*mm,y-4.8*mm,(l['description'] or '')[:58]); c.drawRightString(x3-2*mm,y-4.8*mm,f"{l['qty']:g}"); c.drawRightString(x4-2*mm,y-4.8*mm,money(l['unit_price'])); c.drawRightString(x5-2*mm,y-4.8*mm,money(l['qty']*l['unit_price'])); y-=h
    y-=1*mm; c.setFillColorRGB(.68,.66,.66); c.rect(100*mm,y-8*mm,R-100*mm,8*mm,fill=1,stroke=1); c.setFillColorRGB(0,0,0); c.setFont('Helvetica-Bold',9.5); c.drawString(103*mm,y-5.5*mm,'TOTAL en Ariary'); c.drawRightString(R-2*mm,y-5.5*mm,money(total)); y-=25*mm
    c.setFont('Helvetica-Bold',9); c.drawString(L+5*mm,y,'Arrêté à la somme de :'); c.setFont('Helvetica',9); c.drawString(L+44*mm,y,number_words(total)+'.')
    # bottom reference block fixed near bottom
    by=50*mm; c.setFillColorRGB(.7,.69,.69); c.rect(L,by,R-L,40*mm,fill=1,stroke=0); c.setFillColorRGB(.45,.43,.43); c.rect(L+2*mm,by+32*mm,R-L-4*mm,7*mm,fill=1,stroke=0); c.setFillColorRGB(0,0,0); c.setFont('Helvetica-Bold',9); c.drawString(L+4*mm,by+34*mm,'RÉFÉRENCE :'); c.setFont('Helvetica-Bold',8.5); c.drawString(L+4*mm,by+25*mm,f"Devis : {'-' if d['kind']=='Devis' else ''}"); c.drawString(L+4*mm,by+19*mm,f"Bon de commande : {d['po_number'] or ''}"); c.drawString(L+4*mm,by+13*mm,f"Modalité du règlement : {d['payment_terms'] or ''}"); c.drawString(L+4*mm,by+7*mm,f"Date et lieu de livraison : {d['delivery'] or ''}")
    c.setFont('Helvetica-Bold',9); c.drawString(L+10*mm,41*mm,'Coordonnées bancaires de la société :'); c.setFillColorRGB(.78,.77,.77); c.rect(L,19*mm,R-L,18*mm,fill=1,stroke=1); c.setFillColorRGB(0,0,0); c.setFont('Helvetica',8.5); c.drawString(L+2*mm,32*mm,f"Banque : {COMPANY['bank']}"); c.setFont('Helvetica-Bold',8); heads=['Code banque','Code guichet','N° de compte','Clé']; vals=[COMPANY['bank_code'],COMPANY['branch_code'],COMPANY['account'],COMPANY['key']]; xs=[L+30*mm,L+72*mm,L+120*mm,L+164*mm]
    for i,h in enumerate(heads): c.drawCentredString(xs[i],26*mm,h); c.setFont('Helvetica',8); c.drawCentredString(xs[i],21.5*mm,vals[i]); c.setFont('Helvetica-Bold',8)
    c.showPage(); c.save(); return send_file(out,as_attachment=True,download_name=out.name)

if __name__=='__main__':
    init_db(); app.run(host='0.0.0.0',port=5000,debug=False)
