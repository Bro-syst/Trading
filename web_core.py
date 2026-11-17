"""Shared operations for the isolated local web prototype (schema v2)."""
import json, sqlite3, hashlib, datetime as dt
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

def connect(root):
    db=sqlite3.connect(Path(root)/'registry.sqlite',timeout=5, isolation_level=None)
    db.row_factory=sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON'); db.execute('PRAGMA busy_timeout=5000')
    t.require(db.execute('PRAGMA user_version').fetchone()[0]==2,'Требуется схема v2')
    t.require('actual_time' in {r[1] for r in db.execute('PRAGMA table_info(sends)')},'Несовместимый ранний вариант схемы v2; создайте новый экземпляр или повторите миграцию с v1 в новый каталог')
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
        t.require(src.execute('PRAGMA user_version').fetchone()[0]==1,'Поддерживается только исходная схема v1')
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
            dst.execute('PRAGMA foreign_keys=ON'); dst.executescript('BEGIN IMMEDIATE;\n'+V2+'\nCOMMIT;')
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
    return {'markup':price['markup'] if price else None,'final_rate':final,'pricing_reason':price['reason'] if price else None,'pricing_source':source}

def sent(db,chat,term):
    return bool(db.execute('''SELECT 1 FROM sends s JOIN blocks b ON b.id=s.block_id WHERE b.chat_id=? AND s.term_id=? AND NOT EXISTS (SELECT 1 FROM corrections c WHERE c.send_id=s.id AND c.cancels=1) LIMIT 1''',(chat,term)).fetchone())

def confirmation(db,entity,entity_id,action):
    record=db.execute('SELECT data,at FROM history WHERE entity=? AND entity_id=? AND action=? ORDER BY id DESC LIMIT 1',(entity,entity_id,action)).fetchone()
    if not record: return {'kind':'unspecified','source':None,'at':None}
    data=json.loads(record['data']); return {'kind':data.get('confirmation_kind','unspecified'),'source':data.get('confirmation_source'),'at':record['at'],'reason':data.get('reason')}

def documents(db,root,cid):
    result=[]
    for d in db.execute('SELECT * FROM documents WHERE company_id=? ORDER BY id',(cid,)):
        try:
            f=t.safe(root,d['path']); intact=f.is_file() and t.sha(f.read_bytes())==d['sha256']
        except (OSError,ValueError): intact=False
        result.append({'id':d['id'],'name':d['name'],'status':d['status'],'confirmation':confirmation(db,'document',d['id'],'review'),'intact':intact,'ready':intact and d['status']=='verified'})
    return result

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
        term=x['terms']; v=vat(db,x['company_id']); price=price_info(db,x['term_id'],term)
        reasons=list(x['reasons'])
        # v2 recognises structured fields, but rejects unsupported content rather than dropping it.
        supported={'category','rate','duration','duration_unit','vat','color','conditions','scope_confirmed','review','amount','minimum','currency','restrictions','field_provenance','rate_unit','rate_type','rate_basis','rate_text','duration_text','duration_max','duration_day_type','payment','color_source','color_date'}
        if not set(term)-supported and 'unrendered_fields_review' in reasons: reasons.remove('unrendered_fields_review')
        if price['markup'] is None: reasons.append('markup_not_agreed')
        elif price['pricing_source'] is None: reasons.append('pricing_source_unknown')
        if term.get('rate_unit') not in ('%', 'percent'): reasons.append('rate_unit_unknown')
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
        item={**x,'source_platform':t.chat_platform(db,x['source_chat_id']),'source_time':m['time'] if intact else None,'id':taskids[x['term_id']],'status':status,'reason':reasons[0] if reasons else 'ok','reasons':reasons,'withdrawal':withdrawal,'company_vat':v,'identity_confirmation':confirmation(db,'company',x['company_id'],'identity_decision'),**price,'documents':documents(db,root,x['company_id'])}
        item['version']=t.sha(t.canonical(item).encode()); result.append(item)
    # Only compare explicitly complete, matching terms with a known rate meaning.
    groups={}
    for x in result:
        if x['inn_status']=='verified' and x['status'] not in ('withdrawn','excluded','sent'):
            groups.setdefault(x['company_id'],[]).append(x)
    keys=('rate_unit','rate_type','rate_basis','category','vat','payment','duration_text','duration','duration_unit','duration_day_type','color','conditions','restrictions','amount','minimum','currency')
    for group in groups.values():
        sources={x['source_chat_id'] for x in group}
        if len(sources)<2: continue
        comparable=all(all(k in x['terms'] and x['terms'][k] is not None for k in keys) and x['terms'].get('rate') is not None and not any(r in x['reasons'] for r in ('source_missing_or_changed','identity_review','possible_identity_conflict_review','terms_review','message_versions_review','invalid_field_provenance_review','unrendered_fields_review','unsupported_structured_condition','company_vat_conflict','color_provenance_review','duration_range_review','duration_day_type_review')) and not (any(c in str(x['terms'].get('duration_text','')) for c in ('–','—','-')) and x['terms'].get('duration_max') is None) for x in group)
        comparable=comparable and all(tuple(x['terms'][k] for k in keys)==tuple(group[0]['terms'][k] for k in keys) and x['terms'].get('duration_max',x['terms'].get('duration'))==group[0]['terms'].get('duration_max',group[0]['terms'].get('duration')) for x in group)
        values=[Decimal(x['terms']['rate']) for x in group] if comparable else []
        unique_best=comparable and values.count(min(values))==1
        for x in group:
            x['comparison']={'comparable':comparable,'lowest':bool(unique_best and Decimal(x['terms']['rate'])==min(values)) if comparable else False,'tie':bool(comparable and not unique_best),'note':'Сравнение требует проверки' if not comparable else ('Равные минимальные ставки; выбор вручную' if not unique_best else 'Сопоставимые условия; показана исходная ставка')}
            if unique_best and 'multiple_sources_review' in x['reasons']:
                x['reasons'].remove('multiple_sources_review')
                if x['status']=='review' and not x['reasons']: x['status']='pending'
                x['reason']=x['reasons'][0] if x['reasons'] else 'ok'
    for x in result: x['version']=t.sha(t.canonical({k:v for k,v in x.items() if k!='version'}).encode())
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

