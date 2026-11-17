#!/usr/bin/env python3
"""Local registry CLI. JSON input on stdin, JSON result on stdout."""
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import uuid
from decimal import Decimal, InvalidOperation

SCHEMA = '''
CREATE TABLE IF NOT EXISTS chats(id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);
CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY, chat_id INTEGER NOT NULL REFERENCES chats(id), date TEXT NOT NULL, time TEXT, author TEXT, source_id TEXT, version INTEGER NOT NULL, text TEXT NOT NULL, path TEXT NOT NULL UNIQUE, sha256 TEXT NOT NULL, source_file TEXT, source_heading TEXT, source_copy_path TEXT, source_copy_sha256 TEXT, reply_block_id INTEGER REFERENCES blocks(id), registered_at TEXT NOT NULL, UNIQUE(chat_id,source_id,version));
CREATE TABLE IF NOT EXISTS companies(id INTEGER PRIMARY KEY, name TEXT NOT NULL, inn TEXT, inn_status TEXT NOT NULL CHECK(inn_status IN ('verified','missing','unverified','conflict')));
CREATE UNIQUE INDEX IF NOT EXISTS unique_verified_inn ON companies(inn) WHERE inn_status='verified';
CREATE TABLE IF NOT EXISTS offers(id INTEGER PRIMARY KEY, company_id INTEGER NOT NULL REFERENCES companies(id), message_id INTEGER NOT NULL REFERENCES messages(id));
CREATE TABLE IF NOT EXISTS terms(id INTEGER PRIMARY KEY, offer_id INTEGER NOT NULL REFERENCES offers(id), data TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)), previous_id INTEGER REFERENCES terms(id));
CREATE TABLE IF NOT EXISTS requests(id INTEGER PRIMARY KEY, message_id INTEGER NOT NULL REFERENCES messages(id), data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS documents(id INTEGER PRIMARY KEY, message_id INTEGER NOT NULL REFERENCES messages(id), company_id INTEGER NOT NULL REFERENCES companies(id), name TEXT NOT NULL, path TEXT NOT NULL UNIQUE, sha256 TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'unverified');
CREATE TABLE IF NOT EXISTS blocks(id INTEGER PRIMARY KEY, chat_id INTEGER NOT NULL REFERENCES chats(id), date TEXT NOT NULL, items TEXT NOT NULL, documents TEXT NOT NULL, plan_data TEXT NOT NULL, plan_hash TEXT NOT NULL, text TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sends(id INTEGER PRIMARY KEY, block_id INTEGER NOT NULL REFERENCES blocks(id), term_id INTEGER REFERENCES terms(id), document_id INTEGER REFERENCES documents(id), actual_text TEXT, actual_hash TEXT, recorded_at TEXT NOT NULL, CHECK((term_id IS NULL) != (document_id IS NULL)));
CREATE TABLE IF NOT EXISTS coverage(id INTEGER PRIMARY KEY, chat_id INTEGER NOT NULL REFERENCES chats(id), data TEXT NOT NULL, recorded_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS history(id INTEGER PRIMARY KEY, entity TEXT NOT NULL, entity_id INTEGER, action TEXT NOT NULL, data TEXT NOT NULL, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS risk_decisions(id INTEGER PRIMARY KEY, chat_id INTEGER NOT NULL REFERENCES chats(id), date TEXT NOT NULL, term_id INTEGER NOT NULL REFERENCES terms(id), risk TEXT NOT NULL, state_hash TEXT NOT NULL, reason TEXT NOT NULL, at TEXT NOT NULL);
PRAGMA user_version=1;
'''

def now(): return dt.datetime.now().astimezone().isoformat(timespec='seconds')
def canonical(x): return json.dumps(x, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
def sha(b): return hashlib.sha256(b).hexdigest()
def require(cond, msg):
    if not cond: raise ValueError(msg)
def valid_date(x):
    require(isinstance(x,str), 'date must be YYYY-MM-DD')
    try: require(dt.date.fromisoformat(x).isoformat()==x, 'date must be YYYY-MM-DD')
    except ValueError: raise ValueError('date must be YYYY-MM-DD')
def valid_inn(s):
    if not isinstance(s,str) or not s.isascii() or not s.isdigit(): return False
    n=list(map(int,s))
    if len(n)==10: return sum(a*b for a,b in zip(n,[2,4,10,3,5,9,4,6,8]))%11%10==n[9]
    if len(n)==12: return sum(a*b for a,b in zip(n,[7,2,4,10,3,5,9,4,6,8]))%11%10==n[10] and sum(a*b for a,b in zip(n,[3,7,2,4,10,3,5,9,4,6,8]))%11%10==n[11]
    return False

def safe(root, rel):
    p=(root / rel).resolve()
    require(p.is_relative_to(root), 'path outside project root')
    return p

def save_file(root, directory, suffix, data):
    folder=safe(root, directory); folder.mkdir(parents=True,exist_ok=True)
    dest=folder/(uuid.uuid4().hex+suffix)
    fd,tmp=tempfile.mkstemp(dir=folder, prefix='.pending-')
    try:
        with os.fdopen(fd,'wb') as f: f.write(data); f.flush(); os.fsync(f.fileno())
        os.replace(tmp,dest)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)
    return str(dest.relative_to(root))

def row(db, table, id):
    require(isinstance(id,int) and not isinstance(id,bool), f'{table} id required')
    r=db.execute(f'SELECT * FROM {table} WHERE id=?',(id,)).fetchone()
    require(r is not None, f'{table} {id} not found')
    return r

def registered_files(db):
    files=[(r['path'],r['sha256']) for table in ('messages','documents') for r in db.execute(f'SELECT path,sha256 FROM {table}')] + [(r['source_copy_path'],r['source_copy_sha256']) for r in db.execute('SELECT source_copy_path,source_copy_sha256 FROM messages WHERE source_copy_path IS NOT NULL')]
    if db.execute('PRAGMA user_version').fetchone()[0]>=2:
        for record in db.execute("SELECT data FROM history WHERE entity='document_issue' AND action='open'"):
            issue=json.loads(record['data'])
            if issue.get('path'): files.append((issue['path'],issue['sha256']))
    return files

def hist(db, entity, id, action, data): db.execute('INSERT INTO history(entity,entity_id,action,data,at) VALUES(?,?,?,?,?)',(entity,id,action,canonical(data),now()))
def context_hash(db, exclude_block=None):
    state={table:[list(r) for r in db.execute(f'SELECT * FROM {table} ORDER BY id')] for table in ('messages','companies','offers','terms','requests','documents')}
    state['sends']=[list(r) for r in db.execute('SELECT * FROM sends WHERE block_id IS NOT ? ORDER BY id',(exclude_block,))]
    if db.execute('PRAGMA user_version').fetchone()[0]>=2:
        for table in ('pricing','company_vat','corrections'):
            state[table]=[list(r) for r in db.execute(f'SELECT * FROM {table} ORDER BY id')]
    return sha(canonical(state).encode())
def normalize_term(t):
    require(isinstance(t,dict),'term must be an object')
    t=dict(t)
    require(isinstance(t.get('category'),str) and t['category'].strip(),'category required')
    if t.get('rate') is not None:
        try:
            v=Decimal(str(t['rate']).replace(',','.')); require(v.is_finite() and v>=0,'invalid rate'); t['rate']=str(v)
        except (InvalidOperation, ValueError): raise ValueError('invalid rate')
    if t.get('duration') is not None:
        require(isinstance(t['duration'],(int,float)) and not isinstance(t['duration'],bool) and t['duration']>=0,'invalid duration')
        require(t.get('duration_unit') in ('day','hour','week','month'), 'duration unit required')
    require(t.get('color') in (None,'green','yellow','зелёный','зеленый','жёлтый','желтый'), 'invalid color')
    require(isinstance(t.get('scope_confirmed',False),bool) and isinstance(t.get('review',False),bool),'invalid review flags')
    return t

def validate_provenance(db, root, term):
    provenance=term.get('field_provenance', {})
    require(isinstance(provenance,dict),'field_provenance must be an object')
    for field, evidence in provenance.items():
        require(field in term and field != 'field_provenance','provenance field must exist in terms')
        require(isinstance(evidence,dict) and set(evidence) <= {'message_id','quote'},'invalid field provenance')
        message=row(db,'messages',evidence.get('message_id'))
        f=safe(root,message['path']); require(f.is_file() and sha(f.read_bytes())==message['sha256'] and sha(message['text'].encode())==message['sha256'],'provenance source missing or changed')
        if 'quote' in evidence:
            quote=evidence['quote']; require(isinstance(quote,str) and quote.strip() and quote in message['text'],'quote must be an exact substring of source')

def provenance_sources(db, term):
    result={}
    provenance=term.get('field_provenance',{})
    if not isinstance(provenance,dict): return result
    for field, evidence in provenance.items():
        if not isinstance(evidence,dict) or set(evidence)-{'message_id','quote'} or field not in term or field=='field_provenance': continue
        mid=evidence.get('message_id')
        if not isinstance(mid,int) or isinstance(mid,bool): continue
        message=db.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone()
        if not message or ('quote' in evidence and (not isinstance(evidence['quote'],str) or not evidence['quote'].strip() or evidence['quote'] not in message['text'])): continue
        result[field]={**evidence,'source_path':message['path'],'source_chat_id':message['chat_id'],'source_date':message['date'],'attribution':'указано оператором'}
    return result

