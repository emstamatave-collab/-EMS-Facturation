# EMS Facturation V15
# Multi-devise EUR/MGA, marge interne, fournisseurs et informations achat par ligne.
import html
from datetime import date, timedelta

import app_v14 as base
from flask import request, redirect, url_for, flash, send_file
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader

legacy = base.legacy
app = base.app


def _migrate_v15():
    if base.DATABASE_URL:
        with base.psycopg.connect(base.DATABASE_URL) as con:
            statements = [
                "ALTER TABLE docs ADD COLUMN IF NOT EXISTS currency TEXT DEFAULT 'MGA'",
                "ALTER TABLE docs ADD COLUMN IF NOT EXISTS fx_rate DOUBLE PRECISION DEFAULT 0",
                "ALTER TABLE docs ADD COLUMN IF NOT EXISTS show_conversion INTEGER DEFAULT 0",
                "ALTER TABLE lines ADD COLUMN IF NOT EXISTS purchase_price DOUBLE PRECISION DEFAULT 0",
                "ALTER TABLE lines ADD COLUMN IF NOT EXISTS supplier_ref TEXT DEFAULT ''",
                "ALTER TABLE lines ADD COLUMN IF NOT EXISTS supplier_name TEXT DEFAULT ''",
                "ALTER TABLE lines ADD COLUMN IF NOT EXISTS internal_note TEXT DEFAULT ''",
                "CREATE TABLE IF NOT EXISTS app_settings(key TEXT PRIMARY KEY, value TEXT DEFAULT '')",
                "INSERT INTO app_settings(key,value) VALUES('fx_rate_eur_mga','0') ON CONFLICT (key) DO NOTHING",
                "UPDATE docs SET currency='MGA' WHERE currency IS NULL OR currency=''",
                "UPDATE docs SET fx_rate=0 WHERE fx_rate IS NULL",
                "UPDATE docs SET show_conversion=0 WHERE show_conversion IS NULL",
            ]
            for sql in statements:
                con.execute(sql)
    else:
        con = legacy.db()
        legacy.ensure_column(con, 'docs', 'currency', "TEXT DEFAULT 'MGA'")
        legacy.ensure_column(con, 'docs', 'fx_rate', "REAL DEFAULT 0")
        legacy.ensure_column(con, 'docs', 'show_conversion', "INTEGER DEFAULT 0")
        legacy.ensure_column(con, 'lines', 'purchase_price', "REAL DEFAULT 0")
        legacy.ensure_column(con, 'lines', 'supplier_ref', "TEXT DEFAULT ''")
        legacy.ensure_column(con, 'lines', 'supplier_name', "TEXT DEFAULT ''")
        legacy.ensure_column(con, 'lines', 'internal_note', "TEXT DEFAULT ''")
        con.execute("CREATE TABLE IF NOT EXISTS app_settings(key TEXT PRIMARY KEY,value TEXT DEFAULT '')")
        row = con.execute("SELECT value FROM app_settings WHERE key='fx_rate_eur_mga'").fetchone()
        if not row:
            con.execute("INSERT INTO app_settings(key,value) VALUES(?,?)", ('fx_rate_eur_mga', '0'))
        con.commit()
        con.close()


_migrate_v15()

# Navigation / titre global.
if 'Euro (€) • Ariary (MGA)' not in legacy.TPL:
    legacy.TPL = legacy.TPL.replace('Gestion commerciale • Ariary (MGA)', 'Gestion commerciale • Euro (€) • Ariary (MGA)')
if '>Paramètres<' not in legacy.TPL:
    legacy.TPL = legacy.TPL.replace(
        '<a href="{{url_for(\'stock\')}}">Stock</a><a href="{{url_for(\'new_document\')}}">+ Nouveau</a>',
        '<a href="{{url_for(\'stock\')}}">Stock</a><a href="/settings">Paramètres</a><a href="{{url_for(\'new_document\')}}">+ Nouveau</a>'
    )


def esc(value):
    return html.escape(str(value or ''), quote=True)


def _row_get(row, key, default=None):
    if row is None:
        return default
    try:
        value = row.get(key, default)
    except Exception:
        try:
            value = row[key]
        except Exception:
            value = default
    return default if value is None else value


def _currency(value):
    return 'EUR' if str(value or '').upper() == 'EUR' else 'MGA'


def _symbol(currency):
    return '€' if _currency(currency) == 'EUR' else 'Ar'


def _pdf_code(currency):
    return 'EUR' if _currency(currency) == 'EUR' else 'Ar'


def _number(value, currency):
    value = float(value or 0)
    if _currency(currency) == 'EUR':
        s = f"{value:,.2f}"
        return s.replace(',', 'X').replace('.', ',').replace('X', ' ')
    return f"{value:,.0f}".replace(',', ' ')


def _money(value, currency):
    return f"{_number(value, currency)} {_symbol(currency)}"


def _converted(value, currency, rate):
    rate = float(rate or 0)
    if rate <= 0:
        return None
    value = float(value or 0)
    return value * rate if _currency(currency) == 'EUR' else value / rate


def _to_mga(value, currency, rate):
    value = float(value or 0)
    if _currency(currency) == 'EUR':
        return value * float(rate or 0)
    return value


def _get_setting(key, default=''):
    con = legacy.db()
    try:
        row = con.execute('select value from app_settings where key=?', (key,)).fetchone()
        return str(row['value']) if row else default
    finally:
        con.close()


def _set_setting(key, value):
    con = legacy.db()
    try:
        cur = con.execute('update app_settings set value=? where key=?', (str(value), key))
        if cur.rowcount == 0:
            con.execute('insert into app_settings(key,value) values(?,?)', (key, str(value)))
        con.commit()
    finally:
        con.close()


def _default_fx_rate():
    return max(0, legacy.parse_decimal(_get_setting('fx_rate_eur_mga', '0'), 0))


def _financials(lines):
    sales = 0.0
    purchases = 0.0
    for line in lines:
        qty = float(_row_get(line, 'qty', 0) or 0)
        sale = float(_row_get(line, 'unit_price', 0) or 0)
        disc = float(_row_get(line, 'discount_pct', 0) or 0)
        purchase = float(_row_get(line, 'purchase_price', 0) or 0)
        sales += qty * sale * (1 - disc / 100.0)
        purchases += qty * purchase
    margin = sales - purchases
    margin_rate = (margin / purchases * 100.0) if purchases else 0.0
    mark_rate = (margin / sales * 100.0) if sales else 0.0
    return sales, purchases, margin, margin_rate, mark_rate


