import os
import secrets
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from flask import current_app
import hashlib
import hmac
import math
import io
import calendar
from PIL import Image, UnidentifiedImageError
from functools import wraps

import psycopg2
from psycopg2.extras import RealDictCursor
from flask import Flask, request, session, redirect, url_for, flash, abort, render_template, jsonify, Response
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
if not os.environ.get("SECRET_KEY"):
    raise RuntimeError("SECRET_KEY manquant")
if not os.environ.get("DATABASE_URL"):
    raise RuntimeError("DATABASE_URL manquant")
if not os.environ.get("SETUP_KEY"):
    raise RuntimeError("SETUP_KEY manquant")
app.secret_key = os.environ["SECRET_KEY"]
app.config.update(SESSION_COOKIE_SECURE=True, SESSION_COOKIE_HTTPONLY=True,
                  SESSION_COOKIE_SAMESITE="Lax", PERMANENT_SESSION_LIFETIME=timedelta(days=7),
                  MAX_CONTENT_LENGTH=3 * 1024 * 1024)

DB_URL = os.environ["DATABASE_URL"]
TABLE_SQL = [
"""CREATE TABLE IF NOT EXISTS users (
 id SERIAL PRIMARY KEY, name TEXT NOT NULL, email TEXT NOT NULL UNIQUE,
 password_hash TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'technicien',
 created_at TIMESTAMPTZ DEFAULT NOW())""",
"""CREATE TABLE IF NOT EXISTS clients (
 id SERIAL PRIMARY KEY, name TEXT NOT NULL, contact TEXT, phone TEXT, email TEXT,
 city TEXT, address TEXT, notes TEXT, created_at TIMESTAMPTZ DEFAULT NOW())""",
"""CREATE TABLE IF NOT EXISTS machines (
 id SERIAL PRIMARY KEY, ref TEXT NOT NULL UNIQUE, brand TEXT, model TEXT,
 category TEXT, energy TEXT, serial TEXT, year INTEGER, hours INTEGER,
 capacity INTEGER, lift_height NUMERIC(10,2), mast TEXT,
 stock_type TEXT NOT NULL DEFAULT 'Vente', status TEXT NOT NULL DEFAULT 'Disponible',
 client_id INTEGER REFERENCES clients(id) ON DELETE SET NULL,
 price_sale NUMERIC(14,2), price_day NUMERIC(14,2), price_month NUMERIC(14,2),
 vgp_last DATE, vgp_next DATE, description TEXT, created_at TIMESTAMPTZ DEFAULT NOW())""",
"""CREATE TABLE IF NOT EXISTS interventions (
 id SERIAL PRIMARY KEY, title TEXT NOT NULL,
 client_id INTEGER REFERENCES clients(id) ON DELETE SET NULL,
 machine_id INTEGER REFERENCES machines(id) ON DELETE SET NULL,
 tech_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
 kind TEXT DEFAULT 'SAV', status TEXT DEFAULT 'À planifier',
 scheduled_at TIMESTAMP, description TEXT, created_at TIMESTAMPTZ DEFAULT NOW())""",
"""CREATE TABLE IF NOT EXISTS tasks (
 id SERIAL PRIMARY KEY, title TEXT NOT NULL, description TEXT,
 tech_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
 due_date DATE, status TEXT DEFAULT 'À faire', created_at TIMESTAMPTZ DEFAULT NOW())""",
"""CREATE TABLE IF NOT EXISTS rentals (
 id SERIAL PRIMARY KEY,
 client_id INTEGER REFERENCES clients(id) ON DELETE SET NULL,
 machine_id INTEGER REFERENCES machines(id) ON DELETE SET NULL,
 start_date DATE, end_date DATE, amount_month NUMERIC(14,2),
 status TEXT DEFAULT 'Active', notes TEXT, created_at TIMESTAMPTZ DEFAULT NOW())""",
"""CREATE TABLE IF NOT EXISTS quotes (
 id SERIAL PRIMARY KEY, number TEXT NOT NULL UNIQUE, client_id INTEGER REFERENCES clients(id) ON DELETE SET NULL,
 amount NUMERIC(14,2), status TEXT DEFAULT 'Brouillon', details TEXT,
 created_at TIMESTAMPTZ DEFAULT NOW())""",
"""CREATE TABLE IF NOT EXISTS login_attempts (
 fingerprint TEXT PRIMARY KEY, hits INTEGER NOT NULL DEFAULT 1, updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""",
"""CREATE TABLE IF NOT EXISTS audit_events (
 id BIGSERIAL PRIMARY KEY, user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
 entity_type TEXT NOT NULL, entity_id INTEGER, operation TEXT NOT NULL,
 event_time TIMESTAMPTZ NOT NULL DEFAULT NOW())""",
"""CREATE INDEX IF NOT EXISTS idx_machine_vgp ON machines (vgp_next) WHERE vgp_next IS NOT NULL""",
"""CREATE INDEX IF NOT EXISTS idx_interventions_status ON interventions (status,created_at DESC)""",
"""CREATE INDEX IF NOT EXISTS idx_rentals_due ON rentals (end_date) WHERE status='Active'""",
"""ALTER TABLE interventions ADD COLUMN IF NOT EXISTS duration_hours NUMERIC(6,2)""",
"""ALTER TABLE interventions ADD COLUMN IF NOT EXISTS address TEXT""",
"""ALTER TABLE machines ADD COLUMN IF NOT EXISTS photo_data BYTEA""",
"""ALTER TABLE machines ADD COLUMN IF NOT EXISTS photo_mime TEXT""",
"""ALTER TABLE machines ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW()"""
]
SCHEMA_READY = False