def invalid_provenance(db, term, root=None):
    if 'field_provenance' not in term: return False
    value=term['field_provenance']
    if not isinstance(value,dict) or len(provenance_sources(db,term))!=len(value): return True
    if root is not None:
        try: validate_provenance(db,root,term)
        except (OSError,ValueError,TypeError,KeyError): return True
    return False

REQUEST_KINDS=('request','mention','negotiation','counteroffer','clarification','withdrawal','ambiguous')

def validate_request(db, message, req, require_kind=False):
    require(isinstance(req,dict) and isinstance(req.get('text'),str) and req['text'].strip(),'request text required')
    require(req.get('kind','request') in REQUEST_KINDS,'invalid request kind')
    if require_kind: require('kind' in req,'reply items require explicit kind')
    block_id=req.get('block_id'); block_id=message['reply_block_id'] if block_id is None else block_id; term_id=req.get('term_id')
    if message['reply_block_id'] is not None and block_id is not None: require(block_id==message['reply_block_id'],'request block differs from reply block')
    if block_id is not None:
        block=row(db,'blocks',block_id); require(block['chat_id']==message['chat_id'],'block belongs to another chat')
    if term_id is not None:
        row(db,'terms',term_id)
        if block_id is not None: require(term_id in {x['term_id'] for x in json.loads(block['items'])},'term not in referenced block')

def readable_fields(value, indent=0, field=None):
    """Render every stored field, including unfamiliar structures and nulls."""
    pad=' ' * indent
    if isinstance(value,dict):
        if not value: return pad+'(пусто)\n'
        labels={'category':'Назначение','rate':'Ставка','duration':'Срок','duration_unit':'Единица срока','vat':'НДС','color':'Светофор','conditions':'Условия','restrictions':'Ограничения','amount':'Сумма','minimum':'Минимум','currency':'Валюта','scope_confirmed':'Область применения подтверждена','review':'Требует проверки','field_provenance':'Происхождение полей','message_id':'ID сообщения','quote':'Цитата','source_path':'Путь к исходнику','source_chat_id':'ID чата-источника','company':'Компания','inn':'ИНН','term_id':'ID варианта','reason':'Причина','reasons':'Причины','terms':'Условия','text':'Текст','kind':'Тип','block_id':'ID блока'}
        return ''.join(pad+'- '+labels.get(key,str(key))+':\n'+readable_fields(item,indent+2,key) for key,item in value.items())
    if isinstance(value,list):
        if not value: return pad+'(пусто)\n'
        return ''.join(pad+'-\n'+readable_fields(item,indent+2) for item in value)
    if value is None: return pad+'неизвестно\n'
    if isinstance(value,bool): return pad+('да' if value else 'нет')+'\n'
    if field=='duration_unit' and isinstance(value,str): value={'day':'дни','hour':'часы','week':'недели','month':'месяцы'}.get(value,value)
    if field=='color' and isinstance(value,str): value={'green':'зелёная','yellow':'жёлтая'}.get(value,value)
    return pad+str(value)+'\n'

def plan(db, chat_id, date, apply_decisions=True, exclude_block=None, root=None):
    row(db,'chats',chat_id); valid_date(date)
    result=[]; context=context_hash(db,exclude_block) if apply_decisions else None
    version2 = db.execute('PRAGMA user_version').fetchone()[0]>=2
    factual = ' AND NOT EXISTS (SELECT 1 FROM corrections corr WHERE corr.send_id=s.id AND corr.cancels=1)' if version2 else ''
    supported = {'category','rate','duration','duration_unit','vat','color','conditions','scope_confirmed','review','amount','minimum','currency','restrictions','field_provenance'}
    if version2: supported |= {'rate_unit','rate_type','rate_basis','rate_text','duration_text','duration_max','duration_day_type','payment','color_source','color_date'}
    records=db.execute('''SELECT t.id term_id,t.data,t.active,o.company_id,o.message_id,c.name,c.inn,c.inn_status,m.chat_id source_chat,m.date,m.path source_path,ch.name source_chat_name FROM terms t JOIN offers o ON o.id=t.offer_id JOIN companies c ON c.id=o.company_id JOIN messages m ON m.id=o.message_id JOIN chats ch ON ch.id=m.chat_id WHERE m.date=? ORDER BY t.id''',(date,)).fetchall()
    for r in records:
        t=json.loads(r['data']); reason='ok'
        own=db.execute('SELECT 1 FROM offers o JOIN messages m ON m.id=o.message_id WHERE o.company_id=? AND m.chat_id=? AND m.date=? LIMIT 1',(r['company_id'],chat_id,date)).fetchone()
        sent=db.execute('SELECT 1 FROM sends s JOIN blocks b ON b.id=s.block_id WHERE s.term_id=? AND b.chat_id=?'+factual,(r['term_id'],chat_id)).fetchone()
        sent_company=db.execute('SELECT 1 FROM sends s JOIN blocks b ON b.id=s.block_id JOIN terms t ON t.id=s.term_id JOIN offers o ON o.id=t.offer_id WHERE o.company_id=? AND b.chat_id=? AND t.offer_id != (SELECT offer_id FROM terms WHERE id=?) AND s.block_id IS NOT ?'+factual+' LIMIT 1',(r['company_id'],chat_id,r['term_id'],exclude_block)).fetchone()
        sent_previous=db.execute('''WITH RECURSIVE ancestors(id,previous_id) AS (SELECT id,previous_id FROM terms WHERE id=? UNION ALL SELECT t.id,t.previous_id FROM terms t JOIN ancestors a ON t.id=a.previous_id) SELECT 1 FROM ancestors a JOIN sends s ON s.term_id=a.id JOIN blocks b ON b.id=s.block_id WHERE b.chat_id=? AND s.block_id IS NOT ?'''+factual+' LIMIT 1',(r['term_id'],chat_id,exclude_block)).fetchone()
        sources=db.execute('SELECT COUNT(DISTINCT m.chat_id) FROM offers o JOIN messages m ON m.id=o.message_id WHERE o.company_id=? AND m.date=?',(r['company_id'],date)).fetchone()[0]
        past=db.execute('SELECT 1 FROM offers o JOIN messages m ON m.id=o.message_id WHERE o.company_id=? AND m.chat_id=? AND m.date<? LIMIT 1',(r['company_id'],chat_id,date)).fetchone()
        suspicious=db.execute('''SELECT 1 FROM offers o JOIN companies c ON c.id=o.company_id JOIN messages m ON m.id=o.message_id
          WHERE m.chat_id=? AND m.date=? AND o.company_id!=? AND (lower(trim(c.name))=lower(trim(?)) OR (c.inn IS NOT NULL AND c.inn=?)) LIMIT 1''',(chat_id,date,r['company_id'],r['name'],r['inn'])).fetchone()
        identity_conflict=db.execute('''SELECT 1 FROM companies c WHERE c.id!=? AND (lower(trim(c.name))=lower(trim(?)) OR (c.inn IS NOT NULL AND c.inn=?)) LIMIT 1''',(r['company_id'],r['name'],r['inn'])).fetchone()
        version_conflict=db.execute('''SELECT 1 FROM messages m1 JOIN messages m2 ON m1.chat_id=m2.chat_id AND m1.source_id=m2.source_id AND m1.id!=m2.id WHERE m1.id=? AND m1.source_id IS NOT NULL AND (m2.version>m1.version OR EXISTS(SELECT 1 FROM offers o JOIN terms t ON t.offer_id=o.id AND t.active=1 WHERE o.message_id=m2.id)) LIMIT 1''',(r['message_id'],)).fetchone()
        mentions=False; negotiation=False
        ancestors={x[0] for x in db.execute('WITH RECURSIVE a(id,previous_id) AS (SELECT id,previous_id FROM terms WHERE id=? UNION ALL SELECT t.id,t.previous_id FROM terms t JOIN a ON t.id=a.previous_id) SELECT id FROM a',(r['term_id'],))}
        for request in db.execute('SELECT q.data,m.reply_block_id FROM requests q JOIN messages m ON m.id=q.message_id WHERE m.chat_id=? AND m.date<=?',(chat_id,date)):
            q=json.loads(request['data']); text=str(q.get('text','')).casefold()
            if (len(r['name'].strip())>=3 and r['name'].casefold() in text) or (r['inn'] and r['inn'] in text): mentions=True
            if q.get('kind') in ('negotiation','counteroffer','clarification','withdrawal','ambiguous'):
                if isinstance(q.get('term_id'),int) and not isinstance(q.get('term_id'),bool) and q['term_id'] in ancestors: negotiation=True
                elif q.get('term_id') is None:
                    block_id=q.get('block_id'); block_id=request['reply_block_id'] if block_id is None else block_id
                    block=db.execute('SELECT items FROM blocks WHERE id=? AND chat_id=?',(block_id,chat_id)).fetchone() if isinstance(block_id,int) and not isinstance(block_id,bool) else None
                    if block and ancestors.intersection(x['term_id'] for x in json.loads(block['items'])): negotiation=True
        risks=[]
        for condition,label in ((not r['active'],'inactive'),(own,'recipient_source_today'),(sent,'already_sent'),(suspicious,'possible_recipient_company_review'),(sent_previous or sent_company,'company_previously_sent_review'),(r['inn_status']!='verified','identity_review'),(identity_conflict,'possible_identity_conflict_review'),(version_conflict,'message_versions_review'),(not t.get('scope_confirmed',False) or t.get('review',False),'terms_review'),(t.get('rate') is None,'rate_unknown'),(set(t)-supported,'unrendered_fields_review'),(sources>1,'multiple_sources_review'),(past,'past_recipient_source_review'),(mentions,'recipient_mention_review'),(negotiation,'negotiation_review'),(invalid_provenance(db,t,root),'invalid_field_provenance_review')):
            if condition: risks.append(label)
        unresolved=[]
        for risk in risks:
            decision=apply_decisions and risk.endswith('_review') and risk not in ('identity_review','terms_review','unrendered_fields_review','message_versions_review','invalid_field_provenance_review') and db.execute('SELECT 1 FROM risk_decisions WHERE chat_id=? AND date=? AND term_id=? AND risk=? AND state_hash=? LIMIT 1',(chat_id,date,r['term_id'],risk,context)).fetchone()
            if not decision: unresolved.append(risk)
        reason=unresolved[0] if unresolved else 'ok'
        result.append({'term_id':r['term_id'],'company_id':r['company_id'],'company':r['name'],'inn':r['inn'],'terms':t,'reason':reason,'reasons':unresolved,'message_id':r['message_id'],'source_path':r['source_path'],'source_chat_id':r['source_chat'],'source_chat':r['source_chat_name'],'inn_status':r['inn_status'],'field_provenance':provenance_sources(db,t)})
    return result

