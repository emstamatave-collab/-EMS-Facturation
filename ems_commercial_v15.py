
import os, re, html
from datetime import date
from flask import request, redirect, url_for, flash, send_file
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader

LEGACY = None
APP = None

def _h(v):
    return html.escape(str(v or ""), quote=True)

def _schema():
    con = LEGACY.db()
    try:
        if os.environ.get("DATABASE_URL", "").strip():
            for sql in [
                "ALTER TABLE docs ADD COLUMN IF NOT EXISTS currency TEXT DEFAULT 'MGA'",
                "ALTER TABLE docs ADD COLUMN IF NOT EXISTS exchange_rate DOUBLE PRECISION DEFAULT 0",
                "ALTER TABLE docs ADD COLUMN IF NOT EXISTS show_conversion INTEGER DEFAULT 0",
                "ALTER TABLE lines ADD COLUMN IF NOT EXISTS mms_ref TEXT DEFAULT ''",
                "ALTER TABLE lines ADD COLUMN IF NOT EXISTS supplier_ref TEXT DEFAULT ''",
                "ALTER TABLE lines ADD COLUMN IF NOT EXISTS supplier_name TEXT DEFAULT ''",
                "ALTER TABLE lines ADD COLUMN IF NOT EXISTS purchase_price DOUBLE PRECISION DEFAULT 0",
                "ALTER TABLE lines ADD COLUMN IF NOT EXISTS internal_note TEXT DEFAULT ''",
            ]:
                con.execute(sql)
        else:
            LEGACY.ensure_column(con, "docs", "currency", "TEXT DEFAULT 'MGA'")
            LEGACY.ensure_column(con, "docs", "exchange_rate", "REAL DEFAULT 0")
            LEGACY.ensure_column(con, "docs", "show_conversion", "INTEGER DEFAULT 0")
            LEGACY.ensure_column(con, "lines", "mms_ref", "TEXT DEFAULT ''")
            LEGACY.ensure_column(con, "lines", "supplier_ref", "TEXT DEFAULT ''")
            LEGACY.ensure_column(con, "lines", "supplier_name", "TEXT DEFAULT ''")
            LEGACY.ensure_column(con, "lines", "purchase_price", "REAL DEFAULT 0")
            LEGACY.ensure_column(con, "lines", "internal_note", "TEXT DEFAULT ''")
        con.execute("""CREATE TABLE IF NOT EXISTS app_settings(
            key TEXT PRIMARY KEY,
            value TEXT
        )""")
        con.execute("""CREATE TABLE IF NOT EXISTS supplier_map(
            mms_ref TEXT PRIMARY KEY,
            supplier_name TEXT DEFAULT 'TVH',
            supplier_ref TEXT DEFAULT ''
        )""")
        row = con.execute("select value from app_settings where key='eur_mga_rate'").fetchone()
        if not row:
            con.execute("insert into app_settings(key,value) values(?,?)", ("eur_mga_rate","0"))
        con.commit()
    finally:
        con.close()

def _setting(key, default=""):
    con = LEGACY.db()
    try:
        r = con.execute("select value from app_settings where key=?", (key,)).fetchone()
        return (r["value"] if r else default) or default
    finally:
        con.close()

def _set_setting(key, value):
    con = LEGACY.db()
    try:
        r = con.execute("select key from app_settings where key=?", (key,)).fetchone()
        if r:
            con.execute("update app_settings set value=? where key=?", (str(value), key))
        else:
            con.execute("insert into app_settings(key,value) values(?,?)", (key,str(value)))
        con.commit()
    finally:
        con.close()

def default_rate():
    return max(0.0, LEGACY.parse_decimal(_setting("eur_mga_rate","0"), 0))

def supplier_for_mms(mms_ref):
    ref = str(mms_ref or "").strip().upper()
    if not ref:
        return "", ""
    con = LEGACY.db()
    try:
        r = con.execute("select supplier_name,supplier_ref from supplier_map where upper(mms_ref)=upper(?)", (ref,)).fetchone()
        if r:
            return (r["supplier_name"] or "TVH"), (r["supplier_ref"] or "")
    finally:
        con.close()
    return ("TVH", "") if ref.startswith("MMS-") else ("", "")

def _currency(v):
    return "EUR" if str(v or "").upper() == "EUR" else "MGA"

def _symbol(cur):
    return "€" if _currency(cur) == "EUR" else "Ar"

def _fmt(v, cur):
    v = float(v or 0)
    if _currency(cur) == "EUR":
        s = f"{v:,.2f}".replace(",", " ").replace(".", ",")
        return f"{s} €"
    return f"{v:,.0f}".replace(",", " ") + " Ar"

def _fmt_plain(v, cur):
    v = float(v or 0)
    if _currency(cur) == "EUR":
        return f"{v:,.2f}".replace(",", " ").replace(".", ",")
    return f"{v:,.0f}".replace(",", " ")

def _convert(v, cur, rate):
    rate = float(rate or 0)
    if rate <= 0:
        return None, ("MGA" if _currency(cur)=="EUR" else "EUR")
    if _currency(cur) == "EUR":
        return float(v or 0) * rate, "MGA"
    return float(v or 0) / rate, "EUR"

def _line_sale(l):
    return float(l["qty"] or 0) * float(l["unit_price"] or 0) * (1-float(l["discount_pct"] or 0)/100.0)

def _line_cost(l):
    return float(l["qty"] or 0) * float(l["purchase_price"] or 0)

def _totals(lines):
    sale = sum(_line_sale(l) for l in lines)
    cost = sum(_line_cost(l) for l in lines)
    margin = sale-cost
    rate_margin = (margin/cost*100) if cost else 0
    rate_mark = (margin/sale*100) if sale else 0
    return sale,cost,margin,rate_margin,rate_mark

