"""Shared operations for the isolated local web prototype (schema v2)."""
import json, sqlite3, hashlib, datetime as dt, re, os, secrets, ipaddress
from urllib.parse import urlsplit
from decimal import Decimal, InvalidOperation
from pathlib import Path
import trading as t

V2 = '''
ALTER TABLE sends ADD COLUMN actual_time TEXT;
ALTER TABLE chats ADD COLUMN recipient INTEGER NOT NULL DEFAULT 0 CHECK(recipient IN (0,1));
CREATE TABLE tasks(id INTEGER PRIMARY KEY, chat_id INTEGER NOT NULL REFERENCES chats(id), term_id INTEGER NOT NULL REFERENCES terms(id), date TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(chat_id,term_id,date));
CREATE TABLE company_vat(id INTEGER PRIMARY KEY, company_id INTEGER NOT NULL REFERENCES companies(id), value TEXT NOT NULL, source TEXT NOT NULL, date TEXT NOT NULL, status TEXT NOT NULL, recorded_at TEXT NOT NULL);
CREATE TABLE pricing(id INTEGER PRIMARY KEY, term_id INTEGER NOT NULL REFERENCES terms(id), markup TEXT NOT NULL, reason TEXT NOT NULL, recorded_at TEXT NOT NULL);
CREATE TABLE operations(key TEXT PRIMARY KEY, payload_hash TEXT NOT NULL, result TEXT NOT NULL);
CREATE TABLE corrections(id INTEGER PRIMARY KEY, send_id INTEGER NOT NULL REFERENCES sends(id), reason TEXT NOT NULL, cancels INTEGER NOT NULL CHECK(cancels IN (0,1)), recorded_at TEXT NOT NULL);
CREATE TABLE web_blocks(block_id INTEGER PRIMARY KEY REFERENCES blocks(id), version TEXT NOT NULL);
PRAGMA user_version=2;
'''

V3 = '''
CREATE TABLE invoices(id INTEGER PRIMARY KEY, company_id INTEGER NOT NULL REFERENCES companies(id), message_id INTEGER NOT NULL REFERENCES messages(id), data TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE invoice_offers(offer_id INTEGER PRIMARY KEY REFERENCES offers(id), invoice_id INTEGER NOT NULL REFERENCES invoices(id), reason TEXT NOT NULL);
CREATE TABLE document_classification(document_id INTEGER PRIMARY KEY REFERENCES documents(id), classification TEXT NOT NULL CHECK(classification IN ('company','invoice')), invoice_id INTEGER REFERENCES invoices(id), CHECK((classification='invoice')=(invoice_id IS NOT NULL)));
PRAGMA user_version=3;
'''

def connect(root):
    db=sqlite3.connect(Path(root)/'registry.sqlite',timeout=5, isolation_level=None)
    db.row_factory=sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON'); db.execute('PRAGMA busy_timeout=5000')
    t.require(db.execute('PRAGMA user_version').fetchone()[0] in (2,3),'Требуется схема v2 или v3')
    t.require('actual_time' in {r[1] for r in db.execute('PRAGMA table_info(sends)')},'Несовместимый ранний вариант схемы v2; создайте новый экземпляр или повторите миграцию с v1 в новый каталог')
    if db.execute('PRAGMA user_version').fetchone()[0]==3: t.require(all(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(name,)).fetchone() for name in ('invoices','invoice_offers','document_classification')),'Несовместимая схема v3')
    return db

def migrate_copy(source, destination):
    """Copy a complete v1 instance to a new directory and migrate only that copy."""
    import shutil
    source=Path(source).resolve(); destination=Path(destination).resolve()
    t.require(source.is_dir() and (source/'registry.sqlite').is_file() and not destination.exists() and not destination.is_relative_to(source),'Нужны исходный экземпляр и новый отдельный каталог')
    src=sqlite3.connect(f"file:{source/'registry.sqlite'}?mode=ro",uri=True); src.row_factory=sqlite3.Row
    created=False
    try:
        src.execute('BEGIN')
        version=src.execute('PRAGMA user_version').fetchone()[0]; t.require(version in (1,2),'Поддерживается только исходная схема v1 или v2')
        t.require(src.execute('PRAGMA integrity_check').fetchone()[0]=='ok' and not src.execute('PRAGMA foreign_key_check').fetchone(),'Исходная база повреждена')
        files=t.registered_files(src)
        for path,digest in files:
            f=t.safe(source,path); t.require(f.is_file() and t.sha(f.read_bytes())==digest,'Исходный файл отсутствует или изменён')
        destination.mkdir(parents=True); created=True
        dst=sqlite3.connect(destination/'registry.sqlite')
        try:
            src.backup(dst)
            for path,digest in files:
                f=t.safe(destination,path); f.parent.mkdir(parents=True,exist_ok=True); shutil.copyfile(t.safe(source,path),f)
                t.require(t.sha(f.read_bytes())==digest,'Ошибка копирования файла')
            dst.execute('PRAGMA foreign_keys=ON'); dst.executescript('BEGIN IMMEDIATE;\n'+(V2 if version==1 else V3)+'\nCOMMIT;')
            t.require(dst.execute('PRAGMA integrity_check').fetchone()[0]=='ok' and not dst.execute('PRAGMA foreign_key_check').fetchone(),'Ошибка проверки миграции')
        finally: dst.close()
    except Exception:
        if created: shutil.rmtree(destination)
        raise
    finally: src.close()

def vat(db,cid):
    values=[dict(x) for x in db.execute('SELECT value,source,date,status FROM company_vat WHERE company_id=? ORDER BY id',(cid,))]
    unique={x['value'] for x in values}
    conflict=len(unique)>1 or any(x['status']=='conflict' for x in values)
    return {'value':next(iter(unique)) if len(unique)==1 and not conflict else None,'records':values,'conflict':conflict}

def price_info(db, term_id, term):
    price=db.execute('SELECT * FROM pricing WHERE term_id=? ORDER BY id DESC LIMIT 1',(term_id,)).fetchone()
    source=None
    if price:
        event=db.execute("SELECT data FROM history WHERE entity='pricing' AND entity_id=? AND action='set' ORDER BY id DESC LIMIT 1",(price['id'],)).fetchone()
        if event:
            value=json.loads(event['data']).get('source')
            if isinstance(value,str) and value.strip(): source=value
    final=None
    if price and source and term.get('rate') is not None: final=str(Decimal(str(term['rate']))+Decimal(price['markup']))
    calculated=final
    fields=client_fields(db,term_id)
    if 'final_rate' in fields: final=fields['final_rate']
    result={'markup':price['markup'] if price else None,'final_rate':final,'pricing_reason':price['reason'] if price else None,'pricing_source':source}
    if 'final_rate' in fields: result.update(calculated_final_rate=calculated,final_rate_source='пользователь')
    return result

def client_fields(db,term_id):
    row=db.execute("SELECT data FROM history WHERE entity='client_fields' AND entity_id=? AND action='edit' ORDER BY id DESC LIMIT 1",(term_id,)).fetchone()
    return json.loads(row['data'])['after'] if row else {}

def bank_key_status(fields):
    """Check the specified checksum only; this does not verify the bank or account."""
    bik=fields.get('bik'); account=fields.get('account')
    if not isinstance(bik,str) or not re.fullmatch(r'[0-9]{9}',bik) or not isinstance(account,str) or not re.fullmatch(r'[0-9]{20}',account):
        return 'unchecked'
    if account[:3] not in ('405','406','407','408'):
        return 'unsupported'
    digits=bik[-3:]+account
    return 'valid' if sum(int(digit)*(7,1,3)[i%3] for i,digit in enumerate(digits))%10==0 else 'invalid'

def sent(db,chat,term):
    return bool(db.execute('''SELECT 1 FROM sends s JOIN blocks b ON b.id=s.block_id WHERE b.chat_id=? AND s.term_id=? AND NOT EXISTS (SELECT 1 FROM corrections c WHERE c.send_id=s.id AND c.cancels=1) LIMIT 1''',(chat,term)).fetchone())

def confirmation(db,entity,entity_id,action):
    record=db.execute('SELECT data,at FROM history WHERE entity=? AND entity_id=? AND action=? ORDER BY id DESC LIMIT 1',(entity,entity_id,action)).fetchone()
    if not record: return {'kind':'unspecified','source':None,'at':None}
    data=json.loads(record['data']); return {'kind':data.get('confirmation_kind','unspecified'),'source':data.get('confirmation_source'),'at':record['at'],'reason':data.get('reason')}

def invoice_for_term(db, term_id):
    if db.execute('PRAGMA user_version').fetchone()[0]<3: return None
    record=db.execute('SELECT i.* FROM invoices i JOIN invoice_offers io ON io.invoice_id=i.id JOIN terms t ON t.offer_id=io.offer_id WHERE t.id=?',(term_id,)).fetchone()
    return {'id':record['id'], **json.loads(record['data'])} if record else None

def invoice_matches(db, invoice_id):
    invoice=t.row(db,'invoices',invoice_id); data=json.loads(invoice['data']); result=[]
    hashes={r[0] for r in db.execute('SELECT d.sha256 FROM documents d JOIN document_classification c ON c.document_id=d.id WHERE c.invoice_id=?',(invoice_id,))}
    company=dict(t.row(db,'companies',invoice['company_id'])); company['company_id']=company['id']
    for other in db.execute('SELECT * FROM invoices WHERE company_id IN (SELECT value FROM json_each(?)) AND id!=? ORDER BY id',(t.canonical(t.related_company_ids(db,company)),invoice_id)):
        if not db.execute('SELECT 1 FROM invoice_offers WHERE invoice_id=?',(other['id'],)).fetchone() and db.execute("SELECT 1 FROM history WHERE entity='invoice' AND entity_id=? AND action='identified_as' AND json_extract(data,'$.invoice_id')=?",(other['id'],invoice_id)).fetchone(): continue
        value=json.loads(other['data']); reasons=[]
        if data.get('number') and value.get('number') and data['number'].strip().casefold()==value['number'].strip().casefold(): reasons.append('number')
        if data.get('date') and data.get('amount') is not None and value.get('amount') is not None and data['date']==value.get('date') and Decimal(data['amount'])==Decimal(value['amount']): reasons.append('date_amount')
        other_hashes={r[0] for r in db.execute('SELECT d.sha256 FROM documents d JOIN document_classification c ON c.document_id=d.id WHERE c.invoice_id=?',(other['id'],))}
        if hashes & other_hashes: reasons.append('file_hash')
        if reasons: result.append({'id':other['id'],'reasons':reasons})
    return result

def invoice_issues(db, root, invoice_id):
    result=[]
    for r in db.execute("SELECT h.* FROM history h WHERE h.entity='document_issue' AND h.action='open' AND NOT EXISTS (SELECT 1 FROM history x WHERE x.entity='document_issue' AND x.action='resolve' AND x.entity_id=h.id) ORDER BY h.id"):
        data=json.loads(r['data'])
        if data.get('invoice_id')!=invoice_id: continue
        intact=False
        if data.get('path'):
            try:
                f=t.safe(root,data['path']); intact=f.is_file() and t.sha(f.read_bytes())==data.get('sha256')
            except (OSError,ValueError): pass
        result.append({'id':r['id'],'data':data,'intact':intact})
    return result

def invoice_state(db, root, invoice_id):
    invoice=t.row(db,'invoices',invoice_id)
    docs=documents(db,root,invoice['company_id'],invoice_id,False)
    offers=[list(x) for x in db.execute('SELECT o.id,o.message_id,io.reason FROM offers o JOIN invoice_offers io ON io.offer_id=o.id WHERE io.invoice_id=? ORDER BY o.id',(invoice_id,))]
    return t.sha(t.canonical({'invoice':dict(invoice),'documents':docs,'offers':offers,'issues':invoice_issues(db,root,invoice_id),'possible_matches':invoice_matches(db,invoice_id)}).encode())

