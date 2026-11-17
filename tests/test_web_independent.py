"""Independent end-to-end checks on disposable local registries only."""
import json
import hashlib
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
import web_core

DAY = '2026-11-17'

class LocalWebIndependent(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name).resolve()
        source = base / 'old'
        source.mkdir()
        self.root = source
        self.cli('init')
        self.root = base / 'new'
        web_core.migrate_copy(source, self.root)
        self.db = self.root / 'registry.sqlite'
        self.a = self.cli('chat', {'name': 'Source A'})['chat_id']
        self.b = self.cli('chat', {'name': 'Recipient B'})['chat_id']
        self.cli('recipient', {'chat_id': self.b, 'enabled': True})

    def tearDown(self): self.tmp.cleanup()

    def cli(self, cmd, data=None, db='registry.sqlite', ok=True):
        if cmd == 'pricing' and data is not None and 'source' not in data: data={**data,'source':'ТЕСТ пользователь'}
        p = subprocess.run([sys.executable, str(PROJECT/'trading.py'), '--root', str(self.root), '--db', db, cmd], input=json.dumps(data or {}), text=True, capture_output=True, timeout=15)
        obj = json.loads(p.stdout)
        self.assertEqual(obj['ok'], ok, (cmd, obj, p.stderr))
        return obj

    def offer(self, source=None, company_id=None, inn='7733446222'):
        msg = self.cli('ingest', {'chat_id':source or self.a, 'date':DAY, 'text':'ТЕСТ: цена 10%'})
        offer = {'terms':[{'category':'материалы','rate':'10','rate_unit':'%', 'scope_confirmed':True,'review':False}]}
        if company_id: offer['company_id']=company_id
        else: offer['company']={'name':'ТЕСТ Компания','inn':inn,'inn_status':'verified','confirmation_kind':'user','confirmation_source':'ТЕСТ пользователь'}
        result = self.cli('import', {'message_id':msg['message_id'],'offers':[offer]})
        return msg,result

    def tasks(self, chat=None): return self.cli('tasks', {'chat_id':chat or self.b,'date':DAY})['items']
    def prepare(self, item):
        return self.cli('web-prepare', {'chat_id':self.b,'date':DAY,'task_ids':[item['id']], 'versions':{str(item['id']):item['version']}})

    def test_chat_platform_preserves_provenance_and_roles(self):
        self.assertIsNone(next(x for x in self.cli('status')['chats'] if x['id']==self.a)['platform'])
        self.cli('chat-platform',{'chat_id':self.a,'platform':'telegram','reason':'ТЕСТ указание пользователя'})
        new=self.cli('chat',{'name':'ТЕСТ WhatsApp','platform':'whatsapp'})
        self.assertEqual(self.cli('chat',{'name':'ТЕСТ WhatsApp','platform':'whatsapp'})['chat_id'],new['chat_id'])
        self.cli('chat',{'name':'ТЕСТ WhatsApp','platform':'telegram'},ok=False)
        self.cli('chat-platform',{'chat_id':new['chat_id'],'platform':'other'},ok=False)
        chat=next(x for x in self.cli('status')['chats'] if x['id']==new['chat_id'])
        self.assertEqual((chat['platform'],chat['recipient']),('whatsapp',0))
        _,offer=self.offer(); self.cli('pricing',{'term_id':offer['term_ids'][0],'markup':'0','reason':'ТЕСТ'})
        self.assertEqual(self.tasks()[0]['source_platform'],'telegram')
        self.assertEqual(self.cli('search',{'date':DAY,'q':'ТЕСТ Компания'})['items'][0]['source_platform'],'telegram')
        self.cli('chat-platform',{'chat_id':self.a,'platform':'whatsapp','reason':'ТЕСТ исправление'})
        self.assertEqual(self.tasks()[0]['source_platform'],'whatsapp')
        with sqlite3.connect(self.db) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM history WHERE entity='chat' AND entity_id=? AND action='platform'",(self.a,)).fetchone()[0],2)

    def test_markup_own_company_snapshot_and_idempotency(self):
        _, offer = self.offer(); tid=offer['term_ids'][0]
        item=self.tasks()[0]; self.assertEqual(item['status'],'review'); self.assertIn('markup_not_agreed', item['reasons'])
        self.cli('pricing', {'term_id':tid,'markup':'1','reason':'ТЕСТ +1 п.п.'})
        item=self.tasks()[0]; self.assertEqual(item['status'],'pending')
        block=self.prepare(item); self.assertIn('Цена: 11%',block['text']); self.assertIn('НДС: не указан',block['text']); self.assertNotIn('Source A',block['text'])
        self.assertEqual(self.tasks()[0]['status'],'pending')
        send={'block_id':block['id'],'operation_id':'request-12345','version':block['version'],'task_ids':[item['id']],'document_ids':[]}
        first=self.cli('web-confirm',send); second=self.cli('web-confirm',send); self.assertEqual(first['send_ids'],second['send_ids'])
        self.cli('web-confirm',{**send,'actual_text':'changed'},ok=False)
        self.cli('web-confirm',{**send,'operation_id':'request-99999'},ok=False)
        self.assertEqual(self.tasks()[0]['status'],'sent')
        with sqlite3.connect(self.db) as db: self.assertEqual(db.execute('SELECT COUNT(*) FROM sends').fetchone()[0],1)
        self.cli('draft', {'chat_id':self.b,'date':DAY},ok=False)
        self.cli('confirm', {'block_id':block['id'],'term_ids':[tid]},ok=False)

    def test_own_company_through_different_source_and_role(self):
        _, own=self.offer(source=self.b)
        self.offer(source=self.a,company_id=own['company_ids'][0])
        for item in self.tasks(): self.assertEqual(item['status'],'excluded')
        self.cli('recipient',{'chat_id':self.b,'enabled':False})
        for item in self.tasks(): self.assertNotEqual(item['status'],'pending')
        self.cli('recipient',{'chat_id':self.b,'enabled':True})
        self.assertEqual(len(self.tasks()),2)

    def test_historical_pilot_migration_preserves_originals_and_ids(self):
        base=Path(self.tmp.name); pilot=base/'pilot'; migrated=base/'pilot-v2'
        p=subprocess.run([sys.executable,str(PROJECT/'миграция/пилот.py'),str(pilot)],capture_output=True,text=True,timeout=120)
        self.assertEqual(p.returncode,0,(p.stdout,p.stderr))
        # Registered-file helper expects row access by column name.
        def snapshot(path):
            db=sqlite3.connect(path/'registry.sqlite'); db.row_factory=sqlite3.Row
            try:
                tables=('chats','companies','terms','requests','documents','messages','blocks','sends','history')
                rows={name:[tuple(r) for r in db.execute('SELECT * FROM '+name+' ORDER BY id')] for name in tables}
                files=web_core.t.registered_files(db)
            finally: db.close()
            return rows,{name:hashlib.sha256((path/name).read_bytes()).hexdigest() for name,_ in files}
        before,files=snapshot(pilot)
        web_core.migrate_copy(pilot,migrated)
        after,copies=snapshot(migrated)
        self.assertEqual(before['chats'],[r[:-1] for r in after.pop('chats')])
        before.pop('chats'); self.assertEqual(before,after); self.assertEqual(files,copies)
        self.assertEqual(snapshot(pilot)[1],files)
        self.assertEqual({k:len(before[k]) for k in ('companies','terms','requests','documents')},{'companies':26,'terms':52,'requests':1,'documents':1})

    def test_partial_document_correction_and_backup_restore(self):
        message,offer=self.offer(); tid=offer['term_ids'][0]; cid=offer['company_ids'][0]
        self.cli('pricing',{'term_id':tid,'markup':'0','reason':'ТЕСТ без наценки'})
        card=self.root/'source.pdf'; card.write_bytes(b'test verified card')
        doc=self.cli('document',{'message_id':message['message_id'],'company_id':cid,'path':str(card)})
        self.cli('update',{'action':'document_review','document_id':doc['document_id'],'status':'verified','document_inn':'7733446222','reason':'ТЕСТ подтверждён','confirmation_kind':'user','confirmation_source':'ТЕСТ пользователь'})
        item=self.tasks()[0]; block=self.prepare(item); self.assertEqual([d['id'] for d in block['documents']],[doc['document_id']])
        send={'block_id':block['id'],'version':block['version'],'operation_id':'partial-case-1','task_ids':[item['id']],'document_ids':[doc['document_id']],'actual_text':'ТЕСТ текст'}
        result=self.cli('web-confirm',send); self.assertEqual(len(result['send_ids']),2)
        self.cli('correction',{'send_id':result['send_ids'][0],'reason':'Отметка ошибочна','cancels':True})
        self.assertEqual(self.tasks()[0]['status'],'pending')
        backup=self.cli('backup',{'directory':'backup'}); self.cli('restore',{'backup_directory':backup['path'],'destination':'restored'})
        restored=self.root/'restored'; self.assertEqual((restored/doc['path']).read_bytes(),card.read_bytes())
        db=sqlite3.connect(restored/'registry.sqlite')
        try:
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0],'ok')
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],2)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM sends').fetchone()[0],2)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM corrections').fetchone()[0],1)
        finally: db.close()

    def test_two_variants_of_one_offer_share_one_company_header(self):
        msg=self.cli('ingest',{'chat_id':self.a,'date':DAY,'text':'ТЕСТ две цены'})
        terms=[{'category':'материалы','rate':rate,'rate_unit':'%','vat':vat,'scope_confirmed':True,'review':False} for rate,vat in [('8','с НДС 22%'),('6','безотчёт')]]
        offer=self.cli('import',{'message_id':msg['message_id'],'offers':[{'company':{'name':'ТЕСТ Компания','inn':'7733446222','inn_status':'verified','confirmation_kind':'user','confirmation_source':'ТЕСТ пользователь'},'terms':terms}]})
        for tid in offer['term_ids']: self.cli('pricing',{'term_id':tid,'markup':'0','reason':'ТЕСТ без наценки'})
        items=self.tasks(); self.assertTrue(all(x['status']=='pending' for x in items),items)
        block=self.cli('web-prepare',{'chat_id':self.b,'date':DAY,'task_ids':[x['id'] for x in items],'versions':{str(x['id']):x['version'] for x in items}})
        self.assertEqual(block['text'].count('ТЕСТ Компания — ИНН'),1)
        self.assertIn('8%',block['text']); self.assertIn('6%',block['text']); self.assertIn('с НДС 22%',block['text']); self.assertIn('безотчёт',block['text'])

    def test_v2_identity_requires_confirmation_provenance(self):
        message=self.cli('ingest',{'chat_id':self.a,'date':DAY,'text':'ТЕСТ ИНН'})
        company={'name':'ТЕСТ Компания','inn':'7733446222','inn_status':'verified'}
        payload={'message_id':message['message_id'],'offers':[{'company':company,'terms':[{'category':'материалы','rate':'10','rate_unit':'%','scope_confirmed':True,'review':False}]}]}
        self.cli('import',payload,ok=False)
        payload['offers'][0]['company'].update(confirmation_kind='user',confirmation_source='ТЕСТ пользователь')
        result=self.cli('import',payload)
        history=self.cli('history')['history']
        entry=next(h for h in history if h['entity']=='company' and h['entity_id']==result['company_ids'][0] and h['action']=='identity_decision')
        data=json.loads(entry['data']); self.assertEqual(data['confirmation_kind'],'user'); self.assertEqual(data['confirmation_source'],'ТЕСТ пользователь')

    def test_comparable_sources_choose_lowest_with_own_markup(self):
        source2=self.cli('chat',{'name':'Source C'})['chat_id']; cid=None
        for source,rate,markup in [(self.a,'15','0'),(source2,'13','1')]:
            msg=self.cli('ingest',{'chat_id':source,'date':DAY,'text':'ТЕСТ сопоставимые цены'})
            term={'category':'материалы','rate':rate,'rate_unit':'%','rate_type':'комиссия','rate_basis':'платёж','vat':'22%', 'payment':'перевод','duration_text':'2 дня','duration':2,'duration_unit':'day','duration_day_type':'calendar','color':'green','color_source':'ТЕСТ','color_date':DAY,'conditions':'нет','restrictions':'нет','amount':'нет','minimum':'нет','currency':'нет','scope_confirmed':True,'review':False}
            offer={'terms':[term]}
            if cid is None: offer['company']={'name':'ТЕСТ Компания','inn':'7733446222','inn_status':'verified','confirmation_kind':'user','confirmation_source':'ТЕСТ пользователь'}
            else: offer['company_id']=cid
            imported=self.cli('import',{'message_id':msg['message_id'],'offers':[offer]}); cid=imported['company_ids'][0]
            self.cli('pricing',{'term_id':imported['term_ids'][0],'markup':markup,'reason':'ТЕСТ цена'})
        items=self.tasks(); self.assertEqual(len(items),2)
        self.assertTrue(all(i['comparison']['comparable'] for i in items),items)
        self.assertTrue(items[0]['comparison']['lowest']); self.assertEqual(items[0]['final_rate'],'14')
        self.assertEqual(items[0]['status'],'pending'); block=self.prepare(items[0]); self.assertIn('14%',block['text']); self.assertIn('22%',block['text']); self.assertNotIn('Source C',block['text'])

    def test_multiple_sources_with_different_minimum_are_not_ranked(self):
        source2=self.cli('chat',{'name':'Source C'})['chat_id']; cid=None
        for source,rate,minimum in [(self.a,'15','5 млн'),(source2,'13','10 млн')]:
            msg=self.cli('ingest',{'chat_id':source,'date':DAY,'text':'ТЕСТ разные минимумы'})
            term={'category':'материалы','rate':rate,'rate_unit':'%','rate_type':'комиссия','rate_basis':'платёж', 'vat':'22%', 'payment':'перевод','duration_text':'2 дня','duration':2,'duration_unit':'day','duration_day_type':'calendar','color':'green','color_source':'ТЕСТ','color_date':DAY,'conditions':'одинаковые','restrictions':'нет','minimum':minimum,'scope_confirmed':True,'review':False}
            offer={'terms':[term]}
            if cid is None: offer['company']={'name':'ТЕСТ Компания','inn':'7733446222','inn_status':'verified','confirmation_kind':'user','confirmation_source':'ТЕСТ пользователь'}
            else: offer['company_id']=cid
            imported=self.cli('import',{'message_id':msg['message_id'],'offers':[offer]}); cid=imported['company_ids'][0]
            self.cli('pricing',{'term_id':imported['term_ids'][0],'markup':'0','reason':'ТЕСТ без наценки'})
        items=self.tasks(); self.assertEqual(len(items),2)
        for item in items:
            self.assertFalse(item['comparison']['comparable'],item)
            self.assertEqual(item['status'],'review',item)

    def test_incomplete_v2_schema_is_rejected_before_work(self):
        db=sqlite3.connect(self.db)
        try: db.execute('ALTER TABLE sends DROP COLUMN actual_time'); db.commit()
        finally: db.close()
        self.cli('tasks',{'chat_id':self.b,'date':DAY},ok=False)
        with self.assertRaises(ValueError):
            db=web_core.connect(self.root)
            db.close()

    def test_http_session_origin_and_csrf(self):
        import http.client
        import web
        salt=b'test-salt-123456'; password='temporary test pass'
        (self.root/'local-auth.json').write_text(json.dumps({'salt':salt.hex(),'hash':web.password_hash(password,salt)}))
        server=web.ThreadingHTTPServer(('127.0.0.1',0),web.Handler); server.root=self.root
        thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
        self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        host='127.0.0.1:%d' % server.server_port
        def req(method,path,data=None,headers=None):
            conn=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=5)
            h={'Host':host,**(headers or {})}; payload=None if data is None else json.dumps(data)
            if data is not None: h['Content-Type']='application/json'
            conn.request(method,path,payload,h); response=conn.getresponse(); body=response.read(); result=(response.status,dict(response.getheaders()),json.loads(body) if body else None); conn.close(); return result
        self.assertEqual(req('GET','/api/chats?date='+DAY)[0],401)
        self.assertEqual(req('POST','/api/login',{'password':password})[0],403)
        status,headers,body=req('POST','/api/login',{'password':password},{'Origin':'http://'+host})
        self.assertEqual(status,200); cookie=headers['Set-Cookie'].split(';')[0]; csrf=body['csrf']
        self.assertEqual(req('POST','/api/recipient',{'chat_id':self.b,'enabled':False},{'Origin':'http://evil.invalid','Cookie':cookie,'X-CSRF-Token':csrf})[0],403)
        self.assertEqual(req('POST','/api/recipient',{'chat_id':self.b,'enabled':False},{'Origin':'http://'+host,'Cookie':cookie})[0],403)
        self.assertEqual(req('GET','/api/documents/../../etc/passwd',headers={'Cookie':cookie})[0],409)
        self.assertEqual(req('GET','/api/tasks?chat_id=%d&date=%s' % (self.b,DAY),headers={'Cookie':cookie})[0],200)
        self.assertEqual(req('GET','/api/tasks?chat_id=%d&date=%s' % (self.b,DAY),headers={'Cookie':cookie,'Host':'evil.invalid'})[0],403)
        auth1={'Origin':'http://'+host,'Cookie':cookie,'X-CSRF-Token':csrf}
        status,_,created=req('POST','/api/chat',{'name':'ТЕСТ Новый источник'},auth1); self.assertEqual(status,200,created)
        new_chat=created['chat_id']
        status,_,listing=req('GET','/api/chats?date='+DAY,headers={'Cookie':cookie})
        self.assertFalse(next(x for x in listing['chats'] if x['id']==new_chat)['recipient'])
        self.assertEqual(req('POST','/api/recipient',{'chat_id':new_chat,'enabled':True,'reason':'ТЕСТ решение'},auth1)[0],200)
        self.assertEqual(self.cli('status')['chats'][-1]['id'],new_chat)
        self.assertTrue(next(x for x in self.cli('status')['chats'] if x['id']==new_chat)['recipient'])
        self.assertEqual(req('POST','/api/recipient',{'chat_id':new_chat,'enabled':False,'reason':'ТЕСТ решение'},auth1)[0],200)
        _,offer=self.offer(); tid=offer['term_ids'][0]
        status,_,search=req('GET','/api/search?q=7733446222&date='+DAY,headers={'Cookie':cookie})
        self.assertEqual(status,200,search); self.assertEqual(len(search['results']),1)
        found=search['results'][0]; self.assertIsNone(found['markup']); self.assertIsNone(found['final_rate'])
        self.assertEqual(found['terms']['rate'],'10'); self.assertEqual(found['source_chat'],'Source A')
        self.assertEqual(found['identity_confirmation']['kind'],'user'); self.assertEqual(found['identity_confirmation']['source'],'ТЕСТ пользователь')
        self.cli('pricing',{'term_id':tid,'markup':'1','reason':'ТЕСТ +1 п.п.'})
        status,_,search=req('GET','/api/search?q=7733446222&date='+DAY,headers={'Cookie':cookie})
        found=search['results'][0]; self.assertEqual(found['final_rate'],'11'); self.assertEqual(found['markup'],'1'); self.assertEqual(found['terms']['rate'],'10')
        self.assertEqual(self.tasks()[0]['final_rate'],found['final_rate'])
        status,_,filtered=req('GET','/api/search?q=7733446222&date='+DAY+'&max_rate=9',headers={'Cookie':cookie})
        self.assertEqual(status,200,filtered); self.assertEqual(len(filtered['results']),1)
        self.assertEqual(filtered['results'][0]['match'],'possible'); self.assertIn('rate_comparability',filtered['results'][0]['unknown'])
        cli_search=self.cli('search',{'date':DAY,'q':'7733446222','max_rate':'9'})['items']
        self.assertEqual([(x['term_id'],x['match']) for x in cli_search],[(x['term_id'],x['match']) for x in filtered['results']])
        other_msg=self.cli('ingest',{'chat_id':new_chat,'date':DAY,'text':'ТЕСТ второй источник'})
        other=self.cli('import',{'message_id':other_msg['message_id'],'offers':[{'company_id':offer['company_ids'][0],'terms':[{'category':'материалы','rate':'13','rate_unit':'%','scope_confirmed':True,'review':False}]}]})
        status,_,search=req('GET','/api/search?q=7733446222&date='+DAY,headers={'Cookie':cookie})
        self.assertEqual({x['source_chat'] for x in search['results']},{'Source A','ТЕСТ Новый источник'})
        self.assertIsNone(next(x for x in search['results'] if x['term_id']==other['term_ids'][0])['final_rate'])
        # Keep the concurrency scenario independent of the deliberately unresolved two-source case.
        self.cli('update',{'action':'withdraw','term_id':other['term_ids'][0],'reason':'ТЕСТ сценарий завершён'})
        self.cli('update',{'action':'risk_decision','chat_id':self.b,'date':DAY,'term_id':tid,'reason':'ТЕСТ: снятый источник проверен'})
        status,h2,body2=req('POST','/api/login',{'password':password},{'Origin':'http://'+host})
        cookie2=h2['Set-Cookie'].split(';')[0]; csrf2=body2['csrf']
        status,_,taskdata=req('GET','/api/tasks?chat_id=%d&date=%s' % (self.b,DAY),headers={'Cookie':cookie})
        item=taskdata['tasks'][0]
        auth1={'Origin':'http://'+host,'Cookie':cookie,'X-CSRF-Token':csrf}
        auth2={'Origin':'http://'+host,'Cookie':cookie2,'X-CSRF-Token':csrf2}
        status,_,block=req('POST','/api/blocks',{'chat_id':self.b,'date':DAY,'task_ids':[item['id']],'versions':{str(item['id']):item['version']}},auth1)
        self.assertEqual(status,200,(block,item,self.tasks()))
        barrier=threading.Barrier(2)
        def confirm(args):
            key,auth=args; barrier.wait()
            return req('POST','/api/blocks/%d/send' % block['id'],{'version':block['version'],'operation_id':key,'task_ids':[item['id']],'document_ids':[]},auth)
        with ThreadPoolExecutor(max_workers=2) as pool: outcomes=list(pool.map(confirm,[('http-parallel-1',auth1),('http-parallel-2',auth2)]))
        self.assertEqual(sorted(x[0] for x in outcomes),[200,409],outcomes)
        successful=0 if outcomes[0][0]==200 else 1
        key,auth=[('http-parallel-1',auth1),('http-parallel-2',auth2)][successful]
        retry=req('POST','/api/blocks/%d/send' % block['id'],{'version':block['version'],'operation_id':key,'task_ids':[item['id']],'document_ids':[]},auth)
        self.assertEqual(retry[0],200); self.assertEqual(retry[2],outcomes[successful][2])
        send_id=retry[2]['send_ids'][0]
        status,_,correction=req('POST','/api/correction',{'send_id':send_id,'reason':'ТЕСТ исправление','cancels':True},auth1)
        self.assertEqual(status,200,correction)
        status,_,hist=req('GET','/api/history',headers={'Cookie':cookie}); self.assertEqual(status,200)
        saved=next(x for x in hist['sends'] if x['id']==send_id)
        self.assertEqual(saved['corrections'][0]['reason'],'ТЕСТ исправление'); self.assertEqual(saved['corrections'][0]['cancels'],1)
        self.cli('update',{'action':'company_identity','company_id':offer['company_ids'][0],'inn':'7733446222','inn_status':'verified','reason':'ТЕСТ документ','confirmation_kind':'document_or_external','confirmation_source':'ТЕСТ карточка'})
        status,_,search=req('GET','/api/search?q=7733446222&date='+DAY,headers={'Cookie':cookie})
        for found in search['results']:
            self.assertEqual(found['identity_confirmation']['kind'],'document_or_external')
            self.assertEqual(found['identity_confirmation']['source'],'ТЕСТ карточка')
        history=self.cli('history')['history']
        kinds=[json.loads(h['data']).get('confirmation_kind') for h in history if h['entity']=='company' and h['entity_id']==offer['company_ids'][0] and h['action']=='identity_decision']
        self.assertIn('user',kinds); self.assertIn('document_or_external',kinds)

    def test_document_tampering_marks_prepared_snapshot_stale(self):
        message,offer=self.offer(); tid=offer['term_ids'][0]
        self.cli('pricing',{'term_id':tid,'markup':'0','reason':'ТЕСТ без наценки'})
        card=self.root/'source.pdf'; card.write_bytes(b'test card original')
        doc=self.cli('document',{'message_id':message['message_id'],'company_id':offer['company_ids'][0],'path':str(card)})
        item=self.tasks()[0]; block=self.prepare(item)
        (self.root/doc['path']).write_bytes(b'tampered')
        result=self.cli('web-block',{'block_id':block['id']}); self.assertTrue(result['stale'])
        self.cli('web-confirm',{'block_id':block['id'],'version':block['version'],'operation_id':'stale-case-1','task_ids':[item['id']],'document_ids':[]},ok=False)

    def test_queue_orders_known_source_times_before_name(self):
        import web_fixture
        for i,name,source_time in [(1,'ТЕСТ А','14:00'),(2,'ТЕСТ Я','09:00'),(3,'ТЕСТ Б',None)]:
            message=self.cli('ingest',{'chat_id':self.a,'date':DAY,'text':'ТЕСТ время '+str(i),'time':source_time})
            term={'category':'материалы','rate':'10','rate_unit':'%','scope_confirmed':True,'review':False}
            company={'name':name,'inn':web_fixture.inn(i),'inn_status':'verified','confirmation_kind':'user','confirmation_source':'ТЕСТ'}
            offer=self.cli('import',{'message_id':message['message_id'],'offers':[{'company':company,'terms':[term]}]})
            self.cli('pricing',{'term_id':offer['term_ids'][0],'markup':'0','reason':'ТЕСТ'})
        items=self.tasks(); self.assertEqual([x['company'] for x in items],['ТЕСТ Я','ТЕСТ А','ТЕСТ Б'])
        self.assertEqual([x['source_time'] for x in items],['09:00','14:00',None])

    def test_unmatched_attachment_copy_must_be_intact_before_resolution(self):
        message,offer=self.offer(); source=self.root/'unmatched.pdf'; source.write_bytes(b'TEST ORIGINAL')
        issue=self.cli('document',{'message_id':message['message_id'],'path':str(source)})
        data=next(x for x in self.cli('review')['document_issues'] if x['id']==issue['document_issue_id'])
        (self.root/data['path']).write_bytes(b'TEST TAMPERED')
        self.cli('document-issue-resolve',{'issue_id':issue['document_issue_id'],'company_id':offer['company_ids'][0],'reason':'ТЕСТ'},ok=False)
        self.assertIn(issue['document_issue_id'],[x['id'] for x in self.cli('review')['document_issues']])

    def test_cannot_resolve_missing_attachment_with_corrupt_document(self):
        message,offer=self.offer(); cid=offer['company_ids'][0]; mid=message['message_id']
        issue=self.cli('document',{'message_id':mid,'company_id':cid,'path':str(self.root/'missing.pdf')})
        source=self.root/'card.pdf'; source.write_bytes(b'TEST ORIGINAL')
        doc=self.cli('document',{'message_id':mid,'company_id':cid,'path':str(source)})
        (self.root/doc['path']).write_bytes(b'TEST CHANGED')
        self.cli('document-issue-resolve',{'issue_id':issue['document_issue_id'],'company_id':cid,'document_id':doc['document_id'],'reason':'ТЕСТ'},ok=False)
        self.assertIn(issue['document_issue_id'],[x['id'] for x in self.cli('review')['document_issues']])

    def test_review_v2_exposes_unagreed_price_and_corrupt_card(self):
        message,offer=self.offer(); tid=offer['term_ids'][0]
        review=self.cli('review'); item=next(x for x in review['items'] if x['term_id']==tid)
        self.assertIn('markup_not_agreed',item['reasons'])
        source=self.root/'card.pdf'; source.write_bytes(b'TEST verified')
        doc=self.cli('document',{'message_id':message['message_id'],'company_id':offer['company_ids'][0],'path':str(source)})
        self.cli('update',{'action':'document_review','document_id':doc['document_id'],'status':'verified','document_inn':'7733446222','reason':'ТЕСТ','confirmation_kind':'user','confirmation_source':'ТЕСТ'})
        (self.root/doc['path']).write_bytes(b'TAMPERED')
        review=self.cli('review'); self.assertIn(doc['document_id'],[x['id'] for x in review['documents']])

    def test_missing_and_unmatched_attachments_preserve_and_resolve(self):
        message,offer=self.offer(); mid=message['message_id']; cid=offer['company_ids'][0]
        missing=self.cli('document',{'message_id':mid,'company_id':cid,'path':str(self.root/'missing.pdf')})
        self.assertFalse(missing['file_saved']); self.assertEqual(missing['status'],'review')
        file=self.root/'unmatched.pdf'; file.write_bytes(b'TEST ORIGINAL FILE')
        unmatched=self.cli('document',{'message_id':mid,'path':str(file)})
        self.assertTrue(unmatched['file_saved']); self.assertEqual(unmatched['status'],'review')
        issues={i['id']:i for i in self.cli('review')['document_issues']}
        self.assertEqual(issues[missing['document_issue_id']]['reason'],'file_unavailable')
        self.assertEqual(issues[unmatched['document_issue_id']]['reason'],'company_unresolved')
        saved=issues[unmatched['document_issue_id']]['path']; self.assertEqual((self.root/saved).read_bytes(),file.read_bytes())
        backup=self.cli('backup',{'directory':'issue-backup'}); self.cli('restore',{'backup_directory':backup['path'],'destination':'issue-restored'})
        self.assertEqual((self.root/'issue-restored'/saved).read_bytes(),file.read_bytes())
        self.assertTrue(self.cli('verify')['valid'])
        resolved=self.cli('document-issue-resolve',{'issue_id':unmatched['document_issue_id'],'company_id':cid,'reason':'ТЕСТ связь уточнена'})
        self.assertEqual(resolved['document_status'],'unverified')
        self.assertNotIn(unmatched['document_issue_id'],[i['id'] for i in self.cli('review')['document_issues']])
        self.assertEqual((self.root/saved).read_bytes(),file.read_bytes()); self.assertTrue(self.cli('verify')['valid'])
        source=self.root/'provided.pdf'; source.write_bytes(b'TEST RECOVERED FILE')
        fixed=self.cli('document-issue-resolve',{'issue_id':missing['document_issue_id'],'path':str(source),'reason':'ТЕСТ файл получен'})
        self.assertEqual(fixed['document_status'],'unverified'); self.assertEqual(self.cli('review')['document_issues'],[])

    def test_pricing_requires_source_and_legacy_unknown_is_not_ready(self):
        _,offer=self.offer(); tid=offer['term_ids'][0]
        self.cli('pricing',{'term_id':tid,'markup':'1','reason':'ТЕСТ','source':None},ok=False)
        result=self.cli('pricing',{'term_id':tid,'markup':'1','reason':'ТЕСТ','source':'ТЕСТ пользователь'})
        self.assertEqual(self.tasks()[0]['final_rate'],'11')
        db=sqlite3.connect(self.db)
        try:
            db.execute("UPDATE history SET data=? WHERE entity='pricing' AND entity_id=? AND action='set'",(json.dumps({'term_id':tid,'markup':'1','reason':'старая запись','source':None}),result['pricing_id']))
            db.commit()
        finally: db.close()
        item=self.tasks()[0]; self.assertEqual(item['status'],'review'); self.assertIsNone(item['final_rate']); self.assertIn('pricing_source_unknown',item['reasons'])
        self.cli('pricing',{'term_id':tid,'markup':'1','reason':'ТЕСТ уточнение','source':'ТЕСТ пользователь'})
        self.assertEqual(self.tasks()[0]['final_rate'],'11')

    def test_group_pricing_is_atomic_scoped_and_shared_with_plan(self):
        msg=self.cli('ingest',{'chat_id':self.a,'date':DAY,'text':'ТЕСТ три варианта'})
        terms=[{'category':'материалы','rate':rate,'rate_unit':'%','scope_confirmed':True,'review':False} for rate in ('10','18','20')]
        offer=self.cli('import',{'message_id':msg['message_id'],'offers':[{'company':{'name':'ТЕСТ Компания','inn':'7733446222','inn_status':'verified','confirmation_kind':'user','confirmation_source':'ТЕСТ пользователь'},'terms':terms}]})
        ids=offer['term_ids']
        self.cli('pricing',{'term_ids':[ids[0],999999],'markup':'1','reason':'ТЕСТ группа','source':'ТЕСТ пользователь'},ok=False)
        self.assertTrue(all(x['final_rate'] is None for x in self.tasks()))
        result=self.cli('pricing',{'term_ids':ids[:2],'markup':'1','reason':'ТЕСТ группа','source':'ТЕСТ пользователь'})
        tasks=self.tasks(); plan=self.cli('plan',{'chat_id':self.b,'date':DAY})['items']
        self.assertEqual([(x['term_id'],x['final_rate']) for x in tasks],[(x['term_id'],x['final_rate']) for x in plan])
        prices={x['term_id']:x['final_rate'] for x in tasks}; self.assertEqual(prices,{ids[0]:'11',ids[1]:'19',ids[2]:None})
        history=self.cli('history')['history']; rule=next(x for x in history if x['id']==result['pricing_rule_id'] and x['entity']=='pricing_rule')
        data=json.loads(rule['data']); self.assertEqual(data['term_ids'],ids[:2]); self.assertEqual(data['source'],'ТЕСТ пользователь')

    def test_source_comparison_respects_duration_day_type(self):
        source2=self.cli('chat',{'name':'Source C'})['chat_id']; cid=None
        for source,day_type in [(self.a,'calendar'),(source2,'working')]:
            msg=self.cli('ingest',{'chat_id':source,'date':DAY,'text':'ТЕСТ разные сроки'})
            term={'category':'материалы','rate':'13','rate_unit':'%','rate_type':'комиссия','rate_basis':'платёж','vat':'22%','payment':'перевод','duration_text':'1–2 дня','duration':1,'duration_max':2,'duration_unit':'day','duration_day_type':day_type,'color':'green','color_source':'ТЕСТ','color_date':DAY,'conditions':'нет','restrictions':'нет','amount':'нет','minimum':'нет','currency':'нет','scope_confirmed':True,'review':False}
            offer={'terms':[term]}; offer.update({'company_id':cid} if cid else {'company':{'name':'ТЕСТ Компания','inn':'7733446222','inn_status':'verified','confirmation_kind':'user','confirmation_source':'ТЕСТ пользователь'}})
            result=self.cli('import',{'message_id':msg['message_id'],'offers':[offer]}); cid=result['company_ids'][0]
            self.cli('pricing',{'term_id':result['term_ids'][0],'markup':'0','reason':'ТЕСТ'})
        items=self.tasks(); self.assertEqual(len(items),2)
        self.assertTrue(all(not x['comparison']['comparable'] and x['status']=='review' for x in items))

    def test_lifecycle_roles_withdraw_reopen_and_replace_after_send(self):
        _,offer=self.offer(); tid=offer['term_ids'][0]; self.cli('pricing',{'term_id':tid,'markup':'0','reason':'ТЕСТ без наценки'})
        item=self.tasks()[0]; task_id=item['id']; self.assertEqual(item['status'],'pending')
        self.cli('recipient',{'chat_id':self.b,'enabled':False,'reason':'ТЕСТ роль снята'})
        inactive=self.tasks()[0]; self.assertEqual(inactive['id'],task_id); self.assertIn('recipient_inactive',inactive['reasons'])
        self.cli('recipient',{'chat_id':self.b,'enabled':True,'reason':'ТЕСТ роль возвращена'})
        self.assertEqual(self.tasks()[0]['id'],task_id); self.assertEqual(self.tasks()[0]['status'],'pending')
        self.cli('update',{'action':'withdraw','term_id':tid,'reason':'ТЕСТ снято'})
        withdrawn=self.tasks()[0]; self.assertEqual(withdrawn['status'],'withdrawn'); self.assertEqual(withdrawn['withdrawal']['action'],'withdraw'); self.assertEqual(withdrawn['withdrawal']['reason'],'ТЕСТ снято')
        self.cli('update',{'action':'reopen','term_id':tid,'reason':'ТЕСТ вновь подтверждено','source':'ТЕСТ пользователь'})
        current=self.tasks()[0]; self.assertEqual((current['id'],current['status']),(task_id,'pending'))
        block=self.prepare(current); self.cli('web-confirm',{'block_id':block['id'],'version':block['version'],'operation_id':'lifecycle-send','task_ids':[task_id],'document_ids':[]})
        newterm={'category':'материалы','rate':'12','rate_unit':'%','scope_confirmed':True,'review':False}
        replacement=self.cli('update',{'action':'replace','term_id':tid,'reason':'ТЕСТ новые условия','terms':newterm})['new_term_id']
        items=self.tasks(); old=next(x for x in items if x['term_id']==tid); new=next(x for x in items if x['term_id']==replacement)
        self.assertEqual(old['status'],'sent'); self.assertEqual(new['status'],'review'); self.assertIn('company_previously_sent_review',new['reasons'])
        self.assertIn('markup_not_agreed',new['reasons']); self.assertNotEqual(old['id'],new['id'])

    def test_actual_document_added_after_preparation_requires_explicit_fact(self):
        message,offer=self.offer(); self.cli('pricing',{'term_id':offer['term_ids'][0],'markup':'0','reason':'ТЕСТ без наценки'})
        item=self.tasks()[0]; block=self.prepare(item)
        source=self.root/'later-card.txt'; source.write_text('ТЕСТ новая непроверенная карточка')
        doc=self.cli('document',{'message_id':message['message_id'],'company_id':offer['company_ids'][0],'path':str(source)})
        current=self.cli('web-block',{'block_id':block['id']}); self.assertTrue(current['stale'])
        self.assertFalse(next(d for d in current['document_candidates'] if d['id']==doc['document_id'])['ready'])
        fact={'block_id':block['id'],'version':block['version'],'operation_id':'actual-doc-test','task_ids':[],'document_ids':[doc['document_id']],'stale_send_confirmed':True}
        self.cli('web-confirm',fact,ok=False)
        sent=self.cli('web-confirm',{**fact,'actual_document_confirmed':True,'actual_document_reason':'ТЕСТ файл фактически приложен'})
        self.assertIn('непроверенный документ',sent['warning'])
        history=self.cli('history')['history']; entry=next(x for x in history if x['action']=='actual_send' and json.loads(x['data']).get('exceptional_document_ids')==[doc['document_id']])
        self.assertEqual(json.loads(entry['data'])['actual_document_reason'],'ТЕСТ файл фактически приложен')
        self.assertFalse(next(d for d in self.tasks()[0]['documents'] if d['id']==doc['document_id'])['ready'])

    def test_two_concurrent_confirmations_and_retry(self):
        _, offer=self.offer(); self.cli('pricing',{'term_id':offer['term_ids'][0],'markup':'0','reason':'ТЕСТ без наценки'})
        item=self.tasks()[0]; block=self.prepare(item)
        base={'block_id':block['id'],'version':block['version'],'task_ids':[item['id']],'document_ids':[]}
        barrier=threading.Barrier(2)
        def send(key):
            barrier.wait()
            p=subprocess.run([sys.executable,str(PROJECT/'trading.py'),'--root',str(self.root),'--db',str(self.db),'web-confirm'], input=json.dumps({**base,'operation_id':key}),text=True,capture_output=True,timeout=15)
            return json.loads(p.stdout)
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes=list(pool.map(send,['parallel-1','parallel-2']))
        self.assertEqual(sum(x['ok'] for x in outcomes),1,outcomes)
        key='parallel-1' if outcomes[0]['ok'] else 'parallel-2'
        retry=self.cli('web-confirm',{**base,'operation_id':key})
        self.assertEqual(retry['send_ids'],next(x['send_ids'] for x in outcomes if x['ok']))
        with sqlite3.connect(self.db) as db: self.assertEqual(db.execute('SELECT COUNT(*) FROM sends').fetchone()[0],1)

if __name__ == '__main__': unittest.main()
