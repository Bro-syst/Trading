"""Bank details are validated, versioned, and kept out of automatic text."""
from contextlib import closing
import json
import unittest
import web_core as core
from test_card_save import CardSave, DAY
import test_invoice_independent as fixtures


class BankFields(CardSave):
    invoice=fixtures.InvoiceIndependent.invoice
    task=fixtures.InvoiceIndependent.task
    tasks=fixtures.InvoiceIndependent.tasks
    prepare=fixtures.InvoiceIndependent.prepare

    def test_bank_key_status_and_card_versions(self):
        # Includes leading zeroes and differs in one account digit.
        valid={'bik':'043601607','account':'40702810154400007376'}
        invalid={'bik':'043601607','account':'40702810154400007377'}
        self.assertEqual(core.bank_key_status(valid),'valid')
        self.assertEqual(core.bank_key_status({'bik':'049805746','account':'40602810700000000025'}),'valid')
        self.assertEqual(core.bank_key_status({'bik':'049805746','account':'40602810700000000026'}),'invalid')
        self.assertEqual(core.bank_key_status(invalid),'invalid')
        for fields in ({}, {'bik':valid['bik']}, {'account':valid['account']},
                       {'bik':'１２３４５６７８９','account':valid['account']}):
            self.assertEqual(core.bank_key_status(fields),'unchecked')
        for account in ('00000000000000000001','30101810400000000607','40102810100000010001'):
            self.assertEqual(core.bank_key_status({'bik':valid['bik'],'account':account}),'unsupported')
        with closing(core.connect(self.root)) as db:
            db.execute('BEGIN IMMEDIATE')
            before=self.item(db)
            self.assertEqual(before['bank_key_status'],'unchecked')
            partial=core.save_card(db,self.root,self.old_tid,self.payload(before,{'client_fields':{'bik':valid['bik']}}))['item']
            self.assertEqual(partial['bank_key_status'],'unchecked')
            good=core.save_card(db,self.root,self.old_tid,self.payload(partial,{'client_fields':{'account':valid['account']}}))['item']
            self.assertEqual(good['bank_key_status'],'valid')
            bad=core.save_card(db,self.root,self.old_tid,self.payload(good,{'client_fields':{'account':invalid['account']},'client_comment':'Сохранено вместе'}))['item']
            self.assertEqual(bad['bank_key_status'],'invalid')
            self.assertEqual(bad['client_fields'],invalid)
            self.assertEqual(bad['client_comment']['text'],'Сохранено вместе')
            self.assertEqual(len({before['version'],partial['version'],good['version'],bad['version']}),4)
            with self.assertRaises(ValueError):
                core.save_card(db,self.root,self.old_tid,self.payload(good,{'client_fields':valid}))
            db.commit()
            queued=next(x for x in core.queue(db,self.root,self.b,DAY) if x['term_id']==self.old_tid)
            self.assertEqual(queued['bank_key_status'],'invalid')

    def test_persistence_per_source_and_invoice_and_outgoing_texts(self):
        source=self.cli('chat',{'name':'TEST another source'})['chat_id']
        _,invoice_id,second=self.invoice(source,'BANK-2')
        self.cli('pricing',{'term_id':second,'markup':'0','source':'TEST user','reason':'TEST'})
        first=self.old_tid
        values={first:{'bik':'000000001','account':'00000000000000000001'},
                second:{'bik':'000000002','account':'00000000000000000002'}}
        with closing(core.connect(self.root)) as db:
            db.execute('BEGIN IMMEDIATE')
            for tid in (first,second):
                item=next(x for x in core.search(db,self.root,{'date':DAY})['items'] if x['term_id']==tid)
                changes={'client_fields':values[tid]}
                if tid==first: changes['review']=True
                core.save_card(db,self.root,tid,self.payload(item,changes))
            db.commit()
        with closing(core.connect(self.root)) as db:
            items={x['term_id']:x for x in core.search(db,self.root,{'date':DAY})['items']}
            self.assertEqual(items[first]['company_id'],items[second]['company_id'])
            self.assertEqual(items[first]['inn'],items[second]['inn'])
            self.assertNotEqual(items[first]['source_chat_id'],items[second]['source_chat_id'])
            self.assertEqual(items[second]['invoice']['id'],invoice_id)
            for tid in (first,second): self.assertEqual(items[tid]['client_fields'],values[tid])
            request={'date':DAY,'term_ids':[first,second],
                     'versions':{str(tid):items[tid]['version'] for tid in values},
                     'revisions':{str(tid):items[tid]['public_text']['revision'] for tid in values}}
            text=core.common_preview(db,self.root,request)['text']
            self.assertIn('Оплата счёта',text)
            for details in values.values():
                for value in details.values(): self.assertNotIn(value,text)
        block=self.prepare(self.b,[self.task(first)])
        for details in values.values():
            for value in details.values(): self.assertNotIn(value,block['text'])
        self.assertEqual(self.snapshot(self.v2),self.before)

    def test_bank_details_history_and_manual_text(self):
        with closing(core.connect(self.root)) as db:
            db.execute('BEGIN IMMEDIATE')
            before=self.item(db)
            core.save_public_text(db,self.root,self.old_tid,{'date':DAY,'version':before['version'],'revision':None,'text':'Ручной текст с условиями'})
            before=self.item(db)
            changes={'client_fields':{'bik':'000000001','account':'00000000000000000001'}}
            after=core.save_card(db,self.root,self.old_tid,self.payload(before,changes))['item']
            self.assertEqual(after['client_fields'],changes['client_fields'])
            self.assertNotEqual(after['version'],before['version'])
            self.assertEqual(after['public_text']['text'],'Ручной текст с условиями')
            self.assertFalse(after['public_text']['stale'])
            event=json.loads(db.execute("SELECT data FROM history WHERE entity='client_fields' AND entity_id=? ORDER BY id DESC LIMIT 1",(self.old_tid,)).fetchone()[0])
            self.assertEqual(event['source'],'пользователь')
            self.assertEqual(event['after'],changes['client_fields'])
            with self.assertRaises(ValueError):
                core.save_card(db,self.root,self.old_tid,self.payload(before,changes))
            after=core.save_card(db,self.root,self.old_tid,self.payload(after,{'client_fields':{'bik':None,'account':None}}))['item']
            self.assertIsNone(after['client_fields']['bik'])
            self.assertIsNone(after['client_fields']['account'])
            db.commit()

    def test_automatic_text_never_includes_bank_details(self):
        with closing(core.connect(self.root)) as db:
            db.execute('BEGIN IMMEDIATE')
            before=self.item(db)
            after=core.save_card(db,self.root,self.old_tid,self.payload(before,{'client_fields':{'bik':'000000001','account':'00000000000000000001'}}))['item']
            for value in ('000000001','00000000000000000001','БИК:', 'Р/С:'):
                self.assertNotIn(value,after['public_text']['text'])
                self.assertNotIn(value,after['public_text']['preview'])
            db.commit()

    def test_invalid_values_are_rejected_without_writes(self):
        with closing(core.connect(self.root)) as db:
            before=self.item(db)
            count=db.execute('SELECT COUNT(*) FROM history').fetchone()[0]
            for field,value in [('bik','123'),('bik','１２３４５６７８９'),('bik',123456789),('account','0'*19),('account','0'*21),('account','0'*19+'x')]:
                db.execute('BEGIN IMMEDIATE')
                with self.assertRaises(ValueError):
                    core.save_card(db,self.root,self.old_tid,self.payload(before,{'client_fields':{field:value}}))
                db.rollback()
                self.assertEqual(self.item(db),before)
                self.assertEqual(db.execute('SELECT COUNT(*) FROM history').fetchone()[0],count)