def documents(db,root,cid,invoice_id=None, all_company=True):
    result=[]
    for d in db.execute('SELECT * FROM documents WHERE company_id=? ORDER BY id',(cid,)):
        try:
            f=t.safe(root,d['path']); intact=f.is_file() and t.sha(f.read_bytes())==d['sha256']
        except (OSError,ValueError): intact=False
        cls=db.execute('SELECT classification,invoice_id FROM document_classification WHERE document_id=?',(d['id'],)).fetchone() if db.execute('PRAGMA user_version').fetchone()[0]>=3 else None
        classification=cls['classification'] if cls else 'legacy'; did_invoice=cls['invoice_id'] if cls else None
        if not all_company and not ((invoice_id is not None and did_invoice==invoice_id) or classification=='company' or (invoice_id is None and classification=='legacy')): continue
        valid_link=not did_invoice or bool(db.execute('SELECT 1 FROM invoices WHERE id=? AND company_id=?',(did_invoice,cid)).fetchone())
        item={'id':d['id'],'name':d['name'],'status':d['status'],'confirmation':confirmation(db,'document',d['id'],'review'),'intact':intact,'ready':intact and d['status']=='verified' and valid_link,'classification':classification,'invoice_id':did_invoice}
        uploaded=db.execute("SELECT at FROM history WHERE entity='document' AND entity_id=? AND action='upload' ORDER BY id LIMIT 1",(d['id'],)).fetchone()
        if uploaded: item.update(uploaded_by_user=True,uploaded_at=uploaded['at'])
        result.append(item)
    return result

