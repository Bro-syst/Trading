"""Web upload tests on disposable v3 registries."""
import base64
import http.client
import json
import sqlite3
import threading
import unittest
from contextlib import closing
from unittest.mock import patch
import test_invoice_independent as fixture
import web
import web_core

class DocumentUpload(unittest.TestCase):
    setUp=fixture.InvoiceIndependent.setUp
    cli=fixture.InvoiceIndependent.cli
    snapshot=fixture.InvoiceIndependent.snapshot
    message=fixture.InvoiceIndependent.message
    company_offer=fixture.InvoiceIndependent.company_offer
    invoice=fixture.InvoiceIndependent.invoice

    def test_current_texts_are_carried_forward_only_for_unchanged_variants(self):
        _,iid,tid=self.invoice(self.a,'UPLOAD-CARRY')
        _,_,shared=self.invoice(self.a,'UPLOAD-SHARED',existing=iid)
        _,_,stale=self.invoice(self.a,'UPLOAD-STALE',existing=iid)
        _,other_iid,other=self.invoice(self.a,'UPLOAD-OTHER')
        def items(db):
            return {x['term_id']:x for x in web_core.search(db,self.root,{'date':fixture.DAY})['items']}
        with closing(web_core.connect(self.root)) as db:
            for ident in (self.old_tid,tid,shared,stale,other):
                x=items(db)[ident]
                web_core.save_public_text(db,self.root,ident,{'date':fixture.DAY,'version':x['version'],'revision':None,'text':f'Текст {ident}'})
            raw=json.loads(db.execute('SELECT data FROM terms WHERE id=?',(stale,)).fetchone()[0]); raw['category']='изменено'
            db.execute('UPDATE terms SET data=? WHERE id=?',(json.dumps(raw),stale))
            old=items(db); saved=[]
            db.execute('BEGIN IMMEDIATE')
            result=web_core.upload_documents(db,self.root,tid,{'date':fixture.DAY,'version':old[tid]['version'],'files':[{'name':'new.pdf','data':base64.b64encode(b'file').decode()}]},saved)
            db.commit()
            new=items(db)
            for ident in (self.old_tid,tid,shared,other):
                self.assertFalse(new[ident]['public_text']['stale'])
                self.assertEqual(new[ident]['public_text']['text'],old[ident]['public_text']['text'])
                self.assertNotEqual(new[ident]['public_text']['revision'],old[ident]['public_text']['revision'])
            self.assertTrue(new[stale]['public_text']['stale'])
            self.assertEqual(new[stale]['public_text']['revision'],old[stale]['public_text']['revision'])
            rows=db.execute("SELECT data FROM history WHERE entity='public_text' AND entity_id=? ORDER BY id",(tid,)).fetchall()
            self.assertEqual(len(rows),2); self.assertEqual(json.loads(rows[1][0])['previous_revision'],old[tid]['public_text']['revision'])
            self.assertEqual(db.execute('SELECT status FROM documents WHERE id=?',(result['documents'][0]['document_id'],)).fetchone()[0],'unverified')
            selection={'date':fixture.DAY,'term_ids':[tid,shared], 'versions':{str(i):new[i]['version'] for i in (tid,shared)},'revisions':{str(i):new[i]['public_text']['revision'] for i in (tid,shared)}}
            self.assertIn(f'Текст {tid}',web_core.common_preview(db,self.root,selection)['text'])
            with self.assertRaises(ValueError):
                db.execute('BEGIN IMMEDIATE')
                try: web_core.upload_documents(db,self.root,tid,{'date':fixture.DAY,'version':old[tid]['version'],'files':[{'name':'new.pdf','data':'eA=='}]},[])
                finally: db.rollback()

    def test_unrelated_change_during_upload_does_not_refresh_text(self):
        with closing(web_core.connect(self.root)) as db:
            item=next(x for x in web_core.search(db,self.root,{'date':fixture.DAY})['items'] if x['term_id']==self.old_tid)
            web_core.save_public_text(db,self.root,self.old_tid,{'date':fixture.DAY,'version':item['version'],'revision':None,'text':'Сохранённый текст'})
            original=web_core.t.execute
            def change_data(*args):
                result=original(*args)
                row=db.execute('SELECT data FROM terms WHERE id=?',(self.old_tid,)).fetchone()
                data=json.loads(row[0]); data['category']='новые условия'
                db.execute('UPDATE terms SET data=? WHERE id=?',(json.dumps(data),self.old_tid))
                return result
            db.execute('BEGIN IMMEDIATE')
            with patch.object(web_core.t,'execute',side_effect=change_data):
                web_core.upload_documents(db,self.root,self.old_tid,{'date':fixture.DAY,'version':item['version'],'files':[{'name':'new.pdf','data':'eA=='}]},[])
            db.commit()
            item=next(x for x in web_core.search(db,self.root,{'date':fixture.DAY})['items'] if x['term_id']==self.old_tid)
            self.assertTrue(item['public_text']['stale'])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM history WHERE entity='public_text' AND entity_id=?",(self.old_tid,)).fetchone()[0],1)

    def test_upload_and_failures(self):
        mid,iid,tid=self.invoice(self.a,'UPLOAD')
        salt=b'test-salt-123456'; password='temporary test pass'
        (self.root/'local-auth.json').write_text(json.dumps({'salt':salt.hex(),'hash':web.password_hash(password,salt)}))
        server=web.ThreadingHTTPServer(('127.0.0.1',0),web.Handler); server.root=self.root
        threading.Thread(target=server.serve_forever,daemon=True).start()
        self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        host='127.0.0.1:%d'%server.server_port
        def req(path,p,headers):
            conn=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=10)
            conn.request('POST',path,json.dumps(p),{'Host':host,'Content-Type':'application/json',**headers})
            response=conn.getresponse(); body=json.loads(response.read()); status=response.status; h=dict(response.getheaders()); conn.close(); return status,h,body
        origin={'Origin':'http://'+host}
        _,headers,login=req('/api/login',{'password':password},origin)
        auth={**origin,'Cookie':headers['Set-Cookie'].split(';')[0],'X-CSRF-Token':login['csrf']}
        with closing(web_core.connect(self.root)) as db:
            current=next(x for x in web_core.search(db,self.root,{'date':fixture.DAY})['items'] if x['term_id']==tid)
            old_doc=next(x for x in web_core.documents(db,self.root,self.cid) if x['id']==self.old_doc['document_id'])
            old_snapshot=web_core.t.canonical(old_doc)
            self.assertNotIn('uploaded_by_user',old_doc)
        endpoint=f'/api/terms/{tid}/documents'
        files=[{'name':'счёт.pdf','data':base64.b64encode(b'first').decode()},{'name':'extra.txt','data':base64.b64encode(b'second').decode()}]
        p={'date':fixture.DAY,'version':current['version'],'files':files}
        self.assertEqual(req(endpoint,p,origin)[0],401)
        self.assertEqual(req(endpoint,p,{**auth,'X-CSRF-Token':'wrong'})[0],403)
        self.assertEqual(req(endpoint,{**p,'files':[{'name':'../unsafe','data':'YQ=='}]},auth)[0],409)
        self.assertEqual(req(endpoint,{**p,'files':[{'name':'file','data':'!'}]},auth)[0],409)
        status,_,result=req(endpoint,p,auth); self.assertEqual(status,200,result)
        self.assertNotEqual(result['version'],p['version']); self.assertEqual(len(result['documents']),2)
        for doc,expected in zip(result['documents'],(b'first',b'second')):
            conn=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=10)
            conn.request('GET',f"/api/documents/{doc['document_id']}",headers={'Host':host,'Cookie':auth['Cookie']})
            response=conn.getresponse(); self.assertEqual((response.status,response.read()),(200,expected)); conn.close()
        self.assertEqual(req(endpoint,p,auth)[0],409)
        with closing(sqlite3.connect(self.root/'registry.sqlite')) as db:
            for doc,expected in zip(result['documents'],(b'first',b'second')):
                row=db.execute('SELECT d.message_id,d.company_id,d.status,c.classification,c.invoice_id,d.path FROM documents d JOIN document_classification c ON c.document_id=d.id WHERE d.id=?',(doc['document_id'],)).fetchone()
                self.assertEqual(row[:5],(mid,self.cid,'unverified','invoice',iid)); self.assertEqual((self.root/row[5]).read_bytes(),expected)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM history WHERE entity='document' AND action='upload'").fetchone()[0],2)
        with closing(web_core.connect(self.root)) as db:
            docs=web_core.documents(db,self.root,self.cid)
            self.assertEqual(web_core.t.canonical(next(x for x in docs if x['id']==self.old_doc['document_id'])),old_snapshot)
            for registered in result['documents']:
                doc=next(x for x in docs if x['id']==registered['document_id'])
                timestamp=db.execute("SELECT at FROM history WHERE entity='document' AND entity_id=? AND action='upload'",(doc['id'],)).fetchone()['at']
                self.assertTrue(doc['uploaded_by_user']); self.assertEqual(doc['uploaded_at'],timestamp); self.assertTrue(timestamp)
        original=web.t.execute; calls=0
        def fail_second(*args):
            nonlocal calls
            calls+=1
            if calls==2: raise ValueError('simulated failure')
            return original(*args)
        before=set(self.root.rglob('*'))
        with patch.object(web.t,'execute',side_effect=fail_second):
            self.assertEqual(req(endpoint,{**p,'version':result['version']},auth)[0],409)
        self.assertEqual(set(self.root.rglob('*')),before)
        with closing(sqlite3.connect(self.root/'registry.sqlite')) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM history WHERE entity='document' AND action='upload'").fetchone()[0],2)
        with closing(web_core.connect(self.root)) as db:
            company=next(x for x in web_core.search(db,self.root,{'date':fixture.DAY})['items'] if x['term_id']==self.old_tid)
        company_payload={'date':fixture.DAY,'version':company['version'],'files':[files[0]]}
        status,_,uploaded=req(f'/api/terms/{self.old_tid}/documents',company_payload,auth)
        self.assertEqual(status,200,uploaded)
        with closing(sqlite3.connect(self.root/'registry.sqlite')) as db:
            self.assertEqual(db.execute('SELECT classification,invoice_id FROM document_classification WHERE document_id=?',(uploaded['documents'][0]['document_id'],)).fetchone(),('company',None))

if __name__=='__main__': unittest.main()
