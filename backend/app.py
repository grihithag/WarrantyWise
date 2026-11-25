import os, re, json, sqlite3, hashlib, secrets
from datetime import datetime, timedelta, date
from pathlib import Path

from flask import Flask, request, jsonify, send_file, g
from flask_cors import CORS
from werkzeug.security import generate_password_hash, check_password_hash
from PIL import Image, ImageStat

try:
    import fitz
except Exception:
    fitz = None
try:
    import pytesseract
except Exception:
    pytesseract = None
try:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
except Exception:
    canvas = None

BASE = Path(__file__).resolve().parent
UPLOAD_DIR = BASE / 'uploads'
DB_PATH = BASE / 'warrantywise.db'
UPLOAD_DIR.mkdir(exist_ok=True)

app = Flask(__name__)
CORS(app, resources={r'/api/*': {'origins': '*'}})

CATEGORIES = {
    'Battery': ['battery', 'charging', 'drain', 'charge', 'power'],
    'Display': ['screen', 'display', 'black line', 'dead pixel', 'flicker'],
    'Hardware': ['keyboard', 'speaker', 'port', 'touchpad', 'camera'],
    'Connectivity': ['wifi', 'wi-fi', 'bluetooth', 'network', 'internet'],
    'Software': ['crash', 'freeze', 'update', 'boot', 'error'],
    'Physical Damage': ['broken', 'crack', 'damage', 'drop', 'shatter'],
    'Cooling': ['cooling', 'heat', 'fan', 'temperature', 'not cooling'],
    'Water Leakage': ['leak', 'water', 'drain']
}

TRAIN_COMPLAINTS = {
    'Battery': ['battery drains quickly', 'charging problem', 'laptop shuts down while charging', 'battery is not holding charge'],
    'Display': ['screen has black lines', 'display is flickering', 'dead pixels on monitor'],
    'Hardware': ['keyboard keys not working', 'speaker is not producing sound', 'touchpad stopped working'],
    'Connectivity': ['wifi keeps disconnecting', 'bluetooth cannot connect', 'network failure'],
    'Software': ['system freezes after update', 'application crashes', 'laptop does not boot'],
    'Physical Damage': ['screen cracked after drop', 'body is broken', 'device has physical damage'],
    'Cooling': ['refrigerator is not cooling', 'fan making noise', 'device overheating'],
    'Water Leakage': ['washing machine is leaking', 'water is coming from refrigerator']
}

# ---------------- Database ----------------
def db():
    if 'db' not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db

