"""Public texts are explicit, persistent, versioned, and never send anything."""
import json
from contextlib import closing
import unittest
import test_invoice_independent as fixtures
import web_core
DAY=fixtures.DAY

class CommonPreview(unittest.TestCase):
    cli=fixtures.InvoiceIndependent.cli
    setUp=fixtures.InvoiceIndependent.setUp
    snapshot=fixtures.InvoiceIndependent.snapshot
    message=fixtures.InvoiceIndependent.message
    company_offer=fixtures.InvoiceIndependent.company_offer
    invoice=fixtures.InvoiceIndependent.invoice
    task=fixtures.InvoiceIndependent.task
    tasks=fixtures.InvoiceIndependent.tasks
    prepare=fixtures.InvoiceIndependent.prepare

    def items(self,db): return {x['term_id']:x for x in web_core.search(db,self.root,{'date':DAY})['items']}
    def selected(self,db,ids):
        items=self.items(db)
        return {'date':DAY,'term_ids':ids,'versions':{str(i):items[i]['version'] for i in ids},'revisions':{str(i):items[i]['public_text']['revision'] for i in ids}}
    def save(self,db,tid,text):
        item=self.items(db)[tid]
        return web_core.save_public_text(db,self.root,tid,{'date':DAY,'version':item['version'],'revision':item['public_text']['revision'],'text':text})

    def test_mixed_free_fields_are_not_leaked_and_source_remains(self):
        _,iid,tid=self.invoice(self.a,'A-1')
        self.cli('invoice-update',{'invoice_id':iid,'data':{'subject':'Предмет чат SECRET 10%', 'description':'назначение CHAT 11%', 'discrepancies':['спор суммы 999']},'reason':'source'})
        with closing(web_core.connect(self.root)) as db:
            raw=db.execute('SELECT data FROM terms WHERE id=?',(tid,)).fetchone()[0]; data=json.loads(raw)
            data.update({'category':'CHAT 12% поставка', 'conditions':'чат XYZ цена 9% лимит 5', 'duration_text':'до завтра CHAT'})
            db.execute('UPDATE terms SET data=? WHERE id=?',(json.dumps(data,ensure_ascii=False),tid))
            before=db.execute('SELECT data FROM terms WHERE id=?',(tid,)).fetchone()[0]
            sends=db.execute('SELECT COUNT(*) FROM sends').fetchone()[0]; tasks=db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]
            result=web_core.common_preview(db,self.root,self.selected(db,[tid])); text=result['text']
            for secret in ('SECRET','CHAT','XYZ','999','12345.67','9%','12%'): self.assertNotIn(secret,text)
            self.assertTrue(result['warnings']); self.assertEqual(before,db.execute('SELECT data FROM terms WHERE id=?',(tid,)).fetchone()[0])
            self.assertEqual(sends,db.execute('SELECT COUNT(*) FROM sends').fetchone()[0]); self.assertEqual(tasks,db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0])
        self.assertEqual(self.snapshot(self.v2),self.before)

    def test_persistence_concurrent_revision_and_data_changes(self):
        with closing(web_core.connect(self.root)) as db:
            item=self.items(db)[self.old_tid]; request=self.selected(db,[self.old_tid])
            data={'date':DAY,'version':item['version'],'revision':None,'text':'Ручной текст: назначение и срок 3 дня'}
            saved=web_core.save_public_text(db,self.root,self.old_tid,data)
            self.assertEqual(saved['version'],item['version'])
            with self.assertRaises(ValueError): web_core.save_public_text(db,self.root,self.old_tid,data)
            with self.assertRaises(ValueError): web_core.common_preview(db,self.root,request)
        with closing(web_core.connect(self.root)) as db:
            self.assertEqual(self.items(db)[self.old_tid]['public_text']['text'],data['text'])
            request=self.selected(db,[self.old_tid]); self.assertIn(data['text'],web_core.common_preview(db,self.root,request)['text'])
            raw=json.loads(db.execute('SELECT data FROM terms WHERE id=?',(self.old_tid,)).fetchone()[0]); raw['category']='другое'
            db.execute('UPDATE terms SET data=? WHERE id=?',(json.dumps(raw,ensure_ascii=False),self.old_tid))
            with self.assertRaises(ValueError): web_core.common_preview(db,self.root,request)
            self.assertTrue(self.items(db)[self.old_tid]['public_text']['stale'])
            with self.assertRaises(ValueError): web_core.common_preview(db,self.root,self.selected(db,[self.old_tid]))
            revised=self.save(db,self.old_tid,'Исправленный текст'); self.assertNotEqual(saved['revision'],revised['revision'])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM history WHERE entity='public_text' AND entity_id=?",(self.old_tid,)).fetchone()[0],2)
            self.assertEqual(web_core.common_preview(db,self.root,self.selected(db,[self.old_tid]))['text'],'Исправленный текст')

    def test_price_vat_and_classification_invalidate_saved_text(self):
        with closing(web_core.connect(self.root)) as db:
            for change in ('price','vat','classification'):
                self.save(db,self.old_tid,'Текст до изменения')
                request=self.selected(db,[self.old_tid])
                if change=='price':
                    db.execute("INSERT INTO pricing(term_id,markup,reason,recorded_at) VALUES(?,?,?,?)",(self.old_tid,'2','изменение','2026-11-18'))
                elif change=='vat':
                    cid=self.items(db)[self.old_tid]['company_id']
                    db.execute("INSERT INTO company_vat(company_id,value,source,date,status,recorded_at) VALUES(?,?,?,?,?,?)",(cid,'без НДС','источник',DAY,'unverified',DAY))
                else:
                    db.execute("INSERT INTO history(entity,entity_id,action,data,at) VALUES('term',?,'classification','{}',?)",(self.old_tid,DAY))
                with self.assertRaises(ValueError): web_core.common_preview(db,self.root,request)
                self.assertTrue(self.items(db)[self.old_tid]['public_text']['stale'])

    def test_invoice_changes_invalidate_saved_text(self):
        _,iid,tid=self.invoice(self.a,'A-3')
        with closing(web_core.connect(self.root)) as db:
            self.save(db,tid,'Назначение')
            request=self.selected(db,[tid])
            invoice=json.loads(db.execute('SELECT data FROM invoices WHERE id=?',(iid,)).fetchone()[0]); invoice['description']='новое назначение'
            db.execute('UPDATE invoices SET data=? WHERE id=?',(json.dumps(invoice,ensure_ascii=False),iid))
            with self.assertRaises(ValueError): web_core.common_preview(db,self.root,request)
            self.assertTrue(self.items(db)[tid]['public_text']['stale'])

    def test_invoice_amount_conflicts_and_safe_subject(self):
        _,iid,tid=self.invoice(self.a,'A-1')
        with closing(web_core.connect(self.root)) as db:
            invoice=json.loads(db.execute('SELECT data FROM invoices WHERE id=?',(iid,)).fetchone()[0])
            invoice['discrepancies']=['Идентичность и реквизиты независимо не проверены.']
            db.execute('UPDATE invoices SET data=? WHERE id=?',(json.dumps(invoice,ensure_ascii=False),iid))
            preview=web_core.common_preview(db,self.root,self.selected(db,[tid]))
            self.assertIn('12345.67 ₽',preview['text']); self.assertTrue(preview['warnings'])
            invoice['discrepancies']=['Сумма цифрами 8 900 130 против суммы прописью 5 231 400']
            db.execute('UPDATE invoices SET data=? WHERE id=?',(json.dumps(invoice,ensure_ascii=False),iid))
            preview=web_core.common_preview(db,self.root,self.selected(db,[tid]))
            self.assertIn('Сумма счёта: уточняется',preview['text']); self.assertNotIn('12345.67',preview['text'])
            invoice['discrepancies']=['Суммы 100 и 200 разные']
            db.execute('UPDATE invoices SET data=? WHERE id=?',(json.dumps(invoice,ensure_ascii=False),iid))
            self.assertIn('Сумма счёта: уточняется',web_core.common_preview(db,self.root,self.selected(db,[tid]))['text'])
            invoice['discrepancies']=['Сумма напечатанных строк соответствует итогу; итог 100 вместо 200']
            db.execute('UPDATE invoices SET data=? WHERE id=?',(json.dumps(invoice,ensure_ascii=False),iid))
            self.assertIn('Сумма счёта: уточняется',web_core.common_preview(db,self.root,self.selected(db,[tid]))['text'])
            invoice['discrepancies']=['В счёте шинная продукция; в условиях — запчасти или оборудование; применимость цены не подтверждена.']
            db.execute('UPDATE invoices SET data=? WHERE id=?',(json.dumps(invoice,ensure_ascii=False),iid))
            text=web_core.common_preview(db,self.root,self.selected(db,[tid]))['text']
            self.assertNotIn('Предмет:',text); self.assertNotIn('Назначение:',text)

    def test_recipient_block_safe_text_and_revision_stale(self):
        with closing(web_core.connect(self.root)) as db:
            data=json.loads(db.execute('SELECT data FROM terms WHERE id=?',(self.old_tid,)).fetchone()[0]); data['conditions']='Чат SECRET исходная ставка 9%'
            db.execute('UPDATE terms SET data=? WHERE id=?',(json.dumps(data,ensure_ascii=False),self.old_tid))
        item=self.task(self.old_tid); block=self.prepare(self.b,[item])
        self.assertNotIn('SECRET',block['text'])
        with closing(web_core.connect(self.root)) as db:
            self.assertFalse(web_core.block(db,self.root,block['id'])['stale'])
            self.save(db,self.old_tid,'Текст после создания блока')
            self.assertTrue(web_core.block(db,self.root,block['id'])['stale'])
            # Old snapshots have no public marker and retain their original stale rules.
            items=json.loads(db.execute('SELECT items FROM blocks WHERE id=?',(block['id'],)).fetchone()[0])
            for row in items: row.pop('public_snapshot')
            db.execute('UPDATE blocks SET items=? WHERE id=?',(json.dumps(items,ensure_ascii=False),block['id']))
            self.assertFalse(web_core.block(db,self.root,block['id'])['stale'])

    def test_order_and_recipient_flow(self):
        _,_,tid=self.invoice(self.a,'A-2')
        with closing(web_core.connect(self.root)) as db:
            self.save(db,tid,'Первый'); self.save(db,self.old_tid,'Второй')
            self.assertEqual(web_core.common_preview(db,self.root,self.selected(db,[tid,self.old_tid]))['text'],'Первый\n\n──────────────\n\nВторой')
        item=self.task(self.old_tid); self.assertEqual(item['status'],'pending')
        self.assertEqual(self.prepare(self.b,[item])['text'],'Второй')