def _amount_words(value, currency):
    if _currency(currency) == 'MGA':
        return legacy.number_words(value)
    rounded = int(round(float(value or 0)))
    text = legacy.number_words(rounded)
    if text.endswith(' Ariary'):
        text = text[:-7]
    return text + (' euro' if rounded == 1 else ' euros')


FINANCE_CSS = """
<style>
.fxbox{background:#eef8f1;border:1px solid #cfe6d5;border-radius:12px;padding:14px;margin:14px 0}
.internalbox{background:#fff8e8;border:1px solid #efd8a1;border-radius:12px;padding:14px;margin:14px 0}
.finance-summary{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:10px;margin:14px 0}
.finance-summary>div{background:#fff8e8;border:1px solid #efd8a1;border-radius:10px;padding:10px}
.finance-summary .v{font-size:18px;font-weight:800;margin-top:4px}
.line-table{min-width:1320px}
.line-table td{min-width:105px}
.line-table td:first-child{min-width:260px}
.line-table .supplier-cell{min-width:185px}
.line-table .supplier-cell input+input{margin-top:6px}
.readonly{background:#f3f4f6!important;color:#4b5563}
.checkline{display:flex;gap:8px;align-items:center;font-weight:650}
.checkline input{width:auto!important;min-height:auto!important}
@media(max-width:760px){
 .finance-summary{grid-template-columns:1fr 1fr}
 .line-table{min-width:0}
 .line-table td:nth-child(1):before{content:'Désignation'}
 .line-table td:nth-child(2):before{content:'Quantité'}
 .line-table td:nth-child(3):before{content:'Prix de vente'}
 .line-table td:nth-child(4):before{content:'Conversion'}
 .line-table td:nth-child(5):before{content:'Remise %'}
 .line-table td:nth-child(6):before{content:'Prix achat — interne'}
 .line-table td:nth-child(7):before{content:'Marge — interne'}
 .line-table td:nth-child(8):before{content:'Fournisseur / réf. — interne'}
 .line-table td:nth-child(9):before{content:'Note ligne — interne'}
}
</style>
"""

FORM_SCRIPT = """
<script>
(function(){
 const cur=document.querySelector('[name="currency"]');
 const rate=document.querySelector('[name="fx_rate"]');
 if(!cur||!rate) return;
 function n(v){
   v=(v||'').toString().replace(/\u00a0/g,'').replace(/ /g,'').replace(',','.');
   const x=parseFloat(v); return Number.isFinite(x)?x:0;
 }
 function fmt(v,c){
   if(v===null || !Number.isFinite(v)) return '';
   if(c==='EUR') return v.toLocaleString('fr-FR',{minimumFractionDigits:2,maximumFractionDigits:2})+' €';
   return Math.round(v).toLocaleString('fr-FR')+' Ar';
 }
 function refresh(){
   const c=cur.value==='EUR'?'EUR':'MGA';
   const r=n(rate.value);
   const alt=c==='EUR'?'MGA':'EUR';
   let sale=0, buy=0;
   document.querySelectorAll('.finance-line').forEach(function(row){
      const q=n(row.querySelector('[name="qty"]').value);
      const pu=n(row.querySelector('[name="unit_price"]').value);
      const disc=Math.max(0,Math.min(100,n(row.querySelector('[name="discount_pct"]').value)));
      const pa=n(row.querySelector('[name="purchase_price"]').value);
      const netUnit=pu*(1-disc/100);
      const lineSale=q*netUnit;
      const lineBuy=q*pa;
      sale+=lineSale; buy+=lineBuy;
      let cv=null;
      if(r>0) cv=(c==='EUR')?pu*r:pu/r;
      row.querySelector('.conversion-field').value=cv===null?'':fmt(cv,alt);
      row.querySelector('.margin-field').value=fmt(lineSale-lineBuy,c);
   });
   const margin=sale-buy;
   const marginRate=buy?margin/buy*100:0;
   const markRate=sale?margin/sale*100:0;
   const altSale=r>0?((c==='EUR')?sale*r:sale/r):null;
   document.getElementById('sum-sale').textContent=fmt(sale,c);
   document.getElementById('sum-buy').textContent=fmt(buy,c);
   document.getElementById('sum-margin').textContent=fmt(margin,c);
   document.getElementById('sum-margin-rate').textContent=marginRate.toLocaleString('fr-FR',{maximumFractionDigits:1})+' %';
   document.getElementById('sum-mark-rate').textContent=markRate.toLocaleString('fr-FR',{maximumFractionDigits:1})+' %';
   document.getElementById('sum-converted').textContent=altSale===null?'Taux à renseigner':fmt(altSale,alt);
   document.querySelectorAll('.sale-label').forEach(function(el){el.textContent='P.U. '+(c==='EUR'?'€':'Ar');});
   document.querySelectorAll('.buy-label').forEach(function(el){el.textContent='P.A. '+(c==='EUR'?'€':'Ar')+' — interne';});
   document.querySelectorAll('.conv-label').forEach(function(el){el.textContent='Équiv. '+(alt==='EUR'?'€':'Ar');});
 }
 document.addEventListener('input',function(e){
   if(e.target.closest('.finance-line') || e.target===rate) refresh();
 });
 cur.addEventListener('change',refresh);
 refresh();
})();
</script>
"""


