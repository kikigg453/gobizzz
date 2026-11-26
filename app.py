import os
import json
import uuid
import threading
import requests
import qrcode
import io
import base64
from datetime import datetime, timedelta
from functools import wraps
from flask import Flask, request, jsonify, session, redirect, url_for
import pytz

app = Flask(__name__)
app.secret_key = 'brutalist-qris-KELL-2024-x9KpL3mNv7Qw'

# ==================== CONFIG ====================
BASE_URL = 'https://api.gobiz.co.id'
CLIENT_ID = 'go-biz-web-new'
TZ = pytz.timezone('Asia/Jakarta')
OWNER_USERNAME = 'KELL'
OWNER_PASSWORD = 'KELL9089'
ADMIN_FEE = 100
MIN_WITHDRAW = 5000
PORT = 2010

# ==================== DATABASE ====================
DB_FILE = 'database.json'
db_lock = threading.Lock()

def load_db():
    if not os.path.exists(DB_FILE):
        return {
            "users": {},
            "transactions": [],
            "withdrawals": [],
            "gobiz": {},
            "pending_otp": {},
            "owner_pending_otp": {},
            "dynamic_qr": []
        }
    with open(DB_FILE, 'r') as f:
        return json.load(f)

def save_db(data):
    with open(DB_FILE, 'w') as f:
        json.dump(data, f, indent=2)

def get_db():
    with db_lock:
        return load_db()

def update_db(fn):
    with db_lock:
        data = load_db()
        result = fn(data)
        save_db(data)
        return result

# ==================== GOBIZ CLIENT ====================
class GoBizClient:
    def __init__(self):
        self.token = None
        self.merchant_id = None
        self._load()

    def _load(self):
        db = get_db()
        g = db.get('gobiz', {})
        self.token = g.get('token')
        self.merchant_id = g.get('merchant_id')

    def _save(self):
        def upd(db):
            db['gobiz'] = {
                'token': self.token,
                'merchant_id': self.merchant_id,
                'updated_at': datetime.now(TZ).isoformat()
            }
        update_db(upd)

    def _headers(self, token=None):
        return {
            'Accept': 'application/json, text/plain, */*',
            'Accept-Language': 'id',
            'Authentication-Type': 'go-id',
            'Authorization': f'Bearer {token or self.token or ""}',
            'Content-Type': 'application/json',
            'Gojek-Country-Code': 'ID',
            'Gojek-Timezone': 'Asia/Jakarta',
            'Origin': 'https://portal.gofoodmerchant.co.id',
            'Referer': 'https://portal.gofoodmerchant.co.id/',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36',
            'X-AppVersion': 'platform-v3.107.0-94ce5d57',
            'X-Platform': 'Web',
            'X-User-Type': 'merchant',
            'x-appId': 'go-biz-web-dashboard',
            'x-uniqueid': str(uuid.uuid4()),
        }

    def request_otp(self, phone):
        p = ''.join(c for c in phone if c.isdigit())
        if p.startswith('62'):
            p = p[2:]
        elif p.startswith('0'):
            p = p[1:]
        r = requests.post(f'{BASE_URL}/goid/login/request',
            headers=self._headers(),
            json={
                'client_id': CLIENT_ID,
                'phone_number': p,
                'country_code': '62',
                'login_type': 'otp'
            },
            timeout=30)
        d = r.json()
        if d.get('errors'):
            raise Exception(d['errors'][0].get('message', 'Gagal kirim OTP'))
        resp = d.get('data') or d
        return {'phone': p, 'otp_token': resp.get('otp_token') or resp.get('token')}

    def verify_otp(self, phone, otp_code, otp_token):
        p = ''.join(c for c in phone if c.isdigit())
        if p.startswith('62'):
            p = p[2:]
        elif p.startswith('0'):
            p = p[1:]
        data_field = {'otp': otp_code}
        if otp_token:
            data_field['otp_token'] = otp_token
        else:
            data_field['phone_number'] = p
        r = requests.post(f'{BASE_URL}/goid/token',
            headers=self._headers(),
            json={'client_id': CLIENT_ID, 'grant_type': 'otp', 'data': data_field},
            timeout=30)
        d = r.json()
        if d.get('errors'):
            raise Exception(d['errors'][0].get('message', 'OTP salah'))
        if not d.get('access_token'):
            raise Exception('Token tidak diterima')
        self.token = d['access_token']
        self._save()
        return True

    def is_token_valid(self):
        if not self.token:
            return False
        try:
            r = requests.post(f'{BASE_URL}/v1/merchants/search',
                headers=self._headers(),
                json={'from': 0, 'to': 1, '_source': ['id']},
                timeout=15)
            return r.status_code != 401
        except:
            return False

    def get_merchants(self):
        r = requests.post(f'{BASE_URL}/v1/merchants/search',
            headers=self._headers(),
            json={'from': 0, 'to': 50, '_source': ['id', 'merchant_name']},
            timeout=30)
        if not r.ok:
            raise Exception(f'Gagal ambil merchant: {r.status_code}')
        return r.json()

    def ensure_merchant(self):
        if self.merchant_id:
            return self.merchant_id
        data = self.get_merchants()
        lst = []
        if isinstance(data, list):
            lst = data
        elif isinstance(data, dict):
            if isinstance(data.get('merchants'), list):
                lst = data['merchants']
            elif isinstance(data.get('hits'), dict) and isinstance(data['hits'].get('hits'), list):
                lst = [h.get('_source', h) for h in data['hits']['hits']]
            elif isinstance(data.get('hits'), list):
                lst = data['hits']
            elif isinstance(data.get('data'), list):
                lst = data['data']
        if not lst:
            raise Exception('Tidak ada merchant terasosiasi')
        m = lst[0]
        self.merchant_id = m.get('id') or m.get('merchant_id')
        self._save()
        return self.merchant_id

    def get_transactions(self, days=1, size=50):
        self.ensure_merchant()
        now = datetime.now(TZ)
        start = (now - timedelta(days=days)).isoformat()
        end = now.isoformat()
        url = 'https://api.gojekapi.com/merchant-analytics/v2/merchants/transactions'
        params = {
            'from': '0',
            'size': str(size),
            'statuses': 'SETTLEMENT,CAPTURE,REFUND,PARTIAL_REFUND',
            'payment_types': 'QRIS,GOPAY,OFFLINE_CREDIT_CARD,OFFLINE_DEBIT_CARD,CREDIT_CARD',
            'start_time': start,
            'end_time': end,
            'merchant_ids': self.merchant_id
        }
        r = requests.get(url, headers=self._headers(), params=params, timeout=30)
        if r.status_code == 401:
            self.token = None
            raise Exception('Token expired')
        if not r.ok:
            raise Exception(f'HTTP {r.status_code}')
        return r.json()