def connect():
    return psycopg2.connect(DB_URL, cursor_factory=RealDictCursor, connect_timeout=8)

def ensure_schema():
    global SCHEMA_READY
    if SCHEMA_READY:
        return
    conn = connect()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_xact_lock(83921907)")
                for statement in TABLE_SQL:
                    cur.execute(statement)
        SCHEMA_READY = True
    finally:
        conn.close()

def execute(statement, params=(), fetch="all"):
    ensure_schema()
    conn = connect()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(statement, tuple(params))
                if fetch == "all":
                    return [dict(r) for r in cur.fetchall()]
                if fetch == "one":
                    row = cur.fetchone()
                    return dict(row) if row else None
                return None
    finally:
        conn.close()

def count(table, where="TRUE", params=()):
    return execute("SELECT count(*) AS n FROM " + table + " WHERE " + where, params, "one")["n"]

def log_change(kind, item_id, operation):
    """Minimal audit trail; do not log personal fields or passwords."""
    actor=session.get("user", {}).get("id")
    execute("INSERT INTO audit_events(user_id,entity_type,entity_id,operation) VALUES (%s,%s,%s,%s)",
            (actor, kind, item_id, operation), fetch="none")

def auth_fingerprint(email):
    # Hash both email and IP: no raw credential, address or personal data in lockout table.
    ip=request.remote_addr or "unknown"
    msg=(ip+"|"+email.lower()).encode("utf-8")
    return hmac.new(app.secret_key.encode("utf-8"),msg,hashlib.sha256).hexdigest()

def auth_blocked(email):
    f=auth_fingerprint(email)
    row=execute("SELECT hits FROM login_attempts WHERE fingerprint=%s AND updated_at>NOW()-INTERVAL '15 minutes'",(f,),fetch="one")
    return bool(row and row["hits"]>=8)

def register_failed_login(email):
    f=auth_fingerprint(email)
    execute("""INSERT INTO login_attempts(fingerprint,hits,updated_at)
                VALUES (%s,1,NOW())
                ON CONFLICT (fingerprint) DO UPDATE SET
                hits=CASE WHEN login_attempts.updated_at<NOW()-INTERVAL '15 minutes'
                          THEN 1 ELSE login_attempts.hits+1 END,
                updated_at=NOW()""",(f,),fetch="none")

