"""Atomic card edits preserve source and reject stale card snapshots."""
from contextlib import closing
import json
import unittest
import test_invoice_independent as fixtures
import web_core as core

DAY=fixtures.DAY

class CardSave(unittest.TestCase):
    cli=fixtures.InvoiceIndependent.cli
    setUp=fixtures.InvoiceIndependent.setUp
    snapshot=fixtures.InvoiceIndependent.snapshot
    message=fixtures.InvoiceIndependent.message
    company_offer=fixtures.InvoiceIndependent.company_offer

    def item(self, db):
        return next(x for x in core.search(db,self.root,{'date':DAY})['items'] if x['term_id']==self.old_tid)

    def payload(self, item, changes):
        return {'date':DAY,'version':item['version'],'public_revision':item['public_text']['revision'],
                'comment_revision':item['client_comment']['revision'],
                'review_revision':item['operator_review']['revision'],'changes':changes}

    def test_combination_and_review_uses_final_version(self):
        with closing(core.connect(self.root)) as db:
            db.execute('BEGIN IMMEDIATE')
            x=self.item(db); original=x['terms'].copy()
            core.save_public_text(db,self.root,self.old_tid,{'date':DAY,'version':x['version'],'revision':None,'text':'Ручной текст с особыми условиями'})
            x=self.item(db)
            changes={'client_fields':{'subject':'Транспорт','final_rate':'12,5','delivery':'Через день'},
                     'vat_condition':'НДС 20%',
                     'classification':{'groups':[core.product_groups()[0]],'color':'green','groups_changed':True,'color_changed':True},
                     'client_comment':'Дополнение','review':True}
            item=core.save_card(db,self.root,self.old_tid,self.payload(x,changes))['item']
            self.assertEqual(item['client_fields']['final_rate'],'12.5')
            self.assertEqual(item['client_comment']['text'],'Дополнение')
            self.assertTrue(item['operator_review']['accepted'])
            self.assertEqual(item['operator_review']['state_hash'],item['version'])
            self.assertEqual(item['public_text']['text'],'Ручной текст с особыми условиями')
            self.assertTrue(item['public_text']['stale'])
            self.assertEqual(item['terms']['rate'],original['rate'])
            db.commit()

    def test_internal_fields_and_groups_keep_manual_text_current(self):
        with closing(core.connect(self.root)) as db:
            db.execute('BEGIN IMMEDIATE')
            x=self.item(db)
            core.save_public_text(db,self.root,self.old_tid,{'date':DAY,'version':x['version'],'revision':None,'text':'Ручные условия'})
            x=self.item(db)
            item=core.save_card(db,self.root,self.old_tid,self.payload(x,{
                'client_fields':{'bik':'043601607','account':'40702810154400007376'},
                'classification':{'groups':[core.product_groups()[0]],'color':None,'groups_changed':True,'color_changed':False},
                'client_comment':'Дополнение'}))['item']
            self.assertFalse(item['public_text']['stale'])
            self.assertEqual(item['public_text']['text'],'Ручные условия')
            self.assertEqual(core.compose_client_text(item['public_text'],item['client_comment']),'Ручные условия\nКомментарий: Дополнение')
            db.commit()

    def test_groups_and_color_update_together(self):
        with closing(core.connect(self.root)) as db:
            db.execute('BEGIN IMMEDIATE')
            x=self.item(db)
            core.save_public_text(db,self.root,self.old_tid,{'date':DAY,'version':x['version'],'revision':None,'text':x['public_text']['text']})
            x=self.item(db)
            item=core.save_card(db,self.root,self.old_tid,self.payload(x,{'classification':{
                'groups':[core.product_groups()[0]],'color':'green','groups_changed':True,'color_changed':True}}))['item']
            self.assertFalse(item['public_text']['stale'])
            self.assertIn('Светофор: 🟢 зелёный',item['public_text']['text'])
            db.commit()

    def test_internal_changes_never_clear_existing_stale_text(self):
        with closing(core.connect(self.root)) as db:
            db.execute('BEGIN IMMEDIATE')
            x=self.item(db)
            core.save_public_text(db,self.root,self.old_tid,{'date':DAY,'version':x['version'],'revision':None,'text':'Ручные условия'})
            x=self.item(db)
            stale=core.save_card(db,self.root,self.old_tid,self.payload(x,{'client_fields':{'subject':'Транспорт'}}))['item']
            self.assertTrue(stale['public_text']['stale'])
            item=core.save_card(db,self.root,self.old_tid,self.payload(stale,{
                'client_fields':{'bik':'043601607'},
                'classification':{'groups':[core.product_groups()[0]],'color':'green','groups_changed':True,'color_changed':True}}))['item']
            self.assertTrue(item['public_text']['stale'])
            self.assertEqual(item['public_text']['text'],'Ручные условия')
            self.assertEqual(item['public_text']['revision'],stale['public_text']['revision'])
            db.commit()

    def test_ambiguous_color_keeps_text_stale_with_groups(self):
        with closing(core.connect(self.root)) as db:
            db.execute('BEGIN IMMEDIATE')
            x=self.item(db)
            core.save_public_text(db,self.root,self.old_tid,{'date':DAY,'version':x['version'],'revision':None,'text':'Светофор: уточнить у поставщика'})
            x=self.item(db)
            item=core.save_card(db,self.root,self.old_tid,self.payload(x,{'classification':{
                'groups':[core.product_groups()[0]],'color':'green','groups_changed':True,'color_changed':True}}))['item']
            self.assertTrue(item['public_text']['stale'])
            self.assertEqual(item['public_text']['text'],'Светофор: уточнить у поставщика')
            self.assertEqual(item['public_text']['revision'],x['public_text']['revision'])
            db.commit()

    def test_failure_on_last_step_rolls_back_everything(self):
        with closing(core.connect(self.root)) as db:
            before=self.item(db)
            history=db.execute('SELECT COUNT(*) FROM history').fetchone()[0]
            db.execute('BEGIN IMMEDIATE')
            with self.assertRaises(ValueError):
                core.save_card(db,self.root,self.old_tid,self.payload(before,{'client_fields':{'subject':'Транспорт'},'vat_condition':'НДС 20%','client_comment':'Новый','review':'invalid'}))
            db.rollback()
            self.assertEqual(self.item(db),before)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM history').fetchone()[0],history)

    def test_auto_text_combined_fields_vat_and_color_preserves_origin(self):
        with closing(core.connect(self.root)) as db:
            db.execute('BEGIN IMMEDIATE')
            x=self.item(db)
            core.save_public_text(db,self.root,self.old_tid,{'date':DAY,'version':x['version'],'revision':None,'text':x['public_text']['text']})
            x=self.item(db)
            changes={'client_fields':{'final_rate':'12.5'},'vat_condition':'НДС 20%',
                     'classification':{'groups':[], 'color':'green','groups_changed':False,'color_changed':True}}
            item=core.save_card(db,self.root,self.old_tid,self.payload(x,changes))['item']
            public=item['public_text']
            self.assertFalse(public['stale'])
            self.assertIn('Цена: 12.5%',public['text'])
            self.assertIn('НДС предложения: НДС 20%',public['text'])
            self.assertIn('Светофор: 🟢 зелёный',public['text'])
            self.assertNotIn('НДС предложения:',public['preview'])
            event=json.loads(db.execute("SELECT data FROM history WHERE id=?",(public['revision'],)).fetchone()[0])
            self.assertTrue(event.get('vat_added_line'))
            item=core.classify_vat(db,self.root,self.old_tid,{'date':DAY,'version':item['version'],'vat_condition':'не указан'})['item']
            self.assertNotIn('НДС предложения:',item['public_text']['text'])
            self.assertFalse(item['public_text']['stale'])
            db.commit()

    def test_each_revision_and_version_conflict_precedes_writes(self):
        with closing(core.connect(self.root)) as db:
            before=self.item(db)
            for field,stale in [('version','stale'),('public_revision',999999),('comment_revision',999999),('review_revision',999999)]:
                db.execute('BEGIN IMMEDIATE')
                payload=self.payload(before,{'client_fields':{'subject':'Транспорт'}}); payload[field]=stale
                with self.assertRaises(ValueError): core.save_card(db,self.root,self.old_tid,payload)
                db.rollback()
                self.assertEqual(self.item(db),before)