gobiz = GoBizClient()

# ==================== WATCHER ====================
seen_tx = set()
seen_lock = threading.Lock()

def watcher_loop():
    seeded = False
    while True:
        try:
            data = gobiz.get_transactions(days=1, size=30)
            txs = data.get('transactions') if isinstance(data, dict) else None
            if isinstance(txs, list):
                for tx in txs:
                    txid = tx.get('transaction_id') or tx.get('id') or tx.get('order_id')
                    if not txid:
                        continue
                    with seen_lock:
                        if txid in seen_tx:
                            continue
                        seen_tx.add(txid)
                    if not seeded:
                        continue
                    gross = tx.get('gross_amount', 0)
                    amount = gross / 100 if isinstance(gross, (int, float)) else 0
                    if amount <= 0:
                        continue
                    def apply_match(db):
                        matched = False
                        for uid, u in db['users'].items():
                            for p in u.get('pending_payments', []):
                                if p.get('active') and abs(float(p.get('amount', 0)) - amount) < 0.01:
                                    u['balance'] = u.get('balance', 0) + amount
                                    p['active'] = False
                                    p['paid_at'] = datetime.now(TZ).isoformat()
                                    p['txid'] = txid
                                    db['transactions'].append({
                                        'user': uid,
                                        'amount': amount,
                                        'txid': txid,
                                        'time': datetime.now(TZ).isoformat(),
                                        'raw': tx
                                    })
                                    matched = True
                                    break
                            if matched:
                                break
                        return matched
                    update_db(apply_match)
            if not seeded:
                seeded = True
        except Exception as e:
            print(f'[watcher] {e}')
        time.sleep(6)


def start_watcher():
    t = threading.Thread(target=watcher_loop, daemon=True)
    t.start()