def _save_lines(con, doc_id):
    names = ["description","qty","mms_ref","supplier_ref","supplier_name","purchase_price","unit_price","discount_pct","line_internal_note"]
    data = {n: request.form.getlist(n) for n in names}
    n = max([len(v) for v in data.values()] or [0])
    for i in range(n):
        def g(name, default=""):
            arr=data[name]
            return arr[i] if i < len(arr) else default
        desc=g("description").strip()
        mms=g("mms_ref").strip().upper()
        sref=g("supplier_ref").strip()
        sname=g("supplier_name").strip()
        if mms and not sname:
            sname, auto_ref = supplier_for_mms(mms)
            if not sref:
                sref = auto_ref
        if not (desc or mms or sref):
            continue
        con.execute("""insert into lines(
            doc_id,description,qty,unit_price,discount_pct,mms_ref,supplier_ref,supplier_name,purchase_price,internal_note
        ) values(?,?,?,?,?,?,?,?,?,?)""",(
            doc_id,desc,LEGACY.parse_decimal(g("qty","1"),1),LEGACY.parse_decimal(g("unit_price"),0),
            max(0,min(100,LEGACY.parse_decimal(g("discount_pct"),0))),mms,sref,sname,
            LEGACY.parse_decimal(g("purchase_price"),0),g("line_internal_note")
        ))

def _line_rows(lines, minimum=8):
    rows = list(lines)
    while len(rows) < minimum:
        rows.append({
            "description":"","qty":1,"mms_ref":"","supplier_ref":"","supplier_name":"",
            "purchase_price":0,"unit_price":0,"discount_pct":0,"internal_note":""
        })
    out=[]
    for l in rows:
        out.append(f"""<tr class="ems-line">
<td><textarea name="description" style="min-width:220px">{_h(l['description'])}</textarea></td>
<td><input class="linefield qtyfield qty" name="qty" inputmode="decimal" value="{float(l['qty'] or 1):g}"></td>
<td><input name="mms_ref" value="{_h(l['mms_ref'])}" placeholder="MMS-..."></td>
<td><input name="supplier_ref" value="{_h(l['supplier_ref'])}" placeholder="Réf. TVH"></td>
<td><input name="supplier_name" value="{_h(l['supplier_name'])}" placeholder="TVH"></td>
<td><input class="purchase" name="purchase_price" inputmode="decimal" value="{float(l['purchase_price'] or 0):g}" placeholder="0"></td>
<td><input class="sale" name="unit_price" inputmode="decimal" value="{float(l['unit_price'] or 0):g}" placeholder="0"></td>
<td><input class="discount" name="discount_pct" inputmode="decimal" value="{float(l['discount_pct'] or 0):g}"></td>
<td><input class="equiv" readonly tabindex="-1" placeholder="Auto"></td>
<td><input class="margin" readonly tabindex="-1" placeholder="Auto"></td>
<td><input name="line_internal_note" value="{_h(l['internal_note'])}" placeholder="Note interne"></td>
</tr>""")
    return "".join(out)

_FORM_CSS = """
<style>
.ems-internal{background:#fff8e8;border:1px solid #f0d69b;border-radius:12px;padding:14px;margin:14px 0}
.ems-internal h3{margin-top:0}.ems-check{display:flex;gap:9px;align-items:center}.ems-check input{width:auto;min-height:auto}
.ems-line-table{font-size:13px}.ems-line-table input,.ems-line-table textarea{font-size:14px;min-height:42px;padding:8px}.ems-line-table textarea{min-width:190px}
@media(max-width:760px){
.ems-line-table,.ems-line-table tbody,.ems-line-table tr,.ems-line-table td{display:block;width:100%}.ems-line-table thead{display:none}
.ems-line-table tr{border:1px solid #e5e7eb;border-radius:12px;padding:10px;margin-bottom:14px;background:#fff}
.ems-line-table td{border:0;padding:5px}.ems-line-table td:before{display:block;font-weight:700;font-size:12px;color:#555;margin-bottom:3px}
.ems-line-table td:nth-child(1):before{content:'Désignation client'}
.ems-line-table td:nth-child(2):before{content:'Quantité'}
.ems-line-table td:nth-child(3):before{content:'Réf. MMS — interne'}
.ems-line-table td:nth-child(4):before{content:'Réf. fournisseur — interne'}
.ems-line-table td:nth-child(5):before{content:'Fournisseur — interne'}
.ems-line-table td:nth-child(6):before{content:'Prix achat — interne'}
.ems-line-table td:nth-child(7):before{content:'Prix vente'}
.ems-line-table td:nth-child(8):before{content:'Remise %'}
.ems-line-table td:nth-child(9):before{content:'Conversion — interne'}
.ems-line-table td:nth-child(10):before{content:'Marge ligne — interne'}
.ems-line-table td:nth-child(11):before{content:'Note ligne — interne'}
}
</style>
"""

_FORM_JS = """
<script>
(function(){
 const cur=document.getElementById('currency'), rate=document.getElementById('exchange_rate');
 function num(v){return parseFloat(String(v||'').replace(/\\s/g,'').replace(',','.'))||0}
 function fmt(v,c){return c==='EUR'?v.toLocaleString('fr-FR',{minimumFractionDigits:2,maximumFractionDigits:2})+' €':Math.round(v).toLocaleString('fr-FR')+' Ar'}
 function calc(){
  const c=cur?cur.value:'MGA', r=num(rate?rate.value:0);
  document.querySelectorAll('.ems-line').forEach(tr=>{
   const q=num(tr.querySelector('.qty')?.value), sale=num(tr.querySelector('.sale')?.value), buy=num(tr.querySelector('.purchase')?.value), disc=num(tr.querySelector('.discount')?.value);
   const other=(c==='EUR'?(sale*r):(r>0?sale/r:0)), oc=(c==='EUR'?'MGA':'EUR');
   const eq=tr.querySelector('.equiv'); if(eq) eq.value=r>0?fmt(other,oc):'Taux à définir';
   const margin=(sale*(1-disc/100)-buy)*q, m=tr.querySelector('.margin'); if(m) m.value=fmt(margin,c);
  });
 }
 document.addEventListener('input',e=>{if(e.target.closest('.ems-line')||e.target===cur||e.target===rate)calc()});
 document.addEventListener('change',e=>{if(e.target===cur)calc()});
 calc();
})();
</script>
"""