def block_stale(db, b, root):
    before=json.loads(b['plan_data']); current=plan(db,b['chat_id'],b['date'],exclude_block=b['id'],root=root)
    own_sent={r[0] for r in db.execute('SELECT term_id FROM sends WHERE block_id=? AND term_id IS NOT NULL',(b['id'],))}
    old={x['term_id']:x for x in before}
    for item in current:
        if item['term_id'] in own_sent and item['reason']=='already_sent' and old.get(item['term_id'],{}).get('reason')=='ok':
            item['reasons']=[x for x in item['reasons'] if x!='already_sent']; item['reason']=item['reasons'][0] if item['reasons'] else 'ok'
    # Older snapshots predate additional presentation fields; compare the fields they recorded.
    comparable=[{key:item.get(key) for key in old[item['term_id']]} if item['term_id'] in old else item for item in current]
    if canonical(before)!=canonical(comparable): return True
    companies={x['company_id'] for x in json.loads(b['items'])}
    for item in json.loads(b['items']):
        try: validate_provenance(db,root,item['terms'])
        except (OSError,ValueError,TypeError,KeyError): return True
    docs=[dict(r) for r in db.execute('SELECT id,company_id,name,path,status FROM documents ORDER BY id') if r['company_id'] in companies]
    for d in docs:
        record=row(db,'documents',d['id'])
        try:
            f=safe(root,record['path'])
            if not f.is_file() or sha(f.read_bytes())!=record['sha256']: return True
        except (OSError,ValueError): return True
    return canonical(docs)!=canonical(json.loads(b['documents']))

def chat_platform(db, chat_id):
    record=db.execute("SELECT data FROM history WHERE entity='chat' AND entity_id=? AND action='platform' ORDER BY id DESC LIMIT 1",(chat_id,)).fetchone()
    return json.loads(record['data']).get('platform') if record else None