def search(db, root, p):
    """Search the v2 registry; uncertain filters remain possible matches."""
    date=p.get('date',dt.date.today().isoformat()); t.valid_date(date)
    query=p.get('q',''); t.require(isinstance(query,str),'query must be a string'); query=query.strip().casefold()
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
    colors={'green':'green','зеленыи':'green','зеленая':'green','yellow':'yellow','желтыи':'yellow','желтая':'yellow'}
    def norm(value): return str(value).casefold().replace('ё','е').replace('й','и').strip()
    def normalize_category(value):
        value=norm(value)
        for alias in ('стромат','строиматериалы'): value=value.replace(alias,'строительные материалы')
        return value
    result=[]
    for r in rows:
        if query and query not in r['name'].casefold() and query not in (r['inn'] or ''): continue
        term=json.loads(r['data']); unknown=[]; mismatch=False
        unknown.extend(x for x in risks.get(r['term_id'],[]) if x in ('identity_review','possible_identity_conflict_review','terms_review','message_versions_review','multiple_sources_review','unrendered_fields_review','invalid_field_provenance_review'))
        if category:
            if not term.get('category'): unknown.append('category')
            elif normalize_category(category) not in normalize_category(term['category']): mismatch=True
        if color:
            value=term.get('color')
            if not value or not term.get('color_source') or term.get('color_date')!=date: unknown.append('color')
            elif colors.get(norm(color),norm(color))!=colors.get(norm(value),norm(value)): mismatch=True
        if max_duration is not None:
            val=term.get('duration_max',term.get('duration'))
            if val is None or term.get('duration_unit')!='day' or term.get('duration_day_type')!='calendar' or (any(c in str(term.get('duration_text','')) for c in ('–','—','-')) and term.get('duration_max') is None): unknown.append('duration')
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
            result.append({'term_id':r['term_id'],'company_id':r['company_id'],'company':r['name'],'inn':r['inn'],'inn_status':r['inn_status'],'identity_confirmation':confirmation(db,'company',r['company_id'],'identity_decision'),'company_vat':vat(db,r['company_id']),'terms':term,**price_info(db,r['term_id'],term),'message_id':r['message_id'],'source_path':r['source_path'],'source_chat_id':r['chat_id'],'source_chat':r['source_chat'],'source_platform':t.chat_platform(db,r['chat_id']),'date':r['date'],'documents':documents(db,root,r['company_id']),'match':'possible' if unknown else 'full','unknown':list(dict.fromkeys(unknown))})
    return {'items':result,'coverage_warning':'Проверена только загруженная история; её полнота не предполагается.'}