@app.teardown_appcontext
def close_db(_=None):
    conn = g.pop('db', None)
    if conn:
        conn.close()

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, name TEXT, email TEXT UNIQUE, password TEXT, created_at TEXT);
    CREATE TABLE IF NOT EXISTS products (id INTEGER PRIMARY KEY, user_id INTEGER, name TEXT, brand TEXT, model TEXT, category TEXT,
      serial_number TEXT, purchase_date TEXT, warranty_months INTEGER, expiry_date TEXT, invoice_number TEXT, invoice_path TEXT,
      status TEXT, created_at TEXT);
    CREATE TABLE IF NOT EXISTS policies (id INTEGER PRIMARY KEY, user_id INTEGER, product_id INTEGER, name TEXT, text TEXT,
      warranty_months INTEGER, coverage TEXT, exclusions TEXT, file_path TEXT, created_at TEXT);
    CREATE TABLE IF NOT EXISTS claims (id INTEGER PRIMARY KEY, user_id INTEGER, product_id INTEGER, issue TEXT, category TEXT,
      status TEXT, score INTEGER, reasons TEXT, claim_letter TEXT, created_at TEXT);
    CREATE TABLE IF NOT EXISTS documents (id INTEGER PRIMARY KEY, user_id INTEGER, product_id INTEGER, name TEXT,
      file_path TEXT, text TEXT, doc_hash TEXT, created_at TEXT);
    CREATE TABLE IF NOT EXISTS sessions (token TEXT PRIMARY KEY, user_id INTEGER, created_at TEXT);
    """)
    conn.commit()
    demo = conn.execute('SELECT id FROM users WHERE email=?', ('demo@warrantywise.ai',)).fetchone()
    if not demo:
        conn.execute('INSERT INTO users(name,email,password,created_at) VALUES(?,?,?,?)',
                     ('Alex Morgan','demo@warrantywise.ai',generate_password_hash('demo123'),datetime.now().isoformat()))
        conn.commit()
    conn.close()

init_db()

TOKEN_NAME = 'Authorization'
def current_user():
    token = request.headers.get(TOKEN_NAME, '').replace('Bearer ', '')
    if not token:
        return None
    row = db().execute('SELECT u.* FROM users u JOIN sessions s ON s.user_id=u.id WHERE s.token=?', (token,)).fetchone()
    return row

def auth_required(fn):
    from functools import wraps
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not current_user():
            return jsonify({'error': 'Authentication required'}), 401
        return fn(*args, **kwargs)
    return wrapped

# ---------------- Helpers ----------------
def row_dict(row):
    return dict(row) if row else None

def extract_text(file_path):
    suffix = Path(file_path).suffix.lower()
    text = ''
    try:
        if suffix == '.pdf' and fitz:
            doc = fitz.open(file_path)
            text = '\n'.join(page.get_text() for page in doc)
            doc.close()
        elif suffix in ('.png', '.jpg', '.jpeg', '.webp') and pytesseract:
            text = pytesseract.image_to_string(Image.open(file_path))
    except Exception:
        text = ''
    return text.strip()

def parse_date(text):
    patterns = [r'(\d{4}[-/.]\d{1,2}[-/.]\d{1,2})', r'(\d{1,2}[-/.]\d{1,2}[-/.]\d{4})']
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            value = match.group(1).replace('/', '-').replace('.', '-')
            for fmt in ('%Y-%m-%d', '%d-%m-%Y', '%m-%d-%Y'):
                try: return datetime.strptime(value, fmt).strftime('%Y-%m-%d')
                except ValueError: pass
    return None

def extract_fields(text):
    flat = ' '.join(text.split())
    invoice = re.search(r'(?i)(invoice|bill)[\s#:No.-]*([A-Z0-Z][A-Z0-9/-]*)', flat)
    serial = re.search(r'(?i)(serial|s/n|serial no)[\s:#-]*([A-Z0-9-]{6,})', flat)
    model = re.search(r'(?i)(model|product)[\s:#-]*([A-Z0-9][A-Z0-9 ._-]{3,30})', flat)
    amount = re.search(r'(?i)(total|amount|grand total)[\s:₹$]*([0-9][0-9,]*(?:\.[0-9]{1,2})?)', flat)
    brands = ['Samsung','Apple','Dell','HP','Lenovo','Sony','LG','Asus','Acer','Xiaomi','Bosch','Whirlpool','Haier']
    brand = next((b for b in brands if re.search(rf'\b{b}\b', flat, re.I)), '')
    purchase = parse_date(flat)
    return {'invoice_number': invoice.group(2).strip() if invoice else '', 'serial_number': serial.group(2).strip() if serial else '',
            'model': model.group(2).strip() if model else '', 'amount': amount.group(2).strip() if amount else '',
            'brand': brand, 'purchase_date': purchase}

def image_quality(path):
    try:
        image = Image.open(path).convert('RGB')
        stat = ImageStat.Stat(image)
        brightness = sum(stat.mean) / 3
        width, height = image.size
        score = 100
        issues = []
        if width < 900 or height < 900: issues.append('Low resolution'); score -= 25
        if brightness < 70: issues.append('Poor lighting'); score -= 20
        if brightness > 235: issues.append('Overexposed'); score -= 15
        return {'score': max(score, 0), 'issues': issues, 'width': width, 'height': height}
    except Exception:
        return {'score': 70, 'issues': ['Unable to inspect image'], 'width': None, 'height': None}

def classify_complaint(text):
    lower = text.lower()
    scores = {}
    for category, terms in CATEGORIES.items():
        scores[category] = sum(1 for term in terms if term in lower)
    best = max(scores, key=scores.get)
    if scores[best] == 0:
        best = 'Hardware'
    confidence = min(92, 55 + scores[best] * 15)
    return best, confidence

def policy_summary(text):
    lower = text.lower()
    duration_match = re.search(r'(\d+)\s*(month|months|year|years)', lower)
    months = 0
    if duration_match:
        n = int(duration_match.group(1)); unit = duration_match.group(2)
        months = n * 12 if 'year' in unit else n
    covered = []
    excluded = []
    for term in ['battery','display','manufacturing defect','screen','hardware','software','charging','cooling']:
        if term in lower: covered.append(term.title())
    for term in ['physical damage','liquid damage','water damage','accidental damage','misuse','tampering']:
        if term in lower: excluded.append(term.title())
    return {'warranty_months': months or 12, 'covered': covered, 'exclusions': excluded}

def expiry_status(expiry):
    if not expiry: return 'Unknown'
    end = datetime.strptime(expiry, '%Y-%m-%d').date()
    days = (end - date.today()).days
    if days < 0: return 'Expired'
    if days <= 7: return 'Expiring in 7 days'
    if days <= 15: return 'Expiring in 15 days'
    if days <= 30: return 'Expiring in 30 days'
    return 'Active'

# ---------------- Auth ----------------
@app.post('/api/auth/register')
def register():
    data = request.get_json(force=True)
    name = data.get('name','').strip(); email = data.get('email','').lower().strip(); password = data.get('password','')
    if not name or not email or len(password) < 6:
        return jsonify({'error':'Name and a valid email are required; password must be at least 6 characters'}), 400
    conn = db()
    try:
        conn.execute('INSERT INTO users(name,email,password,created_at) VALUES(?,?,?,?)',(name,email,generate_password_hash(password),datetime.now().isoformat()))
        conn.commit(); user = conn.execute('SELECT * FROM users WHERE email=?',(email,)).fetchone()
    except sqlite3.IntegrityError:
        return jsonify({'error':'An account already exists with this email'}), 409
    token = secrets.token_urlsafe(32)
    conn.execute('INSERT INTO sessions(token,user_id,created_at) VALUES(?,?,?)',(token,user['id'],datetime.now().isoformat())); conn.commit()
    return jsonify({'token':token,'user':{'id':user['id'],'name':user['name'],'email':user['email']}})

@app.post('/api/auth/login')
def login():
    data = request.get_json(force=True)
    email = data.get('email','').lower().strip(); password = data.get('password','')
    user = db().execute('SELECT * FROM users WHERE email=?',(email,)).fetchone()
    if not user or not check_password_hash(user['password'],password): return jsonify({'error':'Invalid email or password'}), 401
    token = secrets.token_urlsafe(32)
    db().execute('INSERT INTO sessions(token,user_id,created_at) VALUES(?,?,?)',(token,user['id'],datetime.now().isoformat())); db().commit()
    return jsonify({'token':token,'user':{'id':user['id'],'name':user['name'],'email':user['email']}})

@app.get('/api/me')
@auth_required
def me():
    u=current_user(); return jsonify({'id':u['id'],'name':u['name'],'email':u['email']})

# ---------------- Documents / OCR ----------------
@app.post('/api/documents/upload')
@auth_required
def upload_document():
    u=current_user(); file=request.files.get('file'); product_id=request.form.get('product_id')
    if not file: return jsonify({'error':'Please choose a file'}), 400
    safe_name = f"{datetime.now().strftime('%Y%m%d%H%M%S')}_{file.filename}"
    path=UPLOAD_DIR/safe_name; file.save(path)
    text=extract_text(str(path)); fields=extract_fields(text)
    quality=image_quality(str(path)) if path.suffix.lower() in ('.png','.jpg','.jpeg','.webp') else {'score':90,'issues':[]}
    digest=hashlib.sha256(path.read_bytes()).hexdigest()
    dup=db().execute('SELECT id,name FROM documents WHERE user_id=? AND doc_hash=?',(u['id'],digest)).fetchone()
    db().execute('INSERT INTO documents(user_id,product_id,name,file_path,text,doc_hash,created_at) VALUES(?,?,?,?,?,?,?)',
                 (u['id'],product_id or None,file.filename,str(path),text,digest,datetime.now().isoformat())); db().commit()
    doc=db().execute('SELECT * FROM documents WHERE user_id=? AND doc_hash=?',(u['id'],digest)).fetchone()
    return jsonify({'document':row_dict(doc),'text':text,'fields':fields,'quality':quality,'duplicate':row_dict(dup)})

@app.post('/api/policies/analyze')
@auth_required
def analyze_policy():
    u=current_user(); file=request.files.get('file'); product_id=request.form.get('product_id')
    if not file: return jsonify({'error':'Please upload a warranty policy'}), 400
    path=UPLOAD_DIR/f"policy_{datetime.now().strftime('%Y%m%d%H%M%S')}_{file.filename}"; file.save(path)
    text=extract_text(str(path)); summary=policy_summary(text)
    db().execute('INSERT INTO policies(user_id,product_id,name,text,warranty_months,coverage,exclusions,file_path,created_at) VALUES(?,?,?,?,?,?,?,?,?)',
                 (u['id'],product_id or None,file.filename,text,summary['warranty_months'],json.dumps(summary['covered']),json.dumps(summary['exclusions']),str(path),datetime.now().isoformat())); db().commit()
    pid=db().execute('SELECT last_insert_rowid()').fetchone()[0]
    return jsonify({'id':pid,'summary':summary,'text':text[:5000]})

# ---------------- Products ----------------
@app.get('/api/products')
@auth_required
def products():
    u=current_user(); rows=db().execute('SELECT * FROM products WHERE user_id=? ORDER BY id DESC',(u['id'],)).fetchall()
    result=[]
    for r in rows:
        d=row_dict(r); d['status']=expiry_status(d['expiry_date']); result.append(d)
    return jsonify(result)

@app.post('/api/products')
@auth_required
def create_product():
    u=current_user(); d=request.get_json(force=True)
    purchase=d.get('purchase_date')
    months=int(d.get('warranty_months') or 12)
    expiry=None
    if purchase:
        expiry=(datetime.strptime(purchase,'%Y-%m-%d') + timedelta(days=months*30)).strftime('%Y-%m-%d')
    c=db(); cur=c.execute('INSERT INTO products(user_id,name,brand,model,category,serial_number,purchase_date,warranty_months,expiry_date,invoice_number,invoice_path,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (u['id'],d.get('name',''),d.get('brand',''),d.get('model',''),d.get('category',''),d.get('serial_number',''),purchase,months,expiry,d.get('invoice_number',''),d.get('invoice_path',''),expiry_status(expiry),datetime.now().isoformat()))
    c.commit(); return jsonify(row_dict(c.execute('SELECT * FROM products WHERE id=?',(cur.lastrowid,)).fetchone())),201

@app.delete('/api/products/<int:pid>')
@auth_required
def delete_product(pid):
    u=current_user(); db().execute('DELETE FROM products WHERE id=? AND user_id=?',(pid,u['id'])); db().commit(); return jsonify({'ok':True})

# ---------------- Claims / AI ----------------
@app.post('/api/claims/assess')
@auth_required
def assess_claim():
    u=current_user(); d=request.get_json(force=True); issue=d.get('issue',''); product_id=d.get('product_id')
    category,confidence=classify_complaint(issue)
    product=db().execute('SELECT * FROM products WHERE id=? AND user_id=?',(product_id,u['id'])).fetchone() if product_id else None
    score=40; reasons=[]
    if product:
        expiry=product['expiry_date']; state=expiry_status(expiry)
        if state=='Active': score+=25; reasons.append('Purchase warranty is active.')
        elif state.startswith('Expired'): reasons.append('The warranty period has expired.')
        else: reasons.append('Warranty expiry is not confirmed.')
        if product['invoice_number']: score+=15; reasons.append('Invoice number is available.')
        else: reasons.append('Purchase invoice details are incomplete.')
        if product['serial_number']: score+=10; reasons.append('Serial number is verified.')
        else: reasons.append('Product serial number is missing.')
    else:
        reasons.append('No linked product record was provided.')
    if issue.strip(): score+=10; reasons.append('A detailed issue description is available.')
    if category=='Physical Damage': score-=15; reasons.append('Physical damage may be excluded by standard policies.')
    score=max(0,min(100,score)); status='Potentially eligible' if score>=70 else 'Manual verification'
    letter=(f"Dear Support Team,\n\nI purchased {product['name'] if product else 'the product'} on {product['purchase_date'] if product else '[purchase date]'}. "
            f"The device is experiencing: {issue}.\n\nBased on the available product and warranty information, I request a review of this issue and confirmation of applicable coverage. "
            "I have included the available purchase invoice and supporting records for verification.\n\nKind regards,\n" + u['name'])
    c=db(); cur=c.execute('INSERT INTO claims(user_id,product_id,issue,category,status,score,reasons,claim_letter,created_at) VALUES(?,?,?,?,?,?,?,?,?)',
        (u['id'],product_id or None,issue,category,status,score,json.dumps(reasons),letter,datetime.now().isoformat())); c.commit()
    return jsonify({'id':cur.lastrowid,'category':category,'confidence':confidence,'score':score,'status':status,'reasons':reasons,'letter':letter,'disclaimer':'Preliminary decision support only; not an official manufacturer approval.'})

@app.get('/api/claims')
@auth_required
def claims():
    u=current_user(); rows=db().execute('SELECT * FROM claims WHERE user_id=? ORDER BY id DESC',(u['id'],)).fetchall()
    result=[]
    for r in rows:
        d=row_dict(r); d['reasons']=json.loads(d['reasons'] or '[]'); result.append(d)
    return jsonify(result)

@app.post('/api/chat')
@auth_required
def chat():
    u=current_user(); data=request.get_json(force=True); question=data.get('question','').lower()
    products=db().execute('SELECT * FROM products WHERE user_id=?',(u['id'],)).fetchall()
    policies=db().execute('SELECT name,text FROM policies WHERE user_id=?',(u['id'],)).fetchall()
    answer=''; sources=[]
    if any(word in question for word in ['warranty','expire','expiry','active','under warranty']):
        if products:
            p=products[0]; state=expiry_status(p['expiry_date'])
            answer=f"{p['name']} is {state.lower()}. Its warranty is recorded until {p['expiry_date'] or 'an unknown date'}."
        else: answer='There are no registered products yet. Add a product to check its warranty status.'
    elif any(word in question for word in ['document','invoice','claim','submit']):
        answer='For a claim, prepare the original invoice, warranty card or policy, product serial number, issue description, and clear photos of the problem.'
    elif any(word in question for word in ['battery','charging','power']):
        answer='Battery and charging issues may be covered if the relevant battery term is active and no exclusion applies. Battery coverage can differ from the main product warranty.'
    elif policies:
        policy=policies[0]; snippet=re.sub(r'\s+',' ',policy['text'])[:500]
        answer=f"Based on the uploaded document '{policy['name']}', I found: {snippet}" if snippet else 'The uploaded policy text could not be read. Re-upload a clearer document.'
        sources=[policy['name']]
    else:
        answer='I need more information. Add product details or upload a warranty policy, and I can tailor the answer to your records.'
    return jsonify({'answer':answer,'sources':sources,'disclaimer':'WarrantyBot provides document-assisted guidance, not an official decision.'})

@app.get('/api/analytics')
@auth_required
def analytics():
    u=current_user(); ps=db().execute('SELECT * FROM products WHERE user_id=?',(u['id'],)).fetchall(); cs=db().execute('SELECT * FROM claims WHERE user_id=?',(u['id'],)).fetchall()
    states={}; categories={}
    for p in ps:
        state=expiry_status(p['expiry_date']); states[state]=states.get(state,0)+1
        cat=p['category'] or 'Uncategorized'; categories[cat]=categories.get(cat,0)+1
    claim_categories={}
    for c in cs: claim_categories[c['category']]=claim_categories.get(c['category'],0)+1
    return jsonify({'summary':{'products':len(ps),'claims':len(cs),'active':states.get('Active',0),'expired':states.get('Expired',0)},
                    'warranty_status':states,'categories':categories,'claim_categories':claim_categories})

@app.post('/api/claims/<int:cid>/pdf')
@auth_required
def claim_pdf(cid):
    u=current_user(); claim=db().execute('SELECT * FROM claims WHERE id=? AND user_id=?',(cid,u['id'])).fetchone()
    if not claim: return jsonify({'error':'Claim not found'}),404
    if canvas is None: return jsonify({'error':'PDF support is unavailable'}),500
    path=UPLOAD_DIR/f'claim_{cid}.pdf'; c=canvas.Canvas(str(path),pagesize=A4); width,height=A4
    c.setTitle('WarrantyWise Claim Kit'); c.setFillColorRGB(0.05,0.09,0.18); c.rect(0,height-100,width,100,fill=1,stroke=0)
    c.setFillColorRGB(1,1,1); c.setFont('Helvetica-Bold',22); c.drawString(40,height-58,'WarrantyWise Claim Kit')
    c.setFont('Helvetica',10); c.drawString(40,height-78,'AI-assisted claim preparation report')
    y=height-140; c.setFillColorRGB(0.08,0.12,0.2); c.setFont('Helvetica-Bold',12); c.drawString(40,y,'Assessment')
    y-=24; c.setFont('Helvetica',11); c.drawString(40,y,f"Status: {claim['status']}    Readiness score: {claim['score']}/100")
    y-=22; c.drawString(40,y,f"Complaint category: {claim['category']}"); y-=32
    c.setFont('Helvetica-Bold',12); c.drawString(40,y,'Claim letter'); y-=22
    c.setFont('Helvetica',10)
    for line in claim['claim_letter'].split('\n'):
        c.drawString(40,y,line[:100]); y-=16
    y-=18; c.setFont('Helvetica-Bold',12); c.drawString(40,y,'Explanation'); y-=22; c.setFont('Helvetica',10)
    for reason in json.loads(claim['reasons'] or '[]'):
        c.drawString(48,y,f'- {reason}'); y-=16
    y-=30; c.setFont('Helvetica-Oblique',8); c.setFillColorRGB(0.45,0.45,0.5); c.drawString(40,y,'Preliminary decision support only. Not an official manufacturer decision.')
    c.save(); return send_file(path,as_attachment=True,download_name=f'warrantywise_claim_{cid}.pdf')

@app.get('/api/health')
def health(): return jsonify({'status':'ok','timestamp':datetime.now().isoformat()})

if __name__=='__main__':
    app.run(debug=True,port=5000)
