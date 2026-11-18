"""Client edits preserve source data and require review of ambiguous texts."""
from contextlib import closing
import json
import unittest
import http.client
import threading
import test_invoice_independent as fixtures
import web_core
import web

DAY=fixtures.DAY

class ClientFields(unittest.TestCase):
    cli=fixtures.InvoiceIndependent.cli
    setUp=fixtures.InvoiceIndependent.setUp
    snapshot=fixtures.InvoiceIndependent.snapshot
    message=fixtures.InvoiceIndependent.message
    company_offer=fixtures.InvoiceIndependent.company_offer

    def item(self, db):
        return next(x for x in web_core.search(db,self.root,{'date':DAY})['items'] if x['term_id']==self.old_tid)

    def edit(self, db, fields):
        x=self.item(db)
        return web_core.edit_client_fields(db,self.root,self.old_tid,{'date':DAY,'version':x['version'],'revision':x['public_text']['revision'],'fields':fields})['item']

    def test_history_cas_manual_text_and_source(self):
        with closing(web_core.connect(self.root)) as db:
            initial=self.item(db); raw=db.execute('SELECT data FROM terms WHERE id=?',(self.old_tid,)).fetchone()[0]
            web_core.save_public_text(db,self.root,self.old_tid,{'date':DAY,'version':initial['version'],'revision':None,'text':'Цена: 10%\nособая цена 11%'})
            item=self.edit(db,{'final_rate':'8,25','delivery':'Через день','subject':'Поставка'})
            self.assertEqual(item['final_rate'],'8.25'); self.assertEqual(item['markup'],'0')
            self.assertTrue(item['public_text']['stale']); self.assertEqual(item['public_text']['text'],'Цена: 10%\nособая цена 11%')
            self.assertEqual(raw,db.execute('SELECT data FROM terms WHERE id=?',(self.old_tid,)).fetchone()[0])
            self.assertIn('client_fields_web_only',next(x for x in web_core.t.plan(db,self.b,DAY,root=self.root) if x['term_id']==self.old_tid)['reasons'])
            self.assertNotIn('rate_unknown',next(x for x in web_core.queue(db,self.root,self.b,DAY) if x['term_id']==self.old_tid)['reasons'])
            a=item['version']; self.edit(db,{'final_rate':'9'}); item=self.edit(db,{'final_rate':'8.25'})
            self.assertNotEqual(a,item['version'])
            with self.assertRaises(ValueError): web_core.edit_client_fields(db,self.root,self.old_tid,{'date':DAY,'version':a,'revision':item['public_text']['revision'],'fields':{'subject':'X'}})
            item=self.edit(db,{'final_rate':None})
            self.assertIn('rate_unknown',next(x for x in web_core.queue(db,self.root,self.b,DAY) if x['term_id']==self.old_tid)['reasons'])
            self.assertIn('Цена: на согласовании',web_core.render_public(db,item)[0])

    def test_auto_update_and_validation(self):
        with closing(web_core.connect(self.root)) as db:
            x=self.item(db); web_core.save_public_text(db,self.root,self.old_tid,{'date':DAY,'version':x['version'],'revision':None,'text':x['public_text']['text']})
            x=self.edit(db,{'final_rate':'7'})
            self.assertFalse(x['public_text']['stale']); self.assertIn('Цена: 7%',x['public_text']['text'])
            for value in ('завтра','3–4 дня','-1',''):
                with self.assertRaises(ValueError): self.edit(db,{'delivery':value})
            self.assertEqual(json.loads(db.execute('SELECT data FROM terms WHERE id=?',(self.old_tid,)).fetchone()[0])['rate'],'10')

    def test_unknown_source_rate_and_approval_does_not_revive(self):
        with closing(web_core.connect(self.root)) as db:
            raw=json.loads(db.execute('SELECT data FROM terms WHERE id=?',(self.old_tid,)).fetchone()[0])
            raw['rate']=None; raw.pop('rate_unit',None)
            db.execute('UPDATE terms SET data=? WHERE id=?',(json.dumps(raw),self.old_tid))
            db.execute('DELETE FROM pricing WHERE term_id=?',(self.old_tid,))
            x=self.edit(db,{'final_rate':'4'})
            self.assertIsNone(x['terms']['rate']); self.assertIsNone(x['markup']); self.assertIsNone(x['calculated_final_rate'])
            self.assertIn('Цена: 4%',web_core.render_public(db,x)[0])
            reasons=next(i for i in web_core.queue(db,self.root,self.b,DAY) if i['term_id']==self.old_tid)['reasons']
            self.assertFalse(set(reasons)&{'rate_unknown','markup_not_agreed','rate_unit_unknown'})
            web_core.set_operator_review(db,self.root,self.old_tid,{'date':DAY,'version':x['version'],'revision':x['operator_review']['revision'],'accepted':True})
            self.edit(db,{'final_rate':'5'}); x=self.edit(db,{'final_rate':'4'})
            self.assertTrue(x['operator_review']['stale']); self.assertFalse(x['operator_review']['accepted'])

    def test_filters_use_current_subject_and_treat_old_groups_and_duration_as_possible(self):
        with closing(web_core.connect(self.root)) as db:
            groups=web_core.product_groups(); x=self.item(db)
            data=dict(x['terms']); data.update(duration=1,duration_unit='day',duration_day_type='calendar',groups=[groups[0]])
            db.execute('UPDATE terms SET data=? WHERE id=?',(json.dumps(data,ensure_ascii=False),self.old_tid))
            self.edit(db,{'subject':'Транспорт','delivery':'Через день'})
            def filtered(**filters):
                return [v for v in web_core.search(db,self.root,{'date':DAY,**filters})['items'] if v['term_id']==self.old_tid]
            self.assertEqual(len(filtered(category='Транспорт')),1)
            self.assertEqual(filtered(category='стройматериалы'),[])
            x=filtered(group=[groups[1]],max_duration='0')[0]
            self.assertIn('group',x['unknown']); self.assertIn('duration',x['unknown'])
            self.assertTrue(x['groups_stale'])
            x=self.item(db)
            web_core.classify_term(db,self.root,self.old_tid,{'date':DAY,'version':x['version'],'groups':[groups[0]],'color':None,'color_changed':False,'groups_changed':True})
            self.assertEqual(filtered(group=[groups[1]]),[])
            self.assertNotIn('group',filtered(group=[groups[0]])[0]['unknown'])
            self.assertFalse(self.item(db)['groups_stale'])
            self.edit(db,{'subject':None,'delivery':None})
            x=filtered(category='Транспорт',group=[groups[1]],max_duration='0')[0]
            self.assertTrue({'group','category','duration'}.issubset(x['unknown']))

    def test_manual_conditions_disable_source_comparison(self):
        _, offer=self.company_offer(self.c,self.cid); second=offer['term_ids'][0]
        self.cli('pricing',{'term_id':second,'markup':'0','reason':'TEST','source':'TEST user'})
        with closing(web_core.connect(self.root)) as db:
            for tid,rate in ((self.old_tid,'10'),(second,'12')):
                data=json.loads(db.execute('SELECT data FROM terms WHERE id=?',(tid,)).fetchone()[0])
                data.update(rate=rate,rate_unit='%',rate_type='комиссия',rate_basis='платёж',vat='22%',payment='перевод',duration_text='2 дня',duration=2,duration_unit='day',duration_day_type='calendar',color='green',color_source='TEST',color_date=DAY,conditions='нет',restrictions='нет',amount='нет',minimum='нет',currency='нет')
                db.execute('UPDATE terms SET data=? WHERE id=?',(json.dumps(data,ensure_ascii=False),tid))
            def tasks(): return [x for x in web_core.queue(db,self.root,self.b,DAY) if x['term_id'] in (self.old_tid,second)]
            self.assertTrue(all(x['comparison']['comparable'] for x in tasks()))
            self.edit(db,{'delivery':None})
            self.assertTrue(all(not x['comparison']['comparable'] and not x['comparison']['lowest'] and 'multiple_sources_review' in x['reasons'] for x in tasks()))
            self.edit(db,{'subject':'Транспорт','delivery':'Следующий день'})
            self.assertTrue(all(not x['comparison']['comparable'] for x in tasks()))

    def test_http_login_csrf_and_persistence(self):
        salt=b'test-salt-123456'; password='temporary test pass'
        (self.root/'local-auth.json').write_text(json.dumps({'salt':salt.hex(),'hash':web.password_hash(password,salt)}))
        server=web.ThreadingHTTPServer(('127.0.0.1',0),web.Handler); server.root=self.root
        threading.Thread(target=server.serve_forever,daemon=True).start()
        self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        host=f'127.0.0.1:{server.server_port}'
        def req(method,path,data=None,headers=None):
            conn=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=5)
            h={'Host':host,**(headers or {})}; body=None if data is None else json.dumps(data)
            if body is not None: h['Content-Type']='application/json'
            conn.request(method,path,body,h); response=conn.getresponse()
            result=(response.status,dict(response.getheaders()),json.loads(response.read())); conn.close(); return result
        status,headers,login=req('POST','/api/login',{'password':password},{'Origin':'http://'+host})
        self.assertEqual(status,200)
        auth={'Origin':'http://'+host,'Cookie':headers['Set-Cookie'].split(';')[0],'X-CSRF-Token':login['csrf']}
        status,_,data=req('GET','/api/search?date='+DAY,headers=auth); self.assertEqual(status,200)
        item=next(x for x in data['results'] if x['term_id']==self.old_tid)
        payload={'date':DAY,'version':item['version'],'revision':item['public_text']['revision'],'fields':{'subject':'Транспорт','final_rate':'12.5','delivery':'Следующий день'}}
        path=f'/api/terms/{self.old_tid}/client-fields'
        self.assertEqual(req('POST',path,payload,{**auth,'X-CSRF-Token':'invalid'})[0],403)
        status,_,saved=req('POST',path,payload,auth); self.assertEqual(status,200)
        self.assertEqual(saved['item']['client_fields'],payload['fields'])
        self.assertEqual(req('POST',path,payload,auth)[0],409)
        with closing(web_core.connect(self.root)) as db:
            self.assertEqual(self.item(db)['client_fields'],payload['fields'])