def _currency_block(currency, rate, show):
    return f"""<div class="ems-internal"><h3>Devise & conversion — interne</h3>
<div class="grid3">
<div><label>Devise du document</label><select name="currency" id="currency"><option value="MGA" {'selected' if currency=='MGA' else ''}>Ariary (Ar)</option><option value="EUR" {'selected' if currency=='EUR' else ''}>Euro (€)</option></select></div>
<div><label>Taux enregistré pour ce document</label><input id="exchange_rate" name="exchange_rate" inputmode="decimal" value="{float(rate or 0):g}" placeholder="1 € = ... Ar"><span class="small muted">Ce taux reste figé sur ce devis/facture.</span></div>
<div><label>Affichage client</label><label class="ems-check"><input type="checkbox" name="show_conversion" value="1" {'checked' if show else ''}> Afficher la conversion sur le PDF client</label><span class="small muted">Décoché = conversion invisible au client.</span></div>
</div><p class="small"><a href="/settings">Modifier le taux général utilisé pour les nouveaux documents</a></p></div>"""

def new_document():
    con=LEGACY.db()
    cls=con.execute("select * from clients order by name").fetchall()
    if request.method=="POST":
        kind=request.form.get("kind") or "Devis"
        number=request.form.get("number") or LEGACY.next_number(kind)
        curcode=_currency(request.form.get("currency"))
        rate=max(0,LEGACY.parse_decimal(request.form.get("exchange_rate"),default_rate()))
        show=1 if request.form.get("show_conversion")=="1" else 0
        cur=con.execute("""insert into docs(
          kind,number,doc_date,due_date,client_id,reference,po_number,payment_terms,delivery,status,notes,internal_note,currency,exchange_rate,show_conversion
        ) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(
          kind,number,request.form.get("doc_date"),request.form.get("due_date"),request.form.get("client_id") or None,
          request.form.get("reference"),request.form.get("po_number"),request.form.get("payment_terms"),request.form.get("delivery"),
          "Brouillon",request.form.get("notes"),request.form.get("internal_note"),curcode,rate,show
        ))
        did=cur.lastrowid
        _save_lines(con,did)
        for f in request.files.getlist("photos"):
            saved=LEGACY.save_upload(f,f"doc_{did}")
            if saved and saved[2].startswith("image/"):
                con.execute("insert into doc_images(doc_id,original_name,stored_name,mime) values(?,?,?,?)",(did,*saved))
        con.commit(); con.close()
        return redirect(url_for("document",doc_id=did))
    con.close()
    today=date.today(); due=today+LEGACY.timedelta(days=30)
    opts="".join(f"<option value='{c['id']}'>{_h(c['name'])}</option>" for c in cls)
    rate=default_rate()
    rows=_line_rows([],8)
    return LEGACY.page(_FORM_CSS+f"""<div class="card"><h2>Nouveau devis / facture</h2>
<form method="post" enctype="multipart/form-data">
<div class="grid3"><div><label>Type</label><select name="kind"><option>Devis</option><option>Facture</option></select></div><div><label>Numéro</label><input name="number" placeholder="Automatique si vide"></div><div><label>Client</label><select name="client_id"><option value="">-- Choisir --</option>{opts}</select></div>
<div><label>Date</label><input type="date" name="doc_date" value="{today.isoformat()}"></div><div><label>Échéance</label><input type="date" name="due_date" value="{due.isoformat()}"></div><div><label>Bon de commande</label><input name="po_number"></div></div>
<p><label>Référence / objet</label><input name="reference"></p>
<div class="grid"><div><label>Modalité de règlement</label><input name="payment_terms" value="Virement sous 30 jours"></div><div><label>Livraison / travaux</label><input name="delivery"></div></div>
{_currency_block("MGA",rate,False)}
<p><label>Notes visibles / générales</label><textarea name="notes"></textarea></p>
<p><label>Note interne EMS — jamais affichée sur le PDF client</label><textarea name="internal_note"></textarea></p>
<p><label>Photos à insérer</label><input type="file" name="photos" multiple accept="image/*"></p>
<h3>Lignes</h3><div class="tablewrap"><table class="ems-line-table"><thead><tr><th>Désignation client</th><th>Qté</th><th>Réf. MMS</th><th>Réf. fournisseur</th><th>Fournisseur</th><th>Prix achat</th><th>Prix vente</th><th>Remise %</th><th>Conversion</th><th>Marge ligne</th><th>Note interne</th></tr></thead><tbody>{rows}</tbody></table></div>
<p><button>Créer le document</button></p></form></div>"""+_FORM_JS)

def edit_document(doc_id):
    con=LEGACY.db()
    d=con.execute("select * from docs where id=?",(doc_id,)).fetchone()
    cls=con.execute("select * from clients order by name").fetchall()
    lines=con.execute("select * from lines where doc_id=? order by id",(doc_id,)).fetchall()
    if not d:
        con.close(); return "Document introuvable",404
    if request.method=="POST":
        curcode=_currency(request.form.get("currency"))
        rate=max(0,LEGACY.parse_decimal(request.form.get("exchange_rate"),float(d["exchange_rate"] or 0)))
        show=1 if request.form.get("show_conversion")=="1" else 0
        con.execute("""update docs set kind=?,number=?,doc_date=?,due_date=?,client_id=?,reference=?,po_number=?,payment_terms=?,delivery=?,status=?,notes=?,internal_note=?,currency=?,exchange_rate=?,show_conversion=? where id=?""",(
            request.form.get("kind"),request.form.get("number"),request.form.get("doc_date"),request.form.get("due_date"),
            request.form.get("client_id") or None,request.form.get("reference"),request.form.get("po_number"),request.form.get("payment_terms"),
            request.form.get("delivery"),request.form.get("status"),request.form.get("notes"),request.form.get("internal_note"),
            curcode,rate,show,doc_id
        ))
        con.execute("delete from lines where doc_id=?",(doc_id,))
        _save_lines(con,doc_id)
        for f in request.files.getlist("photos"):
            saved=LEGACY.save_upload(f,f"doc_{doc_id}")
            if saved and saved[2].startswith("image/"):
                con.execute("insert into doc_images(doc_id,original_name,stored_name,mime) values(?,?,?,?)",(doc_id,*saved))
        con.commit(); con.close(); flash("Document mis à jour."); return redirect(url_for("document",doc_id=doc_id))
    con.close()
    opts="".join(f"<option value='{c['id']}' {'selected' if c['id']==d['client_id'] else ''}>{_h(c['name'])}</option>" for c in cls)
    statuses=["Brouillon","Envoyé","Accepté","Refusé","Facturé","Partiellement payé","Payé","Annulé"]
    sopts="".join(f"<option {'selected' if s==d['status'] else ''}>{s}</option>" for s in statuses)
    rows=_line_rows(lines,max(8,len(lines)+3))
    curcode=_currency(d["currency"]); rate=float(d["exchange_rate"] or 0); show=bool(d["show_conversion"])
    return LEGACY.page(_FORM_CSS+f"""<div class="card"><h2>Modifier {_h(d['kind'])} {_h(d['number'])}</h2>
<form method="post" enctype="multipart/form-data">
<div class="grid3"><div><label>Type</label><select name="kind"><option {'selected' if d['kind']=='Devis' else ''}>Devis</option><option {'selected' if d['kind']=='Facture' else ''}>Facture</option></select></div>
<div><label>Numéro</label><input name="number" value="{_h(d['number'])}"></div><div><label>Statut</label><select name="status">{sopts}</select></div>
<div><label>Client</label><select name="client_id"><option value="">--</option>{opts}</select></div>
<div><label>Date</label><input type="date" name="doc_date" value="{_h(d['doc_date'])}"></div><div><label>Échéance</label><input type="date" name="due_date" value="{_h(d['due_date'])}"></div>
<div><label>Bon de commande</label><input name="po_number" value="{_h(d['po_number'])}"></div></div>
<p><label>Référence</label><input name="reference" value="{_h(d['reference'])}"></p>
<div class="grid"><div><label>Modalité de règlement</label><input name="payment_terms" value="{_h(d['payment_terms'])}"></div><div><label>Livraison / travaux</label><input name="delivery" value="{_h(d['delivery'])}"></div></div>
{_currency_block(curcode,rate,show)}
<p><label>Notes visibles / générales</label><textarea name="notes">{_h(d['notes'])}</textarea></p>
<p><label>Note interne EMS — jamais affichée sur le PDF client</label><textarea name="internal_note">{_h(d['internal_note'])}</textarea></p>
<p><label>Ajouter des photos</label><input type="file" name="photos" multiple accept="image/*"></p>
<div class="tablewrap"><table class="ems-line-table"><thead><tr><th>Désignation client</th><th>Qté</th><th>Réf. MMS</th><th>Réf. fournisseur</th><th>Fournisseur</th><th>Prix achat</th><th>Prix vente</th><th>Remise %</th><th>Conversion</th><th>Marge ligne</th><th>Note interne</th></tr></thead><tbody>{rows}</tbody></table></div>
<p><button>Enregistrer</button></p></form></div>"""+_FORM_JS)

def document(doc_id):
    con=LEGACY.db()
    d=con.execute("""select d.*,c.name client,c.address,c.nif,c.stat,c.email,c.phone from docs d left join clients c on c.id=d.client_id where d.id=?""",(doc_id,)).fetchone()
    lines=con.execute("select * from lines where doc_id=? order by id",(doc_id,)).fetchall()
    images=con.execute("select * from doc_images where doc_id=? order by id",(doc_id,)).fetchall()
    pays=con.execute("select * from payments where doc_id=? order by payment_date desc,id desc",(doc_id,)).fetchall()
    if not d:
        con.close(); return "Document introuvable",404
    total,cost,margin,margin_rate,mark_rate=_totals(lines); paid=LEGACY.paid_for(con,doc_id); con.close()
    cur=_currency(d["currency"]); rate=float(d["exchange_rate"] or 0); balance=max(0,total-paid)
    eq,eqcur=_convert(total,cur,rate)
    trs=[]
    for l in lines:
        sale=_line_sale(l); costline=_line_cost(l); ml=sale-costline; equiv,_c=_convert(float(l["unit_price"] or 0),cur,rate)
        trs.append(f"""<tr><td>{_h(l['description'])}</td><td>{float(l['qty'] or 0):g}</td><td>{_h(l['mms_ref'])}</td><td>{_h(l['supplier_ref'])}</td><td>{_h(l['supplier_name'])}</td><td class="right">{_fmt(l['purchase_price'],cur)}</td><td class="right">{_fmt(l['unit_price'],cur)}</td><td class="right">{float(l['discount_pct'] or 0):g}%</td><td class="right">{_fmt(equiv,_c) if equiv is not None else '—'}</td><td class="right">{_fmt(ml,cur)}</td><td>{_h(l['internal_note'])}</td></tr>""")
    imgs="".join(f"<a href='/document/{doc_id}/image/{im['id']}' target='_blank'><img src='/document/{doc_id}/image/{im['id']}' style='width:150px;height:110px;object-fit:cover;border-radius:8px;margin:5px;border:1px solid #ddd'></a>" for im in images) or '<span class="muted">Aucune photo.</span>'
    ptrs="".join(f"<tr><td>{_h(p['payment_date'])}</td><td>{_h(p['method'])}</td><td>{_h(p['note'])}</td><td class='right'>{_fmt(p['amount'],cur)}</td></tr>" for p in pays) or '<tr><td colspan="4" class="muted">Aucun règlement enregistré.</td></tr>'
    conv=f"<form method='post' action='/document/{doc_id}/convert'><button>Transformer en facture</button></form>" if d["kind"]=="Devis" else ""
    payform=f"""<div class="card"><h3>Enregistrer un règlement</h3><form method="post" action="/document/{doc_id}/payment"><div class="grid3"><div><label>Date</label><input type="date" name="payment_date" value="{date.today().isoformat()}"></div><div><label>Montant ({_symbol(cur)})</label><input type="text" inputmode="decimal" name="amount" value="{balance:g}"></div><div><label>Mode</label><select name="method"><option>Virement</option><option>Espèces</option><option>Chèque</option><option>Mobile Money</option><option>Autre</option></select></div></div><p><label>Note</label><input name="note"></p><button class="success">Ajouter le règlement</button></form></div>""" if d["kind"]=="Facture" else ""
    eqtxt=_fmt(eq,eqcur) if eq is not None else "Taux à définir"
    return LEGACY.page(f"""<div class="card"><div class="row"><div style="flex:1"><h2>{_h(d['kind'])} {_h(d['number'])}</h2><div class="muted">{_h(d['client'])} • {_h(d['doc_date'])}</div></div><a class="btn2" href="/document/{doc_id}/edit">Modifier</a><a class="btn2" href="/document/{doc_id}/pdf">Télécharger PDF</a>{conv}</div>
<hr><div class="grid4"><div><div class="muted">Total vente</div><div class="total">{_fmt(total,cur)}</div></div><div><div class="muted">Équivalent interne</div><div class="total">{eqtxt}</div></div><div><div class="muted">Total achat interne</div><div class="total">{_fmt(cost,cur)}</div></div><div><div class="muted">Marge brute interne</div><div class="total">{_fmt(margin,cur)}</div></div></div>
<div class="ems-internal"><b>Rentabilité interne — jamais visible client</b><br>Taux de marge : {margin_rate:.2f}% &nbsp; • &nbsp; Taux de marque : {mark_rate:.2f}% &nbsp; • &nbsp; Taux du document : 1 € = {_fmt_plain(rate,'MGA')} Ar</div>
<p><b>Statut :</b> <span class="badge">{_h(d['status'])}</span> &nbsp; <b>Référence :</b> {_h(d['reference'])}</p>
<h3>Détail interne</h3><div class="tablewrap"><table class="ems-line-table"><thead><tr><th>Désignation</th><th>Qté</th><th>Réf. MMS</th><th>Réf. fournisseur</th><th>Fournisseur</th><th>Achat</th><th>Vente</th><th>Remise</th><th>Conversion</th><th>Marge</th><th>Note</th></tr></thead><tbody>{''.join(trs)}</tbody></table></div>
<h3>Photos</h3><div>{imgs}</div>
<div class="card" style="background:#fff8e8"><b>Note interne EMS (non visible client/PDF)</b><br>{_h(d['internal_note']).replace(chr(10),'<br>') or '<span class=muted>Aucune note interne.</span>'}</div>
<p><b>Règlement :</b> {_h(d['payment_terms'])}<br><b>Bon de commande :</b> {_h(d['po_number'])}<br><b>Livraison :</b> {_h(d['delivery'])}</p></div>
{payform}<div class="card"><h3>Règlements</h3><div class="tablewrap"><table><tr><th>Date</th><th>Mode</th><th>Note</th><th class="right">Montant</th></tr>{ptrs}</table></div></div>"""+_FORM_CSS)

def documents():
    con=LEGACY.db(); q=request.args.get("q","").strip(); kind=request.args.get("kind","").strip(); status=request.args.get("status","").strip()
    sql="""select d.*,c.name client from docs d left join clients c on c.id=d.client_id where 1=1"""; params=[]
    if q: sql+=" and (d.number like ? or c.name like ? or d.reference like ?)"; params += [f"%{q}%"]*3
    if kind: sql+=" and d.kind=?"; params.append(kind)
    if status: sql+=" and d.status=?"; params.append(status)
    sql+=" order by d.id desc"
    rows=con.execute(sql,params).fetchall()
    trs=[]
    for r in rows:
        total=LEGACY.total_for(con,r["id"]); paid=LEGACY.paid_for(con,r["id"]); cur=_currency(r["currency"])
        trs.append(f"<tr><td>{_h(r['kind'])}</td><td><a href='/document/{r['id']}'>{_h(r['number'])}</a></td><td>{_h(r['client'])}</td><td>{_h(r['doc_date'])}</td><td class='right'>{_fmt(total,cur)}</td><td class='right'>{_fmt(max(0,total-paid),cur)}</td><td><span class='badge'>{_h(r['status'])}</span></td></tr>")
    con.close()
    return LEGACY.page(f"""<div class="card"><div class="row"><h2 style="flex:1">Devis / Factures</h2><a class="btn2" href="/document/new">+ Nouveau document</a></div><form class="row" style="margin:12px 0"><input class="search" name="q" value="{_h(q)}" placeholder="N°, client, référence"><select name="kind" style="width:auto"><option value="">Tous types</option><option {'selected' if kind=='Devis' else ''}>Devis</option><option {'selected' if kind=='Facture' else ''}>Facture</option></select><select name="status" style="width:auto"><option value="">Tous statuts</option>{''.join(f'<option {"selected" if status==s else ""}>{s}</option>' for s in ["Brouillon","Envoyé","Accepté","Refusé","Facturé","Partiellement payé","Payé","Annulé"])}</select><button>Filtrer</button></form><div class="tablewrap"><table><tr><th>Type</th><th>N°</th><th>Client</th><th>Date</th><th class="right">Total</th><th class="right">Solde</th><th>Statut</th></tr>{''.join(trs)}</table></div></div>""")

def convert(doc_id):
    con=LEGACY.db(); d=con.execute("select * from docs where id=?",(doc_id,)).fetchone()
    if not d or d["kind"]!="Devis":
        con.close(); flash("Ce document ne peut pas être transformé en facture."); return redirect(url_for("document",doc_id=doc_id))
    new_number=LEGACY.next_number("Facture")
    cur=con.execute("""insert into docs(kind,number,doc_date,due_date,client_id,reference,po_number,payment_terms,delivery,status,notes,internal_note,currency,exchange_rate,show_conversion)
                      values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(
        "Facture",new_number,date.today().isoformat(),d["due_date"],d["client_id"],d["reference"],d["po_number"],
        d["payment_terms"],d["delivery"],"Brouillon",d["notes"],d["internal_note"],d["currency"],d["exchange_rate"],d["show_conversion"]
    ))
    new_id=cur.lastrowid
    for l in con.execute("select * from lines where doc_id=?",(doc_id,)).fetchall():
        con.execute("""insert into lines(doc_id,description,qty,unit_price,discount_pct,mms_ref,supplier_ref,supplier_name,purchase_price,internal_note)
                       values(?,?,?,?,?,?,?,?,?,?)""",(new_id,l["description"],l["qty"],l["unit_price"],l["discount_pct"],l["mms_ref"],l["supplier_ref"],l["supplier_name"],l["purchase_price"],l["internal_note"]))
    for im in con.execute("select * from doc_images where doc_id=?",(doc_id,)).fetchall():
        con.execute("insert into doc_images(doc_id,original_name,stored_name,mime) values(?,?,?,?)",(new_id,im["original_name"],im["stored_name"],im["mime"]))
    con.execute("update docs set status='Facturé' where id=?",(doc_id,)); con.commit(); con.close()
    flash(f"Facture {new_number} créée. Toutes les données internes ont été conservées.")
    return redirect(url_for("document",doc_id=new_id))

