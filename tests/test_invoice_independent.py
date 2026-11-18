"""Acceptance checks for invoice offers using disposable v2/v3 instances."""
import hashlib
from contextlib import closing
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
import web_core

DAY = '2026-11-18'


class InvoiceIndependent(unittest.TestCase):
    def cli(self, command, payload=None, ok=True):
        p = subprocess.run([sys.executable, str(PROJECT/'trading.py'), '--root', str(self.root),
                            '--db', 'registry.sqlite', command], input=json.dumps(payload or {}),
                           text=True, capture_output=True, timeout=20)
        try:
            result = json.loads(p.stdout)
        except ValueError:
            self.fail(f'{command}: invalid JSON: {p.stdout!r}, stderr={p.stderr!r}')
        self.assertEqual(result['ok'], ok, (command, result, p.stderr))
        return result

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name).resolve()
        self.root = base/'v1'
        self.cli('init')
        # Build a clean v2 fixture with the published v1→v2 schema migration.
        self.v2 = base/'v2'
        shutil.copytree(self.root, self.v2)
        with closing(sqlite3.connect(self.v2/'registry.sqlite')) as db:
            db.executescript(web_core.V2)
        self.root = self.v2
        self.a = self.cli('chat', {'name':'TEST Source A'})['chat_id']
        self.b = self.cli('chat', {'name':'TEST Recipient B'})['chat_id']
        self.c = self.cli('chat', {'name':'TEST Recipient C'})['chat_id']
        for chat in (self.b, self.c): self.cli('recipient', {'chat_id':chat, 'enabled':True})
        msg, old = self.company_offer(self.a)
        self.cid = old['company_ids'][0]
        self.old_tid = old['term_ids'][0]
        self.cli('pricing', {'term_id':self.old_tid,'markup':'0','source':'TEST user','reason':'TEST'})
        path = self.v2/'old-card.pdf'; path.write_bytes(b'TEST old company document')
        self.old_doc = self.cli('document', {'message_id':msg,'company_id':self.cid,'path':str(path)})
        self.cli('update', {'action':'document_review','document_id':self.old_doc['document_id'],
                             'status':'verified','document_inn':'7733446222','reason':'TEST',
                             'confirmation_kind':'user','confirmation_source':'TEST user'})
        self.before = self.snapshot(self.v2)
        self.root = base/'v3'
        web_core.migrate_copy(self.v2, self.root)

    def snapshot(self, root):
        with closing(sqlite3.connect(root/'registry.sqlite')) as db:
            db.row_factory = sqlite3.Row
            tables = ('chats','companies','messages','offers','terms','documents','blocks','sends','history')
            rows = {table:[tuple(row) for row in db.execute(f'SELECT * FROM {table} ORDER BY id')] for table in tables}
            files = web_core.t.registered_files(db)
            return rows, {p:hashlib.sha256((root/p).read_bytes()).hexdigest() for p, _ in files}

    def message(self, chat, text='TEST source message'):
        return self.cli('ingest', {'chat_id':chat,'date':DAY,'text':text})['message_id']

    def company_offer(self, chat, cid=None):
        mid = self.message(chat)
        offer = {'terms':[{'category':'стройматериалы','rate':'10','rate_unit':'%', 'scope_confirmed':True,'review':False}]}
        if cid: offer['company_id'] = cid
        else: offer['company'] = {'name':'TEST Company','inn':'7733446222','inn_status':'verified',
                                  'confirmation_kind':'user','confirmation_source':'TEST user'}
        return mid, self.cli('import', {'message_id':mid,'offers':[offer]})

    def invoice(self, chat, number, cid=None, existing=None, rate='10'):
        mid = self.message(chat, 'TEST invoice '+number)
        cid = cid or self.cid
        iid = existing or self.cli('invoice-create', {'company_id':cid,'message_id':mid,
            'data':{'number':number,'date':DAY,'amount':('12345.67' if number == 'A-1' else str(20000 + int(hashlib.sha256(number.encode()).hexdigest()[:8],16)) + '.67'),'currency':'RUB',
                    'subject':'стройматериалы','vat':'22%'}, 'reason':'TEST source'})['invoice_id']
        offer = {'company_id':cid,'invoice_id':iid,'invoice_link_reason':'TEST explicitly linked',
                 'terms':[{'category':'стройматериалы','rate':rate,'rate_unit':'%', 'scope_confirmed':True,'review':False}]}
        result = self.cli('import', {'message_id':mid,'offers':[offer]})
        return mid, iid, result['term_ids'][0]

    def doc(self, mid, iid, content, cid=None):
        path = self.root/('input-'+str(mid)+'.pdf'); path.write_bytes(content)
        return self.cli('document', {'message_id':mid,'company_id':cid or self.cid,'path':str(path),
                                     'classification':'invoice','invoice_id':iid})

    def verify_doc(self, doc):
        self.cli('update', {'action':'document_review','document_id':doc['document_id'],
            'status':'verified','document_inn':'7733446222','reason':'TEST verified',
            'confirmation_kind':'user','confirmation_source':'TEST user'})

    def price(self, term, markup='0'):
        self.cli('pricing', {'term_id':term,'markup':markup,'source':'TEST user','reason':'TEST'})

    def tasks(self, chat=None):
        return self.cli('tasks', {'chat_id':chat or self.b,'date':DAY})['items']

    def task(self, tid, chat=None):
        return next(x for x in self.tasks(chat) if x['term_id']==tid)

    def prepare(self, chat, items):
        return self.cli('web-prepare', {'chat_id':chat,'date':DAY,'task_ids':[x['id'] for x in items],
                 'versions':{str(x['id']):x['version'] for x in items}})

    def test_migration_preserves_source_and_legacy_workflow(self):
        self.assertEqual(self.snapshot(self.v2), self.before)
        after = self.snapshot(self.root)
        for table, rows in self.before[0].items():
            # Migration may add columns, but existing values and IDs must survive.
            self.assertEqual(rows, [r[:len(rows[0])] for r in after[0][table]] if rows else [])
        self.assertEqual(after[1], self.before[1])
        with closing(sqlite3.connect(self.root/'registry.sqlite')) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],3)
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0],'ok')
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(),[])
        item = self.task(self.old_tid)
        self.assertEqual(item['status'],'pending')
        block = self.prepare(self.b,[item])
        self.assertIn('TEST Company',block['text']); self.assertIn('10%',block['text'])
        self.assertIn(self.old_doc['document_id'], [x['id'] for x in block['documents']])

    def test_distinct_invoices_documents_provenance_and_pricing(self):
        m1,i1,t1=self.invoice(self.a,'A-1'); m2,i2,t2=self.invoice(self.a,'A-2')
        self.assertNotEqual(i1,i2)
        d1=self.doc(m1,i1,b'TEST invoice 1'); d2=self.doc(m2,i2,b'TEST invoice 2')
        self.verify_doc(d1); self.verify_doc(d2)
        self.cli('invoice-review',{'invoice_id':i1,'reason':'TEST reviewed','source':'TEST user'})
        self.cli('invoice-review',{'invoice_id':i2,'reason':'TEST reviewed','source':'TEST user'})
        self.assertEqual(self.task(t1)['status'],'review')
        self.price(t1,'1'); self.price(t2,'0')
        first,second=self.task(t1),self.task(t2)
        self.assertEqual(first['final_rate'],'11'); self.assertEqual(second['final_rate'],'10')
        self.assertIn(d1['document_id'],[d['id'] for d in first['documents']])
        self.assertNotIn(d2['document_id'],[d['id'] for d in first['documents']])
        self.assertIn(d2['document_id'],[d['id'] for d in second['documents']])
        self.assertNotIn(d1['document_id'],[d['id'] for d in second['documents']])
        block=self.prepare(self.b,[first]); self.assertIn('Оплата счёта',block['text'])
        self.assertIn('12345',block['text']); self.assertIn('11%',block['text'])
        self.assertNotIn('TEST Source A',block['text']); self.assertNotIn('наценка',block['text'].lower())
        self.assertNotIn(d2['document_id'],[d['id'] for d in block['documents']])
        current=self.cli('web-block',{'block_id':block['id']})
        self.assertNotIn(d2['document_id'],[d['id'] for d in current['document_candidates']])
        # Explicitly reusing the same invoice preserves both source offers.
        src=self.cli('chat',{'name':'TEST Source D'})['chat_id']
        _,same,t3=self.invoice(src,'A-1',existing=i1,rate='9')
        self.assertEqual(same,i1)
        items={x['term_id']:x for x in self.tasks()}
        self.assertIn(t1,items); self.assertIn(t3,items)
        self.assertNotEqual(items[t1]['source_chat'],items[t3]['source_chat'])
        self.assertFalse(items[t2].get('comparison', {}).get('comparable', False))

    def test_own_invoice_excluded_and_own_company_requires_review(self):
        own_msg,own_id,own_tid=self.invoice(self.b,'OWN')
        _,_,other_tid=self.invoice(self.a,'OWN',existing=own_id)
        self.price(own_tid); self.price(other_tid)
        self.assertEqual(self.task(other_tid)['status'],'excluded')
        self.assertEqual(self.task(self.old_tid)['status'],'excluded')
        # A separate invoice of a company offered by the recipient needs review.
        self.company_offer(self.c,self.cid)
        _,other_id,other_term=self.invoice(self.a,'OTHER')
        self.price(other_term)
        self.assertEqual(self.task(other_term,self.c)['status'],'review')
        self.assertNotEqual(other_id,own_id)

    def test_partial_send_repeat_and_backup(self):
        m1,i1,t1=self.invoice(self.a,'P-1'); m2,i2,t2=self.invoice(self.a,'P-2')
        d1=self.doc(m1,i1,b'TEST file 1'); d2=self.doc(m2,i2,b'TEST file 2')
        for d in (d1,d2): self.verify_doc(d)
        for i in (i1,i2): self.cli('invoice-review',{'invoice_id':i,'reason':'TEST reviewed','source':'TEST user'})
        for t in (t1,t2): self.price(t)
        first,second=self.task(t1),self.task(t2)
        block=self.prepare(self.b,[first,second])
        payload={'block_id':block['id'],'version':block['version'],'operation_id':'TEST-invoice-partial',
                 'task_ids':[first['id']],'document_ids':[d1['document_id']]}
        result=self.cli('web-confirm',payload)
        self.assertEqual(self.cli('web-confirm',payload)['send_ids'],result['send_ids'])
        self.assertEqual(self.task(t1)['status'],'sent')
        self.assertNotEqual(self.task(t2)['status'],'sent')
        self.assertNotEqual(self.task(t1,self.c)['status'],'sent')
        another=self.cli('chat',{'name':'TEST Source E'})['chat_id']
        _,_,t3=self.invoice(another,'P-1',existing=i1,rate='9')
        self.price(t3)
        self.assertEqual(self.task(t3)['status'],'review')
        self.assertNotEqual(self.task(t2)['status'],'sent')
        backup=self.cli('backup',{'directory':'TEST-backup'})
        self.cli('restore',{'backup_directory':backup['path'],'destination':'TEST-restored'})
        restored=self.root/'TEST-restored'
        self.assertEqual((restored/d1['path']).read_bytes(),b'TEST file 1')
        self.assertEqual((restored/d2['path']).read_bytes(),b'TEST file 2')
        with closing(sqlite3.connect(restored/'registry.sqlite')) as db:
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0],'ok')
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(),[])
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],3)

    def test_sent_invoice_cannot_be_relinked_and_snapshot_is_preserved(self):
        _,first,tid=self.invoice(self.a,'LINK-SENT')
        self.price(tid)
        self.cli('invoice-review',{'invoice_id':first,'reason':'TEST reviewed','source':'TEST user'})
        item=self.task(tid); block=self.prepare(self.b,[item])
        self.cli('web-confirm',{'block_id':block['id'],'version':block['version'],
             'operation_id':'TEST-link-sent','task_ids':[item['id']],'document_ids':[]})
        before=self.cli('web-block',{'block_id':block['id']})
        _,second,other=self.invoice(self.a,'LINK-SENT')
        self.assertNotEqual(first,second)
        self.assertIn(second,[m['id'] for m in self.task(tid)['possible_invoice_matches']])
        self.cli('invoice-link-offer',{'term_id':tid,'invoice_id':second,
                 'reason':'TEST identified','source':'TEST user'},ok=False)
        after=self.cli('web-block',{'block_id':block['id']})
        self.assertEqual(after['text'],before['text'])
        self.assertEqual(after['items'][0]['invoice']['id'],first)
        self.assertEqual(self.task(tid)['invoice']['id'],first)
        self.assertEqual(self.task(tid)['status'],'sent')
        self.assertEqual(self.task(other)['invoice']['id'],second)

    def test_explicit_invoice_relink_clears_resolved_possible_match(self):
        _,first,t1=self.invoice(self.a,'LINK-UNSENT')
        source=self.cli('chat',{'name':'TEST Second source'})['chat_id']
        _,second,t2=self.invoice(source,'LINK-UNSENT')
        self.assertNotEqual(first,second)
        self.assertIn(second,[m['id'] for m in self.task(t1)['possible_invoice_matches']])
        self.assertEqual(self.task(t1)['status'],'review')
        found=next(x for x in self.cli('search',{'date':DAY,'q':'LINK-UNSENT'})['items'] if x['term_id']==t1)
        self.assertEqual(found['match'],'possible')
        self.assertIn('possible_invoice_match_review',found['unknown'])
        self.cli('invoice-link-offer',{'term_id':t2,'invoice_id':first,
                 'reason':'TEST identity confirmed','source':'TEST user'})
        self.assertEqual(self.task(t2)['invoice']['id'],first)
        for tid in (t1,t2):
            self.assertNotIn(second,[m['id'] for m in self.task(tid)['possible_invoice_matches']])
        self.assertNotEqual(self.task(t1)['source_chat'],self.task(t2)['source_chat'])

    def test_invoice_file_used_as_provenance_cannot_be_relinked(self):
        mid,first,t1=self.invoice(self.a,'PROVENANCE-1')
        _,second,t2=self.invoice(self.a,'PROVENANCE-2')
        doc=self.doc(mid,first,b'TEST cited invoice')
        data={'number':'PROVENANCE-1','date':DAY,'amount':'12345.67','currency':'RUB',
              'field_provenance':{'amount':{'document_id':doc['document_id'],
                                            'operator_text':'TEST operator read amount'}}}
        self.cli('invoice-update',{'invoice_id':first,'data':data,'reason':'TEST evidence'})
        self.cli('invoice-link-document',{'document_id':doc['document_id'],'invoice_id':second,
                 'reason':'TEST relink attempt','source':'TEST user'},ok=False)
        first_docs=[d['id'] for d in self.task(t1)['documents']]
        second_docs=[d['id'] for d in self.task(t2)['documents']]
        self.assertIn(doc['document_id'],first_docs)
        self.assertNotIn(doc['document_id'],second_docs)
        self.assertEqual(self.task(t1)['invoice']['field_provenance']['amount']['document_id'],doc['document_id'])

    def test_new_company_can_be_created_for_invoice_without_fake_offer(self):
        c={'name':'TEST Invoice-only','inn':'7707083893','inn_status':'verified'}
        self.cli('company-create',{'company':c},ok=False)
        self.cli('company-create',{'company':{**c,'inn':'123','confirmation_kind':'user',
                 'confirmation_source':'TEST user'}},ok=False)
        c.update(confirmation_kind='user',confirmation_source='TEST user')
        cid=self.cli('company-create',{'company':c,'source':'TEST user'})['company_id']
        self.assertEqual(self.cli('company-create',{'company':c})['company_id'],cid)
        self.assertFalse(any(x['company_id']==cid for x in self.tasks()))
        mid,iid,tid=self.invoice(self.a,'NEW-COMPANY',cid=cid)
        self.price(tid)
        self.cli('invoice-review',{'invoice_id':iid,'reason':'TEST reviewed','source':'TEST user'})
        item=self.task(tid)
        self.assertEqual(item['company_id'],cid)
        self.assertEqual(item['offer_type'],'invoice')
        self.assertEqual(item['status'],'pending')
        self.assertEqual([x['term_id'] for x in self.tasks() if x['company_id']==cid],[tid])
        self.assertEqual(self.cli('search',{'date':DAY,'q':'7707083893','offer_type':'company'})['items'],[])
        self.assertEqual([x['term_id'] for x in self.cli('search',{'date':DAY,'q':'7707083893','offer_type':'invoice'})['items']],[tid])

    def test_missing_invoice_file_remains_in_review_until_resolved(self):
        mid,iid,tid=self.invoice(self.a,'MISSING')
        issue=self.cli('document',{'message_id':mid,'company_id':self.cid,
            'path':str(self.root/'absent.pdf'),'classification':'invoice','invoice_id':iid})
        self.assertFalse(issue['file_saved'])
        self.price(tid)
        self.cli('invoice-review',{'invoice_id':iid,'reason':'TEST reviewed','source':'TEST user'})
        item=self.task(tid)
        self.assertEqual(item['status'],'review',item)
        self.assertIn('invoice_document_review',item['reasons'])
        self.assertIn(issue['document_issue_id'],[x['id'] for x in self.cli('review')['document_issues']])
        self.cli('web-prepare',{'chat_id':self.b,'date':DAY,'task_ids':[item['id']],
                 'versions':{str(item['id']):item['version']}},ok=False)
        supplied=self.root/'supplied.pdf'; supplied.write_bytes(b'TEST supplied invoice')
        fixed=self.cli('document-issue-resolve',{'issue_id':issue['document_issue_id'],
                           'path':str(supplied),'reason':'TEST supplied file'})
        self.assertNotIn(issue['document_issue_id'],[x['id'] for x in self.cli('review')['document_issues']])
        self.verify_doc(fixed)
        self.cli('invoice-review',{'invoice_id':iid,'reason':'TEST reviewed after file','source':'TEST user'})
        self.assertEqual(self.task(tid)['status'],'pending')

    def test_download_allows_unverified_intact_but_requires_correct_link(self):
        import http.client
        import threading
        import web
        m,i,t=self.invoice(self.a,'DOWNLOAD')
        d=self.doc(m,i,b'TEST immutable invoice')
        # An invoice cannot be registered as a document of another company.
        other_msg=self.message(self.a)
        other=self.cli('import',{'message_id':other_msg,'offers':[{'company':{
            'name':'TEST Other','inn':'7707083893','inn_status':'verified',
            'confirmation_kind':'user','confirmation_source':'TEST user'},
            'terms':[{'category':'услуги','rate':'10','rate_unit':'%', 'scope_confirmed':True,'review':False}]}]})
        path=self.root/'wrong.pdf'; path.write_bytes(b'TEST wrong')
        self.cli('document',{'message_id':other_msg,'company_id':other['company_ids'][0],
                 'path':str(path),'classification':'invoice','invoice_id':i},ok=False)
        salt=b'test-salt-123456'; password='temporary test pass'
        (self.root/'local-auth.json').write_text(json.dumps({'salt':salt.hex(),'hash':web.password_hash(password,salt)}))
        server=web.ThreadingHTTPServer(('127.0.0.1',0),web.Handler); server.root=self.root
        threading.Thread(target=server.serve_forever,daemon=True).start()
        self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        host='127.0.0.1:%d' % server.server_port
        def req(method,path,data=None,headers=None):
            conn=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=5)
            h={'Host':host,**(headers or {})}; body=None if data is None else json.dumps(data)
            if body is not None: h['Content-Type']='application/json'
            conn.request(method,path,body,h); response=conn.getresponse()
            result=(response.status,dict(response.getheaders()),response.read()); conn.close(); return result
        _,headers,_=req('POST','/api/login',{'password':password},{'Origin':'http://'+host})
        auth={'Cookie':headers['Set-Cookie'].split(';')[0]}
        url='/api/documents/%d' % d['document_id']
        status,_,body=req('GET',url,headers=auth)
        self.assertEqual((status,body),(200,b'TEST immutable invoice'))
        db=web_core.connect(self.root)
        try:
            candidate=next(x for x in web_core.documents(db,self.root,self.cid) if x['id']==d['document_id'])
            self.assertTrue(candidate['intact']); self.assertFalse(candidate['ready'])
        finally: db.close()
        self.verify_doc(d)
        db=web_core.connect(self.root)
        try:
            candidate=next(x for x in web_core.documents(db,self.root,self.cid) if x['id']==d['document_id'])
            self.assertTrue(candidate['ready'],candidate)
        finally: db.close()
        status,_,body=req('GET',url,headers=auth)
        self.assertEqual((status,body),(200,b'TEST immutable invoice'), self.task(t)['documents'])
        db=web_core.connect(self.root)
        try:
            db.execute('UPDATE documents SET company_id=? WHERE id=?',(other['company_ids'][0],d['document_id'])); db.commit()
            self.assertNotEqual(req('GET',url,headers=auth)[0],200)
            db.execute('UPDATE documents SET company_id=? WHERE id=?',(self.cid,d['document_id'])); db.commit()
        finally: db.close()
        (self.root/d['path']).write_bytes(b'TEST tampered')
        self.assertNotEqual(req('GET',url,headers=auth)[0],200)
        (self.root/d['path']).unlink()
        self.assertNotEqual(req('GET',url,headers=auth)[0],200)
        self.assertNotEqual(req('GET','/api/documents/../../etc/passwd',headers=auth)[0],200)


if __name__ == '__main__': unittest.main()