# ==================== HELPERS ====================
def now_str():
    return datetime.now(TZ).strftime('%d %b %Y %H:%M:%S')

def gen_qr_base64(data_text):
    img = qrcode.make(data_text)
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    return 'data:image/png;base64,' + base64.b64encode(buf.getvalue()).decode()

def login_required(f):
    @wraps(f)
    def wrap(*a, **kw):
        if 'user' not in session:
            return redirect(url_for('login'))
        return f(*a, **kw)
    return wrap

def owner_required(f):
    @wraps(f)
    def wrap(*a, **kw):
        if not session.get('is_owner'):
            return redirect(url_for('owner_login'))
        return f(*a, **kw)
    return wrap

# ==================== BASE TEMPLATE (BRUTALIST) ====================
BASE_CSS = """
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Courier New',monospace;background:#fff;color:#000;padding:20px;min-height:100vh}
a{color:#000;text-decoration:none}
.wrap{max-width:960px;margin:0 auto}
.brut{border:4px solid #000;box-shadow:8px 8px 0 #000;background:#fff;padding:20px;margin-bottom:20px}
.btn{display:inline-block;background:#ffde00;border:4px solid #000;padding:12px 20px;font-weight:900;font-family:'Courier New',monospace;cursor:pointer;text-transform:uppercase;font-size:14px;box-shadow:4px 4px 0 #000;transition:all 0.1s}
.btn:hover{transform:translate(-2px,-2px);box-shadow:6px 6px 0 #000}
.btn:active{transform:translate(2px,2px);box-shadow:2px 2px 0 #000}
.btn-red{background:#ff3b30;color:#fff}
.btn-green{background:#00d26a}
.btn-black{background:#000;color:#ffde00}
.btn-sm{padding:6px 12px;font-size:12px;box-shadow:3px 3px 0 #000}
input,select{font-family:'Courier New',monospace;border:4px solid #000;padding:12px;font-size:16px;width:100%;font-weight:700;background:#fff}
input:focus{outline:none;background:#ffde00}
label{display:block;font-weight:900;text-transform:uppercase;margin-bottom:6px;font-size:12px;letter-spacing:1px}
h1{font-size:36px;text-transform:uppercase;font-weight:900;letter-spacing:-1px;margin-bottom:6px}
h2{font-size:24px;text-transform:uppercase;font-weight:900;margin-bottom:12px}
h3{font-size:18px;text-transform:uppercase;font-weight:900;margin-bottom:8px}
.tag{display:inline-block;background:#000;color:#ffde00;padding:4px 10px;font-weight:900;text-transform:uppercase;font-size:11px;letter-spacing:1px}
.tag-green{background:#00d26a;color:#000}
.tag-red{background:#ff3b30;color:#fff}
.tag-yellow{background:#ffde00;color:#000}
.row{display:flex;gap:12px;flex-wrap:wrap}
.col{flex:1;min-width:180px}
.grid{display:grid;gap:16px;grid-template-columns:repeat(auto-fit,minmax(240px,1fr))}
.kpi{background:#000;color:#ffde00;padding:20px;border:4px solid #000}
.kpi-val{font-size:40px;font-weight:900;line-height:1}
.kpi-lbl{font-size:11px;letter-spacing:2px;text-transform:uppercase;margin-top:6px}
table{width:100%;border-collapse:collapse;border:4px solid #000}
th,td{border:2px solid #000;padding:10px;text-align:left;font-size:13px;font-weight:700}
th{background:#000;color:#ffde00;text-transform:uppercase;font-size:11px;letter-spacing:1px}
tr:nth-child(even) td{background:#f5f5f5}
.nav{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:20px}
.nav a{padding:10px 16px;border:4px solid #000;background:#fff;font-weight:900;text-transform:uppercase;font-size:12px;letter-spacing:1px}
.nav a.active{background:#000;color:#ffde00}
.err{background:#ff3b30;color:#fff;border:4px solid #000;padding:12px;font-weight:900;margin-bottom:12px}
.ok{background:#00d26a;border:4px solid #000;padding:12px;font-weight:900;margin-bottom:12px}
.mono{font-family:'Courier New',monospace}
.qrbox{border:4px solid #000;padding:12px;background:#fff;display:inline-block;box-shadow:6px 6px 0 #000}
.qrbox img{display:block;width:260px;height:260px}
.flex{display:flex;gap:16px;align-items:center;flex-wrap:wrap}
.hero{background:#ffde00;border:4px solid #000;padding:32px;box-shadow:10px 10px 0 #000;margin-bottom:20px}
.hero h1{font-size:48px;line-height:0.95}
.hero p{margin-top:8px;font-weight:700}
.text-xs{font-size:11px}
.mt{margin-top:12px}.mb{margin-bottom:12px}
</style>
"""