def _line_row(line=None):
    desc = esc(_row_get(line, 'description', ''))
    qty = float(_row_get(line, 'qty', 1) or 1)
    unit = float(_row_get(line, 'unit_price', 0) or 0)
    disc = float(_row_get(line, 'discount_pct', 0) or 0)
    purchase = float(_row_get(line, 'purchase_price', 0) or 0)
    supplier_ref = esc(_row_get(line, 'supplier_ref', ''))
    supplier_name = esc(_row_get(line, 'supplier_name', ''))
    internal_note = esc(_row_get(line, 'internal_note', ''))
    unit_value = '' if not desc and unit == 0 else f"{unit:g}"
    purchase_value = '' if not desc and purchase == 0 else f"{purchase:g}"
    return f"""<tr class="finance-line">
<td><textarea name="description" style="min-width:260px" placeholder="Désignation client">{desc}</textarea></td>
<td><input class="linefield qtyfield" name="qty" type="text" inputmode="decimal" autocomplete="off" value="{qty:g}"></td>
<td><input class="linefield pricefield" name="unit_price" type="text" inputmode="decimal" autocomplete="off" value="{unit_value}" placeholder="Prix de vente"></td>
<td><input class="conversion-field readonly" type="text" readonly tabindex="-1"></td>
<td><input name="discount_pct" type="text" inputmode="decimal" autocomplete="off" value="{disc:g}"></td>
<td><input name="purchase_price" type="text" inputmode="decimal" autocomplete="off" value="{purchase_value}" placeholder="Prix d'achat"></td>
<td><input class="margin-field readonly" type="text" readonly tabindex="-1"></td>
<td class="supplier-cell"><input name="supplier_ref" value="{supplier_ref}" placeholder="Réf. fournisseur"><input name="supplier_name" value="{supplier_name}" placeholder="Nom fournisseur"></td>
<td><textarea name="line_internal_note" placeholder="Note interne">{internal_note}</textarea></td>
</tr>"""


def _document_form(d, clients, lines, is_new):
    def dg(key, default=''):
        return _row_get(d, key, default)

    currency = _currency(dg('currency', 'MGA'))
    fx_rate = float(dg('fx_rate', 0) or 0)
    if fx_rate <= 0:
        fx_rate = _default_fx_rate()
    opts = ''.join(
        f"<option value='{c['id']}' {'selected' if str(c['id'])==str(dg('client_id','')) else ''}>{esc(c['name'])}</option>"
        for c in clients
    )
    kinds = ''.join(
        f"<option {'selected' if dg('kind','Devis')==k else ''}>{k}</option>" for k in ['Devis','Facture']
    )
    statuses = ['Brouillon','Envoyé','Accepté','Refusé','Facturé','Partiellement payé','Payé','Annulé']
    status_html = ''.join(
        f"<option {'selected' if dg('status','Brouillon')==s else ''}>{s}</option>" for s in statuses
    )
    rows = list(lines)
    rows += [None for _ in range(max(3, 8-len(rows)))]
    rows_html = ''.join(_line_row(row) for row in rows)
    today = date.today()
    doc_date = dg('doc_date', today.isoformat()) or today.isoformat()
    due_date = dg('due_date', (today+timedelta(days=30)).isoformat()) or (today+timedelta(days=30)).isoformat()
    show_checked = 'checked' if int(dg('show_conversion',0) or 0) else ''
    status_field = f"""<div><label>Statut</label><select name="status">{status_html}</select></div>""" if not is_new else ''
    title = 'Nouveau devis / facture' if is_new else f"Modifier {esc(dg('kind'))} {esc(dg('number'))}"
    button = 'Créer le document' if is_new else 'Enregistrer'
    number_placeholder = ' placeholder="Automatique si vide"' if is_new else ''
    number_value = esc(dg('number',''))
    return f"""{FINANCE_CSS}<div class="card"><h2>{title}</h2>
<form method="post" enctype="multipart/form-data">
<div class="grid3">
<div><label>Type</label><select name="kind">{kinds}</select></div>
<div><label>Numéro</label><input name="number" value="{number_value}"{number_placeholder}></div>
{status_field}
<div><label>Client</label><select name="client_id"><option value="">-- Choisir --</option>{opts}</select></div>
<div><label>Date</label><input type="date" name="doc_date" value="{esc(doc_date)}"></div>
<div><label>Échéance</label><input type="date" name="due_date" value="{esc(due_date)}"></div>
<div><label>Bon de commande</label><input name="po_number" value="{esc(dg('po_number',''))}"></div>
</div>
<p><label>Référence / objet</label><input name="reference" value="{esc(dg('reference',''))}"></p>
<div class="grid"><div><label>Modalité de règlement</label><input name="payment_terms" value="{esc(dg('payment_terms','Virement sous 30 jours'))}"></div><div><label>Livraison / travaux</label><input name="delivery" value="{esc(dg('delivery',''))}"></div></div>

<div class="fxbox">
<h3 style="margin-top:0">Devise et conversion</h3>
<div class="grid3">
<div><label>Devise du document</label><select name="currency"><option value="MGA" {'selected' if currency=='MGA' else ''}>Ariary (Ar)</option><option value="EUR" {'selected' if currency=='EUR' else ''}>Euro (€)</option></select></div>
<div><label>Taux utilisé</label><input name="fx_rate" type="text" inputmode="decimal" value="{fx_rate:g}" placeholder="Ex. 5000"><span class="small muted">1 € = ce montant en Ar. Ce taux reste figé dans ce document.</span></div>
<div><label>Conversion totale interne</label><input id="sum-converted" class="readonly" readonly tabindex="-1"></div>
</div>
<p class="checkline"><input type="checkbox" name="show_conversion" value="1" {show_checked}> Afficher la conversion € / Ar sur le PDF client</p>
<div class="small muted">Décochée par défaut : le client ne voit qu'une seule devise. Le taux général se règle dans <a href="/settings">Paramètres</a>.</div>
</div>

<p><label>Notes visibles / générales</label><textarea name="notes">{esc(dg('notes',''))}</textarea></p>
<p><label>Note interne EMS — jamais affichée sur le PDF client</label><textarea name="internal_note">{esc(dg('internal_note',''))}</textarea></p>
<p><label>Photos à insérer dans le devis</label><input type="file" name="photos" multiple accept="image/*"><span class="small muted">Tu peux choisir plusieurs photos depuis la galerie.</span></p>

<h3>Lignes</h3>
<div class="tablewrap"><table class="line-table"><thead><tr>
<th>Désignation</th><th>Qté</th><th class="sale-label">P.U.</th><th class="conv-label">Équiv.</th><th>Remise %</th><th class="buy-label">P.A. — interne</th><th>Marge — interne</th><th>Fournisseur / réf. — interne</th><th>Note interne</th>
</tr></thead><tbody>{rows_html}</tbody></table></div>

<div class="finance-summary">
<div><div class="muted">Total vente HT</div><div class="v" id="sum-sale">—</div></div>
<div><div class="muted">Total achat HT — interne</div><div class="v" id="sum-buy">—</div></div>
<div><div class="muted">Marge brute — interne</div><div class="v" id="sum-margin">—</div></div>
<div><div class="muted">Taux de marge</div><div class="v" id="sum-margin-rate">—</div></div>
<div><div class="muted">Taux de marque</div><div class="v" id="sum-mark-rate">—</div></div>
</div>
<p><button>{button}</button></p>
</form></div>{FORM_SCRIPT}"""


