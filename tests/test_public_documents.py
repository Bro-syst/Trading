"""Capability links for explicitly selected document/offer pairs."""
import http.client
import json
import threading
import unittest
from contextlib import closing
from unittest.mock import patch
import test_invoice_independent as fixture
import web
import web_core as core

class PublicDocuments(unittest.TestCase):
    setUp=fixture.InvoiceIndependent.setUp
    cli=fixture.InvoiceIndependent.cli
    snapshot=fixture.InvoiceIndependent.snapshot
    message=fixture.InvoiceIndependent.message
    company_offer=fixture.InvoiceIndependent.company_offer
    invoice=fixture.InvoiceIndependent.invoice

    def test_share_route_with_session_and_missing_public_address(self):
        salt=b'test-salt-123456'; password='temporary test pass'
        (self.root/'local-auth.json').write_text(json.dumps({'salt':salt.hex(),'hash':web.password_hash(password,salt)}))
        server=web.ThreadingHTTPServer(('127.0.0.1',0),web.Handler); server.root=self.root
        threading.Thread(target=server.serve_forever,daemon=True).start()
        self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        host=f'127.0.0.1:{server.server_port}'; origin={'Origin':'http://'+host}
        def request(method,path,p=None,headers=None):
            conn=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=10)
            conn.request(method,path,json.dumps(p) if p is not None else None,{'Host':host,'Content-Type':'application/json',**(headers or {})})
            res=conn.getresponse(); result=(res.status,dict(res.getheaders()),res.read()); conn.close(); return result
        status,headers,body=request('POST','/api/login',{'password':password},origin)
        self.assertEqual(status,200)
        auth={**origin,'Cookie':headers['Set-Cookie'].split(';')[0],'X-CSRF-Token':json.loads(body)['csrf']}
        endpoint=f"/api/terms/{self.old_tid}/documents/{self.old_doc['document_id']}/share"
        self.assertEqual(request('POST',endpoint,{'selected':True},origin)[0],401)
        self.assertEqual(request('POST',endpoint,{'selected':True},{**auth,'X-CSRF-Token':'wrong'})[0],403)
        with patch.dict('os.environ',{'PUBLIC_BASE_URL':''}):
            status,_,body=request('POST',endpoint,{'selected':True},auth)
            self.assertEqual(status,200); self.assertTrue(json.loads(body)['selected']); self.assertIsNone(json.loads(body)['url'])
            with closing(core.connect(self.root)) as db: self.assertIsNotNone(core.share_state(db,self.old_tid,self.old_doc['document_id']))
        with patch.dict('os.environ',{'PUBLIC_BASE_URL':'https://public.example'}):
            status,_,body=request('POST',endpoint,{'selected':True},auth)
            self.assertEqual(status,200); url=json.loads(body)['url']; path='/public/documents/'+url.rsplit('/',1)[1]
            self.assertEqual(request('GET',path)[0],200)
        with patch.dict('os.environ',{'PUBLIC_BASE_URL':''}):
            self.assertEqual(request('POST',endpoint,{'selected':False},auth)[0],200)
            self.assertEqual(request('GET',path)[0],404)

    def test_links_are_scoped_revocable_and_check_integrity(self):
        did=self.old_doc['document_id']; tid=self.old_tid
        _,created=self.company_offer(self.a, self.cid); other=created['term_ids'][0]
        with closing(core.connect(self.root)) as db, patch.dict('os.environ',{'PUBLIC_BASE_URL':'https://public.example'}):
            first=core.set_document_share(db,self.root,tid,did,{'selected':True})['url']
            second=core.set_document_share(db,self.root,other,did,{'selected':True})['url']
            self.assertNotEqual(first,second)
            token=first.rsplit('/',1)[1]; token2=second.rsplit('/',1)[1]
            self.assertTrue(core.shared_document(db,self.root,token)[1])
            core.set_document_share(db,self.root,tid,did,{'selected':False})
            with self.assertRaises(ValueError): core.shared_document(db,self.root,token)
            self.assertTrue(core.shared_document(db,self.root,token2)[1])
            doc=core.t.row(db,'documents',did); file=core.t.safe(self.root,doc['path']); file.write_bytes(b'changed')
            with self.assertRaises(ValueError): core.shared_document(db,self.root,token2)
            with self.assertRaises(ValueError): core.set_document_share(db,self.root,tid,did,{'selected':True})

    def test_configuration_and_classification_revision(self):
        did=self.old_doc['document_id']; tid=self.old_tid
        with closing(core.connect(self.root)) as db:
            for base in ('http://localhost:8765','https://127.0.0.1','https://192.168.1.2'):
                with patch.dict('os.environ',{'PUBLIC_BASE_URL':base}):
                    self.assertIsNone(core.set_document_share(db,self.root,tid,did,{'selected':True})['url'])
            with patch.dict('os.environ',{'PUBLIC_BASE_URL':'https://public.example'}):
                url=core.set_document_share(db,self.root,tid,did,{'selected':True})['url']; token=url.rsplit('/',1)[1]
                core.t.hist(db,'document',did,'classification',{'classification':'company'})
                core.t.hist(db,'document',did,'classification',{'classification':'legacy'})
                with self.assertRaises(ValueError): core.shared_document(db,self.root,token)
                core.set_document_share(db,self.root,tid,did,{'selected':False})
                replacement=core.set_document_share(db,self.root,tid,did,{'selected':True})['url']
                self.assertNotEqual(url,replacement)

    def test_public_base_url_syntax_and_ports(self):
        valid=('https://public.example', 'https://public.example:1', 'https://public.example:65535',
               'https://8.8.8.8:443', 'https://[2606:4700:4700::1111]:443', 'https://public.example/')
        invalid=('https://public.example:bad', 'https://public.example:', 'https://public.example:0',
                 'https://public.example:65536', 'https://public.example:-1', 'https://public.example:443:5',
                 'https://@public.example', 'https://user:password@public.example',
                 'https://bad host.example', 'https://bad_host.example', 'https://-bad.example',
                 'https://public.example..', 'https://[broken]', 'https://999.1.2.3',
                 'https://public.example/path', 'https://public.example?q=1', 'https://public.example/#fragment',
                 'https://public.example?', 'https://public.example#', 'https://public.example/?', 'https://public.example/#',
                 'https://public.example\\evil', 'https://0x7f000001', 'https://0x7f.1',
                 'https://127.0.0.0x1', 'https://0x7f.0x1',
                 'http://public.example', 'https://localhost.', 'https://127.0.0.1')
        for base in valid:
            with self.subTest(base=base), patch.dict('os.environ',{'PUBLIC_BASE_URL':base}):
                self.assertEqual(core.public_base(),base.rstrip('/'))
        for base in invalid:
            with self.subTest(base=base), patch.dict('os.environ',{'PUBLIC_BASE_URL':base}):
                with self.assertRaises(ValueError): core.public_base()

    def test_block_becomes_stale_when_url_changes_or_selection_is_revoked(self):
        did=self.old_doc['document_id']; tid=self.old_tid
        with closing(core.connect(self.root)) as db, patch.dict('os.environ',{'PUBLIC_BASE_URL':'https://public.example'}):
            core.set_document_share(db,self.root,tid,did,{'selected':True})
            tasks=core.queue(db,self.root,self.b,fixture.DAY); task=next(x for x in tasks if x['term_id']==tid)
            self.assertEqual(task['status'],'pending')
            b=core.prepare(db,self.root,{'chat_id':self.b,'date':fixture.DAY,'task_ids':[task['id']],'versions':{str(task['id']):task['version']}})
            self.assertIn('https://public.example/public/documents/',b['text'])
            self.assertFalse(core.block(db,self.root,b['id'])['stale'])
            with patch.dict('os.environ',{'PUBLIC_BASE_URL':'https://other.example'}): self.assertTrue(core.block(db,self.root,b['id'])['stale'])
            with patch.dict('os.environ',{'PUBLIC_BASE_URL':''}): self.assertTrue(core.block(db,self.root,b['id'])['stale'])
            core.set_document_share(db,self.root,tid,did,{'selected':False})
            self.assertTrue(core.block(db,self.root,b['id'])['stale'])

    def test_selection_without_address_preview_and_later_address(self):
        did=self.old_doc['document_id']; tid=self.old_tid
        with closing(core.connect(self.root)) as db, patch.dict('os.environ',{'PUBLIC_BASE_URL':''}):
            result=core.set_document_share(db,self.root,tid,did,{'selected':True})
            self.assertTrue(result['selected']); self.assertIsNone(result['url'])
            token=core.share_state(db,tid,did)['token']
            item=next(x for x in core.search(db,self.root,{'date':fixture.DAY})['items'] if x['term_id']==tid)
            p={'date':fixture.DAY,'term_ids':[tid],'versions':{str(tid):item['version']},'revisions':{str(tid):item['public_text']['revision']}}
            preview=core.common_preview(db,self.root,p)
            self.assertNotIn('/public/documents/',preview['text'])
            self.assertIn({'term_id':tid,'reason':core.SHARE_ADDRESS_WARNING},preview['warnings'])
            task=next(x for x in core.queue(db,self.root,self.b,fixture.DAY) if x['term_id']==tid)
            block=core.prepare(db,self.root,{'chat_id':self.b,'date':fixture.DAY,'task_ids':[task['id']],'versions':{str(task['id']):task['version']}})
            self.assertIn({'term_id':tid,'reason':core.SHARE_ADDRESS_WARNING},block['warnings'])
            self.assertIn({'term_id':tid,'reason':core.SHARE_ADDRESS_WARNING},core.block(db,self.root,block['id'])['warnings'])
            self.assertFalse(core.block(db,self.root,block['id'])['stale'])
            old_text=block['text']
            with patch.dict('os.environ',{'PUBLIC_BASE_URL':'https://public.example'}):
                old_block=core.block(db,self.root,block['id'])
                self.assertTrue(old_block['stale']); self.assertEqual(old_block['text'],old_text)
                expected_url='https://public.example/public/documents/'+token
                self.assertIn(expected_url,core.common_preview(db,self.root,p)['text'])
                new_block=core.prepare(db,self.root,{'chat_id':self.b,'date':fixture.DAY,'task_ids':[task['id']],'versions':{str(task['id']):task['version']}})
                self.assertIn(expected_url,new_block['text'])
                self.assertFalse(core.block(db,self.root,new_block['id'])['stale'])
                self.assertEqual(core.block(db,self.root,block['id'])['text'],old_text)
            core.set_document_share(db,self.root,tid,did,{'selected':False})
            self.assertIsNone(core.share_state(db,tid,did))
            with self.assertRaises(ValueError): core.shared_document(db,self.root,token)

    def test_without_address_integrity_and_independent_revocation(self):
        did=self.old_doc['document_id']; tid=self.old_tid
        _,created=self.company_offer(self.a,self.cid); other=created['term_ids'][0]
        with closing(core.connect(self.root)) as db, patch.dict('os.environ',{'PUBLIC_BASE_URL':''}):
            core.set_document_share(db,self.root,tid,did,{'selected':True})
            core.set_document_share(db,self.root,other,did,{'selected':True})
            token=core.share_state(db,tid,did)['token']; other_token=core.share_state(db,other,did)['token']
            self.assertNotEqual(token,other_token)
            core.set_document_share(db,self.root,tid,did,{'selected':False})
            with self.assertRaises(ValueError): core.shared_document(db,self.root,token)
            self.assertTrue(core.shared_document(db,self.root,other_token)[1])
            record=core.t.row(db,'documents',did)
            core.t.safe(self.root,record['path']).write_bytes(b'changed')
            with self.assertRaises(ValueError): core.set_document_share(db,self.root,tid,did,{'selected':True})
            item=next(x for x in core.search(db,self.root,{'date':fixture.DAY})['items'] if x['term_id']==other)
            p={'date':fixture.DAY,'term_ids':[other],'versions':{str(other):item['version']},'revisions':{str(other):item['public_text']['revision']}}
            with self.assertRaises(ValueError): core.common_preview(db,self.root,p)

    def test_public_http_and_other_routes_require_session(self):
        did=self.old_doc['document_id']; tid=self.old_tid
        with patch.dict('os.environ',{'PUBLIC_BASE_URL':'https://public.example'}), closing(core.connect(self.root)) as db:
            url=core.set_document_share(db,self.root,tid,did,{'selected':True})['url']; path='/public/documents/'+url.rsplit('/',1)[1]
            server=web.ThreadingHTTPServer(('127.0.0.1',0),web.Handler); server.root=self.root
            threading.Thread(target=server.serve_forever,daemon=True).start()
            self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
            def get(path,host=None):
                conn=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=10)
                conn.request('GET',path,headers={'Host':host or f'127.0.0.1:{server.server_port}'})
                res=conn.getresponse(); result=(res.status,dict(res.getheaders()),res.read()); conn.close(); return result
            status,headers,body=get(path,'public.example')
            self.assertEqual(status,200); self.assertEqual(body,b'TEST old company document')
            self.assertTrue(headers['Content-Disposition'].startswith('attachment;')); self.assertEqual(headers['X-Content-Type-Options'],'nosniff')
            self.assertEqual(get('/public/documents/'+'a'*43)[0],404)
            self.assertEqual(get(f'/api/documents/{did}')[0],401)
            core.set_document_share(db,self.root,tid,did,{'selected':False})
            self.assertEqual(get(path)[0],404)

    def test_common_preview_selection_and_revocation(self):
        did=self.old_doc['document_id']; tid=self.old_tid
        with closing(core.connect(self.root)) as db, patch.dict('os.environ',{'PUBLIC_BASE_URL':'https://public.example'}):
            item=next(x for x in core.search(db,self.root,{'date':fixture.DAY})['items'] if x['term_id']==tid)
            p={'date':fixture.DAY,'term_ids':[tid],'versions':{str(tid):item['version']},'revisions':{str(tid):item['public_text']['revision']}}
            self.assertNotIn('/public/documents/',core.common_preview(db,self.root,p)['text'])
            url=core.set_document_share(db,self.root,tid,did,{'selected':True})['url']
            self.assertIn(url,core.common_preview(db,self.root,p)['text'])
            core.set_document_share(db,self.root,tid,did,{'selected':False})
            self.assertNotIn('/public/documents/',core.common_preview(db,self.root,p)['text'])

    def test_actual_document_relink_and_return_does_not_restore_link(self):
        mid,iid,tid=self.invoice(self.a,'LINK-A')
        _,other_iid,_=self.invoice(self.a,'LINK-B')
        path=self.root/'invoice-share.pdf'; path.write_bytes(b'invoice')
        did=self.cli('document',{'message_id':mid,'company_id':self.cid,'path':str(path),'classification':'invoice','invoice_id':iid})['document_id']
        with closing(core.connect(self.root)) as db, patch.dict('os.environ',{'PUBLIC_BASE_URL':'https://public.example'}):
            token=core.set_document_share(db,self.root,tid,did,{'selected':True})['url'].rsplit('/',1)[1]
            self.assertEqual(core.shared_document(db,self.root,token)[1],b'invoice')
            self.cli('invoice-link-document',{'document_id':did,'invoice_id':other_iid,'reason':'TEST','source':'TEST'})
            with self.assertRaises(ValueError): core.shared_document(db,self.root,token)
            self.cli('invoice-link-document',{'document_id':did,'invoice_id':iid,'reason':'TEST','source':'TEST'})
            with self.assertRaises(ValueError): core.shared_document(db,self.root,token)