def page(title, body, nav=None):
    nav_html = ''
    if nav:
        nav_html = '<div class="nav">' + ''.join(
            f'<a href="{u}" class="{"active" if a else ""}">{l}</a>' for l, u, a in nav
        ) + '</div>'
    return f"""<!DOCTYPE html>
<html lang="id"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} — QRIS BRUTAL</title>{BASE_CSS}</head>
<body><div class="wrap">{nav_html}{body}</div></body></html>"""

# ==================== ROUTES: USER ====================
@app.route('/')
def index():
    if 'user' in session:
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))

@app.route('/register', methods=['GET', 'POST'])
def register():
    err = None
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()
        if not username or not password:
            err = 'Username & password wajib'
        elif len(password) < 4:
            err = 'Password minimal 4 karakter'
        else:
            def add(db):
                if username in db['users']:
                    return False
                db['users'][username] = {
                    'password': password,
                    'balance': 0,
                    'pending_payments': [],
                    'created': datetime.now(TZ).isoformat()
                }
                return True
            ok = update_db(add)
            if not ok:
                err = 'Username sudah dipakai'
            else:
                return redirect(url_for('login'))
    body = f"""
    <div class="hero"><span class="tag">REGISTRASI</span><h1>DAFTAR<br>AKUN</h1><p>Buat akun untuk mulai transaksi.</p></div>
    {f'<div class="err">{err}</div>' if err else ''}
    <div class="brut">
    <form method="post">
    <div class="col mb"><label>Username</label><input name="username" required></div>
    <div class="col mb"><label>Password</label><input type="password" name="password" required></div>
    <button class="btn" type="submit">DAFTAR →</button>
    </form>
    <p class="mt text-xs">Sudah punya akun? <a href="/login"><b>LOGIN</b></a></p>
    </div>"""
    return page('Register', body)

