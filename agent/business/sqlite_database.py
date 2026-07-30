"""Local SQLite backend for development when MySQL is unavailable."""
import hashlib, json, os, sqlite3
from contextlib import contextmanager
from agent.business.config import load_business_config
from agent.models.schemas import Product, RfqRequest, RfqFieldExtraction, Quote, QuoteVersion, QuoteItem, FollowupTask
_conn = None
def get_connection():
    global _conn
    if _conn is None:
        path = load_business_config().database_path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        _conn = sqlite3.connect(path, check_same_thread=False); _conn.row_factory = sqlite3.Row
        _conn.execute('PRAGMA journal_mode=WAL'); _conn.execute('PRAGMA foreign_keys=ON')
    return _conn
@contextmanager
def tx():
    c=get_connection().cursor()
    try: yield c; get_connection().commit()
    except Exception: get_connection().rollback(); raise
    finally: c.close()
def close_all():
    global _conn
    if _conn: _conn.close(); _conn=None
def init_db():
    connection = get_connection()
    connection.executescript('''CREATE TABLE IF NOT EXISTS products(id INTEGER PRIMARY KEY AUTOINCREMENT,sku TEXT UNIQUE,name_cn TEXT,name_en TEXT,category TEXT,specification TEXT,unit TEXT,moq INTEGER,price_usd REAL,inventory INTEGER,lead_time_days INTEGER,active INTEGER DEFAULT 1); CREATE TABLE IF NOT EXISTS rfq_requests(id INTEGER PRIMARY KEY AUTOINCREMENT,session_key TEXT,raw_text TEXT,extracted_json TEXT DEFAULT '{}',status TEXT DEFAULT 'pending',created_at TEXT,updated_at TEXT); CREATE TABLE IF NOT EXISTS quotes(id INTEGER PRIMARY KEY AUTOINCREMENT,rfq_id INTEGER,status TEXT DEFAULT 'draft',current_version INTEGER DEFAULT 0,version_data TEXT DEFAULT '[]',created_at TEXT); CREATE TABLE IF NOT EXISTS followup_tasks(id INTEGER PRIMARY KEY AUTOINCREMENT,quote_id INTEGER,task_type TEXT,title TEXT,description TEXT,due_at TEXT,status TEXT DEFAULT 'pending'); CREATE TABLE IF NOT EXISTS approval_records(id INTEGER PRIMARY KEY AUTOINCREMENT,quote_id INTEGER,version INTEGER,status TEXT DEFAULT 'pending',reviewer TEXT,comment TEXT,content_hash TEXT,created_at TEXT,decided_at TEXT); CREATE TABLE IF NOT EXISTS exchange_rates(id INTEGER PRIMARY KEY AUTOINCREMENT,from_currency TEXT,to_currency TEXT,rate REAL,UNIQUE(from_currency,to_currency));''')
    columns = {row['name'] for row in connection.execute('PRAGMA table_info(approval_records)')}
    for name in ('content_hash', 'created_at', 'decided_at'):
        if name not in columns:
            connection.execute(f'ALTER TABLE approval_records ADD COLUMN {name} TEXT')
    connection.commit()


def _quote_version_payload(cursor, quote_id, version):
    cursor.execute('SELECT current_version,version_data FROM quotes WHERE id=?', (quote_id,))
    row = cursor.fetchone()
    if not row or int(row['current_version']) != int(version):
        return None
    versions = json.loads(row['version_data'] or '[]')
    return next((item for item in versions if int(item.get('version', 0)) == int(version)), None)


def _quote_content_hash(payload):
    value = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(value.encode('utf-8')).hexdigest()
def _p(r): return Product(id=r['id'],sku=r['sku'],name_cn=r['name_cn'] or '',name_en=r['name_en'] or '',category=r['category'] or '',specification=r['specification'] or '',unit=r['unit'] or 'pcs',moq=r['moq'] or 1,price_usd=r['price_usd'] or 0,inventory=r['inventory'] or 0,lead_time_days=r['lead_time_days'] or 15,active=bool(r['active']))
def list_products(category='',keyword='',limit=20,offset=0):
    w=['active=1'];p=[]
    if category:w+=['category=?'];p+=[category]
    if keyword:w+=['(name_en LIKE ? OR name_cn LIKE ? OR sku LIKE ? OR specification LIKE ?)'];p += [f'%{keyword}%']*4
    with tx() as c:c.execute('SELECT * FROM products WHERE '+' AND '.join(w)+' ORDER BY sku LIMIT ? OFFSET ?',p+[limit,offset]);return [_p(r) for r in c.fetchall()]
def get_product_by_sku(sku):
    with tx() as c:c.execute('SELECT * FROM products WHERE sku=?',(sku,));r=c.fetchone();return _p(r) if r else None
def get_product_by_id(pid):
    with tx() as c:c.execute('SELECT * FROM products WHERE id=?',(pid,));r=c.fetchone();return _p(r) if r else None
def check_inventory(sku,quantity):
    p=get_product_by_sku(sku)
    if not p:return {'available':False,'reason':f"SKU '{sku}' 不存在",'inventory':0,'moq':0}
    ok=quantity>=p.moq and quantity<=p.inventory
    return {'available':ok,'reason':'库存充足' if ok else '库存不足或低于 MOQ','inventory':p.inventory,'moq':p.moq}