def _save_lines(con, doc_id):
    descs = request.form.getlist('description')
    qtys = request.form.getlist('qty')
    units = request.form.getlist('unit_price')
    discounts = request.form.getlist('discount_pct')
    purchases = request.form.getlist('purchase_price')
    supplier_refs = request.form.getlist('supplier_ref')
    supplier_names = request.form.getlist('supplier_name')
    internal_notes = request.form.getlist('line_internal_note')
    for i, description in enumerate(descs):
        if not str(description or '').strip():
            continue
        q = qtys[i] if i < len(qtys) else '1'
        p = units[i] if i < len(units) else '0'
        disc = discounts[i] if i < len(discounts) else '0'
        purchase = purchases[i] if i < len(purchases) else '0'
        supplier_ref = supplier_refs[i] if i < len(supplier_refs) else ''
        supplier_name = supplier_names[i] if i < len(supplier_names) else ''
        internal_note = internal_notes[i] if i < len(internal_notes) else ''
        con.execute(
            """insert into lines(doc_id,description,qty,unit_price,discount_pct,purchase_price,supplier_ref,supplier_name,internal_note)
               values(?,?,?,?,?,?,?,?,?)""",
            (
                doc_id, description, legacy.parse_decimal(q,1), legacy.parse_decimal(p,0),
                max(0,min(100,legacy.parse_decimal(disc,0))), max(0,legacy.parse_decimal(purchase,0)),
                supplier_ref, supplier_name, internal_note
            )
        )


@app.route('/settings', methods=['GET','POST'])
def settings_v15():
    if request.method == 'POST':
        rate = max(0, legacy.parse_decimal(request.form.get('fx_rate'), 0))
        _set_setting('fx_rate_eur_mga', f"{rate:g}")
        flash('Taux de change enregistré. Il restera inchangé jusqu’à ta prochaine modification.')
        return redirect('/settings')
    rate = _default_fx_rate()
    return legacy.page(f"""{FINANCE_CSS}<div class="card"><h2>Paramètres EMS</h2>
<div class="fxbox"><h3 style="margin-top:0">Taux de change permanent</h3>
<form method="post"><div style="max-width:420px"><label>1 € =</label><div class="row"><input style="flex:1" name="fx_rate" type="text" inputmode="decimal" value="{rate:g}" placeholder="Saisir le taux"><b>Ar</b></div></div>
<p class="muted">Ce taux n'est jamais mis à jour automatiquement. Les nouveaux devis l'utilisent comme valeur de départ. Chaque devis garde ensuite son propre taux historique.</p>
<button>Enregistrer le taux</button></form></div></div>""")


def home_v15():
    con = legacy.db()
    clients = con.execute('select count(*) n from clients').fetchone()['n']
    devis = con.execute("select count(*) n from docs where kind='Devis'").fetchone()['n']
    fact = con.execute("select count(*) n from docs where kind='Facture'").fetchone()['n']
    invoice_rows = con.execute(
        """select d.id,d.currency,d.fx_rate,
           coalesce(sum(l.qty*l.unit_price*(1-coalesce(l.discount_pct,0)/100.0)),0) total,
           (select coalesce(sum(amount),0) from payments p where p.doc_id=d.id) paid
           from docs d left join lines l on l.doc_id=d.id
           where d.kind='Facture' group by d.id,d.currency,d.fx_rate"""
    ).fetchall()
    billed_mga = sum(_to_mga(r['total'], r['currency'], r['fx_rate']) for r in invoice_rows)
    paid_mga = sum(_to_mga(r['paid'], r['currency'], r['fx_rate']) for r in invoice_rows)
    recent = con.execute(
        """select d.*,c.name client,coalesce(sum(l.qty*l.unit_price*(1-coalesce(l.discount_pct,0)/100.0)),0) total
           from docs d left join clients c on c.id=d.client_id left join lines l on l.doc_id=d.id
           group by d.id,c.name order by d.id desc limit 6"""
    ).fetchall()
    con.close()
    trs = ''.join(
        f"<tr><td>{esc(r['kind'])}</td><td><a href='/document/{r['id']}'>{esc(r['number'])}</a></td><td>{esc(r['client'])}</td><td class='right'>{_money(r['total'],r['currency'])}</td><td><span class='badge'>{esc(r['status'])}</span></td></tr>"
        for r in recent
    )
    return legacy.page(f"""<div class="grid4"><div class="card"><div class="muted">Clients</div><div class="kpi">{clients}</div></div><div class="card"><div class="muted">Devis</div><div class="kpi">{devis}</div></div><div class="card"><div class="muted">Factures</div><div class="kpi">{fact}</div></div><div class="card"><div class="muted">Reste à encaisser — équiv. Ar</div><div class="kpi">{legacy.money(max(0,billed_mga-paid_mga))} Ar</div></div></div>
<div class="card"><h2>Activité</h2><div class="grid"><div><div class="muted">Total facturé — équiv. Ar</div><div class="total">{legacy.money(billed_mga)} Ar</div></div><div><div class="muted">Total encaissé — équiv. Ar</div><div class="total">{legacy.money(paid_mga)} Ar</div></div></div></div>
<div class="card"><h2>Documents récents</h2><div class="tablewrap"><table><tr><th>Type</th><th>N°</th><th>Client</th><th class="right">Total</th><th>Statut</th></tr>{trs}</table></div></div>""")