def _words(amount, cur):
    if _currency(cur)=="MGA":
        return LEGACY.number_words(amount)
    whole=int(float(amount or 0)); cents=int(round((float(amount or 0)-whole)*100))
    txt=LEGACY.number_words(whole).replace(" Ariary"," euro" if whole==1 else " euros")
    if cents:
        txt += f" et {cents} centime" + ("s" if cents>1 else "")
    return txt

def pdf(doc_id):
    con=LEGACY.db()
    d=con.execute("""select d.*,c.name client,c.address,c.nif,c.stat,c.email,c.phone from docs d left join clients c on c.id=d.client_id where d.id=?""",(doc_id,)).fetchone()
    lines=con.execute("select * from lines where doc_id=? order by id",(doc_id,)).fetchall()
    images=con.execute("select * from doc_images where doc_id=? order by id",(doc_id,)).fetchall()
    if not d:
        con.close(); return "Document introuvable",404
    total,_,_,_,_=_totals(lines); con.close()
    cur=_currency(d["currency"]); rate=float(d["exchange_rate"] or 0); sym=_symbol(cur)
    safe=re.sub(r"[^A-Za-z0-9_-]+","-",d["number"]); out=LEGACY.DATA_DIR/f"{d['kind']}_{safe}.pdf"
    c=canvas.Canvas(str(out),pagesize=A4); W,H=A4; L=14*mm; R=W-14*mm; y=H-12*mm
    c.setStrokeColorRGB(.15,.15,.15); c.rect(10*mm,10*mm,W-20*mm,H-20*mm)
    if LEGACY.LOGO.exists(): c.drawImage(ImageReader(str(LEGACY.LOGO)),L,y-20*mm,width=62*mm,height=18.5*mm,preserveAspectRatio=True,mask="auto")
    c.setFont("Helvetica-Bold",12); c.drawString(116*mm,y-5*mm,f"{LEGACY.COMPANY['name']} {d['kind'].upper()} {d['number']}")
    c.setFont("Helvetica-Bold",9); c.drawString(116*mm,y-11*mm,f"Date : {d['doc_date'] or ''}"); c.drawString(116*mm,y-16*mm,f"Échéance : {d['due_date'] or ''}")
    y-=27*mm; c.setFillColorRGB(.68,.66,.66); c.rect(L,y,R-L,6*mm,fill=1,stroke=0); c.setFillColorRGB(0,0,0); c.setFont("Helvetica-Bold",9.5); c.drawString(L+2*mm,y+1.7*mm,"Émetteur :"); c.drawString(100*mm,y+1.7*mm,"Adresse de facturation :"); y-=2*mm
    boxh=38*mm; c.rect(L,y-boxh,R-L,boxh,fill=0,stroke=1); c.line(98*mm,y,98*mm,y-boxh)
    c.setFont("Helvetica-Bold",10); c.drawString(L+2*mm,y-5*mm,LEGACY.COMPANY["name"]); c.setFont("Helvetica",8.5); yy=y-10*mm
    for ln in LEGACY.COMPANY["address"].split("\n"): c.drawString(L+2*mm,yy,ln); yy-=4.5*mm
    c.drawString(L+2*mm,yy-2*mm,f"NIF : {LEGACY.COMPANY['nif']}"); yy-=6*mm; c.drawString(L+2*mm,yy,f"STAT : {LEGACY.COMPANY['stat']}"); yy-=4.5*mm; c.drawString(L+2*mm,yy,f"Mail : {LEGACY.COMPANY['email']}"); yy-=4.5*mm; c.drawString(L+2*mm,yy,f"Tél : {LEGACY.COMPANY['phone']}")
    xx=100*mm; yy=y-5*mm; c.setFont("Helvetica-Bold",10); c.drawString(xx,yy,d["client"] or ""); yy-=5*mm; c.setFont("Helvetica",8.5)
    for ln in (d["address"] or "").split("\n"): c.drawString(xx,yy,ln); yy-=4.5*mm
    yy-=2*mm; c.drawString(xx,yy,f"NIF : {d['nif'] or ''}"); yy-=4.5*mm; c.drawString(xx,yy,f"STAT : {d['stat'] or ''}")
    y-=boxh+9*mm; c.setFont("Helvetica-Bold",10); y=LEGACY.draw_wrapped(c,"REF : "+(d["reference"] or ""),L,y,R-L,"Helvetica-Bold",10,5*mm,2); y-=4*mm
    x1=L; x2=88*mm; x3=112*mm; x4=140*mm; x5=158*mm; x6=R
    c.setFillColorRGB(.68,.66,.66); c.rect(x1,y-8*mm,x6-x1,8*mm,fill=1,stroke=1); c.setFillColorRGB(0,0,0); [c.line(x,y,x,y-8*mm) for x in [x2,x3,x4,x5]]
    c.setFont("Helvetica-Bold",8.2); c.drawCentredString((x1+x2)/2,y-5.5*mm,"DÉSIGNATION"); c.drawCentredString((x2+x3)/2,y-5.5*mm,"QTS"); c.drawCentredString((x3+x4)/2,y-5.5*mm,f"P.U {sym}"); c.drawCentredString((x4+x5)/2,y-5.5*mm,"REM."); c.drawCentredString((x5+x6)/2,y-5.5*mm,f"TOTAL {sym}"); y-=8*mm
    c.setFont("Helvetica",8.2)
    for l in lines:
        h=8*mm; c.rect(x1,y-h,x6-x1,h,fill=0,stroke=1); [c.line(x,y,x,y-h) for x in [x2,x3,x4,x5]]
        c.drawString(x1+2*mm,y-5.2*mm,(l["description"] or "")[:45]); c.drawRightString(x3-2*mm,y-5.2*mm,f"{float(l['qty'] or 0):g}")
        c.drawRightString(x4-2*mm,y-5.2*mm,_fmt_plain(l["unit_price"],cur)); c.drawRightString(x5-2*mm,y-5.2*mm,f"{float(l['discount_pct'] or 0):g}%"); c.drawRightString(x6-2*mm,y-5.2*mm,_fmt_plain(_line_sale(l),cur)); y-=h
    y-=1*mm; c.setFillColorRGB(.68,.66,.66); c.rect(100*mm,y-8*mm,R-100*mm,8*mm,fill=1,stroke=1); c.setFillColorRGB(0,0,0); c.setFont("Helvetica-Bold",9.5); c.drawString(103*mm,y-5.5*mm,f"TOTAL {_symbol(cur)}"); c.drawRightString(R-2*mm,y-5.5*mm,_fmt_plain(total,cur)); y-=13*mm
    if bool(d["show_conversion"]):
        eq,eqcur=_convert(total,cur,rate)
        if eq is not None:
            c.setFont("Helvetica-Bold",8.5); c.drawRightString(R-2*mm,y,f"Équivalent : {_fmt(eq,eqcur)}   (1 € = {_fmt_plain(rate,'MGA')} Ar)"); y-=8*mm
    c.setFont("Helvetica-Bold",9); c.drawString(L+5*mm,y,"Arrêté à la somme de :"); c.setFont("Helvetica",9); c.drawString(L+44*mm,y,_words(total,cur)+".")
    if (d["notes"] or "").strip():
        y-=7*mm; c.setFont("Helvetica-Bold",9); c.drawString(L+5*mm,y,"Notes :"); y-=5*mm; c.setFont("Helvetica",9)
        for note_line in (d["notes"] or "").splitlines():
            c.drawString(L+5*mm,y,note_line[:100]); y-=5*mm
    by=50*mm; c.setFillColorRGB(.7,.69,.69); c.rect(L,by,R-L,40*mm,fill=1,stroke=0); c.setFillColorRGB(.45,.43,.43); c.rect(L+2*mm,by+32*mm,R-L-4*mm,7*mm,fill=1,stroke=0); c.setFillColorRGB(0,0,0); c.setFont("Helvetica-Bold",9); c.drawString(L+4*mm,by+34*mm,"RÉFÉRENCE :"); c.setFont("Helvetica-Bold",8.5); c.drawString(L+4*mm,by+25*mm,f"Devis : {'-' if d['kind']=='Devis' else ''}"); c.drawString(L+4*mm,by+19*mm,f"Bon de commande : {d['po_number'] or ''}"); c.drawString(L+4*mm,by+13*mm,f"Modalité du règlement : {d['payment_terms'] or ''}"); c.drawString(L+4*mm,by+7*mm,f"Date et lieu de livraison : {d['delivery'] or ''}")
    c.setFont("Helvetica-Bold",9); c.drawString(L+10*mm,41*mm,"Coordonnées bancaires de la société :"); c.setFillColorRGB(.78,.77,.77); c.rect(L,19*mm,R-L,18*mm,fill=1,stroke=1); c.setFillColorRGB(0,0,0); c.setFont("Helvetica",8.5); c.drawString(L+2*mm,32*mm,f"Banque : {LEGACY.COMPANY['bank']}")
    c.setFont("Helvetica-Bold",8); heads=["Code banque","Code guichet","N° de compte","Clé"]; vals=[LEGACY.COMPANY["bank_code"],LEGACY.COMPANY["branch_code"],LEGACY.COMPANY["account"],LEGACY.COMPANY["key"]]; xs=[L+30*mm,L+72*mm,L+120*mm,L+164*mm]
    for i,head in enumerate(heads): c.drawCentredString(xs[i],26*mm,head); c.setFont("Helvetica",8); c.drawCentredString(xs[i],21.5*mm,vals[i]); c.setFont("Helvetica-Bold",8)
    if images:
        c.showPage(); c.setFont("Helvetica-Bold",14); c.drawString(L,H-18*mm,f"Photos — {d['kind']} {d['number']}")
        px=L; py=H-32*mm; cellw=84*mm; cellh=62*mm; col=0
        for im in images:
            path=LEGACY.UPLOAD_DIR/im["stored_name"]
            try:
                c.drawImage(ImageReader(str(path)),px,py-cellh,width=cellw,height=cellh,preserveAspectRatio=True,anchor="c",mask="auto")
                c.setFont("Helvetica",7); c.drawString(px,py-cellh-4*mm,(im["original_name"] or "")[:45])
            except Exception:
                c.setFont("Helvetica",8); c.drawString(px,py-8*mm,"Photo non compatible avec le PDF : "+(im["original_name"] or ""))
            col+=1
            if col%2==0: px=L; py-=78*mm
            else: px=L+94*mm
            if py<70*mm and col%2==0:
                c.showPage(); c.setFont("Helvetica-Bold",14); c.drawString(L,H-18*mm,"Photos (suite)"); px=L; py=H-32*mm
    c.showPage(); c.save(); return send_file(out,as_attachment=True,download_name=out.name)

