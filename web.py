#!/usr/bin/env python3
"""Local-only HTTP adapter for the isolated v2 registry."""
import argparse, base64, getpass, hashlib, hmac, json, mimetypes, os, secrets, sqlite3, sys, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs
import trading as t
import web_core as core

STATIC=Path(__file__).resolve().parent/'web_static'
SESSIONS={}; ATTEMPTS=[]

def password_hash(password,salt): return hashlib.scrypt(password.encode(),salt=salt,n=2**14,r=8,p=1).hex()
def setup(root):
    path=root/'local-auth.json'
    password=getpass.getpass('Новый тестовый пароль (не менее 10 символов): ')
    t.require(len(password)>=10,'Пароль слишком короткий'); t.require(password==getpass.getpass('Повторите пароль: '),'Пароли не совпадают')
    salt=secrets.token_bytes(16); data={'salt':salt.hex(),'hash':password_hash(password,salt)}
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
    with os.fdopen(fd,'w') as f: json.dump(data,f)
    os.chmod(path,0o600)

class Handler(BaseHTTPRequestHandler):
    server_version='LocalRegistry'; sys_version=''
    def log_message(self,*args): pass
    def reply(self,status,obj,headers=None):
        body=json.dumps(obj,ensure_ascii=False).encode(); self.send_response(status); self.send_header('Content-Type','application/json; charset=utf-8'); self.send_header('Content-Length',str(len(body))); self.send_header('Cache-Control','no-store'); self.send_header('X-Content-Type-Options','nosniff')
        for k,v in (headers or {}).items(): self.send_header(k,v)
        self.end_headers(); self.wfile.write(body)
    def host_ok(self): return self.headers.get('Host')==f'127.0.0.1:{self.server.server_port}'
    def origin_ok(self): return self.headers.get('Origin')==f'http://127.0.0.1:{self.server.server_port}'
    def session(self):
        from http.cookies import SimpleCookie
        c=SimpleCookie()
        try: c.load(self.headers.get('Cookie','')); token=c['session'].value if 'session' in c else ''
        except Exception: return None
        s=SESSIONS.get(token)
        if s and s['expires']>time.time(): return s
        SESSIONS.pop(token,None); return None
    def body(self):
        t.require(self.headers.get('Content-Type','').split(';')[0]=='application/json','Требуется JSON'); n=int(self.headers.get('Content-Length','0')); t.require(0<n<=1000000,'Неверный размер запроса'); p=json.loads(self.rfile.read(n)); t.require(isinstance(p,dict),'Требуется объект'); return p
    def do_GET(self):
        if not self.host_ok(): return self.reply(403,{'error':'Недопустимый адрес'})
        parsed=urlsplit(self.path); path=parsed.path; q=parse_qs(parsed.query)
        if path in ('/','/index.html','/style.css','/app.js'):
            f=STATIC/('index.html' if path=='/' else path[1:])
            if not f.is_file(): return self.send_error(404)
            data=f.read_bytes(); self.send_response(200); self.send_header('Content-Type',mimetypes.guess_type(f.name)[0] or 'application/octet-stream'); self.send_header('Content-Length',str(len(data))); self.send_header('Content-Security-Policy',"default-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'"); self.send_header('X-Content-Type-Options','nosniff'); self.end_headers(); self.wfile.write(data); return
        session=self.session()
        if path=='/api/session': return self.reply(200,{'authenticated':bool(session),'csrf':session['csrf'] if session else None})
        if not session: return self.reply(401,{'error':'Требуется вход'})
        try:
            db=core.connect(self.server.root)
            try:
                db.execute('BEGIN IMMEDIATE')
                if path=='/api/chats':
                    date=q.get('date',[None])[0]; t.valid_date(date); chats=[]
                    for c in db.execute('SELECT * FROM chats ORDER BY name').fetchall():
                        items=core.queue(db,self.server.root,c['id'],date,create=bool(c['recipient']))
                        counts={s:sum(x['status']==s for x in items) for s in ('pending','review','sent','excluded','withdrawn')}
                        chats.append({'id':c['id'],'name':c['name'],'platform':t.chat_platform(db,c['id']),'recipient':bool(c['recipient']),'counts':counts})
                    dates=[r[0] for r in db.execute('SELECT DISTINCT date FROM tasks WHERE date<? ORDER BY date DESC',(date,))]; past=[]
                    for d in dates:
                        count=sum(sum(x['status'] in ('pending','review') for x in core.queue(db,self.server.root,c['id'],d,False)) for c in db.execute('SELECT id FROM chats').fetchall())
                        if count: past.append({'date':d,'count':count})
                    result={'chats':chats,'past_dates':past}
                elif path=='/api/tasks':
                    date=q.get('date',[None])[0]; chat=int(q.get('chat_id',['0'])[0]); result={'tasks':core.queue(db,self.server.root,chat,date),'coverage':'Проверена только загруженная история; её полнота не предполагается.'}
                elif path.startswith('/api/blocks/'):
                    result=core.block(db,self.server.root,int(path.split('/')[3]))
                elif path=='/api/search':
                    filters={key:q[key][0] for key in ('date','q','category','max_duration','min_rate','max_rate','rate_type','rate_basis','color') if key in q and q[key][0]!=''}
                    found=core.search(db,self.server.root,filters); result={'results':found['items'],'coverage_warning':found['coverage_warning']}
                elif path.startswith('/api/messages/'):
                    m=t.row(db,'messages',int(path.split('/')[3])); f=t.safe(self.server.root,m['path']); t.require(f.is_file() and t.sha(f.read_bytes())==m['sha256'] and t.sha(m['text'].encode())==m['sha256'],'Исходник отсутствует или изменён'); result={'id':m['id'],'text':m['text'],'date':m['date'],'time':m['time'],'author':m['author'],'chat':t.row(db,'chats',m['chat_id'])['name'],'platform':t.chat_platform(db,m['chat_id'])}
                elif path.startswith('/api/documents/'):
                    d=t.row(db,'documents',int(path.split('/')[3])); doc=next(x for x in core.documents(db,self.server.root,d['company_id']) if x['id']==d['id']); t.require(doc['ready'],'Документ не подтверждён, отсутствует или изменён'); data=t.safe(self.server.root,d['path']).read_bytes(); t.require(t.sha(data)==d['sha256'],'Документ изменён'); db.commit(); self.send_response(200); self.send_header('Content-Type','application/octet-stream'); self.send_header('Content-Disposition',"attachment; filename*=UTF-8''"+__import__('urllib.parse',fromlist=['quote']).quote(d['name'],safe='')); self.send_header('Content-Length',str(len(data))); self.send_header('X-Content-Type-Options','nosniff'); self.end_headers(); self.wfile.write(data); return
                elif path=='/api/review': result=t.execute(db,self.server.root,'review',{})
                elif path=='/api/history':
                    sends=[]
                    for send in db.execute('SELECT * FROM sends ORDER BY id DESC').fetchall():
                        item=dict(send); item['corrections']=[dict(x) for x in db.execute('SELECT * FROM corrections WHERE send_id=? ORDER BY id',(send['id'],))]; sends.append(item)
                    result={'history':[dict(x) for x in db.execute('SELECT * FROM history ORDER BY id DESC')], 'sends':sends} 
                else: db.rollback(); return self.reply(404,{'error':'Не найдено'})
                db.commit(); return self.reply(200,result)
            finally: db.close()
        except (ValueError,TypeError,KeyError,sqlite3.Error,OSError) as e: return self.reply(409,{'error':str(e)})
    def do_POST(self):
        if not self.host_ok() or not self.origin_ok(): return self.reply(403,{'error':'Недопустимый источник запроса'})
        path=urlsplit(self.path).path
        try:
            p=self.body()
            if path=='/api/login':
                now=time.time(); ATTEMPTS[:]=[x for x in ATTEMPTS if x>now-60]
                if len(ATTEMPTS)>=5: return self.reply(429,{'error':'Слишком много попыток; повторите позже'})
                ATTEMPTS.append(now); auth=json.loads((self.server.root/'local-auth.json').read_text()); password=p.get('password'); t.require(isinstance(password,str),'Неверный пароль')
                if not hmac.compare_digest(password_hash(password,bytes.fromhex(auth['salt'])),auth['hash']): return self.reply(401,{'error':'Неверный пароль'})
                ATTEMPTS.clear(); token=secrets.token_urlsafe(32); csrf=secrets.token_urlsafe(32); SESSIONS[token]={'csrf':csrf,'expires':now+8*3600}; return self.reply(200,{'authenticated':True,'csrf':csrf},{'Set-Cookie':f'session={token}; HttpOnly; SameSite=Strict; Path=/'})
            session=self.session()
            if not session: return self.reply(401,{'error':'Требуется вход'})
            if not hmac.compare_digest(self.headers.get('X-CSRF-Token',''),session['csrf']): return self.reply(403,{'error':'Ошибка защиты запроса'})
            if path=='/api/logout':
                from http.cookies import SimpleCookie
                c=SimpleCookie(); c.load(self.headers.get('Cookie','')); SESSIONS.pop(c['session'].value,None); return self.reply(200,{'authenticated':False},{'Set-Cookie':'session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0'})
            db=core.connect(self.server.root)
            try:
                db.execute('BEGIN IMMEDIATE')
                if path=='/api/blocks': result=core.prepare(db,self.server.root,p)
                elif path.startswith('/api/blocks/') and path.endswith('/send'): result=core.confirm(db,self.server.root,int(path.split('/')[3]),p)
                elif path=='/api/chat': result=t.execute(db,self.server.root,'chat',p)
                elif path=='/api/chat-platform': result=t.execute(db,self.server.root,'chat-platform',p)
                elif path=='/api/recipient': result=t.execute(db,self.server.root,'recipient',p)
                elif path=='/api/correction': result=t.execute(db,self.server.root,'correction',p)
                else: db.rollback(); return self.reply(404,{'error':'Не найдено'})
                db.commit(); return self.reply(200,result)
            except Exception: db.rollback(); raise
            finally: db.close()
        except (ValueError,TypeError,KeyError,sqlite3.Error,OSError,json.JSONDecodeError) as e:
            result={'error':str(e)}
            if path.startswith('/api/blocks/') and path.endswith('/send') and self.session():
                try:
                    check=core.connect(self.server.root)
                    try:
                        bid=int(path.split('/')[3]); b=t.row(check,'blocks',bid)
                        result['current']={'block':core.block(check,self.server.root,bid),'tasks':core.queue(check,self.server.root,b['chat_id'],b['date'],False)}
                    finally: check.close()
                except (ValueError,TypeError,KeyError,sqlite3.Error,OSError): pass
            return self.reply(409,result)

def main():
    parser=argparse.ArgumentParser(); parser.add_argument('--root',required=True); parser.add_argument('--port',type=int,default=8765); parser.add_argument('--setup-password',action='store_true'); parser.add_argument('--migrate-copy',metavar='V1_DATABASE'); args=parser.parse_args()
    root=Path(args.root).expanduser().resolve()
    if args.migrate_copy: core.migrate_copy(args.migrate_copy,root); print('Создана проверенная копия экземпляра схемы v2.'); return
    t.require(root.is_dir(),'Папка экземпляра должна существовать')
    if args.setup_password: setup(root); print('Тестовый пароль установлен.'); return
    t.require((root/'local-auth.json').is_file(),'Сначала установите пароль через --setup-password'); db=core.connect(root); db.close()
    server=ThreadingHTTPServer(('127.0.0.1',args.port),Handler); server.root=root; print(f'Локальный тест: http://127.0.0.1:{server.server_port}/',flush=True)
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()
if __name__=='__main__': main()