def documents_v15():
    con=legacy.db()
    q=request.args.get('q','').strip()
    kind=request.args.get('kind','').strip()
    status=request.args.get('status','').strip()
    sql="""select d.*,c.name client,coalesce(sum(l.qty*l.unit_price*(1-coalesce(l.discount_pct,0)/100.0)),0) total,
           (select coalesce(sum(amount),0) from payments p where p.doc_id=d.id) paid
           from docs d left join clients c on c.id=d.client_id left join lines l on l.doc_id=d.id where 1=1"""
    params=[]
    if q:
        sql+=' and (d.number like ? or c.name like ? or d.reference like ?)'
        params += [f'%{q}%']*3
    if kind:
        sql+=' and d.kind=?'; params.append(kind)
    if status:
        sql+=' and d.status=?'; params.append(status)
    sql+=' group by d.id,c.name order by d.id desc'
    rows=con.execute(sql,params).fetchall()
    con.close()
    trs=''.join(
        f"<tr><td>{esc(r['kind'])}</td><td><a href='/document/{r['id']}'>{esc(r['number'])}</a></td><td>{esc(r['client'])}</td><td>{esc(r['doc_date'])}</td><td class='right'>{_money(r['total'],r['currency'])}</td><td class='right'>{_money(max(0,float(r['total'])-float(r['paid'])),r['currency'])}</td><td><span class='badge'>{esc(r['status'])}</span></td></tr>"
        for r in rows
    )
    status_opts=''.join(f'<option {"selected" if status==s else ""}>{s}</option>' for s in ['Brouillon','Envoyé','Accepté','Refusé','Facturé','Partiellement payé','Payé','Annulé'])
    return legacy.page(f"""<div class="card"><div class="row"><h2 style="flex:1">Devis / Factures</h2><a class="btn2" href="{url_for('new_document')}">+ Nouveau document</a></div>
<form class="row" style="margin:12px 0"><input class="search" name="q" value="{esc(q)}" placeholder="N°, client, référence"><select name="kind" style="width:auto"><option value="">Tous types</option><option {'selected' if kind=='Devis' else ''}>Devis</option><option {'selected' if kind=='Facture' else ''}>Facture</option></select><select name="status" style="width:auto"><option value="">Tous statuts</option>{status_opts}</select><button>Filtrer</button></form>
<div class="tablewrap"><table><tr><th>Type</th><th>N°</th><th>Client</th><th>Date</th><th class="right">Total</th><th class="right">Solde</th><th>Statut</th></tr>{trs}</table></div></div>""")