def settings():
    if request.method=="POST":
        if request.form.get("save_rate"):
            rate=max(0,LEGACY.parse_decimal(request.form.get("eur_mga_rate"),0)); _set_setting("eur_mga_rate",rate); flash("Taux de change général enregistré.")
        if request.form.get("save_mapping"):
            mms=request.form.get("mms_ref","").strip().upper(); sref=request.form.get("supplier_ref","").strip(); sname=request.form.get("supplier_name","TVH").strip() or "TVH"
            if mms:
                con=LEGACY.db()
                try:
                    r=con.execute("select mms_ref from supplier_map where upper(mms_ref)=upper(?)",(mms,)).fetchone()
                    if r: con.execute("update supplier_map set supplier_name=?,supplier_ref=? where upper(mms_ref)=upper(?)",(sname,sref,mms))
                    else: con.execute("insert into supplier_map(mms_ref,supplier_name,supplier_ref) values(?,?,?)",(mms,sname,sref))
                    con.commit()
                finally: con.close()
                flash("Correspondance fournisseur enregistrée.")
        return redirect("/settings")
    rate=default_rate()
    con=LEGACY.db()
    try:
        cnt=con.execute("select count(*) n from supplier_map").fetchone()["n"]
    finally: con.close()
    return LEGACY.page(f"""<div class="card"><h2>Paramètres EMS</h2><form method="post"><h3>Taux fixe Euro / Ariary</h3><div class="grid"><div><label>1 € =</label><input name="eur_mga_rate" inputmode="decimal" value="{rate:g}" placeholder="Ex. 5000"></div><div style="align-self:end"><button name="save_rate" value="1">Enregistrer le taux</button></div></div><p class="muted">Ce taux ne se met jamais à jour tout seul. Chaque nouveau devis prend une copie du taux du jour de création, puis garde cette valeur même si le taux général change.</p></form></div>
<div class="card"><h2>Correspondance fournisseur interne</h2><p>{cnt} correspondance(s) enregistrée(s). Invisible au client.</p><form method="post"><div class="grid3"><div><label>Référence MMS</label><input name="mms_ref" placeholder="MMS-..."></div><div><label>Fournisseur</label><input name="supplier_name" value="TVH"></div><div><label>Référence fournisseur</label><input name="supplier_ref" placeholder="Référence TVH"></div></div><p><button name="save_mapping" value="1">Enregistrer la correspondance</button></p></form></div>""")