@app.route('/login', methods=['GET', 'POST'])
def login():
    err = None
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()
        db = get_db()
        u = db['users'].get(username)
        if not u or u.get('password') != password:
            err = 'Username/password salah'
        else:
            session['user'] = username
            return redirect(url_for('dashboard'))
    body = f"""
    <div class="hero"><span class="tag">USER LOGIN</span><h1>MASUK<br>USER</h1></div>
    {f'<div class="err">{err}</div>' if err else ''}
    <div class="brut">
    <form method="post">
    <div class="col mb"><label>Username</label><input name="username" required></div>
    <div class="col mb"><label>Password</label><input type="password" name="password" required></div>
    <button class="btn" type="submit">LOGIN →</button>
    </form>
    <p class="mt text-xs">Belum punya akun? <a href="/register"><b>DAFTAR</b></a></p>
    <p class="mt text-xs">Owner? <a href="/owner/login"><b>MASUK OWNER</b></a></p>
    </div>"""
    return page('Login', body)

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/dashboard')
@login_required
def dashboard():
    db = get_db()
    u = db['users'][session['user']]
    bal = u.get('balance', 0)
    history = [t for t in db['transactions'] if t.get('user') == session['user']]
    history = list(reversed(history[-20:]))
    hist_rows = ''.join(
        f"<tr><td>{h['time'][:19]}</td><td>Rp {int(h['amount']):,}</td><td class='mono'>{h['txid'][:16]}</td></tr>"
        for h in history
    ) or "<tr><td colspan='3'>Belum ada transaksi</td></tr>"
    body = f"""
    <div class="hero"><span class="tag">USER PANEL</span><h1>HALO,<br>{session['user']}</h1></div>
    <div class="grid mb">
    <div class="kpi"><div class="kpi-lbl">SALDO ANDA</div><div class="kpi-val">Rp {int(bal):,}</div></div>
    <div class="kpi"><div class="kpi-lbl">TOTAL TRANSAKSI</div><div class="kpi-val">{len(history)}</div></div>
    </div>
    <div class="brut">
    <h2>AJUKAN PEMBAYARAN</h2>
    <p class="mb text-xs">Masukkan nominal, sistem generate QR. Scan pakai GoPay/QRIS apapun. Saldo otomatis bertambah saat terdeteksi.</p>
    <form onsubmit="event.preventDefault();payNow()">
    <div class="row">
    <div class="col mb"><label>Nominal (Rp)</label><input type="number" id="amt" min="1" required></div>
    <div class="col mb" style="display:flex;align-items:flex-end"><button class="btn" type="submit">BUAT QR →</button></div>
    </div>
    </form>
    <div id="qrres"></div>
    </div>
    <div class="brut">
    <h2>RIWAYAT</h2>
    <table><thead><tr><th>Waktu</th><th>Nominal</th><th>TXID</th></tr></thead>
    <tbody>{hist_rows}</tbody></table>
    </div>
    <script>
    async function payNow(){{
      const amt=document.getElementById('amt').value;
      const r=await fetch('/api/create_payment',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{amount:parseInt(amt)}})}});
      const d=await r.json();
      const el=document.getElementById('qrres');
      if(!d.ok){{el.innerHTML='<div class="err mt">'+d.error+'</div>';return;}}
      el.innerHTML='<div class="mt flex"><div class="qrbox"><img src="'+d.qr+'"></div><div><h3>SCAN & BAYAR</h3><p>Nominal: <b>Rp '+d.amount.toLocaleString()+'</b></p><p class="text-xs">Status: <span class="tag tag-yellow" id="status">MENUNGGU</span></p><p class="text-xs mt">Saldo otomatis bertambah saat pembayaran terdeteksi.</p></div></div>';
      const pid=d.payment_id;
      const timer=setInterval(async()=>{{
        const rr=await fetch('/api/payment_status?id='+pid);
        const dd=await rr.json();
        if(dd.paid){{
          clearInterval(timer);
          document.getElementById('status').className='tag tag-green';
          document.getElementById('status').textContent='LUNAS';
          setTimeout(()=>location.reload(),1200);
        }}
      }},3000);
    }}
    </script>"""
    nav = [
        ('DASHBOARD', '/dashboard', True),
        ('PENARIKAN', '/withdraw', False),
        ('LOGOUT', '/logout', False)
    ]
    return page('Dashboard', body, nav)

@app.route('/withdraw', methods=['GET', 'POST'])
@login_required
def withdraw():
    db = get_db()
    u = db['users'][session['user']]
    err = None
    okmsg = None
    if request.method == 'POST':
        try:
            amount = int(request.form.get('amount', 0))
            wallet = request.form.get('wallet', '').strip()
            dest = request.form.get('dest', '').strip()
            if amount < MIN_WITHDRAW:
                err = f'Minimal penarikan Rp {MIN_WITHDRAW:,}'
            elif amount + ADMIN_FEE > u.get('balance', 0):
                err = f'Saldo tidak cukup. Butuh Rp {amount + ADMIN_FEE:,} (termasuk admin Rp {ADMIN_FEE:,})'
            elif not wallet or not dest:
                err = 'Wallet/bank & nomor tujuan wajib'
            else:
                def add(db):
                    usr = db['users'][session['user']]
                    usr['balance'] = usr.get('balance', 0) - (amount + ADMIN_FEE)
                    db['withdrawals'].append({
                        'id': str(uuid.uuid4())[:8],
                        'user': session['user'],
                        'amount': amount,
                        'admin_fee': ADMIN_FEE,
                        'total_deducted': amount + ADMIN_FEE,
                        'wallet': wallet,
                        'dest': dest,
                        'status': 'pending',
                        'created': datetime.now(TZ).isoformat()
                    })
                update_db(add)
                okmsg = f'Penarikan Rp {amount:,} diajukan. Menunggu approval owner.'
        except ValueError:
            err = 'Nominal tidak valid'
    db = get_db()
    u = db['users'][session['user']]
    my_wd = [w for w in db['withdrawals'] if w['user'] == session['user']]
    my_wd = list(reversed(my_wd[-20:]))
    rows = ''.join(
        f"<tr><td>{w['created'][:19]}</td><td>Rp {w['amount']:,}</td><td>Rp {w['admin_fee']:,}</td><td>{w['wallet']}</td><td>{w['dest']}</td><td><span class='tag tag-{'yellow' if w['status']=='pending' else ('green' if w['status']=='approved' else 'red')}'>{w['status'].upper()}</span></td></tr>"
        for w in my_wd
    ) or "<tr><td colspan='6'>Belum ada penarikan</td></tr>"
    body = f"""
    <div class="hero"><span class="tag">PENARIKAN</span><h1>TARIK<br>SALDO</h1><p>Saldo: Rp {int(u.get('balance',0)):,}</p></div>
    {f'<div class="err">{err}</div>' if err else ''}
    {f'<div class="ok">{okmsg}</div>' if okmsg else ''}
    <div class="brut">
    <h2>FORM PENARIKAN</h2>
    <p class="mb text-xs">Minimal Rp {MIN_WITHDRAW:,} • Admin Rp {ADMIN_FEE:,} per penarikan (dipotong dari saldo).</p>
    <form method="post">
    <div class="col mb"><label>Nominal (Rp)</label><input type="number" name="amount" min="{MIN_WITHDRAW}" required></div>
    <div class="col mb"><label>Wallet / Bank</label><input name="wallet" placeholder="GoPay / OVO / BCA / Dana..." required></div>
    <div class="col mb"><label>Nomor Tujuan</label><input name="dest" placeholder="0812xxxx / 1234xxxx" required></div>
    <button class="btn" type="submit">AJUKAN PENARIKAN →</button>
    </form>
    </div>
    <div class="brut">
    <h2>RIWAYAT PENARIKAN</h2>
    <table><thead><tr><th>Waktu</th><th>Nominal</th><th>Admin</th><th>Wallet</th><th>Tujuan</th><th>Status</th></tr></thead>
    <tbody>{rows}</tbody></table>
    </div>"""
    nav = [
        ('DASHBOARD', '/dashboard', False),
        ('PENARIKAN', '/withdraw', True),
        ('LOGOUT', '/logout', False)
    ]
    return page('Penarikan', body, nav)

