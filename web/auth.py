import hashlib, hmac, json, os, secrets, threading, time
from http.cookies import SimpleCookie
from pathlib import Path

PATH = Path(__file__).resolve().parent.parent / '.cache' / 'auth.json'
LOCK = threading.RLock()
SESSIONS = {}
ATTEMPTS = []
ITERATIONS = 600000
from web import account_db
DB_PATH = Path(os.environ.get('ARCHIVE_DB') or PATH.parent / 'accounts.sqlite3')
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
account_db.initialize(DB_PATH)
# A legacy account is imported once, without deleting or changing its source.
if not account_db.account(DB_PATH) and PATH.exists():
    account_db.import_account(DB_PATH, PATH, ITERATIONS)

def configured():
    return bool(account_db.account(DB_PATH))

def authorized(handler):
    try:
        cookie = SimpleCookie(handler.headers.get('Cookie', ''))
        token = cookie['archive_session'].value
    except (KeyError, ValueError):
        return False
    with LOCK:
        expiry = SESSIONS.get(token, 0)
        if expiry <= time.time():
            SESSIONS.pop(token, None)
            return False
        return True

def same_origin(handler):
    origin = handler.headers.get('Origin')
    return not origin or origin == 'http://' + handler.headers.get('Host', '')

def reply(handler, data, status=200, cookie=None):
    body = json.dumps(data, ensure_ascii=False).encode('utf-8')
    handler.send_response(status)
    handler.send_header('Content-Type', 'application/json; charset=utf-8')
    handler.send_header('Cache-Control', 'no-store')
    if cookie is not None:
        handler.send_header('Set-Cookie', 'archive_session=' + cookie + '; Path=/; HttpOnly; SameSite=Strict' + ('; Max-Age=0' if not cookie else '; Max-Age=43200'))
    handler.send_header('Content-Length', str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)

def post(handler, path):
    if not same_origin(handler):
        return reply(handler, {'error':'请求来源无效'}, 403)
    if path == '/api/auth/logout':
        try:
            cookie = SimpleCookie(handler.headers.get('Cookie', ''))
            with LOCK: SESSIONS.pop(cookie['archive_session'].value, None)
        except (KeyError, ValueError): pass
        return reply(handler, {'ok': True}, cookie='')
    try:
        n = int(handler.headers.get('Content-Length', '0'))
        if n < 1 or n > 4096: raise ValueError()
        data = json.loads(handler.rfile.read(n))
        username, password = data['username'], data['password']
        if not isinstance(username, str) or not isinstance(password, str): raise ValueError()
    except (ValueError, KeyError, TypeError):
        return reply(handler, {'error':'请求格式错误'}, 400)
    with LOCK:
        now = time.time()
        ATTEMPTS[:] = [t for t in ATTEMPTS if t > now - 300]
        if len(ATTEMPTS) >= 10:
            return reply(handler, {'error':'尝试次数过多，请五分钟后重试'}, 429)
        ATTEMPTS.append(now)
        if path == '/api/auth/setup':
            if configured(): return reply(handler, {'error':'账号已设置'}, 403)
            if not 1 <= len(username.strip()) <= 80 or not 8 <= len(password) <= 256:
                return reply(handler, {'error':'请输入账号，密码须为 8 至 256 个字符'}, 400)
            salt = secrets.token_bytes(16)
            record = {'username': username.strip(), 'salt': salt.hex(), 'hash': hashlib.pbkdf2_hmac('sha256', password.encode(), salt, ITERATIONS).hex()}
            try: account_db.create_account(DB_PATH, record, ITERATIONS)
            except ValueError: return reply(handler, {'error':'账号已设置'}, 403)
        elif path == '/api/auth/login':
            if not configured(): return reply(handler, {'error':'请先设置管理员账号'}, 400)
            try:
                record = account_db.account(DB_PATH)
                digest = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(record['salt']), record.get('iterations', ITERATIONS)).hex()
                valid = hmac.compare_digest(username.encode('utf-8'), record['username'].encode('utf-8')) and hmac.compare_digest(digest, record['hash'])
            except (ValueError, KeyError, OSError): valid = False
            if not valid: return reply(handler, {'error':'账号或密码错误'}, 401)
        else: return reply(handler, {'error':'接口不存在'}, 404)
        ATTEMPTS.clear()
        token = secrets.token_urlsafe(32)
        SESSIONS[token] = now + 43200
        return reply(handler, {'ok': True}, cookie=token)