def execute(db, root, command, p):
    require(isinstance(p,dict),'JSON input must be an object')
    if db.execute('PRAGMA user_version').fetchone()[0] == 2:
        import web_core as core
        if command=='chat-platform':
            chat=row(db,'chats',p.get('chat_id')); platform=p.get('platform')
            require(platform in ('telegram','whatsapp'), 'Укажите Telegram или WhatsApp')
            old=chat_platform(db,chat['id'])
            if old != platform: hist(db,'chat',chat['id'],'platform',{'platform':platform,'previous':old,'reason':p.get('reason') or 'Указание пользователя'})
            return {'chat_id':chat['id'],'platform':platform}
        if command in ('draft','confirm','check-draft'):
            raise ValueError('Команда v1 недоступна для v2; используйте web-prepare/web-confirm/web-block')
        if command=='recipient':
            chat=row(db,'chats',p.get('chat_id')); enabled=p.get('enabled'); require(isinstance(enabled,bool),'enabled must be boolean')
            db.execute('UPDATE chats SET recipient=? WHERE id=?',(int(enabled),chat['id'])); hist(db,'chat',chat['id'],'recipient',{'enabled':enabled,'reason':p.get('reason')}); return {'chat_id':chat['id'],'recipient':enabled}
        if command=='pricing':
            reason=p.get('reason'); require(isinstance(reason,str) and reason.strip(),'reason required')
            group='term_ids' in p
            require(not (group and 'term_id' in p),'provide term_id or term_ids, not both')
            ids=p.get('term_ids') if group else [p.get('term_id')]
            require(isinstance(ids,list) and ids and all(isinstance(x,int) and not isinstance(x,bool) for x in ids) and len(ids)==len(set(ids)),'distinct term_ids required')
            require(isinstance(p.get('source'),str) and p['source'].strip(),'pricing confirmation source required')
            terms=[row(db,'terms',i) for i in ids]
            try:
                markup=Decimal(str(p['markup']).replace(',','.')); require(markup.is_finite(),'invalid markup')
                for term in terms:
                    rate=json.loads(term['data']).get('rate'); require(rate is not None and (Decimal(str(rate))+markup).is_finite() and Decimal(str(rate))+markup>=0,'invalid final rate')
            except (KeyError,InvalidOperation,ValueError): raise ValueError('invalid markup or rate')
            rule_id=None
            if group:
                rule_id=db.execute('INSERT INTO history(entity,entity_id,action,data,at) VALUES(?,?,?,?,?)',('pricing_rule',None,'set',canonical({'term_ids':ids,'markup':str(markup),'reason':reason,'source':p['source']}),now())).lastrowid
            pricing_ids=[]
            for term in terms:
                pricing_id=db.execute('INSERT INTO pricing(term_id,markup,reason,recorded_at) VALUES(?,?,?,?)',(term['id'],str(markup),reason,now())).lastrowid
                hist(db,'pricing',pricing_id,'set',{'term_id':term['id'],'markup':str(markup),'reason':reason,'source':p.get('source'),'pricing_rule_id':rule_id})
                pricing_ids.append(pricing_id)
            return {'pricing_rule_id':rule_id,'pricing_ids':pricing_ids} if group else {'pricing_id':pricing_ids[0]}
        if command=='document-issue-resolve':
            issue_id=p.get('issue_id'); issue=row(db,'history',issue_id)
            require(issue['entity']=='document_issue' and issue['action']=='open','document issue not found')
            require(not db.execute("SELECT 1 FROM history WHERE entity='document_issue' AND entity_id=? AND action='resolve'",(issue_id,)).fetchone(),'issue already resolved')
            reason=p.get('reason'); require(isinstance(reason,str) and reason.strip(),'resolution reason required')
            data=json.loads(issue['data']); cid=p.get('company_id',data.get('company_id')); row(db,'companies',cid)
            if p.get('document_id') is not None:
                doc=row(db,'documents',p['document_id']); require(doc['message_id']==data['message_id'] and doc['company_id']==cid,'document does not match issue')
                did=doc['id']
            else:
                path=p.get('path') or (str(safe(root,data['path'])) if data.get('path') else None)
                require(isinstance(path,str) and path,'available file or document_id required')
                saved=safe(root,data['path']) if data.get('path') else None
                if saved is not None and Path(path).expanduser().resolve()==saved:
                    require(saved.is_file(),'saved attachment missing or changed')
                    content=saved.read_bytes(); require(sha(content)==data['sha256'],'saved attachment missing or changed')
                    company=row(db,'companies',cid); folder=f"компании/{company['inn'] if company['inn_status']=='verified' else 'id-'+str(cid)}/документы"
                    rel=save_file(root,folder,saved.suffix,content)
                    did=db.execute('INSERT INTO documents(message_id,company_id,name,path,sha256) VALUES(?,?,?,?,?)',(data['message_id'],cid,data['name'],rel,data['sha256'])).lastrowid
                else:
                    result=execute(db,root,'document',{'message_id':data['message_id'],'company_id':cid,'path':path})
                    require('document_id' in result,'file is still unavailable'); did=result['document_id']
                    db.execute('UPDATE documents SET name=? WHERE id=?',(data['name'],did))
            resolved_doc=row(db,'documents',did); file=safe(root,resolved_doc['path']); require(file.is_file() and sha(file.read_bytes())==resolved_doc['sha256'],'document missing or changed')
            hist(db,'document_issue',issue_id,'resolve',{'document_id':did,'company_id':cid,'reason':reason})
            return {'issue_id':issue_id,'document_id':did,'status':'resolved','document_status':row(db,'documents',did)['status']}
        if command=='company-vat':
            c=row(db,'companies',p.get('company_id')); valid_date(p.get('date'))
            require(all(isinstance(p.get(k),str) and p[k].strip() for k in ('value','source','status')),'value, source and status required')
            require(p['status'] in ('unverified','user_confirmed','document_or_external','conflict'),'invalid VAT confirmation status')
            id=db.execute('INSERT INTO company_vat(company_id,value,source,date,status,recorded_at) VALUES(?,?,?,?,?,?)',(c['id'],p['value'],p['source'],p['date'],p['status'],now())).lastrowid; hist(db,'company_vat',id,'record',dict(p)); return {'company_vat_id':id}
        if command=='correction':
            send=row(db,'sends',p.get('send_id')); require(isinstance(p.get('reason'),str) and p['reason'].strip() and isinstance(p.get('cancels'),bool),'reason and cancels required')
            id=db.execute('INSERT INTO corrections(send_id,reason,cancels,recorded_at) VALUES(?,?,?,?)',(send['id'],p['reason'],int(p['cancels']),now())).lastrowid; hist(db,'send',send['id'],'correction',dict(p)); return {'correction_id':id}
        if command in ('plan','tasks'):
            chat=row(db,'chats',p.get('chat_id')); date=p.get('date',dt.date.today().isoformat())
            return {'items':core.queue(db,root,chat['id'],date),'recipient':bool(chat['recipient']),'coverage_warning':'Проверена только загруженная история; её полнота не предполагается.'}
        if command=='search': return core.search(db,root,p)
        if command=='web-prepare': return core.prepare(db,root,p)
        if command=='web-block': return core.block(db,root,p.get('block_id'))
        if command=='web-confirm': return core.confirm(db,root,p.get('block_id'),p)
    if command=='init': return {'schema_version':db.execute('PRAGMA user_version').fetchone()[0]}
    if command=='chat':
        name=p.get('name'); require(isinstance(name,str) and name.strip(),'name required')
        platform=p.get('platform'); version=db.execute('PRAGMA user_version').fetchone()[0]
        require(platform is None or (version>=2 and platform in ('telegram','whatsapp')),'Укажите Telegram или WhatsApp для схемы v2')
        existing=db.execute('SELECT id FROM chats WHERE name=?',(name.strip(),)).fetchone()
        if existing and platform:
            old=chat_platform(db,existing['id']); require(old in (None,platform),'Чат с таким названием уже зарегистрирован в другом мессенджере; уточните его название')
        cur=db.execute('INSERT OR IGNORE INTO chats(name) VALUES(?)',(name.strip(),)); chat_id=db.execute('SELECT id FROM chats WHERE name=?',(name.strip(),)).fetchone()[0]
        if cur.rowcount and version>=2: hist(db,'chat',chat_id,'create',{'name':name.strip()})
        if platform and chat_platform(db,chat_id)!=platform: hist(db,'chat',chat_id,'platform',{'platform':platform,'previous':None,'reason':'Указание пользователя'})
        return {'chat_id':chat_id,**({'platform':chat_platform(db,chat_id)} if version>=2 else {})}
    if command=='ingest':
        chat=p.get('chat_id'); row(db,'chats',chat); date=p.get('date',dt.date.today().isoformat()); valid_date(date)
        text=p.get('text'); require(isinstance(text,str) and text,'text required'); source=p.get('source_id'); require(source is None or isinstance(source,str) and source,'invalid source_id')
        h=sha(text.encode()); version=1
        if source:
            previous=db.execute('SELECT * FROM messages WHERE chat_id=? AND source_id=? ORDER BY version DESC LIMIT 1',(chat,source)).fetchone()
            if previous:
                if previous['sha256']==h: return {'message_id':previous['id'],'duplicate':True}
                version=previous['version']+1
        dup=bool(db.execute('SELECT 1 FROM messages WHERE chat_id=? AND sha256=?',(chat,h)).fetchone())
        source_file=p.get('source_file'); heading=p.get('source_heading'); require(source_file is None or isinstance(source_file,str) and source_file,'invalid source_file'); require(heading is None or isinstance(heading,str),'invalid source_heading')
        reply=p.get('reply_block_id');
        if reply is not None: require(row(db,'blocks',reply)['chat_id']==chat,'reply block belongs to another chat')
        source_data=None
        if source_file is not None:
            source_path=Path(source_file).expanduser(); source_path=source_path if source_path.is_absolute() else root/source_path; source_path=source_path.resolve(); require(source_path.is_file(),'source_file not found'); source_data=source_path.read_bytes()
        path=save_file(root,f'дни/{date}/входящие/chat-{chat}', '.txt',text.encode())
        source_copy=save_file(root,f'дни/{date}/входящие/chat-{chat}',source_path.suffix,source_data) if source_data is not None else None
        cur=db.execute('INSERT INTO messages(chat_id,date,time,author,source_id,version,text,path,sha256,source_file,source_heading,source_copy_path,source_copy_sha256,reply_block_id,registered_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(chat,date,p.get('time'),p.get('author'),source,version,text,path,h,source_file,heading,source_copy,sha(source_data) if source_data is not None else None,reply,now()))
        hist(db,'message',cur.lastrowid,'ingest',{'path':path,'version':version})
        return {'message_id':cur.lastrowid,'path':path,'source_copy_path':source_copy,'source_file':source_file,'source_heading':heading,'version':version,'warnings':['possible_duplicate'] if dup and not source else []}
    if command=='import':
        message=row(db,'messages',p.get('message_id')); offers=p.get('offers',[]); requests=p.get('requests',[])
        require(isinstance(offers,list) and isinstance(requests,list),'offers and requests must be arrays')
        require(not db.execute('SELECT 1 FROM offers WHERE message_id=? UNION ALL SELECT 1 FROM requests WHERE message_id=? LIMIT 1',(message['id'],message['id'])).fetchone(),'message already imported')
        if message['reply_block_id'] is not None: require(row(db,'blocks',message['reply_block_id'])['chat_id']==message['chat_id'],'reply block belongs to another chat')
        prepared=[]
        for o in offers:
            require(isinstance(o,dict),'offer must be object'); terms=o.get('terms'); require(isinstance(terms,list) and terms,'terms required'); ts=[normalize_term(t) for t in terms]
            require(o.get('kind','offer')=='offer','only offer items can enter offers')
            if message['reply_block_id'] is not None: require(o.get('kind')=='offer','reply offers require explicit kind')
            for t in ts: validate_provenance(db,root,t)
            if 'company_id' in o: row(db,'companies',o['company_id'])
            else:
                c=o.get('company'); require(isinstance(c,dict) and isinstance(c.get('name'),str) and c['name'].strip(),'company name required')
                status=c.get('inn_status','unverified' if c.get('inn') else 'missing'); require(status in ('verified','missing','unverified','conflict'),'invalid inn_status'); inn=c.get('inn'); require(inn is None or isinstance(inn,str),'INN must be string')
                if status=='verified':
                    require(valid_inn(inn),'invalid verified INN')
                    if db.execute('PRAGMA user_version').fetchone()[0]>=2:
                        require(c.get('confirmation_kind') in ('user','document_or_external') and isinstance(c.get('confirmation_source'),str) and c['confirmation_source'].strip(),'confirmation kind and source required')
                if status=='missing': require(not inn,'missing INN must be empty')
            prepared.append((o,ts))
        for req in requests: validate_request(db,message,req,require_kind=message['reply_block_id'] is not None)
        if p.get('preview'): return {'preview':True,'offers':len(offers),'terms':sum(len(x[1]) for x in prepared),'requests':len(requests)}
        company_ids=[]; term_ids=[]; request_ids=[]
        for o,ts in prepared:
            if 'company_id' in o: cid=o['company_id']
            else:
                c=o['company']; inn=c.get('inn'); status=c.get('inn_status','unverified' if inn else 'missing'); existing=db.execute("SELECT id FROM companies WHERE inn=? AND inn_status='verified'",(inn,)).fetchone() if status=='verified' else None
                cid=existing[0] if existing else db.execute('INSERT INTO companies(name,inn,inn_status) VALUES(?,?,?)',(c['name'],inn,status)).lastrowid
                if status=='verified' and db.execute('PRAGMA user_version').fetchone()[0]>=2:
                    hist(db,'company',cid,'identity_decision',{'inn':inn,'inn_status':status,'confirmation_kind':c['confirmation_kind'],'confirmation_source':c['confirmation_source'],'reason':c['confirmation_source']})
            company_ids.append(cid); oid=db.execute('INSERT INTO offers(company_id,message_id) VALUES(?,?)',(cid,message['id'])).lastrowid
            for t in ts:
                tid=db.execute('INSERT INTO terms(offer_id,data) VALUES(?,?)',(oid,canonical(t))).lastrowid; term_ids.append(tid); hist(db,'term',tid,'import',t)
        for req in requests: request_ids.append(db.execute('INSERT INTO requests(message_id,data) VALUES(?,?)',(message['id'],canonical(req))).lastrowid)
        return {'company_ids':company_ids,'term_ids':term_ids,'request_ids':request_ids}
    if command=='document':
        m=row(db,'messages',p.get('message_id')); cid=p.get('company_id'); raw=p.get('path'); name=p.get('name')
        if db.execute('PRAGMA user_version').fetchone()[0]>=2:
            require(raw is None or isinstance(raw,str) and raw.strip(),'invalid document path')
            require(name is None or isinstance(name,str) and name.strip(),'invalid document name')
            if cid is not None: row(db,'companies',cid)
            source=Path(raw).expanduser().resolve() if raw else None
            available=bool(source and source.is_file())
            data=None
            if available:
                try: data=source.read_bytes()
                except OSError: available=False
            if not available or cid is None:
                title=name or (source.name if source else None); require(title,'document name required')
                rel=save_file(root,f"дни/{m['date']}/реквизиты",source.suffix,data) if available else None
                issue={'message_id':m['id'],'company_id':cid,'name':title,'original_path':raw,'path':rel,'sha256':sha(data) if data is not None else None,'file_available':available,'reason':'company_unresolved' if available else 'file_unavailable','reasons':(['file_unavailable'] if not available else [])+(['company_unresolved'] if cid is None else [])}
                issue_id=db.execute('INSERT INTO history(entity,entity_id,action,data,at) VALUES(?,?,?,?,?)',('document_issue',None,'open',canonical(issue),now())).lastrowid
                return {'document_issue_id':issue_id,'status':'review','file_saved':available,'reason':issue['reason']}
        m=row(db,'messages',p.get('message_id')); c=row(db,'companies',cid); source=Path(raw or '').expanduser().resolve(); require(source.is_file(),'document file not found')
        data=source.read_bytes(); folder=f"компании/{c['inn'] if c['inn_status']=='verified' else 'id-'+str(c['id'])}/документы"; rel=save_file(root,folder,source.suffix,data)
        did=db.execute('INSERT INTO documents(message_id,company_id,name,path,sha256) VALUES(?,?,?,?,?)',(m['id'],c['id'],source.name,rel,sha(data))).lastrowid; return {'document_id':did,'path':rel,'status':'unverified'}
    if command in ('plan','draft'):
        chat=p.get('chat_id'); date=p.get('date',dt.date.today().isoformat()); items=plan(db,chat,date,root=root)
        result={'items':items,'coverage_warning':'Проверена только загруженная история; полнота не предполагается.'}
        if command=='draft':
            selected=[x for x in items if x['reason']=='ok']; lines=[]
            exclusions=[x for x in items if x['reason']!='ok']; reason_summary={}
            for item in exclusions:
                for reason in item['reasons']: reason_summary[reason]=reason_summary.get(reason,0)+1
            if not selected:
                result.update({'block_id':None,'items':[],'exclusions':exclusions,'text':'','documents':[],'document_warnings':[],'reason_summary':reason_summary,'warning':'Нет подходящих предложений; черновик не создан.','warning_code':'NO_ELIGIBLE_ITEMS'})
                return result
            for x in selected:
                t=x['terms']; fields=[]
                for key,label in (('vat','НДС'),('color','Светофор'),('amount','Сумма'),('minimum','Минимум'),('currency','Валюта'),('conditions','Условия'),('restrictions','Ограничения')):
                    if t.get(key) is not None and t.get(key)!='':
                        value=t[key]
                        if key=='color': value={'green':'зелёная','yellow':'жёлтая'}.get(value,value)
                        fields.append(f"{label}: {value}")
                lines.append(f"{x['company']} (ИНН {x['inn']}) — {t['category']}, {t['rate']}%" + (f", срок: {t['duration']} { {'day':'дн.','hour':'ч.','week':'нед.','month':'мес.'}.get(t.get('duration_unit'),t.get('duration_unit',''))}" if t.get('duration') is not None else '') + (', '+', '.join(fields) if fields else ''))
            docs=[dict(r) for r in db.execute('SELECT id,company_id,name,path,status FROM documents WHERE company_id IN (SELECT o.company_id FROM offers o JOIN terms t ON t.offer_id=o.id WHERE t.id IN (%s))'%(','.join('?' for _ in selected) or 'NULL'),[x['term_id'] for x in selected])]
            for doc in docs:
                record=row(db,'documents',doc['id']); f=safe(root,record['path']); require(f.is_file() and sha(f.read_bytes())==record['sha256'],'document missing or changed')
            text='\n'.join(lines); ph=sha(canonical(items).encode()); bid=db.execute('INSERT INTO blocks(chat_id,date,items,documents,plan_data,plan_hash,text,created_at) VALUES(?,?,?,?,?,?,?,?)',(chat,date,canonical(selected),canonical(docs),canonical(items),ph,text,now())).lastrowid
            # Re-evaluate immediately before returning.
            require(sha(canonical(plan(db,chat,date,root=root)).encode())==ph,'draft became stale')
            hist(db,'block',bid,'draft',{'template_version':'1','plan_hash':ph})
            result.update({'block_id':bid,'items':selected,'exclusions':exclusions,'reason_summary':reason_summary,'text':text,'documents':[d for d in docs if d['status']=='verified'],'document_warnings':[dict(d, warning='Карточка не проверена и не готова к прикреплению.') for d in docs if d['status']!='verified'],'template_version':'1'})
        return result
    if command=='check-draft':
        b=row(db,'blocks',p.get('block_id')); return {'block_id':b['id'],'stale':block_stale(db,b,root),'items':plan(db,b['chat_id'],b['date'],root=root)}
    if command=='confirm':
        b=row(db,'blocks',p.get('block_id')); stale=block_stale(db,b,root); require(not stale or p.get('stale_send_confirmed') is True,'draft is stale; prepare again or explicitly confirm actual stale send')
        ids=p.get('term_ids',[]); docs=p.get('document_ids',[]); require(isinstance(ids,list) and isinstance(docs,list),'IDs must be arrays'); require(len(ids)==len(set(ids)) and len(docs)==len(set(docs)),'duplicate IDs')
        require(ids or docs,'nothing to confirm')
        allowed={x['term_id']:x['company_id'] for x in json.loads(b['items'])}; require(all(i in allowed for i in ids),'term not in draft')
        require(not any(db.execute('SELECT 1 FROM sends s JOIN blocks b ON b.id=s.block_id WHERE b.chat_id=? AND s.term_id=?',(b['chat_id'],i)).fetchone() for i in ids),'term already confirmed')
        document_warnings=[]; extra_documents=[]
        for d in docs:
            doc=row(db,'documents',d); in_draft=d in {x['id'] for x in json.loads(b['documents'])}
            require(doc['company_id'] in allowed.values(),'document company not in draft')
            if not in_draft:
                require(stale and p.get('stale_send_confirmed') is True and isinstance(p.get('extra_document_reason'),str) and p['extra_document_reason'].strip(),'document not in draft; actual send requires a reason')
                extra_documents.append(d); document_warnings.append(f'Документ {d} не входил в черновик; записан факт отправки.')
            if doc['status']!='verified': document_warnings.append(f'Документ {d} имеет статус {doc["status"]}; записан факт отправки без подтверждения пригодности.')
            require(not db.execute('SELECT 1 FROM sends s JOIN blocks b ON b.id=s.block_id WHERE b.chat_id=? AND s.document_id=?',(b['chat_id'],d)).fetchone(),'document already confirmed')
        actual=p.get('actual_text'); require(actual is None or isinstance(actual,str),'actual_text must be string or null'); ah=sha(actual.encode()) if actual is not None else None
        for tid in ids: db.execute('INSERT INTO sends(block_id,term_id,actual_text,actual_hash,recorded_at) VALUES(?,?,?,?,?)',(b['id'],tid,actual,ah,now()))
        for did in docs: db.execute('INSERT INTO sends(block_id,document_id,actual_text,actual_hash,recorded_at) VALUES(?,?,?,?,?)',(b['id'],did,actual,ah,now()))
        
        if stale or document_warnings: hist(db,'block',b['id'],'confirm_stale_send' if stale else 'confirm_document_warnings',{'term_ids':ids,'document_ids':docs,'warning':'Фактическая отправка устаревшего блока; актуальность не подтверждена.' if stale else None,'extra_document_ids':extra_documents,'extra_document_reason':p.get('extra_document_reason') if extra_documents else None,'document_warnings':document_warnings})
        return {'confirmed_term_ids':ids,'confirmed_document_ids':docs,'actual_text_known':actual is not None,'stale':stale,'warnings':(['Фактическая отправка устаревшего блока; актуальность не подтверждена.'] if stale else [])+document_warnings}
    if command=='search':
        date=p.get('date',dt.date.today().isoformat()); valid_date(date); rows=db.execute('SELECT t.id,t.data,c.name,c.inn,c.inn_status,c.id company_id,m.chat_id,m.date,m.id message_id,m.path source_path,ch.name source_chat FROM terms t JOIN offers o ON o.id=t.offer_id JOIN companies c ON c.id=o.company_id JOIN messages m ON m.id=o.message_id JOIN chats ch ON ch.id=m.chat_id WHERE t.active=1 AND m.date=?',(date,)).fetchall(); results=[]
        search_risks={}
        if rows:
            search_risks={x['term_id']:x['reasons'] for x in plan(db,rows[0]['chat_id'],date,apply_decisions=False,root=root)}
        for r in rows:
            t=json.loads(r['data']); unknown=[]; mismatch=False
            unknown.extend(x for x in search_risks.get(r['id'],[]) if x in ('possible_identity_conflict_review','message_versions_review','multiple_sources_review','unrendered_fields_review','invalid_field_provenance_review'))
            if r['inn_status']!='verified': unknown.append('identity_review')
            if not t.get('scope_confirmed',False) or t.get('review',False): unknown.append('terms_review')
            for key,op in (('min_rate','min'),('max_rate','max')):
                if p.get(key) is not None:
                    try: limit=Decimal(str(p[key])); require(limit.is_finite(),'invalid rate filter')
                    except InvalidOperation: raise ValueError('invalid rate filter')
                    if t.get('rate') is None: unknown.append('rate')
                    elif db.execute('PRAGMA user_version').fetchone()[0]>=2 and (not t.get('rate_type') or not t.get('rate_basis') or not p.get('rate_type') or not p.get('rate_basis') or p.get('rate_type')!=t.get('rate_type') or p.get('rate_basis')!=t.get('rate_basis') or t.get('rate_unit') not in ('%','percent')): unknown.append('rate_comparability')
                    elif (op=='min' and Decimal(t['rate'])<limit) or (op=='max' and Decimal(t['rate'])>limit): mismatch=True
            for key in ('category','color','vat'):
                if p.get(key) is not None:
                    if t.get(key) is None: unknown.append(key)
                    else:
                        wanted=str(p[key]).casefold().replace('ё','е'); value=str(t[key]).casefold().replace('ё','е')
                        if key=='color':
                            colors={'green':'зеленый','зеленая':'зеленый','yellow':'желтый','желтая':'желтый'}; wanted=colors.get(wanted,wanted); value=colors.get(value,value)
                        if key=='category':
                            for alias in ('стромат','стройматериалы'):
                                wanted=wanted.replace(alias,'строительные материалы'); value=value.replace(alias,'строительные материалы')
                        if wanted not in value: mismatch=True
            if p.get('max_duration') is not None:
                if t.get('duration') is None or t.get('duration_unit')!='day': unknown.append('duration')
                elif t['duration']>p['max_duration']: mismatch=True
            if not mismatch: results.append({'term_id':r['id'],'company':r['name'],'inn':r['inn'],'source_chat_id':r['chat_id'],'source_chat':r['source_chat'],'message_id':r['message_id'],'source_path':r['source_path'],'company_id':r['company_id'],'inn_status':r['inn_status'],'terms':t,'match':'possible' if unknown else 'full','unknown':unknown,'field_provenance':provenance_sources(db,t),'documents':[dict(x) for x in db.execute('SELECT id,path,status FROM documents WHERE company_id=(SELECT o.company_id FROM offers o JOIN terms t ON t.offer_id=o.id WHERE t.id=?)',(r['id'],))]})
        return {'items':results}
    if command=='review':
        items=[]
        for r in db.execute('SELECT t.id,t.data,c.id company_id,c.name,c.inn,c.inn_status,m.id message_id,m.path source_path,m.chat_id,ch.name source_chat,m.date FROM terms t JOIN offers o ON o.id=t.offer_id JOIN companies c ON c.id=o.company_id JOIN messages m ON m.id=o.message_id JOIN chats ch ON ch.id=m.chat_id WHERE t.active=1 ORDER BY t.id'):
            t=json.loads(r['data']); reasons=[]
            if r['inn_status']!='verified': reasons.append('identity_review')
            if t.get('review') or not t.get('scope_confirmed'): reasons.append('terms_review')
            if t.get('rate') is None: reasons.append('rate_unknown')
            if db.execute('SELECT COUNT(DISTINCT m.chat_id) FROM offers o JOIN messages m ON m.id=o.message_id WHERE o.company_id=(SELECT o2.company_id FROM offers o2 JOIN terms t2 ON t2.offer_id=o2.id WHERE t2.id=?)',(r['id'],)).fetchone()[0]>1: reasons.append('multiple_sources_review')
            if db.execute('SELECT 1 FROM companies WHERE id!=? AND (lower(trim(name))=lower(trim(?)) OR (inn IS NOT NULL AND inn=?)) LIMIT 1',(r['company_id'],r['name'],r['inn'])).fetchone(): reasons.append('possible_identity_conflict_review')
            if db.execute('''SELECT 1 FROM messages m1 JOIN messages m2 ON m1.chat_id=m2.chat_id AND m1.source_id=m2.source_id AND m1.id!=m2.id WHERE m1.id=? AND m1.source_id IS NOT NULL AND (m2.version>m1.version OR EXISTS(SELECT 1 FROM offers o JOIN terms t ON t.offer_id=o.id AND t.active=1 WHERE o.message_id=m2.id)) LIMIT 1''',(r['message_id'],)).fetchone(): reasons.append('message_versions_review')
            supported={'category','rate','duration','duration_unit','vat','color','conditions','scope_confirmed','review','amount','minimum','currency','restrictions','field_provenance'}
            if db.execute('PRAGMA user_version').fetchone()[0]>=2:
                supported |= {'rate_unit','rate_type','rate_basis','rate_text','duration_text','duration_max','duration_day_type','payment','color_source','color_date'}
                price=core.price_info(db,r['id'],t)
                if price['markup'] is None: reasons.append('markup_not_agreed')
                elif price['pricing_source'] is None: reasons.append('pricing_source_unknown')
                if t.get('rate_unit') not in ('%','percent'): reasons.append('rate_unit_unknown')
                if t.get('duration_text') and t.get('duration') is None: reasons.append('duration_unit_unknown')
                if t.get('duration_max') is not None and t.get('duration_max')!=t.get('duration') and not t.get('duration_text'): reasons.append('duration_range_review')
                if t.get('color') and (not t.get('color_source') or t.get('color_date')!=r['date']): reasons.append('color_provenance_review')
                if t.get('duration_day_type') not in (None,'calendar','working'): reasons.append('duration_day_type_review')
                if core.vat(db,r['company_id'])['conflict']: reasons.append('company_vat_conflict')
                if any(isinstance(t.get(k),(dict,list,bool)) for k in ('vat','payment','duration_text','conditions','restrictions','amount','minimum','currency','rate_type','rate_basis')): reasons.append('unsupported_structured_condition')
                try:
                    msg=row(db,'messages',r['message_id']); f=safe(root,msg['path']); intact=f.is_file() and sha(f.read_bytes())==msg['sha256'] and sha(msg['text'].encode())==msg['sha256']
                except (OSError,ValueError): intact=False
                if not intact: reasons.append('source_missing_or_changed')
            if set(t)-supported: reasons.append('unrendered_fields_review')
            if invalid_provenance(db,t,root): reasons.append('invalid_field_provenance_review')
            if reasons: items.append({'term_id':r['id'],'company_id':r['company_id'],'company':r['name'],'inn':r['inn'],'inn_status':r['inn_status'],'message_id':r['message_id'],'source_path':r['source_path'],'source_chat_id':r['chat_id'],'source_chat':r['source_chat'],'date':r['date'],'terms':t,'field_provenance':provenance_sources(db,t),'reasons':reasons})
        docs=[dict(r) for r in db.execute("SELECT d.id,d.company_id,c.name company,c.inn,c.inn_status,d.status,d.path,d.name,m.id message_id,m.path source_path,m.chat_id source_chat_id,ch.name source_chat,m.date FROM documents d JOIN companies c ON c.id=d.company_id JOIN messages m ON m.id=d.message_id JOIN chats ch ON ch.id=m.chat_id WHERE d.status!='verified' ORDER BY d.id")]
        for doc in docs: doc['reasons']=['document_'+doc['status']]
        if db.execute('PRAGMA user_version').fetchone()[0]>=2:
            for record in db.execute("SELECT d.id,d.company_id,c.name company,c.inn,c.inn_status,d.status,d.path,d.name,d.sha256,m.id message_id,m.path source_path,m.chat_id source_chat_id,ch.name source_chat,m.date FROM documents d JOIN companies c ON c.id=d.company_id JOIN messages m ON m.id=d.message_id JOIN chats ch ON ch.id=m.chat_id WHERE d.status='verified' ORDER BY d.id"):
                doc=dict(record)
                try:
                    f=safe(root,doc['path']); intact=f.is_file() and sha(f.read_bytes())==doc['sha256']
                except (OSError,ValueError): intact=False
                if not intact:
                    doc.pop('sha256'); doc['reasons']=['document_missing_or_changed']; docs.append(doc)
        ambiguous=[]
        for r in db.execute('SELECT q.id,q.data,m.id message_id,m.path source_path,m.chat_id,ch.name source_chat,m.date FROM requests q JOIN messages m ON m.id=q.message_id JOIN chats ch ON ch.id=m.chat_id ORDER BY q.id'):
            data=json.loads(r['data'])
            if data.get('kind')=='ambiguous': ambiguous.append({'request_id':r['id'],'data':data,'message_id':r['message_id'],'source_path':r['source_path'],'source_chat_id':r['chat_id'],'source_chat':r['source_chat'],'date':r['date'],'reasons':['classification_review']})
        result={'items':items,'documents':docs,'requests':ambiguous}
        if db.execute('PRAGMA user_version').fetchone()[0]>=2:
            result['document_issues']=[{'id':r['id'],**json.loads(r['data']),'recorded_at':r['at']} for r in db.execute("SELECT h.* FROM history h WHERE h.entity='document_issue' AND h.action='open' AND NOT EXISTS (SELECT 1 FROM history x WHERE x.entity='document_issue' AND x.action='resolve' AND x.entity_id=h.id) ORDER BY h.id")]
        return result
    if command=='update':
        if p.get('action')=='batch':
            decisions=p.get('decisions'); require(isinstance(decisions,list) and decisions,'decisions must be a nonempty array')
            for decision in decisions:
                require(isinstance(decision,dict) and decision.get('action') in ('company_identity','document_review','risk_decision','request_classification','withdraw','replace'),'invalid batch decision')
                require(decision.get('decision_confirmed') is True,'each decision must be explicitly confirmed')
                require(isinstance(decision.get('reason'),str) and decision['reason'].strip(),'each decision requires a reason')
            return {'decisions':[execute(db,root,'update',decision) for decision in decisions]}
        if p.get('action')=='request_classification':
            request=row(db,'requests',p.get('request_id')); message=row(db,'messages',request['message_id']); before=json.loads(request['data']); after=dict(before)
            kind=p.get('kind'); require(kind in REQUEST_KINDS,'invalid request kind')
            reason=p.get('reason'); require(isinstance(reason,str) and reason.strip(),'decision reason required')
            after['kind']=kind
            for field in ('term_id','block_id'):
                if field in p:
                    if p[field] is None: after.pop(field,None)
                    else: after[field]=p[field]
            validate_request(db,message,after,require_kind=True)
            db.execute('UPDATE requests SET data=? WHERE id=?',(canonical(after),request['id']))
            hist(db,'request',request['id'],'request_classification',{'before':before,'after':after,'reason':reason})
            return {'request_id':request['id'],'kind':kind,'data':after}
        if p.get('action')=='risk_decision':
            chat=p.get('chat_id'); date=p.get('date'); term=p.get('term_id'); reason=p.get('reason'); require(isinstance(reason,str) and reason.strip(),'decision reason required'); items=plan(db,chat,date,root=root); item=next((x for x in items if x['term_id']==term),None); require(item is not None,'term not in plan'); risk=item['reason']; require(risk.endswith('_review') and risk not in ('identity_review','terms_review','message_versions_review','unrendered_fields_review','invalid_field_provenance_review'),'this reason cannot be overridden'); fingerprint=context_hash(db)
            did=db.execute('INSERT INTO risk_decisions(chat_id,date,term_id,risk,state_hash,reason,at) VALUES(?,?,?,?,?,?,?)',(chat,date,term,risk,fingerprint,reason,now())).lastrowid; hist(db,'risk_decision',did,'approve',{'chat_id':chat,'date':date,'term_id':term,'risk':risk,'state_hash':fingerprint,'reason':reason}); return {'decision_id':did,'risk':risk,'state_hash':fingerprint}
        if p.get('action')=='document_review':
            if db.execute('PRAGMA user_version').fetchone()[0]>=2 and p.get('status')=='verified':
                require(p.get('confirmation_kind') in ('user','document_or_external') and isinstance(p.get('confirmation_source'),str) and p['confirmation_source'].strip(),'confirmation kind and source required')
            doc=row(db,'documents',p.get('document_id')); company=row(db,'companies',doc['company_id']); status=p.get('status'); require(status in ('verified','unverified','conflict'),'invalid document status'); reason=p.get('reason'); require(isinstance(reason,str) and reason.strip(),'decision reason required')
            if status=='verified':
                require(company['inn_status']=='verified' and p.get('document_inn')==company['inn'],'verified document requires matching verified company INN'); f=safe(root,doc['path']); require(f.is_file() and sha(f.read_bytes())==doc['sha256'],'document missing or changed')
            db.execute('UPDATE documents SET status=? WHERE id=?',(status,doc['id'])); hist(db,'document',doc['id'],'review',{'before':doc['status'],'status':status,'document_inn':p.get('document_inn'),'reason':reason,'confirmation_kind':p.get('confirmation_kind','unspecified'),'confirmation_source':p.get('confirmation_source')}); return {'document_id':doc['id'],'status':status}
        if p.get('action')=='company_identity':
            if db.execute('PRAGMA user_version').fetchone()[0]>=2 and p.get('inn_status')=='verified':
                require(p.get('confirmation_kind') in ('user','document_or_external') and isinstance(p.get('confirmation_source'),str) and p['confirmation_source'].strip(),'confirmation kind and source required')
            c=row(db,'companies',p.get('company_id')); inn=p.get('inn'); status=p.get('inn_status'); require(status in ('verified','missing','unverified','conflict'),'invalid inn_status'); require(inn is None or isinstance(inn,str),'INN must be string')
            if status=='verified': require(valid_inn(inn),'invalid verified INN')
            if status=='missing': require(not inn,'missing INN must be empty')
            reason=p.get('reason'); require(isinstance(reason,str) and reason.strip(),'decision reason required')
            db.execute('UPDATE companies SET inn=?,inn_status=? WHERE id=?',(inn,status,c['id']))
            if c['inn']!=inn or c['inn_status']!=status:
                for d in db.execute("SELECT id,status FROM documents WHERE company_id=? AND status='verified'",(c['id'],)).fetchall():
                    db.execute("UPDATE documents SET status='unverified' WHERE id=?",(d['id'],)); hist(db,'document',d['id'],'identity_changed',{'previous_status':d['status'],'company_id':c['id']})
            hist(db,'company',c['id'],'identity_decision',{'before':dict(c),'inn':inn,'inn_status':status,'reason':reason,'confirmation_kind':p.get('confirmation_kind','unspecified'),'confirmation_source':p.get('confirmation_source')}); return {'company_id':c['id'],'inn':inn,'inn_status':status}
        if p.get('action')=='reopen':
            old=row(db,'terms',p.get('term_id')); require(not old['active'],'term already active'); require(isinstance(p.get('reason'),str) and p['reason'].strip() and isinstance(p.get('source'),str) and p['source'].strip(),'reason and confirmation source required')
            require(not db.execute('SELECT 1 FROM terms WHERE previous_id=?',(old['id'],)).fetchone(),'replaced term cannot be reopened')
            db.execute('UPDATE terms SET active=1 WHERE id=?',(old['id'],)); hist(db,'term',old['id'],'reopen',{'reason':p['reason'],'source':p['source']}); return {'term_id':old['id'],'active':True}
        old=row(db,'terms',p.get('term_id')); require(old['active'],'term already inactive'); action=p.get('action'); require(action in ('withdraw','replace'),'action must be withdraw or replace'); require(isinstance(p.get('reason'),str) and p['reason'].strip(),'decision reason required')
        data=normalize_term(p.get('terms')) if action=='replace' else None
        if data is not None: validate_provenance(db,root,data)
        db.execute('UPDATE terms SET active=0 WHERE id=?',(old['id'],)); new=None
        if data is not None: new=db.execute('INSERT INTO terms(offer_id,data,previous_id) VALUES(?,?,?)',(old['offer_id'],canonical(data),old['id'])).lastrowid
        hist(db,'term',old['id'],action,{'new_term_id':new,'reason':p.get('reason')}); return {'term_id':old['id'],'new_term_id':new}
    if command in ('status','list'):
        result={table:[dict(r) for r in db.execute(f'SELECT * FROM {table} ORDER BY id')] for table in ('chats','companies','blocks','sends','coverage')}
        if db.execute('PRAGMA user_version').fetchone()[0]>=2:
            for chat in result['chats']: chat['platform']=chat_platform(db,chat['id'])
        for record in result['coverage']: record['data']=json.loads(record['data'])
        for block in result['blocks']:
            for field in ('items','documents','plan_data'): block[field]=json.loads(block[field])
        review=execute(db,root,'review',{})
        issue_count=len(review.get('document_issues',[]))
        result['open_reviews']={'items':len(review['items']),'documents':len(review['documents']),'requests':len(review['requests']),'total':len(review['items'])+len(review['documents'])+len(review['requests'])+issue_count}
        if db.execute('PRAGMA user_version').fetchone()[0]>=2: result['open_reviews']['document_issues']=issue_count
        result['coverage_warning']='Наличие сообщений не доказывает полноту интервала.'
        return result
    if command=='history':
        result={'history':[dict(x) for x in db.execute('SELECT * FROM history ORDER BY id')]}
        result['requests']=[dict(x) for x in db.execute('SELECT * FROM requests ORDER BY id')]
        result['blocks']=[dict(x) for x in db.execute('SELECT * FROM blocks ORDER BY id')]
        result['sends']=[dict(x) for x in db.execute('SELECT * FROM sends ORDER BY id')]
        return result
    if command=='coverage':
        chat=p.get('chat_id'); row(db,'chats',chat)
        if 'record' in p:
            require(isinstance(p['record'],dict),'record must be object'); db.execute('INSERT INTO coverage(chat_id,data,recorded_at) VALUES(?,?,?)',(chat,canonical(p['record']),now()))
        return {'records':[{'id':r['id'],'data':json.loads(r['data']),'recorded_at':r['recorded_at']} for r in db.execute('SELECT * FROM coverage WHERE chat_id=?',(chat,))], 'warning':'Наличие сообщений не доказывает полноту интервала.'}
    if command=='export':
        date=p.get('date',dt.date.today().isoformat()); valid_date(date); output=safe(root,p.get('directory','generated')); output.mkdir(parents=True,exist_ok=True)
        dest=output/f'{date}-report.md'; index=output/f'{date}-index.md'; requests=output/f'{date}-requests.md'; plans=output/f'{date}-plans.md'; sends=output/f'{date}-sends.md'
        require(not any(x.exists() for x in (dest,index,requests,plans,sends)),'report already exists; choose another directory')
        rows=db.execute('SELECT t.id,c.name,c.inn,c.inn_status,t.data,m.id message_id,m.path source_path,ch.name chat FROM terms t JOIN offers o ON o.id=t.offer_id JOIN companies c ON c.id=o.company_id JOIN messages m ON m.id=o.message_id JOIN chats ch ON ch.id=m.chat_id WHERE m.date=? AND t.active=1 ORDER BY t.id',(date,)).fetchall()
        header='# Сформировано, не редактировать вручную\n\nВремя: '+now()+'\n\nПроверена только загруженная история; её полнота не предполагается.\n\n'
        report=header+'## Предложения\n\n'
        for r in rows:
            term=json.loads(r['data'])
            for key in ('rate','duration','duration_unit','vat','color','amount','minimum','currency','conditions','restrictions'): term.setdefault(key,None)
            report+=f"### Вариант {r['id']}: {r['name']}\n\nИНН: {r['inn'] or 'не указан'}; статус: {r['inn_status']}. Чат: {r['chat']}; сообщение {r['message_id']}; исходник: {r['source_path']}.\n\nУсловия:\n"+readable_fields(term)+'\n'
        companies=db.execute('SELECT id,name,inn,inn_status FROM companies ORDER BY id').fetchall()
        index_text=header+''.join(f"- {r['id']}: {r['name']}, ИНН {r['inn'] or 'не указан'}, статус {r['inn_status']}\n" for r in companies)
        reqs=db.execute('SELECT q.id,q.data,m.id message_id,m.path source_path,m.chat_id FROM requests q JOIN messages m ON m.id=q.message_id WHERE m.date=? ORDER BY q.id',(date,)).fetchall()
        requests_text=header
        for r in reqs:
            requests_text+=f"## Запись {r['id']}; чат {r['chat_id']}; сообщение {r['message_id']}\n\nИсходник: {r['source_path']}\n\n"+readable_fields(json.loads(r['data']))+'\n'
        plans_text=header
        for b in db.execute('SELECT * FROM blocks WHERE date=? ORDER BY id',(date,)):
            plans_text+=f"## Блок {b['id']}; чат {b['chat_id']}; создан {b['created_at']}\n\nПлан на момент подготовки:\n"
            for item in json.loads(b['plan_data']):
                plans_text+=readable_fields(item)+'\n'
            plans_text+='Снимок подготовленного текста:\n\n'+b['text']+'\n\nКарточки на момент подготовки:\n'+readable_fields(json.loads(b['documents']))+'\n'
        sends_text=header
        for send in db.execute('SELECT s.* FROM sends s JOIN blocks b ON b.id=s.block_id WHERE b.date=? ORDER BY s.id',(date,)):
            sends_text+=f"## Запись {send['id']}; блок {send['block_id']}\n\nВариант: {send['term_id'] if send['term_id'] is not None else 'не указан'}; документ: {send['document_id'] if send['document_id'] is not None else 'не указан'}; зафиксировано: {send['recorded_at']}.\n\nФактический текст: "
            sends_text+=('неизвестен\n\n' if send['actual_text'] is None else '\n\n'+send['actual_text']+'\n\n')
        for path,content in ((dest,report),(index,index_text),(requests,requests_text),(plans,plans_text),(sends,sends_text)):
            with path.open('x',encoding='utf-8') as f: f.write(content)
        return {'path':str(dest.relative_to(root)),'index':str(index.relative_to(root)),'requests':str(requests.relative_to(root)),'plans':str(plans.relative_to(root)),'sends':str(sends.relative_to(root)),'count':len(rows)}
    if command=='verify':
        errors=[]; integrity=db.execute('PRAGMA integrity_check').fetchall()
        if any(r[0]!='ok' for r in integrity): errors += [r[0] for r in integrity]
        errors += [str(tuple(r)) for r in db.execute('PRAGMA foreign_key_check')]
        registered=set()
        for path,digest in registered_files(db):
            registered.add(path)
            try:
                f=safe(root,path); require(f.is_file() and sha(f.read_bytes())==digest,f"missing or changed: {path}")
            except (ValueError,OSError) as e: errors.append(str(e))
        orphans=[]
        for directory in ('дни','компании'):
            base=root/directory
            if base.exists():
                for f in base.rglob('*'):
                    if f.is_file() and len(f.stem)==32 and all(c in '0123456789abcdef' for c in f.stem) and str(f.relative_to(root)) not in registered: orphans.append(str(f.relative_to(root)))
        if orphans: errors.extend('orphan: '+x for x in orphans)
        return {'valid':not errors,'errors':errors,'registered_files':len(registered),'orphans':orphans}
    if command=='backup':
        target=safe(root,p.get('directory','backups/'+dt.datetime.now().strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:8])); require(not target.exists(),'backup destination exists'); target.mkdir(parents=True)
        out=sqlite3.connect(target/'registry.sqlite'); out.row_factory=sqlite3.Row
        try:
            db.backup(out); snapshot_files=registered_files(out)
        finally: out.close()
        files=[]
        for path,digest in snapshot_files:
            src=safe(root,path); require(src.is_file() and sha(src.read_bytes())==digest,'source file missing or changed'); dst=target/'files'/path; dst.parent.mkdir(parents=True,exist_ok=True); shutil.copyfile(src,dst); files.append({'path':path,'sha256':digest})
        manifest={'created_at':now(),'database_sha256':sha((target/'registry.sqlite').read_bytes()),'files':files}; (target/'manifest.json').write_text(canonical(manifest),encoding='utf-8'); return {'path':str(target.relative_to(root)),'files':len(files)}
    if command=='restore':
        source=safe(root,p.get('backup_directory','')); dest=safe(root,p.get('destination','')); require(source.is_dir() and source!=root,'backup directory required'); require(dest!=root and not dest.exists(),'restore destination must not exist')
        manifest=json.loads((source/'manifest.json').read_text(encoding='utf-8')); database=source/'registry.sqlite'; require(sha(database.read_bytes())==manifest['database_sha256'],'backup database hash mismatch')
        probe=sqlite3.connect(f'file:{database}?mode=ro',uri=True); probe.row_factory=sqlite3.Row
        try:
            require(probe.execute('PRAGMA integrity_check').fetchone()[0]=='ok','backup database integrity failed'); require(not probe.execute('PRAGMA foreign_key_check').fetchone(),'backup foreign key check failed')
            listed={(x['path'],x['sha256']) for x in manifest['files']}; actual=set(registered_files(probe)); require(listed==actual,'backup manifest differs from database')
        finally: probe.close()
        for item in manifest['files']:
            f=safe(source/'files',item['path']); require(f.is_file() and sha(f.read_bytes())==item['sha256'],'backup file hash mismatch')
        dest.mkdir(parents=True); shutil.copyfile(database,dest/'registry.sqlite')
        for item in manifest['files']:
            src=safe(source/'files',item['path']); dst=safe(dest,item['path']); dst.parent.mkdir(parents=True,exist_ok=True); shutil.copyfile(src,dst); require(sha(dst.read_bytes())==item['sha256'],'restored file hash mismatch')
        require(sha((dest/'registry.sqlite').read_bytes())==manifest['database_sha256'],'restored database hash mismatch')
        return {'path':str(dest.relative_to(root)),'database':str((dest/'registry.sqlite').relative_to(root)),'files':len(manifest['files'])}
    raise ValueError('unknown command')