# ==================== API: USER ====================
@app.route('/api/create_payment', methods=['POST'])
@login_required
def api_create_payment():
    try:
        d = request.get_json()
        amount = int(d.get('amount', 0))
        if amount <= 0:
            return jsonify(ok=False, error='Nominal tidak valid')
        pid = str(uuid.uuid4())[:8]
        # QR payload plain — banyak QRIS reader bisa parse teks nominal
        qr_text = f"QRIS|PAY|{amount}|{pid}|{session['user']}"
        qr_b64 = gen_qr_base64(qr_text)
        def add(db):
            u = db['users'][session['user']]
            u.setdefault('pending_payments', []).append({
                'id': pid,
                'amount': amount,
                'qr': qr_b64,
                'created': datetime.now(TZ).isoformat(),
                'active': True,
                'paid_at': None,
                'txid': None
            })
        update_db(add)
        return jsonify(ok=True, payment_id=pid, amount=amount, qr=qr_b64)
    except Exception as e:
        return jsonify(ok=False, error=str(e))

@app.route('/api/payment_status')
@login_required
def api_payment_status():
    pid = request.args.get('id')
    db = get_db()
    u = db['users'][session['user']]
    for p in u.get('pending_payments', []):
        if p['id'] == pid:
            return jsonify(paid=not p.get('active', True), amount=p['amount'])
    return jsonify(paid=False)

# ==================== ROUTES: OWNER ====================
@app.route('/owner/login', methods=['GET', 'POST'])
def owner_login():
    err = None
    if request.method == 'POST':
        u = request.form.get('username', '').strip()
        p = request.form.get('password', '').strip()
        if u == OWNER_USERNAME and p == OWNER_PASSWORD:
            session['is_owner'] = True
            return redirect(url_for('owner_dashboard'))
        err = 'Kredensial owner salah'
    body = f"""
    <div class="hero"><span class="tag">OWNER ACCESS</span><h1>LOGIN<br>OWNER</h1></div>
    {f'<div class="err">{err}</div>' if err else ''}
    <div class="brut">
    <form method="post">
    <div class="col mb"><label>Username</label><input name="username" required></div>
    <div class="col mb"><label>Password</label><input type="password" name="password" required></div>
    <button class="btn" type="submit">MASUK →</button>
    </form>
    </div>"""
    return page('Owner Login', body)