def create_rfq(session_key,raw_text):
    with tx() as c:c.execute("INSERT INTO rfq_requests(session_key,raw_text,created_at,updated_at) VALUES(?,?,datetime('now'),datetime('now'))",(session_key,raw_text));return c.lastrowid
def update_rfq_extraction(rfq_id,e):
    with tx() as c:c.execute("UPDATE rfq_requests SET extracted_json=?,status='extracted',updated_at=datetime('now') WHERE id=?",(json.dumps(e.__dict__,ensure_ascii=False),rfq_id))
def get_rfq(rfq_id):
    with tx() as c:c.execute('SELECT * FROM rfq_requests WHERE id=?',(rfq_id,));r=c.fetchone()
    if not r:return None
    try:e=RfqFieldExtraction(**json.loads(r['extracted_json'] or '{}'))
    except Exception:e=RfqFieldExtraction()
    return RfqRequest(id=r['id'],session_key=r['session_key'],raw_text=r['raw_text'],extracted=e,status=r['status'],created_at=r['created_at'],updated_at=r['updated_at'])
def create_quote(rfq_id,*args):
    with tx() as c:c.execute("INSERT INTO quotes(rfq_id,created_at) VALUES(?,datetime('now'))",(rfq_id,));return c.lastrowid
def add_quote_version(qid,v):
    with tx() as c:
        c.execute('SELECT current_version,version_data FROM quotes WHERE id=?',(qid,));r=c.fetchone();n=(r['current_version'] or 0)+1;v.version=n;d=json.loads(r['version_data'] or '[]');d.append({**v.__dict__,'items':[i.__dict__ for i in v.items]});c.execute("UPDATE quotes SET current_version=?,version_data=? WHERE id=?",(n,json.dumps(d,ensure_ascii=False),qid));return n
def update_quote_status(qid,status):
    with tx() as c:c.execute('UPDATE quotes SET status=? WHERE id=?',(status,qid))
def get_quote(qid):
    with tx() as c:c.execute('SELECT * FROM quotes WHERE id=?',(qid,));r=c.fetchone()
    if not r:return None
    vs=[QuoteVersion(version=x.get('version',1),items=[QuoteItem(**i) for i in x.get('items',[])],**{k:v for k,v in x.items() if k not in ('version','items')}) for x in json.loads(r['version_data'] or '[]')]
    return Quote(id=r['id'],rfq_id=r['rfq_id'],status=r['status'],current_version=r['current_version'],versions=vs,created_at=r['created_at'])
def list_quotes_by_session(key,limit=10):
    with tx() as c:c.execute('SELECT q.id FROM quotes q JOIN rfq_requests r ON r.id=q.rfq_id WHERE r.session_key=? LIMIT ?',(key,limit));ids=[r['id'] for r in c.fetchall()]
    return [get_quote(i) for i in ids]
def create_followup(rfq_id,qid,task_type,title,description,due_at):
    with tx() as c:c.execute('INSERT INTO followup_tasks(quote_id,task_type,title,description,due_at) VALUES(?,?,?,?,?)',(qid,task_type,title,description,due_at));return c.lastrowid
def list_pending_followups(limit=20):
    with tx() as c:c.execute("SELECT * FROM followup_tasks WHERE status='pending' LIMIT ?",(limit,));return [FollowupTask(id=r['id'],quote_id=r['quote_id'],task_type=r['task_type'],title=r['title'],description=r['description'],due_at=r['due_at'],status=r['status']) for r in c.fetchall()]
def create_approval(qid,version):
    with tx() as c:
        payload = _quote_version_payload(c, qid, version)
        if payload is None:
            raise ValueError('approval_quote_version_stale')
        c.execute("INSERT INTO approval_records(quote_id,version,content_hash,created_at) VALUES(?,?,?,datetime('now'))",(qid,version,_quote_content_hash(payload)))
        return c.lastrowid
def approve(aid,reviewer,comment='',approved=True):
    with tx() as c:
        c.execute('SELECT * FROM approval_records WHERE id=?',(aid,)); row=c.fetchone()
        if not row:
            raise ValueError('approval_not_found')
        payload = _quote_version_payload(c, row['quote_id'], row['version'])
        if approved and (payload is None or row['content_hash'] != _quote_content_hash(payload)):
            raise ValueError('approval_quote_version_stale')
        status = 'approved' if approved else 'rejected'
        c.execute("UPDATE approval_records SET status=?,reviewer=?,comment=?,decided_at=datetime('now') WHERE id=?",(status,reviewer,comment,aid))
        c.execute('UPDATE quotes SET status=? WHERE id=?',(status,row['quote_id']))
def upsert_exchange_rate(a,b,rate):
    with tx() as c:c.execute('INSERT OR REPLACE INTO exchange_rates(from_currency,to_currency,rate) VALUES(?,?,?)',(a.upper(),b.upper(),rate))
def get_exchange_rate(a,b):
    if a.upper()==b.upper():return 1.0
    with tx() as c:c.execute('SELECT rate FROM exchange_rates WHERE from_currency=? AND to_currency=?',(a.upper(),b.upper()));r=c.fetchone();return r['rate'] if r else None