def _postprocess_site_requests(response):
    if request.method=="POST" and request.path in ("/demande-piece","/api/site/part-request") and response.status_code < 400:
        con=LEGACY.db()
        try:
            rows=con.execute("""select l.id,l.description,l.mms_ref from lines l join docs d on d.id=l.doc_id
                                where d.internal_note like '%[EMS_SITE_REQUEST:%'
                                  and (coalesce(l.mms_ref,'')='' or coalesce(l.supplier_name,'')='')
                                order by l.id desc limit 50""").fetchall()
            for l in rows:
                ref=(l["mms_ref"] or "").strip()
                if not ref:
                    m=re.search(r"Réf\.\s*MMS\s*:\s*([^\n\r]+)",l["description"] or "",re.I)
                    ref=m.group(1).strip().upper() if m else ""
                if ref:
                    sname,sref=supplier_for_mms(ref)
                    con.execute("""update lines set mms_ref=?,supplier_name=case when coalesce(supplier_name,'')='' then ? else supplier_name end,
                                   supplier_ref=case when coalesce(supplier_ref,'')='' then ? else supplier_ref end where id=?""",(ref,sname or "TVH",sref,l["id"]))
            con.commit()
        finally:
            con.close()
    return response

def install(app, legacy):
    global LEGACY, APP
    LEGACY=legacy; APP=app
    _schema()
    if 'Paramètres' not in legacy.TPL:
        legacy.TPL=legacy.TPL.replace("<a href=\"{{url_for('logout')}}\">Déconnexion</a>","<a href=\"/settings\">Paramètres</a><a href=\"{{url_for('logout')}}\">Déconnexion</a>")
    if "new_document" in app.view_functions: app.view_functions["new_document"]=new_document
    if "edit_document" in app.view_functions: app.view_functions["edit_document"]=edit_document
    if "document" in app.view_functions: app.view_functions["document"]=document
    if "documents" in app.view_functions: app.view_functions["documents"]=documents
    if "convert" in app.view_functions: app.view_functions["convert"]=convert
    if "pdf" in app.view_functions: app.view_functions["pdf"]=pdf
    app.add_url_rule("/settings","ems_settings",settings,methods=["GET","POST"])
    app.after_request(_postprocess_site_requests)