@app.route('/owner/logout')
def owner_logout():
    session.pop('is_owner', None)
    return redirect(url_for('owner_login'))

@app.route('/owner')
@owner_required
def owner_dashboard():
    db = get_db()
    g = db.get('gobiz', {})
    connected = bool(g.get('token'))
    total_users = len(db['users'])
    total_balance = sum(u.get('balance', 0) for u in db['users'].values())
    pending_wd = [w for w in db['withdrawals'] if w['status'] == 'pending']
    approved_wd = [w for w in db['withdrawals'] if w['status'] == 'approved']
    total_income = sum(w['admin_fee'] for w in approved_wd)
    conn_tag = '<span class="tag tag-green">TERHUBUNG</span>' if connected else '<span class="tag tag-red">BELUM LOGIN</span>'
    body = f"""
    <div class="hero"><span class="tag">OWNER PANEL</span><h1>DASHBOARD<br>OWNER</h1><p>Status GoBiz: {conn_tag}</p></div>
    <div class="grid mb">
    <div class="kpi"><div class="kpi-lbl">TOTAL USER</div><div class="kpi-val">{total_users}</div></div>
    <div class="kpi"><div class="kpi-lbl">TOTAL SALDO USER</div><div class="kpi-val">Rp {int(total_balance):,}</div></div>
    <div class="kpi"><div class="kpi-lbl">PENARIKAN PENDING</div><div class="kpi-val">{len(pending_wd)}</div></div>
    <div class="kpi"><div class="kpi-lbl">PENDAPATAN ADMIN</div><div class="kpi-val">Rp {int(total_income):,}</div></div>
    </div>
    <div class="brut">
    <h2>LANGKAH 1 — HUBUNGKAN GOBIZ</h2>
    <p class="mb text-xs">Masukkan nomor HP GoBiz → kirim OTP → verifikasi. Setelah terhubung, QR dinamis otomatis tersedia untuk user.</p>
    <form onsubmit="event.preventDefault();reqOtp()">
    <div class="row">
    <div class="col mb"><label>Nomor HP GoBiz (08xxx)</label><input id="phone" required></div>
    <div class="col mb" style="display:flex;align-items:flex-end"><button class="btn" type="submit">KIRIM OTP</button></div>
    </div>
    </form>
    <div id="otp1"></div>
    <div id="otp2"></div>
    </div>
    <script>
    async function reqOtp(){{
      const phone=document.getElementById('phone').value;
      const r=await fetch('/owner/api/request_otp',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{phone}})}});
      const d=await r.json();
      if(!d.ok){{document.getElementById('otp1').innerHTML='<div class="err mt">'+d.error+'</div>';return;}}
      document.getElementById('otp1').innerHTML='<div class="ok mt">OTP terkirim ke +62'+d.phone+'. Cek SMS.</div>';
      document.getElementById('otp2').innerHTML='<div class="mt"><label>Kode OTP</label><input id="otpcode"><button class="btn mt" onclick="verifyOtp()">VERIFIKASI OTP</button></div>';
    }}
    async function verifyOtp(){{
      const code=document.getElementById('otpcode').value;
      const r=await fetch('/owner/api/verify_otp',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{otp:code}})}});
      const d=await r.json();
      if(!d.ok){{alert(d.error);return;}}
      alert('Berhasil terhubung!');
      location.reload();
    }}
    </script>
    <div class="brut">
    <h2>LANGKAH 2 — PENARIKAN USER</h2>
    <table><thead><tr><th>ID</th><th>User</th><th>Nominal</th><th>Admin</th><th>Wallet</th><th>Tujuan</th><th>Aksi</th></tr></thead>
    <tbody id="wdtable"></tbody></table>
    </div>
    <script>
    async function loadWd(){{
      const r=await fetch('/owner/api/withdrawals');
      const d=await r.json();
      const t=document.getElementById('wdtable');
      if(!d.items.length){{t.innerHTML='<tr><td colspan="7">Tidak ada penarikan</td></tr>';return;}}
      t.innerHTML=d.items.map(w=>`<tr>
        <td class="mono">${{w.id}}</td><td>${{w.user}}</td><td>Rp ${{w.amount.toLocaleString()}}</td>
        <td>Rp ${{w.admin_fee.toLocaleString()}}</td><td>${{w.wallet}}</td><td>${{w.dest}}</td>
        <td>${{w.status==='pending'?`<button class="btn btn-green btn-sm" onclick="act('${{w.id}}','approve')">✓ BERHASIL</button> <button class="btn btn-red btn-sm" onclick="act('${{w.id}}','reject')">✗ TOLAK</button>`:`<span class="tag tag-${{w.status==='approved'?'green':'red'}}">${{w.status.toUpperCase()}}</span>`}}</td>
      </tr>`).join('');
    }}
    async function act(id,action){{
      const r=await fetch('/owner/api/withdrawal_action',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{id,action}})}});
      const d=await r.json();
      if(!d.ok){{alert(d.error);return;}}
      loadWd();
    }}
    loadWd();
    setInterval(loadWd,5000);
    </script>"""
    nav = [
        ('DASHBOARD', '/owner', True),
        ('LOGOUT OWNER', '/owner/logout', False)
    ]
    return page('Owner Dashboard', body, nav)

