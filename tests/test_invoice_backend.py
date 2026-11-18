"""Focused safety regressions for schema v3."""
from test_invoice_independent import InvoiceIndependent, DAY

class InvoiceBackendRegressions(InvoiceIndependent):
    def test_web_search_and_queue_keep_unverified_status_without_identity_block(self):
        cid=self.cli('company-create', {'company':{'name':'Unique unverified','inn':'7707083893','inn_status':'unverified'}})['company_id']
        _, offer=self.company_offer(self.a,cid)
        tid=offer['term_ids'][0]
        self.price(tid)
        item=self.task(tid)
        self.assertEqual(item['inn_status'],'unverified')
        self.assertNotIn('identity_review',item['reasons'])
        found=next(x for x in self.cli('search', {'date':DAY})['items'] if x['term_id']==tid)
        self.assertEqual(found['inn_status'],'unverified')
        self.assertNotIn('identity_review',found['unknown'])

    def test_web_rejects_two_sources_with_same_inn_after_review(self):
        cid = self.cli('company-create', {'company':{'name':'TEST Company','inn':'7733446222','inn_status':'unverified'}})['company_id']
        _, second=self.company_offer(self.c, cid)
        tid=second['term_ids'][0]
        self.price(tid)
        for term in (self.old_tid, tid):
            self.assertIn('multiple_sources_review', self.task(term)['reasons'])
            self.cli('update', {'action':'risk_decision','chat_id':self.b,'date':DAY,'term_id':term,'reason':'reviewed'})
        items=[self.task(term) for term in (self.old_tid,tid)]
        self.assertTrue(all(x['status']=='pending' for x in items))
        self.cli('web-prepare', {'chat_id':self.b,'date':DAY,'task_ids':[x['id'] for x in items],
                 'versions':{str(x['id']):x['version'] for x in items}}, ok=False)

    def test_invoice_and_company_overlap_across_unverified_company_ids(self):
        company = {'name':'TEST Company','inn':'7733446222','inn_status':'unverified'}
        cid = self.cli('company-create', {'company':company})['company_id']
        self.company_offer(self.b, cid)
        _, iid, tid = self.invoice(self.a, 'OTHER-INVOICE', cid=cid)
        item = self.task(tid)
        self.assertIn('recipient_company_invoice_review', item['reasons'])
        self.assertIn('invoice_review', item['reasons'])
        self.assertNotIn('recipient_source_today', item['reasons'])
        self.assertEqual(item['inn_status'], 'unverified')

    def test_distinct_invoice_ids_stay_distinct_across_company_ids(self):
        cid = self.cli('company-create', {'company':{'name':'TEST Company','inn':'7733446222','inn_status':'unverified'}})['company_id']
        self.invoice(self.b, 'INVOICE-OWN', cid=self.cid)
        _, _, tid = self.invoice(self.a, 'INVOICE-OTHER', cid=cid)
        self.assertNotIn('recipient_source_today', self.task(tid)['reasons'])
        _, shared, _ = self.invoice(self.b, 'INVOICE-SHARED', cid=cid)
        _, _, shared_tid = self.invoice(self.a, 'INVOICE-SHARED', cid=cid, existing=shared)
        self.assertEqual(self.task(shared_tid)['status'], 'excluded')

    def test_reclassification_preserves_facts_issue_price_and_stales_draft(self):
        mid, iid, tid = self.invoice(self.a, 'TYPE')
        self.cli('invoice-update', {'invoice_id':iid,'data':{'number':'421','amount':'18978289','subject':'назначение платежа','vat':'Без НДС'},'reason':'source'})
        self.price(tid,'1')
        self.cli('invoice-review', {'invoice_id':iid,'reason':'reviewed','source':'user'})
        item=self.task(tid)
        block=self.prepare(self.b,[item])
        issue=self.cli('document', {'message_id':mid,'company_id':self.cid,'path':str(self.root/'missing.pdf'), 'classification':'invoice','invoice_id':iid})
        before=self.snapshot(self.root)
        self.cli('invoice-offer-to-company', {'term_id':tid,'reason':'incorrect type','source':'user','source_condition':'not in source'},ok=False)
        result=self.cli('invoice-offer-to-company', {'term_id':tid,'reason':'incorrect type','source':'user','source_condition':'TEST invoice TYPE'})
        self.assertEqual(result['previous_invoice_id'],iid)
        item=self.task(tid)
        self.assertEqual(item['offer_type'],'company')
        self.assertEqual(item['terms']['amount'],'18978289')
        self.assertNotIn('currency',item['terms'])
        for value in ('421','назначение платежа','Без НДС'): self.assertIn(value,item['terms']['conditions'])
        self.assertIn('TEST invoice TYPE',item['terms']['conditions'])
        self.assertIn('reclassified_invoice_document_review',item['reasons'])
        self.assertEqual(item['markup'],'1')
        self.assertTrue(self.cli('web-block', {'block_id':block['id']})['stale'])
        self.assertEqual(self.snapshot(self.root)[1],before[1])
        shown=self.cli('invoice-show', {'invoice_id':iid})
        self.assertTrue(any(x['id']==issue['document_issue_id'] for x in shown['issues']))
        import sqlite3
        with sqlite3.connect(self.root/'registry.sqlite') as db:
            db.row_factory=sqlite3.Row
            import trading
            self.assertTrue(trading.block_stale(db, trading.row(db,'blocks',block['id']),self.root))
            event=db.execute("SELECT data FROM history WHERE entity='offer' AND action='invoice_to_company'").fetchone()
            self.assertIsNotNone(event)
            self.assertEqual(db.execute('SELECT count(*) FROM invoices WHERE id=?',(iid,)).fetchone()[0],1)

    def test_reclassification_preserves_old_invoice_source_exclusion(self):
        _, iid, own = self.invoice(self.b, 'SHARED')
        _, _, other = self.invoice(self.a, 'SHARED',existing=iid)
        self.assertEqual(self.task(other)['status'],'excluded')
        self.cli('invoice-offer-to-company', {'term_id':own,'reason':'correct type','source':'user'})
        self.assertEqual(self.task(other)['status'],'excluded')

    def test_reclassification_dismisses_only_explicit_unshared_issue(self):
        mid, iid, tid=self.invoice(self.a, 'ISSUE')
        issue=self.cli('document', {'message_id':mid,'company_id':self.cid,'path':str(self.root/'absent.pdf'),'classification':'invoice','invoice_id':iid})['document_issue_id']
        self.cli('invoice-offer-to-company', {'term_id':tid,'reason':'correct type','source':'user','dismiss_issue_ids':[issue],'issue_reason':'invoice file does not apply'})
        self.assertNotIn('reclassified_invoice_document_review',self.task(tid)['reasons'])
        self.assertFalse(self.cli('invoice-show', {'invoice_id':iid})['issues'])
        import sqlite3, json
        with sqlite3.connect(self.root/'registry.sqlite') as db:
            data=json.loads(db.execute("SELECT data FROM history WHERE entity='document_issue' AND entity_id=? AND action='resolve'",(issue,)).fetchone()[0])
            self.assertIsNone(data['document_id']); self.assertEqual(data['disposition'],'not_applicable')

    def test_reclassification_cannot_dismiss_shared_invoice_issue(self):
        mid, iid, tid=self.invoice(self.a, 'SHARED-ISSUE')
        self.invoice(self.c, 'SHARED-ISSUE',existing=iid)
        issue=self.cli('document', {'message_id':mid,'company_id':self.cid,'path':str(self.root/'absent.pdf'),'classification':'invoice','invoice_id':iid})['document_issue_id']
        self.cli('invoice-offer-to-company', {'term_id':tid,'reason':'correct type','source':'user','dismiss_issue_ids':[issue],'issue_reason':'not applicable'},ok=False)
        self.assertEqual(self.task(tid)['offer_type'],'invoice')
        self.assertTrue(self.cli('invoice-show', {'invoice_id':iid})['issues'])

    def test_reclassification_refuses_recorded_sends(self):
        _, iid, tid = self.invoice(self.a, 'TYPE-SENT')
        self.price(tid)
        self.cli('invoice-review', {'invoice_id':iid,'reason':'reviewed','source':'user'})
        item=self.task(tid); block=self.prepare(self.b,[item])
        self.cli('web-confirm', {'block_id':block['id'],'version':block['version'],'operation_id':'TYPE-SENT','task_ids':[item['id']],'document_ids':[]})
        self.cli('invoice-offer-to-company', {'term_id':tid,'reason':'correct type','source':'user'},ok=False)
        self.assertEqual(self.task(tid)['offer_type'],'invoice')

    def test_company_source_in_invoice_excludes_company_offer(self):
        self.invoice(self.b, 'OWN')
        self.assertEqual(self.task(self.old_tid)['status'], 'excluded')

    def test_overlap_decision_expires_when_invoice_changes(self):
        self.company_offer(self.b, self.cid)
        _, iid, tid = self.invoice(self.a, 'A-1')
        self.price(tid)
        self.cli('invoice-review', {'invoice_id':iid,'reason':'reviewed','source':'user'})
        task = self.task(tid)
        self.assertIn('recipient_company_invoice_review', task['reasons'])
        decision = self.cli('update', {'action':'risk_decision', 'chat_id':self.b, 'date':DAY,
                                     'term_id':tid, 'reason':'checked'})
        self.assertEqual(decision['risk'], 'recipient_company_invoice_review')
        self.assertNotIn('recipient_company_invoice_review', self.task(tid)['reasons'])
        self.cli('invoice-update', {'invoice_id':iid, 'data':{'number':'A-1','amount':'1','currency':'RUB'},'reason':'changed'})
        self.assertIn('recipient_company_invoice_review', self.task(tid)['reasons'])

    def test_unavailable_invoice_file_blocks_after_review(self):
        mid, iid, tid = self.invoice(self.a, 'FILE')
        self.price(tid)
        issue = self.cli('document', {'message_id':mid,'company_id':self.cid,'path':str(self.root/'missing.pdf'),
                                     'classification':'invoice','invoice_id':iid})
        self.assertIn('document_issue_id',issue)
        self.cli('invoice-review', {'invoice_id':iid,'reason':'reviewed','source':'user'})
        self.assertIn('invoice_document_review', self.task(tid)['reasons'])
        self.assertEqual(self.task(tid)['status'],'review')

    def test_legacy_file_requires_explicit_company_classification(self):
        _, iid, tid = self.invoice(self.a, 'LEGACY')
        self.assertNotIn(self.old_doc['document_id'],[d['id'] for d in self.task(tid)['documents']])
        self.cli('document-classify', {'document_id':self.old_doc['document_id'], 'classification':'company',
                                       'reason':'confirmed applicable', 'source':'user'})
        self.assertIn(self.old_doc['document_id'],[d['id'] for d in self.task(tid)['documents']])

    def test_possible_duplicate_requires_recipient_specific_review(self):
        self.invoice(self.b, 'SAME')
        _, iid, tid = self.invoice(self.a, 'SAME')
        self.price(tid)
        self.cli('invoice-review', {'invoice_id':iid,'reason':'reviewed','source':'user'})
        item = self.task(tid)
        self.assertEqual(item['status'],'review')
        self.assertIn('possible_invoice_match_review',item['reasons'])
        self.assertTrue(item['possible_invoice_matches'])

    def test_possible_match_amount_is_numeric_even_with_different_numbers(self):
        _, first, _ = self.invoice(self.b, 'FIRST')
        _, second, term = self.invoice(self.a, 'SECOND')
        self.cli('invoice-update', {'invoice_id':first,'data':{'number':'FIRST','date':DAY,'amount':'100.0','currency':'RUB'},'reason':'test'})
        self.cli('invoice-update', {'invoice_id':second,'data':{'number':'SECOND','date':DAY,'amount':'100.00','currency':'USD'},'reason':'test'})
        item = self.task(term)
        self.assertIn({'id':first,'reasons':['date_amount']},item['possible_invoice_matches'])
        self.assertIn('possible_invoice_match_review',item['reasons'])

    def test_invoice_first_company_needs_no_fictitious_offer(self):
        company=self.cli('company-create', {'company':{'name':'Invoice-only Co','inn_status':'missing'}})['company_id']
        mid=self.message(self.a,'Invoice-only message')
        iid=self.cli('invoice-create', {'company_id':company,'message_id':mid,'data':{'number':'NEW'},'reason':'source'})['invoice_id']
        term=self.cli('import', {'message_id':mid,'offers':[{'company_id':company,'invoice_id':iid,'invoice_link_reason':'source',
             'terms':[{'category':'goods','rate':'10','rate_unit':'%','scope_confirmed':True}]}]})['term_ids'][0]
        self.assertEqual(self.task(term)['offer_type'],'invoice')
        import sqlite3
        with sqlite3.connect(self.root/'registry.sqlite') as db:
            self.assertEqual(db.execute('SELECT count(*) FROM offers WHERE company_id=?',(company,)).fetchone()[0],1)

    def test_confirmed_duplicate_relinks_offer_with_history(self):
        _, own, _ = self.invoice(self.b, 'DUPLICATE')
        _, other, term = self.invoice(self.a, 'DUPLICATE')
        self.assertIn('possible_invoice_match_review',self.task(term)['reasons'])
        self.cli('invoice-link-offer', {'term_id':term,'invoice_id':own,'reason':'same invoice','source':'user'})
        after=self.task(term)
        self.assertEqual(after['status'],'excluded')
        self.assertNotIn(other,[x['id'] for x in after['possible_invoice_matches']])
        import sqlite3
        with sqlite3.connect(self.root/'registry.sqlite') as db:
            self.assertEqual(db.execute('SELECT count(*) FROM invoices WHERE id IN (?,?)',(own,other)).fetchone()[0],2)
            self.assertEqual(db.execute("SELECT count(*) FROM history WHERE entity='offer' AND action='invoice_relink'").fetchone()[0],1)

    def test_sent_offer_cannot_be_relinked_or_history_reassigned(self):
        _, first, term = self.invoice(self.a, 'SENT')
        self.price(term)
        self.cli('invoice-review', {'invoice_id':first,'reason':'reviewed','source':'user'})
        item=self.task(term)
        block=self.prepare(self.b,[item])
        self.cli('web-confirm', {'block_id':block['id'],'version':block['version'],'operation_id':'TEST-sent-relink',
                                 'task_ids':[item['id']],'document_ids':[]})
        mid=self.message(self.a,'second invoice')
        second=self.cli('invoice-create', {'company_id':self.cid,'message_id':mid,'data':{'number':'SECOND'},'reason':'source'})['invoice_id']
        self.cli('invoice-link-offer', {'term_id':term,'invoice_id':second,'reason':'same','source':'user'},ok=False)
        self.assertEqual(self.task(term)['invoice']['id'],first)

    def test_search_reports_possible_invoice_matches(self):
        self.invoice(self.b, 'MATCH')
        _, _, term=self.invoice(self.a, 'MATCH')
        result=self.cli('search', {'date':DAY,'offer_type':'invoice'})['items']
        item=next(x for x in result if x['term_id']==term)
        self.assertEqual(item['match'],'possible')
        self.assertIn('possible_invoice_match_review',item['unknown'])


    def test_queue_date_and_block_staleness(self):
        item=self.task(self.old_tid)
        self.assertEqual(item['date'],DAY)
        block=self.prepare(self.b,[item])
        self.assertFalse(self.cli('web-block',{'block_id':block['id']})['stale'])
        import json, sqlite3
        with sqlite3.connect(self.root/'registry.sqlite') as db:
            items=json.loads(db.execute('SELECT items FROM blocks WHERE id=?',(block['id'],)).fetchone()[0])
            items[0].pop('date')
            import trading
            items[0]['version']=trading.sha(trading.canonical({k:v for k,v in items[0].items() if k!='version'}).encode())
            db.execute('UPDATE blocks SET items=? WHERE id=?',(json.dumps(items),block['id']))
        self.assertTrue(self.cli('web-block',{'block_id':block['id']})['stale'])

def load_tests(loader, tests, pattern):
    import unittest
    return unittest.TestSuite(InvoiceBackendRegressions(name) for name in InvoiceBackendRegressions.__dict__ if name.startswith('test_'))