def new_document_v15():
    con=legacy.db()
    clients=con.execute('select * from clients order by name').fetchall()
    if request.method=='POST':
        kind=request.form.get('kind') or 'Devis'
        number=request.form.get('number') or legacy.next_number(kind)
        currency=_currency(request.form.get('currency'))
        rate=max(0,legacy.parse_decimal(request.form.get('fx_rate'),_default_fx_rate()))
        show=1 if request.form.get('show_conversion') else 0
        cur=con.execute(
            """insert into docs(kind,number,doc_date,due_date,client_id,reference,po_number,payment_terms,delivery,status,notes,internal_note,currency,fx_rate,show_conversion)
               values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (kind,number,request.form.get('doc_date'),request.form.get('due_date'),request.form.get('client_id') or None,
             request.form.get('reference'),request.form.get('po_number'),request.form.get('payment_terms'),request.form.get('delivery'),
             'Brouillon',request.form.get('notes'),request.form.get('internal_note'),currency,rate,show)
        )
        did=cur.lastrowid
        _save_lines(con,did)
        for f in request.files.getlist('photos'):
            saved=legacy.save_upload(f,f'doc_{did}')
            if saved and saved[2].startswith('image/'):
                con.execute('insert into doc_images(doc_id,original_name,stored_name,mime) values(?,?,?,?)',(did,*saved))
        con.commit(); con.close()
        return redirect(url_for('document',doc_id=did))
    con.close()
    seed={
        'kind':'Devis','number':'','status':'Brouillon','client_id':'','doc_date':date.today().isoformat(),
        'due_date':(date.today()+timedelta(days=30)).isoformat(),'po_number':'','reference':'',
        'payment_terms':'Virement sous 30 jours','delivery':'','notes':'','internal_note':'',
        'currency':'MGA','fx_rate':_default_fx_rate(),'show_conversion':0
    }
    return legacy.page(_document_form(seed,clients,[],True))


def document_v15(doc_id):
    con=legacy.db()
    d=con.execute("""select d.*,c.name client,c.address,c.nif,c.stat,c.email,c.phone
                     from docs d left join clients c on c.id=d.client_id where d.id=?""",(doc_id,)).fetchone()
    lines=con.execute('select * from lines where doc_id=?',(doc_id,)).fetchall()
    images=con.execute('select * from doc_images where doc_id=? order by id',(doc_id,)).fetchall()
    pays=con.execute('select * from payments where doc_id=? order by payment_date desc,id desc',(doc_id,)).fetchall()
    if not d:
        con.close(); return 'Document introuvable',404
    total=legacy.total_for(con,doc_id)
    paid=legacy.paid_for(con,doc_id)
    con.close()
    currency=_currency(d['currency'])
    rate=float(d['fx_rate'] or 0)
    balance=max(0,float(total)-float(paid))
    sales,purchases,margin,margin_rate,mark_rate=_financials(lines)
    alt_total=_converted(total,currency,rate)
    alt_currency='MGA' if currency=='EUR' else 'EUR'
    rows=[]
    for l in lines:
        qty=float(l['qty'] or 0); unit=float(l['unit_price'] or 0); disc=float(l['discount_pct'] or 0)
        purchase=float(l['purchase_price'] or 0)
        net=qty*unit*(1-disc/100.0)
        buy=qty*purchase
        line_margin=net-buy
        supplier=''.join([
            f"<b>{esc(l['supplier_name'])}</b>" if l['supplier_name'] else '',
            f"<br>Réf. {esc(l['supplier_ref'])}" if l['supplier_ref'] else ''
        ]) or '<span class="muted">—</span>'
        note=esc(l['internal_note']) or '<span class="muted">—</span>'
        conv=_converted(unit,currency,rate)
        rows.append(f"""<tr><td>{esc(l['description'])}</td><td>{qty:g}</td><td class="right">{_money(unit,currency)}</td><td class="right">{_money(conv,alt_currency) if conv is not None else '—'}</td><td class="right">{disc:g}%</td><td class="right">{_money(purchase,currency)}</td><td class="right">{_money(line_margin,currency)}</td><td>{supplier}</td><td>{note}</td></tr>""")
    trs=''.join(rows)
    imgs=''.join(f"<a href='/document/{doc_id}/image/{im['id']}' target='_blank'><img src='/document/{doc_id}/image/{im['id']}' style='width:150px;height:110px;object-fit:cover;border-radius:8px;margin:5px;border:1px solid #ddd'></a>" for im in images) or '<span class="muted">Aucune photo.</span>'
    ptrs=''.join(f"<tr><td>{esc(p['payment_date'])}</td><td>{esc(p['method'])}</td><td>{esc(p['note'])}</td><td class='right'>{_money(p['amount'],currency)}</td></tr>" for p in pays) or '<tr><td colspan="4" class="muted">Aucun règlement enregistré.</td></tr>'
    conv_form=f"<form method='post' action='/document/{doc_id}/convert'><button>Transformer en facture</button></form>" if d['kind']=='Devis' else ''
    payform=f"""<div class="card"><h3>Enregistrer un règlement</h3><form method="post" action="/document/{doc_id}/payment"><div class="grid3"><div><label>Date</label><input type="date" name="payment_date" value="{date.today().isoformat()}"></div><div><label>Montant ({_symbol(currency)})</label><input type="text" inputmode="decimal" autocomplete="off" name="amount" value="{balance:g}" placeholder="Montant"></div><div><label>Mode</label><select name="method"><option>Virement</option><option>Espèces</option><option>Chèque</option><option>Mobile Money</option><option>Autre</option></select></div></div><p><label>Note</label><input name="note"></p><button class="success">Ajouter le règlement</button></form></div>""" if d['kind']=='Facture' else ''
    alt_block=_money(alt_total,alt_currency) if alt_total is not None else 'Taux à renseigner'
    return legacy.page(f"""{FINANCE_CSS}<div class="card"><div class="row"><div style="flex:1"><h2>{esc(d['kind'])} {esc(d['number'])}</h2><div class="muted">{esc(d['client'] or 'Sans client')} • {esc(d['doc_date'])}</div></div><a class="btn2" href="/document/{doc_id}/edit">Modifier</a><a class="btn2" href="/document/{doc_id}/pdf">Télécharger PDF</a>{conv_form}</div><hr>
<div class="grid3"><div><div class="muted">Total vente</div><div class="total">{_money(total,currency)}</div></div><div><div class="muted">Conversion interne</div><div class="total">{alt_block}</div></div><div><div class="muted">Solde</div><div class="total">{_money(balance,currency)}</div></div></div>
<div class="finance-summary"><div><div class="muted">Total achat HT</div><div class="v">{_money(purchases,currency)}</div></div><div><div class="muted">Marge brute</div><div class="v">{_money(margin,currency)}</div></div><div><div class="muted">Taux de marge</div><div class="v">{margin_rate:.1f} %</div></div><div><div class="muted">Taux de marque</div><div class="v">{mark_rate:.1f} %</div></div><div><div class="muted">Taux change</div><div class="v">1 € = {_number(rate,'MGA')} Ar</div></div></div>
<p><b>Statut :</b> <span class="badge">{esc(d['status'])}</span> &nbsp; <b>Référence :</b> {esc(d['reference'])} &nbsp; <b>Conversion sur PDF client :</b> {'Oui' if int(d['show_conversion'] or 0) else 'Non'}</p>
<h3>Client</h3><div>{esc(d['client'])}<br>{esc(d['address']).replace(chr(10),'<br>')}<br>NIF : {esc(d['nif'])}<br>STAT : {esc(d['stat'])}</div>
<h3>Détail interne</h3><div class="tablewrap"><table class="line-table"><tr><th>Désignation</th><th>Qté</th><th class="right">P.U.</th><th class="right">Conversion</th><th class="right">Remise</th><th class="right">P.A. interne</th><th class="right">Marge</th><th>Fournisseur / réf.</th><th>Note interne</th></tr>{trs}
<tr><td colspan="5" class="right"><b>TOTAUX</b></td><td class="right"><b>{_money(purchases,currency)}</b></td><td class="right"><b>{_money(margin,currency)}</b></td><td colspan="2"></td></tr></table></div>
<h3>Photos du devis</h3><div>{imgs}</div>
<div class="internalbox"><b>Note interne EMS — jamais visible client/PDF</b><br>{esc(d['internal_note']).replace(chr(10),'<br>') or '<span class="muted">Aucune note interne.</span>'}</div>
<p><b>Arrêté à la somme de :</b> {_amount_words(total,currency)}.</p><p><b>Règlement :</b> {esc(d['payment_terms'])}<br><b>Bon de commande :</b> {esc(d['po_number'])}<br><b>Livraison :</b> {esc(d['delivery'])}</p></div>{payform}
<div class="card"><h3>Règlements</h3><div class="tablewrap"><table><tr><th>Date</th><th>Mode</th><th>Note</th><th class="right">Montant</th></tr>{ptrs}</table></div></div>""")


def edit_document_v15(doc_id):
    con=legacy.db()
    d=con.execute('select * from docs where id=?',(doc_id,)).fetchone()
    clients=con.execute('select * from clients order by name').fetchall()
    lines=con.execute('select * from lines where doc_id=?',(doc_id,)).fetchall()
    if not d:
        con.close(); return 'Document introuvable',404
    if request.method=='POST':
        currency=_currency(request.form.get('currency'))
        rate=max(0,legacy.parse_decimal(request.form.get('fx_rate'),_default_fx_rate()))
        show=1 if request.form.get('show_conversion') else 0
        con.execute(
            """update docs set kind=?,number=?,doc_date=?,due_date=?,client_id=?,reference=?,po_number=?,payment_terms=?,delivery=?,status=?,notes=?,internal_note=?,currency=?,fx_rate=?,show_conversion=? where id=?""",
            (request.form.get('kind'),request.form.get('number'),request.form.get('doc_date'),request.form.get('due_date'),
             request.form.get('client_id') or None,request.form.get('reference'),request.form.get('po_number'),
             request.form.get('payment_terms'),request.form.get('delivery'),request.form.get('status'),
             request.form.get('notes'),request.form.get('internal_note'),currency,rate,show,doc_id)
        )
        con.execute('delete from lines where doc_id=?',(doc_id,))
        _save_lines(con,doc_id)
        for f in request.files.getlist('photos'):
            saved=legacy.save_upload(f,f'doc_{doc_id}')
            if saved and saved[2].startswith('image/'):
                con.execute('insert into doc_images(doc_id,original_name,stored_name,mime) values(?,?,?,?)',(doc_id,*saved))
        con.commit(); con.close()
        flash('Document mis à jour.')
        return redirect(url_for('document',doc_id=doc_id))
    # Pour les demandes créées depuis le site avant le choix d'un taux, propose le taux général sans altérer le document.
    if float(d['fx_rate'] or 0) <= 0 and _default_fx_rate() > 0:
        class Proxy(dict):
            pass
        proxy={k:d[k] for k in d.keys()}
        proxy['fx_rate']=_default_fx_rate()
        d=proxy
    con.close()
    return legacy.page(_document_form(d,clients,lines,False))


def convert_v15(doc_id):
    con=legacy.db()
    d=con.execute('select * from docs where id=?',(doc_id,)).fetchone()
    if not d or d['kind']!='Devis':
        con.close(); flash('Ce document ne peut pas être transformé en facture.'); return redirect(url_for('document',doc_id=doc_id))
    new_number=legacy.next_number('Facture')
    cur=con.execute(
        """insert into docs(kind,number,doc_date,due_date,client_id,reference,po_number,payment_terms,delivery,status,notes,internal_note,currency,fx_rate,show_conversion)
           values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        ('Facture',new_number,date.today().isoformat(),d['due_date'],d['client_id'],d['reference'],d['po_number'],
         d['payment_terms'],d['delivery'],'Brouillon',d['notes'],d['internal_note'],d['currency'],d['fx_rate'],d['show_conversion'])
    )
    new_id=cur.lastrowid
    for l in con.execute('select * from lines where doc_id=?',(doc_id,)).fetchall():
        con.execute(
            """insert into lines(doc_id,description,qty,unit_price,discount_pct,purchase_price,supplier_ref,supplier_name,internal_note)
               values(?,?,?,?,?,?,?,?,?)""",
            (new_id,l['description'],l['qty'],l['unit_price'],l['discount_pct'],l['purchase_price'],l['supplier_ref'],l['supplier_name'],l['internal_note'])
        )
    for im in con.execute('select * from doc_images where doc_id=?',(doc_id,)).fetchall():
        con.execute('insert into doc_images(doc_id,original_name,stored_name,mime) values(?,?,?,?)',(new_id,im['original_name'],im['stored_name'],im['mime']))
    con.execute("update docs set status='Facturé' where id=?",(doc_id,))
    con.commit(); con.close()
    flash(f'Facture {new_number} créée. Toutes les informations internes, la devise et le taux ont été conservés.')
    return redirect(url_for('document',doc_id=new_id))