def clear_failed_login(email):
    execute("DELETE FROM login_attempts WHERE fingerprint=%s",(auth_fingerprint(email),),fetch="none")

def csrf():
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_urlsafe(32)
    return session["_csrf"]

def check_csrf():
    if not secrets.compare_digest(str(request.form.get("_csrf", "")), csrf()):
        abort(403, "Formulaire expiré, rechargez la page.")

@app.context_processor
def shared():
    return {"csrf":csrf, "me":session.get("user"), "today":date.today()}

@app.after_request
def add_headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "same-origin"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Cache-Control"] = "private, no-store"
    if request.is_secure:
        resp.headers["Strict-Transport-Security"] = "max-age=31536000"
    return resp

def require_login(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if "user" not in session:
            return redirect(url_for("login", next=request.path))
        return fn(*args, **kwargs)
    return wrapper

def require_admin(fn):
    @wraps(fn)
    @require_login
    def wrapper(*args, **kwargs):
        if session["user"]["role"] != "admin":
            abort(403)
        return fn(*args, **kwargs)
    return wrapper

# Column type: text, textarea, number, decimal, date, datetime, choice, foreign
FIELDS = {
"clients": [
 ("name", "Raison sociale / Nom", "text", True),
 ("contact", "Contact", "text", False),("phone", "Téléphone", "text", False),
 ("email", "E-mail", "text", False),("city", "Ville", "text", False),
 ("address", "Adresse", "textarea", False),("notes", "Notes internes", "textarea", False)],
"machines": [
 ("ref", "Référence EMS", "text", True), ("brand", "Marque", "text", False),
 ("model", "Modèle", "text", False),("category", "Type de machine", "text", False),
 ("energy", "Énergie", "choice:Électrique|Diesel|Gaz|Autre", False),
 ("serial", "Numéro de série", "text", False),("year", "Année", "number", False),
 ("hours", "Heures", "number", False),("capacity", "Capacité (kg)", "number", False),
 ("lift_height", "Hauteur de levage (m)", "decimal", False),("mast", "Mât", "choice:Duplex|Triplex|Simplex|Autre", False),
 ("stock_type", "Catégorie", "choice:Vente|Location|Client", True),
 ("status", "Disponibilité", "choice:Disponible|En location|Vendu|Chez client|Maintenance", True),
 ("client_id", "Client associé", "foreign:clients", False),
 ("price_sale", "Prix vente (Ar)", "decimal", False),
 ("price_day", "Location / jour (Ar)", "decimal", False),("price_month", "Location / mois (Ar)", "decimal", False),
 ("vgp_last", "Dernière VGP", "date", False),("vgp_next", "Prochaine VGP", "date", False),
 ("description", "Description", "textarea", False)],
"interventions": [
 ("title", "Objet", "text", True), ("client_id", "Client", "foreign:clients", False),
 ("machine_id", "Machine", "foreign:machines", False),
 ("tech_id", "Technicien", "foreign:users", False),
 ("kind", "Type", "choice:SAV|Livraison|Reprise|VGP|Entretien", False),
 ("status", "Statut", "choice:À planifier|Planifiée|En cours|À facturer|Clôturée", False),
 ("scheduled_at", "Date et heure", "datetime", False),
 ("duration_hours", "Durée prévue (heures)", "decimal", False),
 ("address", "Adresse intervention", "text", False),
 ("description", "Travaux / compte rendu", "textarea", False)],
"tasks": [
 ("title", "Tâche", "text", True),("tech_id", "Assignée à", "foreign:users", False),
 ("due_date", "Échéance", "date", False),
 ("status", "Statut", "choice:À faire|En cours|Terminée", False),
 ("description", "Détails", "textarea", False)],
"rentals": [
 ("client_id", "Client", "foreign:clients", True),("machine_id", "Machine", "foreign:machines", True),
 ("start_date", "Début", "date", False),("end_date", "Fin", "date", False),
 ("amount_month", "Mensualité (Ar)", "decimal", False),
 ("status", "Statut", "choice:Active|Terminée", False),
 ("notes", "Notes", "textarea", False)],
"quotes": [
 ("number", "Numéro d'offre", "text", True),("client_id", "Client", "foreign:clients", False),
 ("amount", "Montant (Ar)", "decimal", False),
 ("status", "Statut", "choice:Brouillon|Envoyée|Acceptée|Refusée", False),
 ("details", "Description de l'offre", "textarea", False)]
}
TITLES={"machines":"Machines","interventions":"Interventions","tasks":"Tâches","clients":"Clients","rentals":"Locations","quotes":"Offres"}
MAIN={"machines":"ref","clients":"name","interventions":"title","tasks":"title","rentals":"id","quotes":"number"}
BADGE={"machines":"status","interventions":"status","tasks":"status","rentals":"status","quotes":"status"}

def fk_options(kind):
    if kind=="clients":
        return execute("SELECT id, name AS label FROM clients ORDER BY name LIMIT 1000")
    if kind=="machines":
        return execute("SELECT id, CONCAT(COALESCE(brand,''),' ',COALESCE(model,''),' · ',ref) AS label FROM machines ORDER BY id DESC LIMIT 1000")
    return execute("SELECT id, name AS label FROM users ORDER BY name LIMIT 100")

def validate_form(kind):
    vals={}
    for col,label,ftype,required in FIELDS[kind]:
        raw=request.form.get(col,"").strip()
        if required and not raw:
            raise ValueError("Champ obligatoire : "+label)
        if not raw:
            vals[col]=None if ftype in ("number","decimal","date","datetime") or ftype.startswith("foreign:") else ""
            continue
        if ftype=="number":
            number=int(raw)
            if number<0 or number>100000000:
                raise ValueError("Valeur hors limites : "+label)
            vals[col]=number
        elif ftype=="decimal":
            try:
                amount=Decimal(raw.replace(",","."))
            except InvalidOperation:
                raise ValueError("Montant incorrect : "+label)
            if not amount.is_finite() or abs(amount)>Decimal("999999999999"):
                raise ValueError("Montant hors limites : "+label)
            if amount<0:
                raise ValueError("La valeur ne peut pas être négative : "+label)
            vals[col]=amount
        elif ftype.startswith("foreign:"):
            vals[col]=int(raw)
        elif ftype.startswith("choice:"):
            opts=ftype.split(":",1)[1].split("|")
            if raw not in opts: raise ValueError("Valeur incorrecte : "+label)
            vals[col]=raw
        elif ftype=="date":
            vals[col]=date.fromisoformat(raw)
        elif ftype=="datetime":
            vals[col]=datetime.fromisoformat(raw)
        else:
            vals[col]=raw[:5000] if ftype=="textarea" else raw[:255]
    return vals

def label_map(kind):
    options={}
    for col,label,ftype,required in FIELDS[kind]:
        if ftype.startswith("foreign:"):
            options[col]=fk_options(ftype.split(":",1)[1])
    return options

def display_value(col, value, options):
    if value is None or value=="":
        return "—"
    if col in options:
        return next((x["label"] for x in options[col] if x["id"]==value), "—")
    if isinstance(value,datetime): return value.strftime("%d/%m/%Y %H:%M")
    if isinstance(value,date): return value.strftime("%d/%m/%Y")
    return str(value)

app.jinja_env.filters["emsvalue"] = lambda value: ("—" if value is None or value == "" else str(value))

@app.get("/health")
def health():
    # Readiness means the PostgreSQL connection actually works, not only that
    # the worker previously completed a schema check.
    try:
        execute("SELECT 1 AS ok",fetch="one")
        return jsonify({"ok": True, "app": "EMS SAV"})
    except Exception:
        current_app.logger.exception("Database health check failed")
        return jsonify({"ok": False}), 503

@app.route("/setup", methods=["GET","POST"])
def setup():
    if count("users")>0:
        return redirect(url_for("login"))
    if request.method=="POST":
        check_csrf()
        key=request.form.get("key","")
        if auth_blocked("setup"):
            flash("Trop de tentatives. Réessayez dans 15 minutes.", "error")
            return render_template("auth.html",title="Activer EMS SAV",setup=True),429
        if not secrets.compare_digest(key,os.environ["SETUP_KEY"]):
            register_failed_login("setup")
            flash("Code d'activation incorrect.", "error")
        elif len(request.form.get("password",""))<12:
            flash("Mot de passe : 12 caractères minimum.", "error")
        else:
            name=request.form.get("name","").strip()
            email=request.form.get("email","").strip().lower()
            if not name or "@" not in email:
                flash("Nom ou e-mail incorrect.", "error")
            else:
                execute("INSERT INTO users (name,email,password_hash,role) VALUES (%s,%s,%s,'admin') ON CONFLICT (email) DO NOTHING",
                        (name,email,generate_password_hash(request.form["password"])),fetch="none")
                flash("Compte administrateur créé. Connectez-vous.", "success")
                return redirect(url_for("login"))
    return render_template("auth.html",title="Activer EMS SAV",setup=True)

@app.route("/login", methods=["GET","POST"])
def login():
    if request.method=="POST":
        check_csrf()
        email=request.form.get("email","").strip().lower()
        pwd=request.form.get("password","")
        user=execute("SELECT id,name,email,role,password_hash FROM users WHERE email=%s",(email,),fetch="one")
        if auth_blocked(email):
            flash("Trop de tentatives. Réessayez dans 15 minutes.", "error")
            return render_template("auth.html",title="Connexion EMS SAV",setup=False),429
        if user and check_password_hash(user["password_hash"],pwd):
            clear_failed_login(email)
            session.clear()
            session.permanent=True
            session["user"]={k:user[k] for k in ("id","name","email","role")}
            csrf()
            return redirect(url_for("dashboard"))
        register_failed_login(email)
        flash("Identifiants invalides.", "error")
    if count("users")==0:
        return redirect(url_for("setup"))
    return render_template("auth.html",title="Connexion EMS SAV",setup=False)

@app.post("/logout")
@require_login
def logout():
    check_csrf()
    session.clear()
    return redirect(url_for("login"))

@app.get("/")
@require_login
def dashboard():
    stats={"machines":count("machines","status='Disponible'"),
           "interventions":count("interventions","status IN ('À planifier','Planifiée','En cours')"),
           "invoice":count("interventions","status='À facturer'"),
           "tasks":count("tasks","status!='Terminée'")}
    overdue=execute("SELECT * FROM machines WHERE vgp_next IS NOT NULL AND vgp_next<=%s ORDER BY vgp_next ASC LIMIT 12",(date.today(),))
    plan=execute("SELECT * FROM interventions WHERE status='À planifier' ORDER BY created_at DESC LIMIT 12")
    upcoming=execute("SELECT * FROM rentals WHERE status='Active' AND end_date IS NOT NULL AND end_date<=%s ORDER BY end_date ASC LIMIT 12",
                     (date.today()+timedelta(days=30),))
    names={x["id"]:x["name"] for x in fk_options("clients")}
    return render_template("dashboard.html",stats=stats,overdue=overdue,plan=plan,upcoming=upcoming,names=names,title="Tableau de bord")

@app.get("/r/<kind>")
@require_login
def listing(kind):
    if kind not in FIELDS: abort(404)
    category=request.args.get("categorie","")
    search=request.args.get("q","").strip()[:120]
    status=request.args.get("statut","")[:40]
    tech=request.args.get("technicien","")
    type_filter=request.args.get("type","")[:40]
    order=request.args.get("tri","")[:30]
    where=[];params=[]
    if kind=="machines" and category in ("Vente","Location","Client"):
        where.append("stock_type=%s");params.append(category)
    if kind in ("interventions","tasks","rentals") and status:
        valid={"interventions":["À planifier","Planifiée","En cours","À facturer","Clôturée"],
               "tasks":["À faire","En cours","Terminée"],
               "rentals":["Active","Terminée"]}[kind]
        if status in valid:
            where.append("status=%s");params.append(status)
    if kind=="interventions" and type_filter in ["SAV","Livraison","Reprise","VGP","Entretien"]:
        where.append("kind=%s");params.append(type_filter)
    if kind in ("interventions","tasks") and tech.isdigit():
        where.append("tech_id=%s");params.append(int(tech))
    if search:
        cols=[MAIN[kind]]
        if kind=="machines": cols+=["brand","model","serial"]
        if kind=="clients": cols+=["city","contact"]
        where.append("("+" OR ".join("CAST("+col+" AS TEXT) ILIKE %s" for col in cols)+")")
        params.extend(["%"+search+"%"]*len(cols))
    sql="SELECT * FROM "+kind
    if where: sql+=" WHERE "+" AND ".join(where)
    sort_map={
        "machines":{"prix":"price_sale","heures":"hours","annee":"year","capacite":"capacity","nouveaux":"id"},
        "interventions":{"date":"scheduled_at","nouveaux":"id"}}
    sort_col=sort_map.get(kind,{}).get(order,"id")
    direction="ASC" if request.args.get("sens")=="asc" else "DESC"
    sql+=" ORDER BY "+sort_col+" "+direction+" NULLS LAST, id DESC LIMIT 250"
    rows=execute(sql,params)
    options=label_map(kind)
    counts={}
    if kind=="machines":
        for row in execute("SELECT stock_type, count(*) AS n FROM machines GROUP BY stock_type"):
            counts[row["stock_type"]]=row["n"]
        counts["Toutes"]=sum(counts.values())
    return render_template("list.html",title=TITLES[kind],kind=kind,rows=rows,
                           main=MAIN[kind],badgestatus=BADGE.get(kind),counts=counts,
                           fields=FIELDS[kind],options=options,search=search,category=category,
                           status=status,tech=tech,type_filter=type_filter,order=order,
                           direction=direction,display_value=display_value)

@app.route("/r/<kind>/new",methods=["GET","POST"])
@app.route("/r/<kind>/<int:item_id>/edit",methods=["GET","POST"])
@require_login
def edit(kind,item_id=None):
    if kind not in FIELDS: abort(404)
    if kind in ("quotes","clients","machines","rentals") and session["user"]["role"]!="admin": abort(403)
    row=None
    if item_id is not None:
        row=execute("SELECT * FROM "+kind+" WHERE id=%s",(item_id,),fetch="one")
        if not row: abort(404)
    if request.method=="POST":
        check_csrf()
        try:
            data=validate_form(kind)
            cols=list(data.keys())
            if item_id is None:
                sql="INSERT INTO "+kind+" ("+",".join(cols)+") VALUES ("+",".join(["%s"]*len(cols))+") RETURNING id"
                new=execute(sql,list(data.values()),fetch="one")
                item_id=new["id"]
                log_change(kind,item_id,"création")
                flash("Fiche enregistrée.", "success")
            else:
                sql="UPDATE "+kind+" SET "+",".join(c+"=%s" for c in cols)+" WHERE id=%s"
                execute(sql,list(data.values())+[item_id],fetch="none")
                log_change(kind,item_id,"modification")
                flash("Modifications enregistrées.", "success")
            return redirect(url_for("detail",kind=kind,item_id=item_id))
        except (ValueError,psycopg2.Error) as exc:
            flash("Enregistrement impossible : "+str(exc)[:190], "error")
            row=dict(request.form)
    return render_template("form.html",title=("Modifier " if item_id else "Ajouter ") + TITLES[kind].lower(),
                           kind=kind,fields=FIELDS[kind],row=row or {},options=label_map(kind))

@app.get("/r/<kind>/<int:item_id>")
@require_login
def detail(kind,item_id):
    if kind not in FIELDS: abort(404)
    row=execute("SELECT * FROM "+kind+" WHERE id=%s",(item_id,),fetch="one")
    if not row: abort(404)
    return render_template("detail.html",title=TITLES[kind],kind=kind,item=row,
                           fields=FIELDS[kind],options=label_map(kind),display_value=display_value)

@app.post("/r/<kind>/<int:item_id>/status")
@require_login
def set_status(kind,item_id):
    check_csrf()
    statuses={"tasks":["À faire","En cours","Terminée"],
              "interventions":["À planifier","Planifiée","En cours","À facturer","Clôturée"],
              "rentals":["Active","Terminée"]}
    if kind not in statuses: abort(404)
    status=request.form.get("status","")
    if status not in statuses[kind]: abort(400)
    execute("UPDATE "+kind+" SET status=%s WHERE id=%s",(status,item_id),fetch="none")
    log_change(kind,item_id,"statut : "+status)
    return redirect(url_for("detail",kind=kind,item_id=item_id))


@app.route("/planning")
@require_login
def planning():
    """Liste et calendrier mensuel, basé sur les interventions EMS."""
    today=date.today()
    try:
        year=int(request.args.get("annee",today.year))
        month=int(request.args.get("mois",today.month))
        if not (2020<=year<=2100 and 1<=month<=12):
            raise ValueError
    except ValueError:
        return redirect(url_for("planning"))
    first=date(year,month,1)
    nxt=date(year+1,1,1) if month==12 else date(year,month+1,1)
    prev=date(year-1,12,1) if month==1 else date(year,month-1,1)
    last=(nxt-timedelta(days=1))
    jobs=execute("""SELECT i.id,i.title,i.kind,i.status,i.scheduled_at,
                   c.name AS client_name,u.name AS tech_name
                   FROM interventions i LEFT JOIN clients c ON c.id=i.client_id
                   LEFT JOIN users u ON u.id=i.tech_id
                   WHERE i.scheduled_at >= %s AND i.scheduled_at < %s
                   ORDER BY i.scheduled_at, i.id""",
                 (first,nxt))
    by_day={}
    for job in jobs:
        key=job["scheduled_at"].date()
        by_day.setdefault(key,[]).append(job)
    weeks=calendar.Calendar(firstweekday=0).monthdatescalendar(year,month)
    return render_template("planning.html",title="Planning",weeks=weeks,
                           jobs=jobs,by_day=by_day,month_label=first.strftime("%m/%Y"),
                           month=month,year=year,previous=prev,following=nxt,today=today)

@app.post("/r/machines/<int:item_id>/photo")
@require_admin
def upload_machine_photo(item_id):
    check_csrf()
    upload=request.files.get("photo")
    if upload is None or not upload.filename:
        flash("Sélectionne une photo.", "error")
        return redirect(url_for("detail",kind="machines",item_id=item_id))
    try:
        raw=upload.read(2*1024*1024+1)
        if len(raw)>2*1024*1024:
            raise ValueError("Photo supérieure à 2 Mo.")
        img=Image.open(io.BytesIO(raw))
        img.verify()
        img=Image.open(io.BytesIO(raw))
        img.thumbnail((1200,1200))
        if img.mode not in ("RGB","RGBA"): img=img.convert("RGB")
        buffer=io.BytesIO()
        img.save(buffer,format="WEBP",quality=80,method=4)
        content=buffer.getvalue()
        if len(content)>1200000:
            raise ValueError("Photo trop volumineuse après compression.")
        execute("UPDATE machines SET photo_data=%s, photo_mime=%s, updated_at=NOW() WHERE id=%s",
                (psycopg2.Binary(content),"image/webp",item_id),fetch="none")
        log_change("machines",item_id,"nouvelle photo")
        flash("Photo enregistrée.", "success")
    except (UnidentifiedImageError,OSError,ValueError,Image.DecompressionBombError):
        flash("Image invalide ou trop volumineuse. Utilise JPG, PNG ou WebP.", "error")
    return redirect(url_for("detail",kind="machines",item_id=item_id))

@app.get("/r/machines/<int:item_id>/photo")
@require_login
def machine_photo(item_id):
    photo=execute("SELECT photo_data,photo_mime FROM machines WHERE id=%s",(item_id,),fetch="one")
    if photo is None or not photo.get("photo_data"):
        abort(404)
    content=bytes(photo["photo_data"])
    resp=Response(content,mimetype="image/webp")
    resp.headers["Cache-Control"]="private, max-age=300"
    return resp

@app.route("/users",methods=["GET","POST"])
@require_admin
def users():
    if request.method=="POST":
        check_csrf()
        name=request.form.get("name","").strip()
        email=request.form.get("email","").strip().lower()
        password=request.form.get("password","")
        role=request.form.get("role","technicien")
        if role not in ("admin","technicien") or not name or "@" not in email or len(password)<12:
            flash("Nom, e-mail, rôle ou mot de passe incorrect (12 caractères min).","error")
        else:
            try:
                new_user=execute("INSERT INTO users(name,email,password_hash,role) VALUES (%s,%s,%s,%s) RETURNING id",
                        (name,email,generate_password_hash(password),role),fetch="one")
                log_change("users",new_user["id"],"création")
                flash("Collaborateur créé.","success")
                return redirect(url_for("users"))
            except psycopg2.Error:
                flash("Cette adresse e-mail est peut-être déjà utilisée.","error")
    rows=execute("SELECT id,name,email,role FROM users ORDER BY name")
    return render_template("users.html",title="Collaborateurs",rows=rows)

@app.route("/password",methods=["GET","POST"])
@require_login
def password():
    if request.method=="POST":
        check_csrf()
        current=execute("SELECT password_hash FROM users WHERE id=%s",(session["user"]["id"],),fetch="one")
        new=request.form.get("new","")
        if current and check_password_hash(current["password_hash"],request.form.get("old","")) and len(new)>=12:
            execute("UPDATE users SET password_hash=%s WHERE id=%s",
                    (generate_password_hash(new),session["user"]["id"]),fetch="none")
            flash("Mot de passe modifié.","success")
        else:
            flash("Mot de passe incorrect ou trop court (12 caractères min).","error")
    return render_template("password.html",title="Sécurité du compte")

@app.get("/manifest.webmanifest")
def manifest():
    return jsonify({"name":"EMS Tamatave · SAV","short_name":"EMS SAV","start_url":"/",
                    "display":"standalone","background_color":"#f7faf8","theme_color":"#08784d",
                    "icons":[{"src":"/icon.svg","sizes":"any","type":"image/svg+xml","purpose":"any maskable"}]})

@app.get("/icon.svg")
def icon():
    svg='<svg xmlns="http://www.w3.org/2000/svg" width="512" height="512" viewBox="0 0 512 512"><rect width="512" height="512" rx="110" fill="#08784d"/><text x="256" y="260" fill="white" font-size="140" font-weight="900" text-anchor="middle" font-family="Arial">EMS</text><text x="256" y="324" fill="#ceeedc" font-size="38" font-weight="700" text-anchor="middle" font-family="Arial">SAV</text></svg>'
    return Response(svg,mimetype="image/svg+xml")