def main():
    parser=argparse.ArgumentParser(description='Локальный прототип реестра: JSON на stdin, JSON на stdout.', formatter_class=argparse.RawDescriptionHelpFormatter, epilog='''Команды:
  init, chat, ingest, import, document, search, plan, draft, check-draft,
  confirm, review, update, coverage, status, list, history, export, verify,
  backup, restore.
Примеры JSON: chat: {"name":"Чат"}; plan: {"chat_id":1,"date":"2026-11-17"};
review: {}; draft: {"chat_id":1,"date":"2026-11-17"}.
Полный контракт: КОНТРАКТ-CLI.md. Отправка выполняется человеком.''')
    parser.add_argument('--root',default='.',help='корень экземпляра'); parser.add_argument('--db',default='trading.sqlite',help='база внутри корня'); parser.add_argument('command',help='команда из списка ниже'); args=parser.parse_args()
    try:
        root=Path(args.root).expanduser().resolve(); root.mkdir(parents=True,exist_ok=True); dbpath=Path(args.db).expanduser(); dbpath=dbpath if dbpath.is_absolute() else root/dbpath; dbpath=dbpath.resolve(); require(dbpath.is_relative_to(root),'database must be inside project root')
        payload=json.load(sys.stdin); db=sqlite3.connect(dbpath,timeout=10); db.row_factory=sqlite3.Row; db.execute('PRAGMA foreign_keys=ON'); db.execute('PRAGMA busy_timeout=10000')
        version=db.execute('PRAGMA user_version').fetchone()[0]; require(version in (0,1,2),'unsupported schema version')
        if version==0:
            require(args.command=='init','database not initialized'); require(not db.execute("SELECT 1 FROM sqlite_master WHERE type IN ('table','view','trigger') AND name NOT LIKE 'sqlite_%' LIMIT 1").fetchone(),'nonempty database with unknown schema'); db.executescript(SCHEMA)
        else:
            require(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='risk_decisions'").fetchone() is not None and {'source_copy_path','reply_block_id'} <= {r[1] for r in db.execute('PRAGMA table_info(messages)')} and {'documents','plan_data'} <= {r[1] for r in db.execute('PRAGMA table_info(blocks)')},'unsupported or incomplete schema version 1')
        if version==2:
            require('actual_time' in {r[1] for r in db.execute('PRAGMA table_info(sends)')},'Несовместимый ранний вариант схемы v2; создайте новый экземпляр или повторите миграцию с v1 в новый каталог')
        if version==2 and args.command not in ('backup','restore'):
            db.execute('BEGIN IMMEDIATE')
        with db: result=execute(db,root,args.command,payload)
        db.close(); print(json.dumps({'ok':True,**result},ensure_ascii=False))
    except (ValueError,TypeError,KeyError,OSError,sqlite3.Error,json.JSONDecodeError) as e:
        print(json.dumps({'ok':False,'error':str(e),'code':type(e).__name__},ensure_ascii=False)); sys.exit(1)
if __name__=='__main__': main()