# ==================== API: OWNER ====================
@app.route('/owner/api/request_otp', methods=['POST'])
@owner_required
def owner_api_request_otp():
    try:
        d = request.get_json()
        phone = d.get('phone', '').strip()
        if not phone:
            return jsonify(ok=False, error='Nomor wajib')
        res = gobiz.request_otp(phone)
        def store(db):
            db['owner_pending_otp'] = {
                'phone': res['phone'],
                'otp_token': res.get('otp_token'),
                'created': datetime.now(TZ).isoformat()
            }
        update_db(store)
        return jsonify(ok=True, phone=res['phone'])
    except Exception as e:
        return jsonify(ok=False, error=str(e))

@app.route('/owner/api/verify_otp', methods=['POST'])
@owner_required
def owner_api_verify_otp():
    try:
        d = request.get_json()
        otp = d.get('otp', '').strip()
        db = get_db()
        po = db.get('owner_pending_otp', {})
        if not po.get('phone'):
            return jsonify(ok=False, error='Belum request OTP')
        gobiz.verify_otp(po['phone'], otp, po.get('otp_token'))
        # deteksi merchant
        try:
            gobiz.ensure_merchant()
        except Exception as e:
            print('[owner] merchant detect:', e)
        def clear(db):
            db['owner_pending_otp'] = {}
        update_db(clear)
        return jsonify(ok=True, merchant=gobiz.merchant_id)
    except Exception as e:
        return jsonify(ok=False, error=str(e))

@app.route('/owner/api/withdrawals')
@owner_required
def owner_api_withdrawals():
    db = get_db()
    items = list(reversed(db['withdrawals'][-100:]))
    return jsonify(items=items)

@app.route('/owner/api/withdrawal_action', methods=['POST'])
@owner_required
def owner_api_withdrawal_action():
    try:
        d = request.get_json()
        wid = d.get('id')
        action = d.get('action')
        if action not in ('approve', 'reject'):
            return jsonify(ok=False, error='Aksi tidak valid')
        def do(db):
            for w in db['withdrawals']:
                if w['id'] == wid:
                    if w['status'] != 'pending':
                        return False, 'Sudah diproses'
                    if action == 'approve':
                        w['status'] = 'approved'
                        w['approved_at'] = datetime.now(TZ).isoformat()
                    else:
                        w['status'] = 'rejected'
                        w['rejected_at'] = datetime.now(TZ).isoformat()
                        # refund
                        u = db['users'].get(w['user'])
                        if u:
                            u['balance'] = u.get('balance', 0) + w['total_deducted']
                    return True, None
            return False, 'Tidak ditemukan'
        ok, err = update_db(do)
        if not ok:
            return jsonify(ok=False, error=err)
        return jsonify(ok=True)
    except Exception as e:
        return jsonify(ok=False, error=str(e))

# ==================== START ====================
if __name__ == '__main__':
    start_watcher()
    print('=' * 60)
    print('  BRUTALIST QRIS SYSTEM')
    print('=' * 60)
    print(f'  URL      : http://0.0.0.0:{PORT}')
    print(f'  User     : /register')
    print(f'  Owner    : /owner/login  ({OWNER_USERNAME})')
    print(f'  Admin fee: Rp {ADMIN_FEE}')
    print(f'  Min WD   : Rp {MIN_WITHDRAW}')
    print('=' * 60)
    app.run(host='0.0.0.0', port=PORT, debug=False, threaded=True)