def pdf_v15(doc_id):
    con=legacy.db()
    d=con.execute("""select d.*,c.name client,c.address,c.nif,c.stat,c.email,c.phone
                     from docs d left join clients c on c.id=d.client_id where d.id=?""",(doc_id,)).fetchone()
    lines=con.execute('select * from lines where doc_id=?',(doc_id,)).fetchall()
    images=con.execute('select * from doc_images where doc_id=? order by id',(doc_id,)).fetchall()
    total=legacy.total_for(con,doc_id)
    con.close()
    if not d:
        return 'Document introuvable',404
    currency=_currency(d['currency'])
    rate=float(d['fx_rate'] or 0)
    show_conversion=bool(int(d['show_conversion'] or 0))
    alt_currency='MGA' if currency=='EUR' else 'EUR'
    alt_total=_converted(total,currency,rate)
    safe=legacy.re.sub(r'[^A-Za-z0-9_-]+','-',d['number'])
    out=legacy.DATA_DIR/f"{d['kind']}_{safe}.pdf"
    c=canvas.Canvas(str(out),pagesize=A4)
    W,H=A4; L=14*mm; R=W-14*mm; y=H-12*mm
    c.setStrokeColorRGB(.15,.15,.15); c.rect(10*mm,10*mm,W-20*mm,H-20*mm)
    if legacy.LOGO.exists():
        c.drawImage(ImageReader(str(legacy.LOGO)),L,y-20*mm,width=62*mm,height=18.5*mm,preserveAspectRatio=True,mask='auto')
    c.setFont('Helvetica-Bold',12); c.drawString(116*mm,y-5*mm,f"{legacy.COMPANY['name']} {d['kind'].upper()} {d['number']}")
    c.setFont('Helvetica-Bold',9); c.drawString(116*mm,y-11*mm,f"Date : {d['doc_date'] or ''}"); c.drawString(116*mm,y-16*mm,f"Échéance : {d['due_date'] or ''}")
    y-=27*mm; c.setFillColorRGB(.68,.66,.66); c.rect(L,y,R-L,6*mm,fill=1,stroke=0); c.setFillColorRGB(0,0,0); c.setFont('Helvetica-Bold',9.5); c.drawString(L+2*mm,y+1.7*mm,'Émetteur :'); c.drawString(100*mm,y+1.7*mm,'Adresse de facturation :'); y-=2*mm
    boxh=38*mm; c.rect(L,y-boxh,R-L,boxh,fill=0,stroke=1); c.line(98*mm,y,98*mm,y-boxh)
    c.setFont('Helvetica-Bold',10); c.drawString(L+2*mm,y-5*mm,legacy.COMPANY['name']); c.setFont('Helvetica',8.5); yy=y-10*mm
    for ln in legacy.COMPANY['address'].split('\n'):
        c.drawString(L+2*mm,yy,ln); yy-=4.5*mm
    c.drawString(L+2*mm,yy-2*mm,f"NIF : {legacy.COMPANY['nif']}"); yy-=6*mm
    c.drawString(L+2*mm,yy,f"STAT : {legacy.COMPANY['stat']}"); yy-=4.5*mm
    c.drawString(L+2*mm,yy,f"Mail : {legacy.COMPANY['email']}"); yy-=4.5*mm
    c.drawString(L+2*mm,yy,f"Tél : {legacy.COMPANY['phone']}")
    xx=100*mm; yy=y-5*mm; c.setFont('Helvetica-Bold',10); c.drawString(xx,yy,d['client'] or ''); yy-=5*mm; c.setFont('Helvetica',8.5)
    for ln in (d['address'] or '').split('\n'):
        c.drawString(xx,yy,ln); yy-=4.5*mm
    yy-=2*mm; c.drawString(xx,yy,f"NIF : {d['nif'] or ''}"); yy-=4.5*mm; c.drawString(xx,yy,f"STAT : {d['stat'] or ''}")
    y-=boxh+9*mm; c.setFont('Helvetica-Bold',10); y=legacy.draw_wrapped(c,'REF : '+(d['reference'] or ''),L,y,R-L,'Helvetica-Bold',10,5*mm,2); y-=4*mm

    x1=L; x2=88*mm; x3=112*mm; x4=140*mm; x5=158*mm; x6=R
    c.setFillColorRGB(.68,.66,.66); c.rect(x1,y-8*mm,x6-x1,8*mm,fill=1,stroke=1); c.setFillColorRGB(0,0,0)
    [c.line(x,y,x,y-8*mm) for x in [x2,x3,x4,x5]]
    c.setFont('Helvetica-Bold',8.2)
    c.drawCentredString((x1+x2)/2,y-5.5*mm,'DÉSIGNATION')
    c.drawCentredString((x2+x3)/2,y-5.5*mm,'QTS')
    c.drawCentredString((x3+x4)/2,y-5.5*mm,'P.U. '+_pdf_code(currency))
    c.drawCentredString((x4+x5)/2,y-5.5*mm,'REM.')
    c.drawCentredString((x5+x6)/2,y-5.5*mm,'TOTAL '+_pdf_code(currency))
    y-=8*mm; c.setFont('Helvetica',8.2)
    for l in lines:
        h=8*mm
        c.rect(x1,y-h,x6-x1,h,fill=0,stroke=1)
        [c.line(x,y,x,y-h) for x in [x2,x3,x4,x5]]
        c.drawString(x1+2*mm,y-5.2*mm,(l['description'] or '')[:45])
        c.drawRightString(x3-2*mm,y-5.2*mm,f"{l['qty']:g}")
        c.drawRightString(x4-2*mm,y-5.2*mm,_number(l['unit_price'],currency))
        c.drawRightString(x5-2*mm,y-5.2*mm,f"{float(l['discount_pct'] or 0):g}%")
        net=l['qty']*l['unit_price']*(1-float(l['discount_pct'] or 0)/100)
        c.drawRightString(x6-2*mm,y-5.2*mm,_number(net,currency))
        y-=h

    y-=1*mm
    c.setFillColorRGB(.68,.66,.66); c.rect(100*mm,y-8*mm,R-100*mm,8*mm,fill=1,stroke=1); c.setFillColorRGB(0,0,0)
    c.setFont('Helvetica-Bold',9.5); c.drawString(103*mm,y-5.5*mm,'TOTAL en '+('euros' if currency=='EUR' else 'Ariary')); c.drawRightString(R-2*mm,y-5.5*mm,_number(total,currency))
    y-=11*mm
    if show_conversion and alt_total is not None:
        c.setFont('Helvetica-Bold',9)
        c.drawString(103*mm,y-4*mm,'Équivalent '+('Ariary' if alt_currency=='MGA' else 'euros'))
        c.drawRightString(R-2*mm,y-4*mm,_number(alt_total,alt_currency))
        y-=6*mm
        c.setFont('Helvetica',8)
        c.drawRightString(R-2*mm,y-3*mm,f"Taux utilisé : 1 EUR = {_number(rate,'MGA')} Ar")
        y-=8*mm
    else:
        y-=12*mm

    c.setFont('Helvetica-Bold',9); c.drawString(L+5*mm,y,'Arrêté à la somme de :'); c.setFont('Helvetica',9); c.drawString(L+44*mm,y,_amount_words(total,currency)+'.')
    if (d['notes'] or '').strip():
        y-=7*mm; c.setFont('Helvetica-Bold',9); c.drawString(L+5*mm,y,'Notes :'); y-=5*mm; c.setFont('Helvetica',9)
        for note_line in (d['notes'] or '').splitlines():
            c.drawString(L+5*mm,y,note_line[:100]); y-=5*mm

    by=50*mm
    c.setFillColorRGB(.7,.69,.69); c.rect(L,by,R-L,40*mm,fill=1,stroke=0)
    c.setFillColorRGB(.45,.43,.43); c.rect(L+2*mm,by+32*mm,R-L-4*mm,7*mm,fill=1,stroke=0)
    c.setFillColorRGB(0,0,0); c.setFont('Helvetica-Bold',9); c.drawString(L+4*mm,by+34*mm,'RÉFÉRENCE :')
    c.setFont('Helvetica-Bold',8.5)
    c.drawString(L+4*mm,by+25*mm,f"Devis : {'-' if d['kind']=='Devis' else ''}")
    c.drawString(L+4*mm,by+19*mm,f"Bon de commande : {d['po_number'] or ''}")
    c.drawString(L+4*mm,by+13*mm,f"Modalité du règlement : {d['payment_terms'] or ''}")
    c.drawString(L+4*mm,by+7*mm,f"Date et lieu de livraison : {d['delivery'] or ''}")
    c.setFont('Helvetica-Bold',9); c.drawString(L+10*mm,41*mm,'Coordonnées bancaires de la société :')
    c.setFillColorRGB(.78,.77,.77); c.rect(L,19*mm,R-L,18*mm,fill=1,stroke=1); c.setFillColorRGB(0,0,0)
    c.setFont('Helvetica',8.5); c.drawString(L+2*mm,32*mm,f"Banque : {legacy.COMPANY['bank']}")
    c.setFont('Helvetica-Bold',8)
    heads=['Code banque','Code guichet','N° de compte','Clé']
    vals=[legacy.COMPANY['bank_code'],legacy.COMPANY['branch_code'],legacy.COMPANY['account'],legacy.COMPANY['key']]
    xs=[L+30*mm,L+72*mm,L+120*mm,L+164*mm]
    for i,h in enumerate(heads):
        c.drawCentredString(xs[i],26*mm,h); c.setFont('Helvetica',8); c.drawCentredString(xs[i],21.5*mm,vals[i]); c.setFont('Helvetica-Bold',8)

    if images:
        c.showPage(); c.setFont('Helvetica-Bold',14); c.drawString(L,H-18*mm,f"Photos — {d['kind']} {d['number']}")
        px=L; py=H-32*mm; cellw=84*mm; cellh=62*mm; col=0
        for im in images:
            path=legacy.UPLOAD_DIR/im['stored_name']
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

    c.showPage(); c.save()
    return send_file(out,as_attachment=True,download_name=out.name)


# Remplace les écrans existants sans changer leurs URLs.
app.view_functions['home'] = home_v15
app.view_functions['documents'] = documents_v15
app.view_functions['new_document'] = new_document_v15
app.view_functions['document'] = document_v15
app.view_functions['edit_document'] = edit_document_v15
app.view_functions['convert'] = convert_v15
app.view_functions['pdf'] = pdf_v15
