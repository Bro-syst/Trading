import json
import unittest
import test_web_independent as fixture
import web_core as core
import trading as trading

DAY=fixture.DAY

class OfferReview(unittest.TestCase):
    def setUp(self):
        self.f=fixture.LocalWebIndependent('test_chat_platform_preserves_provenance_and_roles')
        self.f.setUp(); self.addCleanup(self.f.tearDown)
        _,offer=self.f.offer(); self.tid=offer['term_ids'][0]
        self.f.cli('pricing',{'term_id':self.tid,'markup':'0','reason':'тест'})
        self.db=core.connect(self.f.root); self.addCleanup(self.db.close)
        term=json.loads(self.db.execute('SELECT data FROM terms WHERE id=?',(self.tid,)).fetchone()[0]); term['review']=True
        self.db.execute('UPDATE terms SET data=? WHERE id=?',(json.dumps(term),self.tid))

    def card(self): return next(x for x in core.search(self.db,self.f.root,{'date':DAY})['items'] if x['term_id']==self.tid)
    def task(self): return next(x for x in core.queue(self.db,self.f.root,self.f.b,DAY) if x['term_id']==self.tid)
    def decision(self,accepted=True,version=None):
        card=self.card()
        return core.set_operator_review(self.db,self.f.root,self.tid,{'date':DAY,'version':version or card['version'],'revision':card['operator_review']['revision'],'accepted':accepted})

    def test_accept_revoke_and_change(self):
        old=self.card(); self.assertEqual(self.task()['status'],'review')
        self.decision(); item=self.task(); self.assertEqual(item['status'],'pending')
        self.assertIn('terms_review',item['reasons']); self.assertIn('terms_review',item['acknowledged_reasons'])
        self.decision(False); self.assertEqual(self.task()['status'],'review')
        self.decision(); data=self.card()['terms']; data['rate']='12'
        self.db.execute('UPDATE terms SET data=? WHERE id=?',(json.dumps(data),self.tid))
        self.assertTrue(self.card()['operator_review']['stale']); self.assertEqual(self.task()['status'],'review')
        with self.assertRaises(ValueError): self.decision(version=old['version'])
        events=self.db.execute("SELECT action,data FROM history WHERE entity='offer_review' ORDER BY id").fetchall()
        self.assertEqual([x['action'] for x in events],['accept','revoke','accept'])
        self.assertEqual(json.loads(events[0]['data'])['source'],'user')

    def test_recipient_exclusion_and_inactive(self):
        self.decision(); self.assertEqual(self.task()['status'],'pending')
        self.db.commit(); self.f.offer(source=self.f.b,company_id=self.card()['company_id'])
        self.assertEqual(self.task()['status'],'excluded')
        self.db.execute('UPDATE chats SET recipient=0 WHERE id=?',(self.f.b,))
        self.assertNotEqual(self.task()['status'],'pending')

    def test_recipient_history_still_needs_separate_decision(self):
        self.decision(); self.db.commit()
        msg=self.f.cli('ingest',{'chat_id':self.f.b,'date':'2026-11-16','text':'ТЕСТ прошлое предложение'})
        self.f.cli('import',{'message_id':msg['message_id'],'offers':[{'company_id':self.card()['company_id'],'terms':[{'category':'материалы','rate':'10','scope_confirmed':True}]}]})
        task=self.task(); self.assertEqual(task['status'],'review')
        self.assertIn('past_recipient_source_review',task['reasons'])
        self.assertNotIn('past_recipient_source_review',task['acknowledged_reasons'])

    def test_missing_pricing_is_not_accepted(self):
        self.db.execute('DELETE FROM pricing WHERE term_id=?',(self.tid,))
        self.decision(); task=self.task()
        self.assertEqual(task['status'],'review')
        self.assertIn('markup_not_agreed',task['reasons'])
        self.assertNotIn('markup_not_agreed',task['acknowledged_reasons'])

    def test_legacy_version_and_reapproval_invalidates_block(self):
        task=self.task()
        old={k:v for k,v in task.items() if k not in ('version','operator_review','acknowledged_reasons')}
        self.assertEqual(task['version'],trading.sha(trading.canonical(old).encode()))
        self.decision(); task=self.task()
        block=core.prepare(self.db,self.f.root,{'chat_id':self.f.b,'date':DAY,'task_ids':[task['id']],'versions':{str(task['id']):task['version']}})
        self.decision(False); self.decision(True)
        self.assertTrue(core.block(self.db,self.f.root,block['id'])['stale'])

    def test_concurrent_decision_requires_current_revision(self):
        card=self.card(); self.decision()
        with self.assertRaises(ValueError):
            core.set_operator_review(self.db,self.f.root,self.tid,{'date':DAY,'version':card['version'],'revision':card['operator_review']['revision'],'accepted':False})
        self.assertTrue(self.card()['operator_review']['accepted'])

    def test_sent_and_withdrawn_remain_blocked(self):
        self.decision(); task=self.task()
        block=core.prepare(self.db,self.f.root,{'chat_id':self.f.b,'date':DAY,'task_ids':[task['id']],'versions':{str(task['id']):task['version']}})
        core.confirm(self.db,self.f.root,block['id'],{'operation_id':'test-send-12345','version':block['version'],'task_ids':[task['id']],'document_ids':[]})
        self.assertEqual(self.task()['status'],'sent')
        self.db.execute('UPDATE terms SET active=0 WHERE id=?',(self.tid,))
        self.assertNotEqual(self.task()['status'],'pending')