def public_base():
    base=os.environ.get('PUBLIC_BASE_URL','').strip().rstrip('/')
    t.require(bool(base), 'Публичный адрес файлов ещё не настроен.')
    try:
        parsed=urlsplit(base)
        port=parsed.port  # Accessing this property validates the port syntax and range.
    except ValueError:
        raise ValueError('Укажите PUBLIC_BASE_URL как публичный адрес сервера без пути') from None
    t.require(bool(base) and '?' not in base and '#' not in base and parsed.scheme in ('http','https') and bool(parsed.hostname) and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment and parsed.path in ('','/'), 'Укажите PUBLIC_BASE_URL как публичный адрес сервера без пути')
    t.require(not any(ord(c)<32 or ord(c)==127 for c in base) and bool(re.fullmatch(r'(?:\[[0-9A-Fa-f:.]+\]|[^:@\[\]]+)(?::[0-9]+)?',parsed.netloc)) and (port is None or port>0), 'Неверный адрес или порт PUBLIC_BASE_URL')
    hostname=parsed.hostname.lower().removesuffix('.')
    t.require(parsed.scheme=='https' and hostname!='localhost' and not hostname.endswith('.localhost'), 'Нужен публичный HTTPS-адрес')
    try: address=ipaddress.ip_address(parsed.hostname)
    except ValueError: address=None
    if address is None:
        try: ascii_host=hostname.encode('idna').decode('ascii')
        except UnicodeError: raise ValueError('Неверное имя сервера PUBLIC_BASE_URL') from None
        labels=ascii_host.split('.')
        t.require(len(ascii_host)<=253 and len(labels)>1 and all(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?',label) for label in labels) and not re.fullmatch(r'(?:[0-9]+|0x[0-9a-f]*)',labels[-1]), 'Неверное имя сервера PUBLIC_BASE_URL')
    t.require(address is None or address.is_global, 'Нужен публичный HTTPS-адрес')
    return base

def optional_public_base():
    try: return public_base()
    except ValueError: return None

def link_revision(db,term_id,did):
    term=t.row(db,'terms',term_id)
    doc=db.execute("SELECT MAX(id) FROM history WHERE entity='document' AND entity_id=? AND action IN ('classification','invoice_relink')",(did,)).fetchone()[0]
    offer=db.execute("SELECT MAX(id) FROM history WHERE entity='offer' AND entity_id=? AND action IN ('invoice_relink','invoice_to_company')",(term['offer_id'],)).fetchone()[0]
    return [doc,offer]

def share_state(db, term_id, did):
    row=db.execute("SELECT action,data FROM history WHERE entity='document_share' AND entity_id=? AND json_extract(data,'$.term_id')=? ORDER BY id DESC LIMIT 1",(did,term_id)).fetchone()
    if not row or row['action']!='enable': return None
    return json.loads(row['data'])

def share_info(db, term_id, doc):
    state=share_state(db,term_id,doc['id']); url=None
    if state:
        try: url=public_base()+'/public/documents/'+state['token']
        except ValueError: pass
    return {'share_selected':bool(state),'share_url':url}

def decorate_shares(db, obj, term_id=None):
    if isinstance(obj,list):
        for x in obj: decorate_shares(db,x,term_id)
    elif isinstance(obj,dict):
        term_id=obj.get('term_id',term_id)
        if term_id and {'id','intact','classification','ready'} <= obj.keys(): obj.update(share_info(db,term_id,obj))
        for x in list(obj.values()): decorate_shares(db,x,term_id)
    return obj

def shareable(db,doc):
    return doc['intact'] and doc['status'] in ('verified','unverified') and (not doc['invoice_id'] or bool(db.execute('SELECT 1 FROM invoices WHERE id=? AND company_id=(SELECT company_id FROM documents WHERE id=?)',(doc['invoice_id'],doc['id'])).fetchone()))

def set_document_share(db,root,term_id,did,p):
    t.require(set(p)=={'selected'} and type(p['selected']) is bool,'Нужна отметка selected')
    record=t.row(db,'documents',did); term=t.row(db,'terms',term_id); offer=t.row(db,'offers',term['offer_id'])
    invoice=invoice_for_term(db,term_id)
    allowed=documents(db,root,offer['company_id'],invoice['id'] if invoice else None,False)
    doc=next((d for d in allowed if d['id']==did),None)
    state=share_state(db,term_id,did)
    if p['selected']:
        base=optional_public_base()
        t.require(doc is not None and shareable(db,doc),'Для отправки нужен целый документ, связанный с предложением, без конфликта')
        if not state:
            state={'term_id':term_id,'token':secrets.token_urlsafe(32),'sha256':record['sha256'],'company_id':record['company_id'],'classification':doc['classification'],'invoice_id':doc['invoice_id'],'link_revision':link_revision(db,term_id,did)}
            t.hist(db,'document_share',did,'enable',state)
        t.require(state.get('link_revision')==link_revision(db,term_id,did),'Связь документа изменилась; снимите отметку и выберите заново')
        return {'term_id':term_id,'document_id':did,'selected':True,'url':base+'/public/documents/'+state['token'] if base else None}
    if state: t.hist(db,'document_share',did,'disable',{'term_id':term_id})
    return {'term_id':term_id,'document_id':did,'selected':False,'url':None}

def shared_document(db,root,token):
    t.require(bool(re.fullmatch(r'[A-Za-z0-9_-]{43}',token)),'Документ недоступен')
    rows=db.execute("SELECT entity_id,data FROM history h WHERE entity='document_share' AND action='enable' AND NOT EXISTS (SELECT 1 FROM history later WHERE later.entity='document_share' AND later.entity_id=h.entity_id AND json_extract(later.data,'$.term_id')=json_extract(h.data,'$.term_id') AND later.id>h.id)").fetchall()
    for row in rows:
        state=json.loads(row['data'])
        if not secrets.compare_digest(state['token'],token): continue
        record=t.row(db,'documents',row['entity_id']); term=t.row(db,'terms',state['term_id']); offer=t.row(db,'offers',term['offer_id']); invoice=invoice_for_term(db,state['term_id'])
        cls=db.execute('SELECT classification,invoice_id FROM document_classification WHERE document_id=?',(record['id'],)).fetchone() if db.execute('PRAGMA user_version').fetchone()[0]>=3 else None
        classification=cls['classification'] if cls else 'legacy'; invoice_id=cls['invoice_id'] if cls else None
        allowed=record['company_id']==offer['company_id'] and ((invoice is not None and invoice_id==invoice['id']) or classification=='company' or (invoice is None and classification=='legacy'))
        if invoice_id: allowed=allowed and bool(db.execute('SELECT 1 FROM invoices WHERE id=? AND company_id=?',(invoice_id,record['company_id'])).fetchone())
        t.require(state.get('link_revision')==link_revision(db,state['term_id'],record['id']) and allowed and record['status'] in ('verified','unverified') and record['sha256']==state['sha256'] and record['company_id']==state['company_id'] and classification==state['classification'] and invoice_id==state['invoice_id'],'Документ недоступен')
        data=t.safe(root,record['path']).read_bytes(); t.require(t.sha(data)==record['sha256'],'Документ недоступен')
        return record['name'],data
    raise ValueError('Документ недоступен')

def client_document_links(db, term_id, docs):
    selected=[d for d in docs if share_state(db,term_id,d['id'])]
    if not selected: return ''
    for d in selected:
        state=share_state(db,term_id,d['id']); record=t.row(db,'documents',d['id'])
        t.require(state.get('link_revision')==link_revision(db,term_id,d['id']) and shareable(db,d) and state['sha256']==record['sha256'] and state['company_id']==record['company_id'] and state['classification']==d['classification'] and state['invoice_id']==d['invoice_id'],'Отмеченный файл недоступен или изменил связь; снимите отметку и проверьте файл')
    base=optional_public_base()
    if not base: return ''
    return '\n'+'\n'.join(('Счёт: ' if d['classification']=='invoice' else 'Документ: ')+d['name']+' — '+base+'/public/documents/'+share_state(db,term_id,d['id'])['token'] for d in selected)

def sync_tasks(db,chat,date):
    t.valid_date(date); c=t.row(db,'chats',chat)
    if c['recipient']:
        for r in db.execute('SELECT t.id FROM terms t JOIN offers o ON o.id=t.offer_id JOIN messages m ON m.id=o.message_id WHERE m.date=?',(date,)).fetchall():
            cur=db.execute('INSERT OR IGNORE INTO tasks(chat_id,term_id,date,created_at) VALUES(?,?,?,?)',(chat,r['id'],date,t.now()))
            if cur.rowcount: t.hist(db,'task',cur.lastrowid,'create',{'chat_id':chat,'term_id':r['id'],'date':date})

def queue(db,root,chat,date,create=True):
    if create: sync_tasks(db,chat,date)
    c=t.row(db,'chats',chat)
    base=t.plan(db,chat,date,apply_decisions=True,root=root)
    taskids={r['term_id']:r['id'] for r in db.execute('SELECT id,term_id FROM tasks WHERE chat_id=? AND date=?',(chat,date))}
    result=[]
    for x in base:
        if x['term_id'] not in taskids: continue
        invoice=invoice_for_term(db,x['term_id']); term=x['terms']; v=vat(db,x['company_id']); price=price_info(db,x['term_id'],term)
        reasons=[r for r in x['reasons'] if r!='client_fields_web_only']
        # v2 recognises structured fields, but rejects unsupported content rather than dropping it.
        supported={'category','rate','duration','duration_unit','vat','color','conditions','scope_confirmed','review','amount','minimum','currency','restrictions','field_provenance','rate_unit','rate_type','rate_basis','rate_text','duration_text','duration_max','duration_day_type','payment','color_source','color_date','groups','vat_classification'}
        if not set(term)-supported and 'unrendered_fields_review' in reasons: reasons.remove('unrendered_fields_review')
        if invoice:
            state=invoice_state(db,root,invoice['id'])
            reviewed=db.execute("SELECT 1 FROM history WHERE entity='invoice' AND entity_id=? AND action='review' AND json_extract(data,'$.state_hash')=? LIMIT 1",(invoice['id'],state)).fetchone()
            if not reviewed: reasons.append('invoice_review')
            if invoice_issues(db,root,invoice['id']) or any(not d['ready'] for d in documents(db,root,x['company_id'],invoice['id'],False)): reasons.append('invoice_document_review')
        if price['markup'] is None: reasons.append('markup_not_agreed')
        elif price['pricing_source'] is None: reasons.append('pricing_source_unknown')
        if term.get('rate_unit') not in ('%', 'percent'): reasons.append('rate_unit_unknown')
        edited_fields=client_fields(db,x['term_id'])
        if edited_fields.get('final_rate') is not None:
            reasons=[r for r in reasons if r not in ('rate_unknown','markup_not_agreed','pricing_source_unknown','rate_unit_unknown')]
        elif 'final_rate' in edited_fields and 'rate_unknown' not in reasons:
            reasons.append('rate_unknown')
        if term.get('duration_text') and term.get('duration') is None: reasons.append('duration_unit_unknown')
        if term.get('duration_max') is not None and term.get('duration_max')!=term.get('duration') and not term.get('duration_text'): reasons.append('duration_range_review')
        if term.get('duration_day_type') not in (None,'calendar','working'): reasons.append('duration_day_type_review')
        if term.get('color') and (not term.get('color_source') or term.get('color_date')!=date): reasons.append('color_provenance_review')
        if v['conflict']: reasons.append('company_vat_conflict')
        if any(isinstance(term.get(k),(dict,list,bool)) for k in ('vat','payment','duration_text','conditions','restrictions','amount','minimum','currency','rate_type','rate_basis')): reasons.append('unsupported_structured_condition')
        try:
            m=t.row(db,'messages',x['message_id']); f=t.safe(root,m['path']); intact=f.is_file() and t.sha(f.read_bytes())==m['sha256'] and t.sha(m['text'].encode())==m['sha256']
        except (ValueError,OSError): intact=False
        if not intact: reasons.append('source_missing_or_changed')
        if not c['recipient']: reasons.append('recipient_inactive')
        # Existing CLI's sent check includes corrected records; use current factual state.
        reasons=[r for r in reasons if r!='already_sent']
        was_sent=sent(db,chat,x['term_id'])
        if was_sent: status='sent'
        elif 'recipient_source_today' in reasons: status='excluded'
        elif 'inactive' in reasons: status='withdrawn'
        elif reasons: status='review'
        else: status='pending'
        withdrawal=None
        if 'inactive' in reasons:
            event=db.execute("SELECT action,data,at FROM history WHERE entity='term' AND entity_id=? AND action IN ('withdraw','replace') ORDER BY id DESC LIMIT 1",(x['term_id'],)).fetchone()
            if event:
                detail=json.loads(event['data']); withdrawal={'action':event['action'],'reason':detail.get('reason'),'new_term_id':detail.get('new_term_id'),'at':event['at']}
        item={**x,'date':date,'source_platform':t.chat_platform(db,x['source_chat_id']),'source_time':m['time'] if intact else None,'id':taskids[x['term_id']],'status':status,'reason':reasons[0] if reasons else 'ok','reasons':reasons,'withdrawal':withdrawal,'company_vat':v,'identity_confirmation':confirmation(db,'company',x['company_id'],'identity_decision'),**price,'offer_type':'invoice' if invoice else 'company','invoice':invoice,'possible_invoice_matches':invoice_matches(db,invoice['id']) if invoice else [],'documents':documents(db,root,x['company_id'],invoice['id'] if invoice else None,False),'company_documents':documents(db,root,x['company_id'])}
        item['version']=t.sha(t.canonical(item).encode()); result.append(item)
    # Only compare explicitly complete, matching terms with a known rate meaning.
    groups={}
    for x in result:
        if x['inn_status']=='verified' and x['status'] not in ('withdrawn','excluded','sent'):
            groups.setdefault((x['company_id'],x['invoice']['id'] if x['invoice'] else None),[]).append(x)
    keys=('rate_unit','rate_type','rate_basis','category','vat','payment','duration_text','duration','duration_unit','duration_day_type','color','conditions','restrictions','amount','minimum','currency')
    for group in groups.values():
        sources={x['source_chat_id'] for x in group}
        if len(sources)<2: continue
        comparable=all(all(k in x['terms'] and x['terms'][k] is not None for k in keys) and x['terms'].get('rate') is not None and not any(r in x['reasons'] for r in ('source_missing_or_changed','identity_review','possible_identity_conflict_review','terms_review','message_versions_review','invalid_field_provenance_review','unrendered_fields_review','unsupported_structured_condition','company_vat_conflict','color_provenance_review','duration_range_review','duration_day_type_review')) and not (any(c in str(x['terms'].get('duration_text','')) for c in ('–','—','-')) and x['terms'].get('duration_max') is None) for x in group)
        comparable=comparable and all(not ({'subject','delivery'} & client_fields(db,x['term_id']).keys()) for x in group)
        conditions=[vat_condition(x['terms']) for x in group]
        comparable=comparable and all(c['status'] in ('explicit','inferred') and c['value']!='не указан' for c in conditions) and len({c['value'] for c in conditions})==1
        comparable=comparable and all(tuple(x['terms'][k] for k in keys)==tuple(group[0]['terms'][k] for k in keys) and x['terms'].get('duration_max',x['terms'].get('duration'))==group[0]['terms'].get('duration_max',group[0]['terms'].get('duration')) for x in group)
        values=[Decimal(x['terms']['rate']) for x in group] if comparable else []
        unique_best=comparable and values.count(min(values))==1
        for x in group:
            x['comparison']={'comparable':comparable,'lowest':bool(unique_best and Decimal(x['terms']['rate'])==min(values)) if comparable else False,'tie':bool(comparable and not unique_best),'note':'Сравнение требует проверки' if not comparable else ('Равные минимальные ставки; выбор вручную' if not unique_best else 'Сопоставимые условия; показана исходная ставка')}
            if unique_best and 'multiple_sources_review' in x['reasons']:
                x['reasons'].remove('multiple_sources_review')
                if x['status']=='review' and not x['reasons']: x['status']='pending'
                x['reason']=x['reasons'][0] if x['reasons'] else 'ok'
    cards={x['term_id']:x for x in search(db,root,{'date':date})['items']}
    recipient_risks={'recipient_company_invoice_review','possible_invoice_match_review','possible_recipient_company_review',
                     'company_previously_sent_review','past_recipient_source_review',
                     'recipient_mention_review','negotiation_review',
                     'recipient_source_today','recipient_inactive','inactive',
                     'source_missing_or_changed','rate_unknown','markup_not_agreed',
                     'pricing_source_unknown','rate_unit_unknown'}
    for x in result:
        card=cards.get(x['term_id'])
        if card and card['client_fields']: x['client_fields']=card['client_fields']
        decision=card['operator_review'] if card else {'accepted':False,'stale':False,'at':None,'state_hash':None,'revision':None}
        x['operator_review']=decision
        x['acknowledged_reasons']=[r for r in x['reasons'] if decision['accepted'] and r not in recipient_risks]
        if x['status']=='review' and not any(r not in x['acknowledged_reasons'] for r in x['reasons']): x['status']='pending'
        event=db.execute("SELECT MAX(id) FROM history WHERE entity='term' AND entity_id=? AND action='classification'",(x['term_id'],)).fetchone()[0]
        version_data={k:v for k,v in x.items() if k!='version' and (decision['state_hash'] is not None or k not in ('operator_review','acknowledged_reasons'))}
        if event is not None: version_data={'item':version_data,'classification':event}
        edit_revision=db.execute("SELECT MAX(id) FROM history WHERE entity='client_fields' AND entity_id=? AND action='edit'",(x['term_id'],)).fetchone()[0]
        if edit_revision is not None: version_data={'item':version_data,'client_fields_revision':edit_revision}
        comment=client_comment(db,x['term_id'],date)
        if comment['revision'] is not None: version_data={'item':version_data,'client_comment_revision':comment['revision']}
        x['version']=t.sha(t.canonical(version_data).encode())
        x['bank_key_status']=bank_key_status(card['client_fields'] if card else {})
    def time_key(value):
        try:
            parsed=dt.time.fromisoformat(value) if isinstance(value,str) else None
            return parsed.isoformat() if parsed and parsed.tzinfo is None else None
        except ValueError: return None
    earliest={}
    for x in result:
        key=(x['status'],x['company_id']); value=time_key(x['source_time'])
        if value is not None and (key not in earliest or value<earliest[key]): earliest[key]=value
    order={'pending':0,'review':1,'sent':2,'withdrawn':3,'excluded':4}
    result.sort(key=lambda x: (order[x['status']],earliest.get((x['status'],x['company_id']),'~'),x['company'].casefold(),0 if x.get('comparison',{}).get('lowest') else 1,time_key(x['source_time']) or '~',x['term_id']))
    return result

def sources(db, date):
    """Chats with active offer variants on this date, regardless of recipient role."""
    t.valid_date(date)
    rows=db.execute('''SELECT ch.id,ch.name,COUNT(*) count FROM terms t
        JOIN offers o ON o.id=t.offer_id JOIN messages m ON m.id=o.message_id
        JOIN chats ch ON ch.id=m.chat_id WHERE t.active=1 AND m.date=?
        GROUP BY ch.id,ch.name ORDER BY ch.name,ch.id''',(date,)).fetchall()
    return [{'id':r['id'],'name':r['name'],'platform':t.chat_platform(db,r['id']),'count':r['count']} for r in rows]

def product_groups():
    """Read the editable project dictionary on each request, never a copied list."""
    lines=(Path(__file__).resolve().parent/'ТОВАРНЫЕ-ГРУППЫ.md').read_text(encoding='utf-8').splitlines()
    groups=[]
    for line in lines:
        if line.startswith('| ') and line.count('|') >= 3:
            name=line.split('|')[1].strip()
            if name not in ('Группа','---') and name and not set(name)<=set('- :'):
                groups.append(name)
    t.require(groups and len(groups)==len(set(groups)), 'Некорректный справочник товарных групп')
    return groups

VAT_VALUES = ('НДС 22%', 'НДС 20%', 'НДС 10%', 'НДС 7%', 'НДС 5%', 'НДС 0%', 'Безотчёт', 'Без НДС', 'не указан')

def vat_condition(term):
    """Only exact, unambiguous field values; never infer from arbitrary prose."""
    raw={key:term.get(key) for key in ('vat','payment')}
    selected=term.get('vat_classification')
    if isinstance(selected,dict) and selected.get('value') in VAT_VALUES:
        if selected.get('inputs') != raw:
            return {'value':None,'status':'conflict','source':'пользователь'}
        return {'value':selected['value'],'status':'explicit','source':'пользователь'}
    values=[]; uncertain=False
    for key,value in raw.items():
        if value is None or value=='': continue
        if not isinstance(value,str): uncertain=True; continue
        text=value.strip().casefold().replace('ё','е')
        if key=='payment' and text in ('перевод','без предоплаты'): continue
        matches={v.casefold().replace('ё','е'):v for v in VAT_VALUES}
        if key=='vat': matches.update({v.removeprefix('НДС ').casefold():v for v in VAT_VALUES[:6]})
        normalized=matches.get(text)
        if normalized is None and key=='vat':
            match=re.fullmatch(r'(?:с\s*)?ндс\s*(22|20|10|7|5|0)\s*%?',text)
            if match: normalized='НДС '+match.group(1)+'%'
        if normalized is None: uncertain=True
        else: values.append(normalized)
    if len(set(values))>1: return {'value':None,'status':'conflict','source':'исходные условия'}
    if uncertain or not values: return {'value':None,'status':'unknown','source':None}
    return {'value':values[0],'status':'inferred','source':'исходные условия'}

def classify_vat(db,root,term_id,p):
    t.require(set(p)=={'date','version','vat_condition'},'Неверные поля условия НДС')
    t.valid_date(p['date']); t.require(isinstance(p['version'],str),'Нужна версия предложения')
    value=p['vat_condition']; t.require(isinstance(value,str) and value in VAT_VALUES,'Неверное условие НДС')
    current=next ( (x for x in search(db,root,{'date':p['date']})['items'] if x['term_id']==term_id), None)
    t.require(current is not None and current['version']==p['version'],'Данные изменились или предложение снято; обновите список')
    public=current['public_text']; replacement=None; added_line=False; auto_fields=False
    if public['revision'] is not None and not public['stale']:
        text=public['text']; lines=text.splitlines(keepends=True)
        # Only touch our exact standalone line. Other VAT wording is ambiguous,
        # except the distinct company field, which belongs to another scope.
        vat_lines=[i for i,line in enumerate(lines) if ('ндс' in line.casefold() or 'безотчет' in line.casefold().replace('ё','е')) and not line.startswith('НДС компании:') and line.rstrip('\r\n')!='НДС: не указан']
        recognized=[i for i in vat_lines if lines[i].rstrip('\r\n') in ('НДС предложения: '+v for v in VAT_VALUES)]
        previous=db.execute("SELECT data FROM history WHERE id=? AND entity='public_text' AND entity_id=?",(public['revision'],term_id)).fetchone()
        prior=public_text_origin(db,term_id,public['revision'],json.loads(previous['data'])) if previous else {}
        auto_fields=(prior.get('reason')=='auto_generated' or prior.get('auto_fields') is True) and recognized==[2]
        if value=='не указан':
            if len(vat_lines)==1 and vat_lines==recognized and auto_fields:
                index=vat_lines[0]; ending=lines[index][len(lines[index].rstrip('\r\n')):]
                lines[index]='НДС предложения: не указан'+ending; replacement=''.join(lines)
            elif len(vat_lines)==1 and vat_lines==recognized and (prior.get('added_line') is True or prior.get('vat_added_line') is True):
                del lines[vat_lines[0]]; replacement=''.join(lines)
        elif not vat_lines:
            separator='\r\n' if '\r\n' in text else '\n'
            replacement=text+('' if text.endswith(('\n','\r')) else separator)+'НДС предложения: '+value; added_line=True
        elif len(vat_lines)==1 and vat_lines==recognized:
            index=vat_lines[0]; ending=lines[index][len(lines[index].rstrip('\r\n')):]
            lines[index]='НДС предложения: '+value+ending; replacement=''.join(lines); added_line=prior.get('added_line') is True or prior.get('vat_added_line') is True
        if replacement is not None and (not replacement.strip() or len(replacement)>20000): replacement=None
    term=dict(current['terms']); before=term.get('vat_classification')
    after={'value':value,'source':'пользователь','decision_date':dt.date.today().isoformat(), 'inputs':{k:term.get(k) for k in ('vat','payment')}}
    term['vat_classification']=after
    db.execute('UPDATE terms SET data=? WHERE id=?',(t.canonical(term),term_id))
    t.hist(db,'term',term_id,'classification',{'before':before,'after':after,'source':'пользователь','decision_date':after['decision_date'],'field':'vat_classification'})
    item=next(x for x in search(db,root,{'date':p['date']})['items'] if x['term_id']==term_id)
    if replacement is not None:
        t.hist(db,'public_text',term_id,'save',{'version':item['version'],'text':replacement,'reason':'vat_classification','previous_revision':public['revision'],'added_line':added_line,'auto_fields':auto_fields})
        item=next(x for x in search(db,root,{'date':p['date']})['items'] if x['term_id']==term_id)
    return {'item':item}

def upload_documents(db,root,term_id,p,saved):
    import base64, binascii, tempfile
    from pathlib import Path
    t.require(set(p)=={'date','version','files'},'Неверные поля загрузки')
    t.valid_date(p['date'])
    t.require(isinstance(p['version'],str),'Нужна версия предложения')
    t.require(db.in_transaction,'Загрузка требует транзакцию записи')
    current=next((x for x in search(db,root,{'date':p['date']})['items'] if x['term_id']==term_id),None)
    t.require(current is not None and current['version']==p['version'],'Данные изменились или предложение снято; обновите список')
    t.require(db.execute('PRAGMA user_version').fetchone()[0]>=3,'Для загрузки нужна схема v3')
    files=p['files']; t.require(isinstance(files,list) and 1<=len(files)<=10,'Выберите от 1 до 10 файлов')
    decoded=[]; total=0
    for f in files:
        t.require(isinstance(f,dict) and set(f)=={'name','data'},'Неверные поля файла')
        name=f['name']; encoded=f['data']
        t.require(isinstance(name,str) and 0<len(name)<=200 and name not in ('.','..') and '/' not in name and '\\' not in name and not any(ord(c)<32 or ord(c)==127 for c in name),'Недопустимое имя файла')
        t.require(len(Path(name).suffix)<=32,'Слишком длинное расширение файла')
        t.require(isinstance(encoded,str) and len(encoded)<=((10*1024*1024+2)//3)*4,'Файл слишком большой')
        try: data=base64.b64decode(encoded,validate=True)
        except (ValueError,binascii.Error): raise ValueError('Неверное содержимое файла')
        t.require(0<len(data)<=10*1024*1024,'Файл пуст или слишком большой')
        total+=len(data); t.require(total<=20*1024*1024,'Общий размер файлов превышает 20 МиБ')
        decoded.append((name,data))
    term=t.row(db,'terms',term_id); offer=t.row(db,'offers',term['offer_id']); invoice=invoice_for_term(db,term_id)
    dates=[r[0] for r in db.execute('''SELECT DISTINCT m.date FROM terms t JOIN offers o ON o.id=t.offer_id
        JOIN messages m ON m.id=o.message_id WHERE t.active=1 AND o.company_id=?''',(offer['company_id'],))]
    def candidates():
        return {x['term_id']:x for date in dates for x in search(db,root,{'date':date})['items'] if x['company_id']==offer['company_id']}
    before=candidates()
    def risk_states():
        values={}
        for date in dates:
            first=db.execute('''SELECT m.chat_id FROM terms t JOIN offers o ON o.id=t.offer_id JOIN companies c ON c.id=o.company_id
                JOIN messages m ON m.id=o.message_id WHERE t.active=1 AND m.date=? ORDER BY c.name,t.id LIMIT 1''',(date,)).fetchone()
            if first: values.update({x['term_id']:x['reasons'] for x in t.plan(db,first[0],date,apply_decisions=False,root=root)})
        return values
    def non_document_state(item, added, risks):
        # Retain every displayed field and all pre-existing documents; ignore only
        # the newly registered files and document-derived review indicators.
        value={k:v for k,v in item.items() if k not in ('version','public_text','operator_review','match','unknown','documents','company_documents')}
        value['unknown']=[x for x in item['unknown'] if x not in ('invoice_review','invoice_document_review')]
        for key in ('documents','company_documents'):
            value[key]=[d for d in item[key] if d['id'] not in added]
        inv=item['invoice']
        value['invoice_issues']=invoice_issues(db,root,inv['id']) if inv else []
        value['invoice_offers']=[list(x) for x in db.execute('SELECT o.id,o.message_id,io.reason FROM offers o JOIN invoice_offers io ON io.offer_id=o.id WHERE io.invoice_id=? ORDER BY o.id',(inv['id'],))] if inv else []
        value['message_row']=dict(t.row(db,'messages',item['message_id']))
        value['term_row']=dict(t.row(db,'terms',item['term_id']))
        value['classification_event']=db.execute("SELECT MAX(id) FROM history WHERE entity='term' AND entity_id=? AND action='classification'",(item['term_id'],)).fetchone()[0]
        value['risks']=risks.get(item['term_id'],[])
        return t.canonical(value)
    risks_before=risk_states()
    states={tid:non_document_state(item,set(),risks_before) for tid,item in before.items()}
    results=[]
    with tempfile.TemporaryDirectory(prefix='trading-upload-') as temp:
        for index,(name,data) in enumerate(decoded):
            folder=Path(temp)/str(index); folder.mkdir(); source=folder/name; source.write_bytes(data)
            result=t.execute(db,root,'document',{'message_id':offer['message_id'],'company_id':offer['company_id'],'path':str(source),'classification':'invoice' if invoice else 'company','invoice_id':invoice['id'] if invoice else None})
            saved.append(result['path']); results.append(result)
            t.hist(db,'document',result['document_id'],'upload',{'term_id':term_id,'message_id':offer['message_id'],'source':'Загрузка пользователем через веб','name':name})
    added={result['document_id'] for result in results}
    after=candidates()
    risks_after=risk_states()
    for tid,old in before.items():
        new=after.get(tid); public=old['public_text']
        if (new and public['revision'] is not None and not public['stale']
                and new['version']!=old['version']
                and new['public_text']['revision']==public['revision']
                and non_document_state(new,added,risks_after)==states[tid]):
            t.hist(db,'public_text',tid,'save',{'version':new['version'],'text':public['text'],
                'reason':'document_upload','previous_revision':public['revision'],'document_ids':sorted(added)})
    updated=next(x for x in search(db,root,{'date':p['date']})['items'] if x['term_id']==term_id)
    return {'documents':results,'version':updated['version']}

def classify_term(db,root,term_id,p):
    t.require(set(p)=={'date','version','groups','color','color_changed','groups_changed'},'Неверные поля классификации')
    t.require(isinstance(p['color_changed'],bool) and isinstance(p['groups_changed'],bool),'Нужно указать изменённые поля')
    date=p['date']; t.valid_date(date)
    groups=p['groups']; color=p['color']
    t.require(isinstance(groups,list) and all(isinstance(x,str) for x in groups) and len(groups)==len(set(groups)), 'Неверные товарные группы')
    allowed=product_groups()
    if p['groups_changed']: t.require(all(x in allowed for x in groups),'Товарная группа отсутствует в актуальном справочнике')
    t.require(color in (None,'green','yellow','red'), 'Неверный цвет светофора')
    t.require(isinstance(p['version'],str),'Нужна версия предложения')
    current=next((x for x in search(db,root,{'date':date})['items'] if x['term_id']==term_id),None)
    t.require(current is not None and current['version']==p['version'],'Данные изменились или предложение снято; обновите список')
    public=current['public_text']
    replacement=None
    if p['color_changed'] and public['revision'] is not None and not public['stale']:
        # Only replace an unambiguous, standalone traffic-light line in an
        # already reviewed text. All other manual wording stays byte-for-byte.
        lines=list(re.finditer(r'(?m)^Светофор:[^\r\n]*(?=\r?$)',public['text']))
        recognized=r'Светофор: (?:не указан|(?:🟢 )?зел[её]ный|(?:🟡 )?ж[её]лтый|(?:🔴 )?красный)'
        if len(lines)==1 and re.fullmatch(recognized,lines[0].group()):
            value={None:'не указан','green':'🟢 зелёный','yellow':'🟡 жёлтый','red':'🔴 красный'}[color]
            replacement=public['text'][:lines[0].start()]+'Светофор: '+value+public['text'][lines[0].end():]
            if len(replacement)>20000: replacement=None
    if p['groups_changed'] and not p['color_changed'] and public['revision'] is not None and not public['stale']:
        # Product groups are internal labels; they do not change client text.
        replacement=public['text']
    term=dict(current['terms']); before={k:term.get(k) for k in ('groups','color','color_source','color_date')}
    if p['groups_changed']:
        if groups: term['groups']=[x for x in allowed if x in groups]
        else: term.pop('groups',None)
    if p['color_changed']:
        if color is None:
            for key in ('color','color_source','color_date'): term.pop(key,None)
        else: term.update(color=color,color_source='пользователь',color_date=date)
    after={k:term.get(k) for k in ('groups','color','color_source','color_date')}
    if before!=after or p['color_changed'] or p['groups_changed']:
        db.execute('UPDATE terms SET data=? WHERE id=?',(t.canonical(term),term_id))
        t.hist(db,'term',term_id,'classification',{'before':before,'after':after,'source':'пользователь','decision_date':dt.date.today().isoformat(),'color_changed':p['color_changed'],'groups_changed':p['groups_changed']})
    item=next(x for x in search(db,root,{'date':date})['items'] if x['term_id']==term_id)
    if replacement is not None:
        previous=db.execute("SELECT data FROM history WHERE id=? AND entity='public_text' AND entity_id=?",(public['revision'],term_id)).fetchone()
        prior=public_text_origin(db,term_id,public['revision'],json.loads(previous['data'])) if previous else {}
        save_public_text(db,root,term_id,{'date':date,'version':item['version'],'revision':public['revision'],'text':replacement},origin={
            'reason':'color_classification' if p['color_changed'] else 'unchanged_text','previous_revision':public['revision'],
            'auto_fields':prior.get('reason')=='auto_generated' or prior.get('auto_fields') is True,
            'vat_added_line':prior.get('added_line') is True or prior.get('vat_added_line') is True})
        item=next(x for x in search(db,root,{'date':date})['items'] if x['term_id']==term_id)
    return {'item':item}

def search(db, root, p):
    """Search the v2 registry; uncertain filters remain possible matches."""
    date=p.get('date',dt.date.today().isoformat()); t.valid_date(date)
    query=p.get('q',''); t.require(isinstance(query,str),'query must be a string'); query=query.strip().casefold()
    source=p.get('source_chat_id')
    if source not in (None,''):
        t.require(not isinstance(source,bool) and str(source).isascii() and str(source).isdigit() and int(source)>0,'invalid source chat id')
        source=int(source)
    groups=p.get('group',[]); groups=[groups] if isinstance(groups,str) else groups
    allowed_groups=product_groups() if groups else []
    t.require(isinstance(groups,list) and all(isinstance(x,str) for x in groups) and len(groups)==len(set(groups)) and all(x in allowed_groups for x in groups),'Неверный фильтр товарных групп')
    vat_filter=p.get('vat_condition'); t.require(vat_filter in (None,'') or vat_filter in VAT_VALUES,'Неверный фильтр условия НДС')
    category=p.get('category'); color=p.get('color'); rate_type=p.get('rate_type'); rate_basis=p.get('rate_basis')
    for value in (category,color,rate_type,rate_basis): t.require(value is None or isinstance(value,str),'invalid filter')
    def limit(name):
        value=p.get(name)
        if value is None or value=='': return None
        try: number=Decimal(str(value).replace(',','.'))
        except (InvalidOperation, ValueError): raise ValueError('invalid '+name)
        t.require(number.is_finite() and number>=0,'invalid '+name); return number
    max_rate=limit('max_rate'); min_rate=limit('min_rate'); max_duration=limit('max_duration')
    rows=db.execute("""SELECT t.id term_id,t.data,c.id company_id,c.name,c.inn,c.inn_status,m.id message_id,m.chat_id,m.date,m.path source_path,ch.name source_chat FROM terms t JOIN offers o ON o.id=t.offer_id JOIN companies c ON c.id=o.company_id JOIN messages m ON m.id=o.message_id JOIN chats ch ON ch.id=m.chat_id WHERE t.active=1 AND m.date=? ORDER BY c.name,t.id""",(date,)).fetchall()
    risks={}
    if rows: risks={x['term_id']:x['reasons'] for x in t.plan(db,rows[0]['chat_id'],date,apply_decisions=False,root=root)}
    colors={'green':'green','зеленыи':'green','зеленая':'green','yellow':'yellow','желтыи':'yellow','желтая':'yellow','red':'red','красныи':'red','красная':'red'}
    def norm(value): return str(value).casefold().replace('ё','е').replace('й','и').strip()
    def normalize_category(value):
        value=norm(value)
        for alias in ('стромат','строиматериалы'): value=value.replace(alias,'строительные материалы')
        return value
    result=[]
    offer_type=p.get('offer_type'); t.require(offer_type in (None,'','all','company','invoice'),'invalid offer type')
    for r in rows:
        if source not in (None,'') and r['chat_id']!=source: continue
        invoice=invoice_for_term(db,r['term_id'])
        if offer_type in ('company','invoice') and offer_type!=('invoice' if invoice else 'company'): continue
        if query and query not in r['name'].casefold() and query not in (r['inn'] or '') and not (invoice and query in t.canonical(invoice).casefold()): continue
        term=json.loads(r['data']); fields=client_fields(db,r['term_id']); unknown=[]; mismatch=False
        condition=vat_condition(term)
        if vat_filter:
            if condition['status'] in ('unknown','conflict') or (condition['value']=='не указан' and vat_filter!='не указан'): unknown.append('vat_condition')
            elif condition['value']!=vat_filter: mismatch=True
        if invoice:
            state=invoice_state(db,root,invoice['id'])
            if not db.execute("SELECT 1 FROM history WHERE entity='invoice' AND entity_id=? AND action='review' AND json_extract(data,'$.state_hash')=? LIMIT 1",(invoice['id'],state)).fetchone(): unknown.append('invoice_review')
            if invoice_issues(db,root,invoice['id']) or any(not d['ready'] for d in documents(db,root,r['company_id'],invoice['id'],False)): unknown.append('invoice_document_review')
        unknown.extend(x for x in risks.get(r['term_id'],[]) if x in ('identity_review','possible_identity_conflict_review','terms_review','message_versions_review','multiple_sources_review','unrendered_fields_review','invalid_field_provenance_review','possible_invoice_match_review','reclassified_invoice_document_review'))
        subject=fields['subject'] if 'subject' in fields else term.get('category')
        any_category=bool(re.search(r'\bлюб(?:ой|ая|ые|ое|ого|ых|ому|ым|ую|ыми)\b',str(subject or '').casefold()))
        subject_revision=db.execute("SELECT MAX(id) FROM history WHERE entity='client_fields' AND entity_id=? AND action='edit' AND json_type(data,'$.changes.subject') IS NOT NULL",(r['term_id'],)).fetchone()[0]
        group_revision=db.execute("SELECT MAX(id) FROM history WHERE entity='term' AND entity_id=? AND action='classification' AND json_extract(data,'$.groups_changed')=1",(r['term_id'],)).fetchone()[0]
        groups_stale=subject_revision is not None and (group_revision is None or group_revision<subject_revision)
        if groups:
            assigned=term.get('groups',[])
            if groups_stale or not isinstance(assigned,list) or not assigned or any(x not in allowed_groups for x in assigned) or any_category or 'Другое / группа не определена' in assigned: unknown.append('group')
            elif not set(groups).intersection(assigned): mismatch=True
        if category:
            if not subject or any_category: unknown.append('category')
            elif normalize_category(category) not in normalize_category(subject): mismatch=True
        if color:
            value=term.get('color')
            if not value or not term.get('color_source') or term.get('color_date')!=date: unknown.append('color')
            elif colors.get(norm(color),norm(color))!=colors.get(norm(value),norm(value)): mismatch=True
        if max_duration is not None:
            val=term.get('duration_max',term.get('duration'))
            if 'delivery' in fields or val is None or term.get('duration_unit')!='day' or term.get('duration_day_type')!='calendar' or (any(c in str(term.get('duration_text','')) for c in ('–','—','-')) and term.get('duration_max') is None): unknown.append('duration')
            else:
                try: value=Decimal(str(val)); t.require(value.is_finite() and value>=0,'invalid duration')
                except (ValueError,InvalidOperation): unknown.append('duration')
                else:
                    if value>max_duration: mismatch=True
        if max_rate is not None or min_rate is not None:
            value=term.get('rate')
            if value is None: unknown.append('rate')
            elif not rate_type or not rate_basis or not term.get('rate_type') or not term.get('rate_basis') or norm(rate_type)!=norm(term['rate_type']) or norm(rate_basis)!=norm(term['rate_basis']) or term.get('rate_unit') not in ('%','percent'): unknown.append('rate_comparability')
            else:
                try: rate=Decimal(str(value)); t.require(rate.is_finite(),'invalid rate')
                except (ValueError,InvalidOperation): unknown.append('rate')
                else:
                    if (max_rate is not None and rate>max_rate) or (min_rate is not None and rate<min_rate): mismatch=True
        try:
            msg=t.row(db,'messages',r['message_id']); f=t.safe(root,msg['path']); intact=f.is_file() and t.sha(f.read_bytes())==msg['sha256'] and t.sha(msg['text'].encode())==msg['sha256']
        except (OSError,ValueError): intact=False
        if not intact: unknown.append('source_missing_or_changed')
        if not mismatch:
            result.append({'term_id':r['term_id'],'company_id':r['company_id'],'company':r['name'],'inn':r['inn'],'inn_status':r['inn_status'],'identity_confirmation':confirmation(db,'company',r['company_id'],'identity_decision'),'company_vat':vat(db,r['company_id']),'vat_condition':condition,'terms':term,'client_fields':fields,'groups_stale':groups_stale,**price_info(db,r['term_id'],term),'message_id':r['message_id'],'source_path':r['source_path'],'source_chat_id':r['chat_id'],'source_chat':r['source_chat'],'source_platform':t.chat_platform(db,r['chat_id']),'date':r['date'],'offer_type':'invoice' if invoice else 'company','invoice':invoice,'possible_invoice_matches':invoice_matches(db,invoice['id']) if invoice else [],'documents':documents(db,root,r['company_id'],invoice['id'] if invoice else None,False),'company_documents':documents(db,root,r['company_id']),'match':'possible' if unknown else 'full','unknown':list(dict.fromkeys(unknown))})
    for item in result:
        # Bind the preview to both displayed data and the underlying invoice/document state.
        inv=item['invoice']
        state=invoice_state(db,root,inv['id']) if inv else None
        message=dict(t.row(db,'messages',item['message_id']))
        termrow=dict(t.row(db,'terms',item['term_id']))
        version_item={k:v for k,v in item.items() if k not in ('match','unknown','vat_condition','groups_stale') and not (k=='client_fields' and not v)}
        try:
            source=t.safe(root,message['path'])
            source_intact=source.is_file() and t.sha(source.read_bytes())==message['sha256'] and t.sha(message['text'].encode())==message['sha256']
        except (ValueError,OSError): source_intact=False
        reviewed=bool(inv and db.execute("SELECT 1 FROM history WHERE entity='invoice' AND entity_id=? AND action='review' AND json_extract(data,'$.state_hash')=? LIMIT 1",(inv['id'],state)).fetchone())
        classification=db.execute("SELECT MAX(id) FROM history WHERE entity='term' AND entity_id=? AND action='classification'",(item['term_id'],)).fetchone()[0]
        version_data={'item':version_item,'invoice_state':state,'invoice_reviewed':reviewed,'source_intact':source_intact,'risks':risks.get(item['term_id'],[]),'message':message,'term':termrow}
        if classification is not None: version_data['classification']=classification
        edit_revision=db.execute("SELECT MAX(id) FROM history WHERE entity='client_fields' AND entity_id=? AND action='edit'",(item['term_id'],)).fetchone()[0]
        if edit_revision is not None: version_data['client_fields_revision']=edit_revision
        item['version']=t.sha(t.canonical(version_data).encode())
        item['bank_key_status']=bank_key_status(item['client_fields'])
        item['operator_review']=operator_review(db,item['term_id'],item['date'],item['version'])
        item['public_text']=public_text(db,item)
        item['client_comment']=client_comment(db,item['term_id'],date)
    return {'items':result,'coverage_warning':'Проверена только загруженная история; её полнота не предполагается.'}

def operator_review(db,term_id,date,version):
    row=db.execute("SELECT id,data,at FROM history WHERE entity='offer_review' AND entity_id=? AND json_extract(data,'$.date')=? ORDER BY id DESC LIMIT 1",(term_id,date)).fetchone()
    if not row: return {'accepted':False,'stale':False,'at':None,'state_hash':None,'revision':None}
    data=json.loads(row['data']); stale=data['state_hash']!=version
    return {'accepted':bool(data['accepted'] and not stale),'stale':bool(data['accepted'] and stale),'at':row['at'],'state_hash':data['state_hash'],'revision':row['id']}

def set_operator_review(db,root,term_id,p):
    date=p.get('date'); version=p.get('version'); accepted=p.get('accepted')
    t.require(set(p)=={'date','version','accepted','revision'},'Неверные поля решения')
    t.require(p['revision'] is None or type(p['revision']) is int,'Нужна ревизия решения')
    t.valid_date(date); t.require(isinstance(accepted,bool),'Укажите решение оператора')
    item=next((x for x in search(db,root,{'date':date})['items'] if x['term_id']==term_id),None)
    t.require(item is not None and isinstance(version,str) and item['version']==version,'Данные изменились; обновите карточку')
    t.require(item['operator_review']['revision']==p['revision'],'Решение изменилось; обновите карточку')
    t.hist(db,'offer_review',term_id,'accept' if accepted else 'revoke',{'date':date,'state_hash':version,'accepted':accepted,'source':'user'})
    return {'term_id':term_id,'date':date,'accepted':accepted,'version':version}

def client_comment(db,term_id,date):
    row=db.execute("SELECT id,data FROM history WHERE entity='client_comment' AND entity_id=? AND action='save' AND json_extract(data,'$.date')=? ORDER BY id DESC LIMIT 1",(term_id,date)).fetchone()
    return {'revision':row['id'],'text':json.loads(row['data'])['text']} if row else {'revision':None,'text':''}

def save_client_comment(db,root,term_id,p):
    t.require(set(p)=={'date','version','revision','text'},'Неверные поля комментария')
    t.valid_date(p['date']); t.require(type(term_id) is int and term_id>0,'Неверный вариант')
    t.require(isinstance(p['text'],str) and len(p['text'])<=20000,'Комментарий должен быть текстом до 20000 символов')
    t.require(isinstance(p['version'],str) and (p['revision'] is None or type(p['revision']) is int),'Нужны версия и редакция')
    current=next((x for x in search(db,root,{'date':p['date']})['items'] if x['term_id']==term_id),None)
    t.require(current is not None and current['version']==p['version'],'Данные изменились или предложение снято; обновите список')
    t.require(current['client_comment']['revision']==p['revision'],'Комментарий изменился; обновите список')
    text=p['text'] if p['text'].strip() else ''
    if text!=current['client_comment']['text']:
        t.hist(db,'client_comment',term_id,'save',{'date':p['date'],'version':p['version'],'text':text,'previous_revision':p['revision']})
    return {'term_id':term_id,'version':current['version'],**client_comment(db,term_id,p['date'])}

def save_card(db,root,term_id,p):
    """Save changed card sections in the caller's single write transaction."""
    t.require(db.in_transaction,'Сохранение карточки требует транзакцию записи')
    t.require(set(p)=={'date','version','public_revision','comment_revision','review_revision','changes'},'Неверные поля карточки')
    t.valid_date(p['date'])
    t.require(type(term_id) is int and term_id>0 and isinstance(p['version'],str),'Нужна версия предложения')
    for key in ('public_revision','comment_revision','review_revision'):
        t.require(p[key] is None or type(p[key]) is int,'Нужны ревизии карточки')
    changes=p['changes']
    t.require(isinstance(changes,dict) and changes and set(changes)<={'client_fields','vat_condition','classification','client_comment','review'},'Неверные изменения карточки')
    item=next((x for x in search(db,root,{'date':p['date']})['items'] if x['term_id']==term_id),None)
    t.require(item is not None and item['version']==p['version'],'Данные изменились или предложение снято; обновите список')
    t.require(item['public_text']['revision']==p['public_revision'],'Текст изменился; обновите список')
    t.require(item['client_comment']['revision']==p['comment_revision'],'Комментарий изменился; обновите список')
    t.require(item['operator_review']['revision']==p['review_revision'],'Решение изменилось; обновите карточку')
    date=p['date']
    if 'client_fields' in changes:
        fields=changes['client_fields']
        t.require(isinstance(fields,dict),'Неверные изменяемые поля')
        item=edit_client_fields(db,root,term_id,{'date':date,'version':item['version'],'revision':item['public_text']['revision'],'fields':dict(fields)})['item']
    if 'vat_condition' in changes:
        item=classify_vat(db,root,term_id,{'date':date,'version':item['version'],'vat_condition':changes['vat_condition']})['item']
    if 'classification' in changes:
        classification=changes['classification']
        t.require(isinstance(classification,dict) and set(classification)=={'groups','color','groups_changed','color_changed'},'Неверные поля классификации')
        item=classify_term(db,root,term_id,{'date':date,'version':item['version'],**classification})['item']
    if 'client_comment' in changes:
        save_client_comment(db,root,term_id,{'date':date,'version':item['version'],'revision':item['client_comment']['revision'],'text':changes['client_comment']})
        item=next(x for x in search(db,root,{'date':date})['items'] if x['term_id']==term_id)
    if 'review' in changes:
        set_operator_review(db,root,term_id,{'date':date,'version':item['version'],'revision':item['operator_review']['revision'],'accepted':changes['review']})
        item=next(x for x in search(db,root,{'date':date})['items'] if x['term_id']==term_id)
    return {'item':item}

def client_vat_projection(text, origin, header):
    """Combine only the exact standalone VAT fields; leave free text untouched."""
    lines=text.splitlines(keepends=True)
    company=[]; offer=[]
    company_values={'не указан','на согласовании','С НДС','Без НДС',
                    *(f'{n}%' for n in (0,5,7,10,20,22)),
                    *(f'НДС {n}%' for n in (0,5,7,10,20,22))}
    for i,line in enumerate(lines):
        raw=line.rstrip('\r\n')
        for prefix in ('НДС компании: ','НДС: '):
            if raw.startswith(prefix) and raw[len(prefix):] in company_values:
                company.append((i,raw[len(prefix):])); break
        if raw.startswith('НДС предложения: ') and raw[len('НДС предложения: '):] in VAT_VALUES:
            offer.append((i,raw[len('НДС предложения: '):]))
    if len(company)!=1 or len(offer)!=1: return text
    ci,cv=company[0]; oi,ov=offer[0]
    if not lines or lines[0].rstrip('\r\n')!=header or ci!=1 or (origin=='auto' and oi!=2) or (origin=='classified' and oi!=len(lines)-1): return text
    if origin not in ('auto','classified'): return text
    # Preserve distinct scopes when values differ; only the display drops a repeated label.
    display=lambda v: v[4:] if re.fullmatch(r'НДС (?:0|5|7|10|20|22)%',v) else v
    cvalue=display(cv); ovalue=display(ov)
    value=cvalue if ov=='не указан' else ovalue if cv=='не указан' or cvalue.casefold()==ovalue.casefold() else f'компании: {cvalue}; предложения: {ovalue}'
    ending=lines[ci][len(lines[ci].rstrip('\r\n')):]
    lines[ci]='НДС: '+value+ending
    del lines[oi]
    return ''.join(lines)

def compose_client_text(public,comment):
    text=public.get('preview',public['text']); value=comment['text']
    if not value: return text
    line='Комментарий: '+value
    lines=text.splitlines(); comment_lines=line.splitlines()
    headers=[i for i,s in enumerate(lines) if s.strip().startswith('Комментарий:')]
    if headers:
        start=headers[0]
        t.require(lines[start:]==comment_lines,'В тексте уже есть другой комментарий; проверьте его вручную')
        return text
    return text+('' if text.endswith(('\n','\r','\x0b','\x0c','\x85','\u2028','\u2029')) else '\n')+line

def public_text_origin(db,term_id,revision,data):
    previous=data; cursor=revision
    while previous.get('reason') in ('document_upload','unchanged_text') and type(previous.get('previous_revision')) is int and previous['previous_revision']<cursor:
        cursor=previous['previous_revision']
        prior=db.execute("SELECT data FROM history WHERE id=? AND entity='public_text' AND entity_id=? AND action='save'",(cursor,term_id)).fetchone()
        if not prior: break
        prior=json.loads(prior['data'])
        if prior.get('text')!=data['text']: break
        previous=prior
    return previous

def public_text(db, item):
    """Latest operator text lives in the existing append-only history, outside the data hash."""
    row=db.execute("SELECT id,data FROM history WHERE entity='public_text' AND entity_id=? AND action='save' ORDER BY id DESC LIMIT 1",(item['term_id'],)).fetchone()
    auto,warnings=render_public(db,item)
    header=f"{item['company']} — ИНН {item['inn'] or 'не указан'}"
    if not row: return {'revision':None,'text':auto,'preview':client_vat_projection(auto,'auto',header),'stale':False,'warnings':warnings,'generated_signal':True}
    data=json.loads(row['data']); stale=data['version']!=item['version']
    origin=None; previous=public_text_origin(db,item['term_id'],row['id'],data)
    if previous.get('reason')=='vat_classification' and previous.get('added_line') is True: origin='classified'
    if previous.get('reason')=='auto_generated': origin='auto'
    if previous.get('reason') in ('vat_classification','client_fields','color_classification') and previous.get('auto_fields') is True: origin='auto'
    if previous.get('reason') in ('client_fields','color_classification') and previous.get('vat_added_line') is True: origin='classified'
    if stale and db.execute("SELECT 1 FROM history WHERE entity='term' AND entity_id=? AND action='classification' AND id>? AND json_extract(data,'$.field')='vat_classification' LIMIT 1",(item['term_id'],row['id'])).fetchone():
        warnings.append('Условие НДС предложения изменено; проверьте сохранённый текст и при необходимости исправьте его вручную.')
    if stale and db.execute("SELECT 1 FROM history WHERE entity='client_fields' AND entity_id=? AND action='edit' AND id>? LIMIT 1",(item['term_id'],row['id'])).fetchone():
        warnings.append('Изменены поля для клиента; проверьте сохранённый текст и исправьте его вручную.')
    preview=client_vat_projection(data['text'],origin,header)
    if preview==data['text'] and re.search(r'(?m)^НДС(?: компании)?: ',preview) and re.search(r'(?m)^НДС предложения: ',preview):
        warnings.append('Проверьте строки НДС в сохранённом тексте вручную: их происхождение неоднозначно.')
    return {'revision':row['id'],'text':data['text'],'preview':preview,'stale':stale,'warnings':warnings,'generated_signal':origin=='auto'}

SAFE_SUBJECTS={'любые','стройматериалы, смр','грузовые запчасти','строительные материалы','щебень','аккумуляторные батареи','транспортные услуги','добавки к бетону','запчасти, комплектующие, шины','фанера','пиломатериалы','перевалка груза','запчасти или оборудование'}

def safe_subject(db,x):
    """A small controlled vocabulary plus evidence; never clean free text into a subject."""
    inv=x.get('invoice') or {}; term=x['terms']
    source=t.row(db,'messages',x['message_id'])['text'].casefold()
    category=term.get('category') if not inv else None; provenance=term.get('field_provenance') or {}
    if isinstance(category,str) and category.casefold() in SAFE_SUBJECTS:
        proof=provenance.get('category') or {}; quote=proof.get('quote') if isinstance(proof,dict) else None
        if category.casefold() in source or (proof.get('message_id')==x['message_id'] and isinstance(quote,str) and quote.casefold()==category.casefold() and quote.casefold() in source): return category
    if inv and any(not isinstance(d,str) or any(v in d.casefold() for v in ('предмет','номенклатур','применимост')) for d in inv.get('discrepancies') or []): return None
    subject=inv.get('subject'); proof=(inv.get('field_provenance') or {}).get('subject') or {}
    if isinstance(subject,str) and subject.casefold() in SAFE_SUBJECTS and isinstance(proof,dict):
        op=proof.get('operator_text'); doc_id=proof.get('document_id')
        if isinstance(op,str) and subject.casefold() in op.casefold() and any(d['id']==doc_id and d['classification']=='invoice' and d['invoice_id']==inv.get('id') and d['intact'] for d in x['documents']): return subject
    return None

def amount_conflict(inv):
    for item in inv.get('discrepancies') or []:
        if not isinstance(item,str): return True
        text=item.casefold()
        if text == 'строительная ткань: 4000 × 197,80 = 791 200,00 ₽, напечатанная сумма строки 791 194,40 ₽; разница 5,60 ₽. сумма напечатанных строк соответствует итогу.': continue
        if any(x in text for x in ('сумм','итог','ндс','стоимост')): return True
    return False

def render_public(db,x):
    """Only structured, bounded values are safe to put in an automatic client draft."""
    z=x['terms']; inv=x.get('invoice'); v=x['company_vat']; warnings=[]
    lines=[f"{x['company']} — ИНН {x['inn'] or 'не указан'}"]
    vat_value=v['value'] if not v['conflict'] else None
    if vat_value and vat_value.strip().lower() in ('с ндс','без ндс','ндс 20%','ндс 22%','ндс 10%','ндс 7%','ндс 5%','ндс 0%','20%','22%','10%','7%','5%','0%'):
        lines.append('НДС компании: '+vat_value)
    else:
        lines.append('НДС компании: '+('на согласовании' if v['records'] else 'не указан'))
        if v['records']: warnings.append('Проверьте НДС компании и перенесите согласованное значение вручную.')
    condition=x.get('vat_condition') or vat_condition(z)
    if condition['status'] in ('explicit','inferred') and (condition['value']!='Безотчёт' or condition['status']=='explicit'):
        lines.append('НДС предложения: '+condition['value'])
    elif condition['status']=='inferred' and condition['value']=='Безотчёт':
        warnings.append('Проверьте условие безотчёта вручную.')
    elif condition['status']=='conflict':
        warnings.append('Проверьте расхождение условия НДС предложения.')
    if inv:
        lines.append('Оплата счёта')
        if inv.get('discrepancies'): warnings.append('Проверьте расхождения счёта и существенные условия.')
        if amount_conflict(inv):
            lines.append('Сумма счёта: уточняется')
            warnings.append('Проверьте расхождение суммы или НДС счёта.')
        elif inv.get('amount') is not None:
            try:
                amount=Decimal(str(inv['amount']))
                if amount.is_finite() and amount>=0:
                    currency={'RUB':'₽','USD':'USD','EUR':'EUR'}.get(inv.get('currency'))
                    lines.append('Сумма счёта: '+str(amount)+((' '+currency) if currency else ''))
            except (InvalidOperation,ValueError): warnings.append('Проверьте сумму счёта вручную.')
        if inv.get('description'): warnings.append('Перенесите точное платёжное назначение и существенные условия вручную, проверив служебные сведения.')
    price_conflict=bool(inv and any(isinstance(d,str) and 'применимост' in d.casefold() and 'цен' in d.casefold() for d in inv.get('discrepancies') or []))
    fields=x.get('client_fields',{})
    if x['final_rate'] is not None and (z.get('rate_unit') in ('%','percent') or 'final_rate' in fields) and not price_conflict:
        lines.append('Цена: '+str(x['final_rate'])+'%')
    else: lines.append('Цена: на согласовании')
    subject=fields['subject'] if 'subject' in fields else safe_subject(db,x)
    if subject: lines.append(('Предмет: ' if inv else 'Назначение: ')+subject)
    if (not subject and (z.get('category') or (inv or {}).get('subject'))) or z.get('conditions') or z.get('restrictions') or z.get('payment') or z.get('rate_text'):
        warnings.append('Проверьте назначение и существенные условия и перенесите их вручную без служебных сведений.')
    if 'delivery' in fields:
        if fields['delivery'] is not None: lines.append('Выдача: '+fields['delivery'])
    elif z.get('duration_text'):
        warnings.append('Проверьте формулировку срока выдачи и перенесите её вручную.')
    if 'delivery' not in fields and z.get('duration') is not None and z.get('duration_unit') in ('day','hour','week','month'):
        try:
            duration=Decimal(str(z['duration'])); maximum=Decimal(str(z.get('duration_max',z['duration'])))
            if duration.is_finite() and maximum.is_finite() and 0<=duration<=maximum:
                number=str(duration)+(('–'+str(maximum)) if maximum!=duration else '')
                unit={'day':'дн.','hour':'ч.','week':'нед.','month':'мес.'}[z['duration_unit']]
                if z['duration_unit']=='day': unit={'calendar':'календарных дней','working':'рабочих дней'}.get(z.get('duration_day_type'),unit)
                lines.append('Выдача: '+number+' '+unit)
        except (InvalidOperation,ValueError,TypeError): warnings.append('Проверьте срок выдачи вручную.')
    color={'green':'🟢 зелёный','yellow':'🟡 жёлтый','red':'🔴 красный','красный':'🔴 красный','зелёный':'🟢 зелёный','зеленый':'🟢 зелёный','жёлтый':'🟡 жёлтый','желтый':'🟡 жёлтый'}.get(z.get('color') if isinstance(z.get('color'),str) else None)
    if color and (not z.get('color_source') or z.get('color_date')!=x['date']):
        warnings.append('Проверьте актуальность светофора вручную.')
        color=None
    lines.append('Светофор: '+(color or 'не указан'))
    ignored={'category','conditions','restrictions','payment','rate_text','rate','rate_unit','duration','duration_max','duration_unit','duration_day_type','duration_text','color','color_source','color_date','scope_confirmed','review','field_provenance','groups','vat_classification'}
    if any(value is not None for key,value in z.items() if key not in ignored): warnings.append('Проверьте дополнительные условия и перенесите существенные вручную.')
    return '\n'.join(lines),warnings

def edit_client_fields(db,root,term_id,p):
    t.require(set(p)=={'date','version','revision','fields'},'Неверные поля изменения')
    t.valid_date(p['date'])
    t.require(type(term_id) is int and term_id>0 and isinstance(p['version'],str) and (p['revision'] is None or type(p['revision']) is int),'Нужны версия и редакция')
    changes=p['fields']; t.require(isinstance(changes,dict) and changes and set(changes)<={'subject','final_rate','delivery','bik','account'},'Неверные изменяемые поля')
    for key,value in changes.items():
        if value is None: continue
        t.require(isinstance(value,str) and value.strip()==value and bool(value) and len(value)<=1000 and '\n' not in value and '\r' not in value,'Неверное значение поля')
        if key in ('bik','account'):
            t.require(bool(re.fullmatch(r'[0-9]{%d}' % (9 if key=='bik' else 20),value)), 'БИК должен содержать 9 цифр, расчётный счёт — 20 цифр')
        if key=='delivery': t.require(value in ('День в день','Следующий день','Через день','3 - 4 дня'),'Неверный срок выдачи')
        if key=='final_rate':
            t.require(bool(re.fullmatch(r'\d{1,6}(?:[.,]\d{1,6})?',value)),'Цена должна быть неотрицательным числом')
            changes[key]=str(Decimal(value.replace(',','.')))
    current=next((x for x in search(db,root,{'date':p['date']})['items'] if x['term_id']==term_id),None)
    t.require(current is not None and current['version']==p['version'],'Данные изменились или предложение снято; обновите список')
    public=current['public_text']; t.require(public['revision']==p['revision'],'Текст изменился; обновите список')
    before=current['client_fields']; after={**before,**changes}
    if after==before: return {'item':current}
    old_auto,_=render_public(db,current)
    t.hist(db,'client_fields',term_id,'edit',{'date':p['date'],'before':before,'after':after,'changes':changes,'source':'пользователь'})
    item=next(x for x in search(db,root,{'date':p['date']})['items'] if x['term_id']==term_id)
    new_auto,_=render_public(db,item)
    # Replace only an unambiguous exact generated line. Otherwise preserve the
    # complete manual text and leave it stale for explicit review.
    previous=db.execute("SELECT data FROM history WHERE id=? AND entity='public_text' AND entity_id=?",(public['revision'],term_id)).fetchone() if public['revision'] is not None else None
    prior=public_text_origin(db,term_id,public['revision'],json.loads(previous['data'])) if previous else {}
    auto_origin=prior.get('reason')=='auto_generated' or prior.get('auto_fields') is True
    if public['revision'] is not None and not public['stale'] and (auto_origin or set(changes)<={'bik','account'}):
        lines=public['text'].splitlines(keepends=True); old=old_auto.splitlines(); new=new_auto.splitlines(); safe=True
        labels={'subject':('Предмет: ' if current['offer_type']=='invoice' else 'Назначение: '),'final_rate':'Цена: ','delivery':'Выдача: '}
        for key in changes:
            if key in ('bik','account'): continue
            label=labels[key]; old_lines=[line for line in old if line.startswith(label)]; new_lines=[line for line in new if line.startswith(label)]
            matches=[i for i,line in enumerate(lines) if line.rstrip('\r\n').startswith(label)]
            if len(old_lines)!=1 or len(matches)!=1 or lines[matches[0]].rstrip('\r\n')!=old_lines[0] or len(new_lines)>1:
                safe=False; break
            i=matches[0]; ending=lines[i][len(lines[i].rstrip('\r\n')):]
            if new_lines: lines[i]=new_lines[0]+ending
            else: del lines[i]
        replacement=''.join(lines)
        if safe and replacement.strip():
            t.hist(db,'public_text',term_id,'save',{'version':item['version'],'text':replacement,'reason':'unchanged_text' if set(changes)<={'bik','account'} else 'client_fields','previous_revision':public['revision'],'auto_fields':prior.get('reason')=='auto_generated' or prior.get('auto_fields') is True,'vat_added_line':prior.get('added_line') is True or prior.get('vat_added_line') is True})
            item=next(x for x in search(db,root,{'date':p['date']})['items'] if x['term_id']==term_id)
    return {'item':item}

def save_public_text(db,root,term_id,p,origin=None):
    t.require(set(p)=={'date','version','revision','text'},'Неверные поля текста')
    t.valid_date(p['date']); t.require(type(term_id) is int and term_id>0,'Неверный вариант')
    t.require(isinstance(p['text'],str) and bool(p['text'].strip()) and len(p['text'])<=20000,'Нужен непустой текст до 20000 символов')
    t.require(isinstance(p['version'],str) and (p['revision'] is None or type(p['revision']) is int),'Нужны версия и редакция')
    current=next((x for x in search(db,root,{'date':p['date']})['items'] if x['term_id']==term_id),None)
    t.require(current is not None and current['version']==p['version'],'Данные изменились или предложение снято; обновите список')
    t.require(current['public_text']['revision']==p['revision'],'Текст изменился; обновите список')
    entry={'version':p['version'],'text':p['text']}
    if current['public_text']['revision'] is not None and p['text']==current['public_text']['text']:
        entry.update(reason='unchanged_text',previous_revision=current['public_text']['revision'])
    elif current['public_text']['revision'] is None and p['text']==current['public_text']['text']:
        entry['reason']='auto_generated'
    if origin is not None: entry.update(origin)
    t.hist(db,'public_text',term_id,'save',entry)
    latest=public_text(db,current)
    return {'term_id':term_id,'version':current['version'],**latest}

SHARE_ADDRESS_WARNING='Отмеченные файлы не включены в текст: публичный адрес файлов не настроен.'

def share_warnings(db,term_id,docs):
    return [SHARE_ADDRESS_WARNING] if not optional_public_base() and any(share_state(db,term_id,d['id']) for d in docs) else []

def common_preview(db,root,p):
    """Check both source data and saved revisions before composing selected variants."""
    date=p.get('date'); t.valid_date(date)
    ids=p.get('term_ids'); versions=p.get('versions'); revisions=p.get('revisions'); comment_revisions=p.get('comment_revisions')
    t.require(isinstance(ids,list) and ids and all(type(i) is int and i>0 for i in ids) and len(ids)==len(set(ids)),'Выберите разные варианты предложений')
    keys={str(i) for i in ids}
    t.require(isinstance(versions,dict) and set(versions)==keys and all(isinstance(v,str) for v in versions.values()),'Версии выбранных предложений обязательны')
    t.require(isinstance(revisions,dict) and set(revisions)==keys and all(v is None or type(v) is int for v in revisions.values()),'Редакции выбранных текстов обязательны')
    if comment_revisions is not None:
        t.require(isinstance(comment_revisions,dict) and set(comment_revisions)==keys and all(v is None or type(v) is int for v in comment_revisions.values()),'Редакции комментариев обязательны')
    current={x['term_id']:x for x in search(db,root,{'date':date})['items']}; selected=[]; warnings=[]
    for i in ids:
        x=current.get(i)
        t.require(x is not None and versions[str(i)]==x['version'],'Данные изменились или предложение снято; обновите список')
        public=x['public_text']
        t.require(revisions[str(i)]==public['revision'],'Текст изменился; обновите список')
        t.require((comment_revisions or {}).get(str(i))==x['client_comment']['revision'],'Комментарий изменился; обновите список')
        t.require(not public['stale'],'Сохранённый текст устарел; проверьте и сохраните его заново')
        selected.append(compose_client_text(public,x['client_comment'])+client_document_links(db,x['term_id'],x['documents'])); warnings.extend({'term_id':i,'reason':w} for w in public['warnings']+share_warnings(db,i,x['documents']))
    return {'text':'\n\n──────────────\n\n'.join(selected),'term_ids':ids,'warnings':warnings}

def render(items):
    lines=[]; grouped={}
    for x in items:
        z=x['terms']; v=x['company_vat']['value'] or 'не указан'
        s=[f"{x['company']} — ИНН {x['inn']}",f'НДС: {v}',f"Назначение: {z['category']}"]
        if x.get('invoice'):
            inv=x['invoice']; s.append('Оплата счёта')
            if inv.get('amount') is not None: s.append('Сумма счёта: '+str(inv['amount'])+(' '+str(inv['currency']) if inv.get('currency') else ' (валюта не указана)'))
        label='Цена'+(' (НДС: '+str(z['vat'])+')' if z.get('vat') else '')+(' '+str(z['payment']) if z.get('payment') else '')
        s.append(f"{label}: {x['final_rate']}%")
        if z.get('rate_type'): s.append('Тип ставки: '+str(z['rate_type']))
        if z.get('rate_basis'): s.append('Основание ставки: '+str(z['rate_basis']))
        if z.get('duration_text'): s.append('Выдача: '+str(z['duration_text']))
        elif z.get('duration') is not None:
            unit={'day':'дн.','hour':'ч.','week':'нед.','month':'мес.'}.get(z.get('duration_unit'),z.get('duration_unit',''))
            if z.get('duration_unit')=='day' and z.get('duration_day_type') in ('calendar','working'): unit={'calendar':'календарных дней','working':'рабочих дней'}[z['duration_day_type']]
            s.append(f"Выдача: {z['duration']} {unit}")
        color={'green':'🟢 зелёный','yellow':'🟡 жёлтый','red':'🔴 красный','красный':'🔴 красный','зелёный':'🟢 зелёный','зеленый':'🟢 зелёный','жёлтый':'🟡 жёлтый','желтый':'🟡 жёлтый'}.get(z.get('color'),'не указан')
        s.append('Светофор: '+color)
        for key,label in [('conditions','Условия'),('restrictions','Ограничение'),('amount','Сумма'),('minimum','Минимум'),('currency','Валюта')]:
            if z.get(key) is not None: s.append(f'{label}: {z[key]}')
        if (z.get('amount') is not None or z.get('minimum') is not None) and z.get('currency') is None: s.append('Валюта: не указана')
        grouped.setdefault((x['company_id'],x.get('invoice',{}).get('id') if x.get('invoice') else None),[]).append(s)
    for variants in grouped.values():
        first=variants[0]; text='\n'.join(first)
        for variant in variants[1:]: text+='\n\n'+ '\n'.join(variant[2:])
        lines.append(text)
    return '\n\n──────────────\n\n'.join(lines)

def prepare(db,root,p):
    chat=p.get('chat_id'); date=p.get('date'); ids=p.get('task_ids'); versions=p.get('versions')
    t.require(isinstance(ids,list) and ids and len(ids)==len(set(ids)),'Выберите задачи'); t.require(isinstance(versions,dict),'Версии задач обязательны')
    items=queue(db,root,chat,date); byid={x['id']:x for x in items}; selected=[]
    for i in ids:
        x=byid.get(i); t.require(x and x['status']=='pending' and versions.get(str(i))==x['version'],'Данные изменились; обновите задачи')
        for previous in selected:
            if previous['company_id'] in t.related_company_ids(db,x) and (previous.get('invoice') or {}).get('id')==(x.get('invoice') or {}).get('id'):
                old_offer=t.row(db,'terms',previous['term_id'])['offer_id']; new_offer=t.row(db,'terms',x['term_id'])['offer_id']
                t.require(old_offer==new_offer,'Выберите один источник для компании в блоке')
        x['share_snapshot']={str(d['id']):share_state(db,x['term_id'],d['id']) for d in x['documents'] if share_state(db,x['term_id'],d['id'])}
        x['share_base_snapshot']=optional_public_base() if x['share_snapshot'] else None
        selected.append(x)
    public={x['term_id']:x for x in search(db,root,{'date':date})['items']}; pieces=[]; warnings=[]
    for x in selected:
        current=public.get(x['term_id']); t.require(current is not None,'Предложение снято; обновите задачи')
        draft=current['public_text']; t.require(not draft['stale'],'Сохранённый текст устарел; проверьте и сохраните его заново')
        x['public_snapshot']={'version':current['version'],'revision':draft['revision'],'comment_revision':current['client_comment']['revision']}
        pieces.append(compose_client_text(draft,current['client_comment'])+client_document_links(db,x['term_id'],x['documents'])); warnings.extend({'term_id':x['term_id'],'reason':w} for w in draft['warnings']+share_warnings(db,x['term_id'],x['documents']))
    docs=list({d['id']:d for x in selected for d in x['documents'] if d['ready']}.values()); text='\n\n──────────────\n\n'.join(pieces); snapshot=t.canonical(selected); version=t.sha(snapshot.encode())
    bid=db.execute('INSERT INTO blocks(chat_id,date,items,documents,plan_data,plan_hash,text,created_at) VALUES(?,?,?,?,?,?,?,?)',(chat,date,snapshot,t.canonical(docs),snapshot,version,text,t.now())).lastrowid
    db.execute('INSERT INTO web_blocks VALUES(?,?)',(bid,version)); t.hist(db,'block',bid,'draft',{'template_version':'1','plan_hash':version})
    return {'id':bid,'text':text,'task_ids':ids,'documents':docs,'version':version,'stale':False,'warnings':warnings}

def block(db,root,bid):
    b=t.row(db,'blocks',bid); ver=db.execute('SELECT version FROM web_blocks WHERE block_id=?',(bid,)).fetchone(); t.require(ver,'Блок не относится к веб-приложению')
    items=json.loads(b['items']); current={x['id']:x for x in queue(db,root,b['chat_id'],b['date'],False)}
    stale=any(x['id'] not in current or current[x['id']]['version']!=x['version'] for x in items)
    stale=stale or any(x.get('share_snapshot') != {str(d['id']):share_state(db,x['term_id'],d['id']) for d in current[x['id']]['documents'] if share_state(db,x['term_id'],d['id'])} for x in items if 'share_snapshot' in x and x['id'] in current)
    stale=stale or any(x.get('share_base_snapshot') != optional_public_base() for x in items if x.get('share_snapshot') and 'share_base_snapshot' in x)
    if any('public_snapshot' in x for x in items):
        public={x['term_id']:x for x in search(db,root,{'date':b['date']})['items']}
        stale=stale or any(x['term_id'] not in public or public[x['term_id']]['version']!=x['public_snapshot']['version'] or public[x['term_id']]['public_text']['revision']!=x['public_snapshot']['revision'] or public[x['term_id']]['client_comment']['revision']!=x['public_snapshot'].get('comment_revision') or public[x['term_id']]['public_text']['stale'] for x in items if 'public_snapshot' in x)
    return {'id':bid,'chat_id':b['chat_id'],'chat':t.row(db,'chats',b['chat_id'])['name'],'date':b['date'],'items':items,'text':b['text'],'task_ids':[x['id'] for x in items],'documents':json.loads(b['documents']),'document_candidates':list({d['id']:d for x in items for d in documents(db,root,x['company_id'],(x.get('invoice') or {}).get('id'),False)}.values()),'version':ver[0],'stale':stale,'warnings':[{'term_id':x['term_id'],'reason':SHARE_ADDRESS_WARNING} for x in items if x.get('share_snapshot') and not x.get('share_base_snapshot')]}

def confirm(db,root,bid,p):
    key=p.get('operation_id'); t.require(isinstance(key,str) and 8<=len(key)<=200,'Нужен уникальный идентификатор операции')
    digest=t.sha(t.canonical({'block_id':bid,**p}).encode()); existing=db.execute('SELECT * FROM operations WHERE key=?',(key,)).fetchone()
    if existing:
        t.require(existing['payload_hash']==digest,'Идентификатор операции использован с другими данными'); return json.loads(existing['result'])
    b=t.row(db,'blocks',bid); snap=block(db,root,bid); t.require(p.get('version')==snap['version'],'Версия блока изменилась')
    ids=p.get('task_ids',[]); docs=p.get('document_ids',[]); t.require(isinstance(ids,list) and isinstance(docs,list) and (ids or docs) and len(ids)==len(set(ids)) and len(docs)==len(set(docs)),'Выберите фактически отправленные позиции или документы')
    items={x['id']:x for x in json.loads(b['items'])}; t.require(all(i in items for i in ids),'Задача не входит в блок')
    candidates={x['id']:x for x in snap['document_candidates']}; snapshot_ids={x['id'] for x in snap['documents']}
    t.require(all(d in candidates for d in docs),'Документ не зарегистрирован за компанией блока')
    exceptional=[d for d in docs if d not in snapshot_ids or not candidates[d]['ready']]
    if exceptional:
        t.require(p.get('actual_document_confirmed') is True and isinstance(p.get('actual_document_reason'),str) and p['actual_document_reason'].strip(),'Для непроверенного или нового документа подтвердите фактическую отправку и укажите причину')
    repeated=any(sent(db,b['chat_id'],items[i]['term_id']) for i in ids) or any(db.execute('SELECT 1 FROM sends s JOIN blocks b ON b.id=s.block_id WHERE b.chat_id=? AND s.document_id=? AND NOT EXISTS (SELECT 1 FROM corrections c WHERE c.send_id=s.id AND c.cancels=1)',(b['chat_id'],d)).fetchone() for d in docs)
    t.require(not repeated or p.get('repeat') is True,'Уже отправлено; обновите состояние')
    t.require(not snap['stale'] or p.get('stale_send_confirmed') is True or repeated and p.get('repeat') is True,'Блок изменился; подтвердите фактическую отправку устаревшего блока')
    actual=p.get('actual_text'); t.require(actual is None or isinstance(actual,str),'Неверный фактический текст')
    actual_time=p.get('actual_time'); t.require(actual_time is None or isinstance(actual_time,str) and bool(actual_time.strip()),'Неверное время отправки')
    ah=t.sha(actual.encode()) if actual is not None else None; send_ids=[]
    for tid in [items[i]['term_id'] for i in ids]: send_ids.append(db.execute('INSERT INTO sends(block_id,term_id,actual_text,actual_hash,recorded_at,actual_time) VALUES(?,?,?,?,?,?)',(bid,tid,actual,ah,t.now(),actual_time)).lastrowid)
    for did in docs: send_ids.append(db.execute('INSERT INTO sends(block_id,document_id,actual_text,actual_hash,recorded_at,actual_time) VALUES(?,?,?,?,?,?)',(bid,did,actual,ah,t.now(),actual_time)).lastrowid)
    warnings=([ 'Повторная фактическая отправка' ] if repeated else []) + ([ 'Фактическая отправка устаревшего блока' ] if snap['stale'] else []) + ([ 'Зафиксирован фактически отправленный новый или непроверенный документ; его пригодность не подтверждена' ] if exceptional else [])
    warning='; '.join(warnings) if warnings else None
    result={'id':send_ids[0],'send_ids':send_ids,'warning':warning}; t.hist(db,'block',bid,'actual_send',{'send_ids':send_ids,'warning':warning,'actual_time':actual_time,'exceptional_document_ids':exceptional,'actual_document_reason':p.get('actual_document_reason') if exceptional else None}); db.execute('INSERT INTO operations VALUES(?,?,?)',(key,digest,t.canonical(result))); return result