def render(items):
    lines=[]; grouped={}
    for x in items:
        z=x['terms']; v=x['company_vat']['value'] or 'не указан'
        s=[f"{x['company']} — ИНН {x['inn']}",f'НДС: {v}',f"Назначение: {z['category']}"]
        label='Цена'+(' (НДС: '+str(z['vat'])+')' if z.get('vat') else '')+(' '+str(z['payment']) if z.get('payment') else '')
        s.append(f"{label}: {x['final_rate']}%")
        if z.get('rate_type'): s.append('Тип ставки: '+str(z['rate_type']))
        if z.get('rate_basis'): s.append('Основание ставки: '+str(z['rate_basis']))
        if z.get('duration_text'): s.append('Выдача: '+str(z['duration_text']))
        elif z.get('duration') is not None:
            unit={'day':'дн.','hour':'ч.','week':'нед.','month':'мес.'}.get(z.get('duration_unit'),z.get('duration_unit',''))
            if z.get('duration_unit')=='day' and z.get('duration_day_type') in ('calendar','working'): unit={'calendar':'календарных дней','working':'рабочих дней'}[z['duration_day_type']]
            s.append(f"Выдача: {z['duration']} {unit}")
        color={'green':'🟢 зелёный','yellow':'🟡 жёлтый','зелёный':'🟢 зелёный','зеленый':'🟢 зелёный','жёлтый':'🟡 жёлтый','желтый':'🟡 жёлтый'}.get(z.get('color'),'не указан')
        s.append('Светофор: '+color)
        for key,label in [('conditions','Условия'),('restrictions','Ограничение'),('amount','Сумма'),('minimum','Минимум'),('currency','Валюта')]:
            if z.get(key) is not None: s.append(f'{label}: {z[key]}')
        if (z.get('amount') is not None or z.get('minimum') is not None) and z.get('currency') is None: s.append('Валюта: не указана')
        grouped.setdefault(x['company_id'],[]).append(s)
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
            if previous['company_id']==x['company_id']:
                old_offer=t.row(db,'terms',previous['term_id'])['offer_id']; new_offer=t.row(db,'terms',x['term_id'])['offer_id']
                t.require(old_offer==new_offer,'Выберите один источник для компании в блоке')
        selected.append(x)
    docs=list({d['id']:d for x in selected for d in x['documents'] if d['ready']}.values()); text=render(selected); snapshot=t.canonical(selected); version=t.sha(snapshot.encode())
    bid=db.execute('INSERT INTO blocks(chat_id,date,items,documents,plan_data,plan_hash,text,created_at) VALUES(?,?,?,?,?,?,?,?)',(chat,date,snapshot,t.canonical(docs),snapshot,version,text,t.now())).lastrowid
    db.execute('INSERT INTO web_blocks VALUES(?,?)',(bid,version)); t.hist(db,'block',bid,'draft',{'template_version':'1','plan_hash':version})
    return {'id':bid,'text':text,'task_ids':ids,'documents':docs,'version':version,'stale':False}

def block(db,root,bid):
    b=t.row(db,'blocks',bid); ver=db.execute('SELECT version FROM web_blocks WHERE block_id=?',(bid,)).fetchone(); t.require(ver,'Блок не относится к веб-приложению')
    items=json.loads(b['items']); current={x['id']:x for x in queue(db,root,b['chat_id'],b['date'],False)}
    stale=any(x['id'] not in current or current[x['id']]['version']!=x['version'] for x in items)
    return {'id':bid,'chat_id':b['chat_id'],'chat':t.row(db,'chats',b['chat_id'])['name'],'date':b['date'],'items':items,'text':b['text'],'task_ids':[x['id'] for x in items],'documents':json.loads(b['documents']),'document_candidates':list({d['id']:d for x in items for d in documents(db,root,x['company_id'])}.values()),'version':ver[0],'stale':stale}

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
