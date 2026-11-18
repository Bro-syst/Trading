"""Classification is isolated per variant and invalidates stale work."""
import json
import sqlite3
import unittest
from unittest.mock import patch
import test_web_independent as fixture_module
DAY = fixture_module.DAY
import web_core as core

class ProductGroups(unittest.TestCase):
    def setUp(self):
        self.fixture=fixture_module.LocalWebIndependent('test_chat_platform_preserves_provenance_and_roles')
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        _, offer=self.fixture.offer()
        self.tid=offer['term_ids'][0]
        self.fixture.cli('pricing',{'term_id':self.tid,'markup':'0','reason':'тест'})
        self.db=core.connect(self.fixture.root)
        self.addCleanup(self.db.close)
        self.groups=core.product_groups()

    def item(self):
        return next(x for x in core.search(self.db,self.fixture.root,{'date':DAY})['items'] if x['term_id']==self.tid)

    def save(self,item,groups,color,color_changed=True,groups_changed=True):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            result=core.classify_term(self.db,self.fixture.root,self.tid,{'date':DAY,'version':item['version'],'groups':groups,'color':color,'color_changed':color_changed,'groups_changed':groups_changed})
            self.db.commit()
            return result['item']
        except Exception:
            self.db.rollback()
            raise

    def test_history_cas_search_and_drafts(self):
        initial=self.item(); task=self.fixture.tasks()[0]; block=self.fixture.prepare(task)
        edited=self.save(initial,self.groups[:2],'green')
        self.assertEqual(edited['terms']['category'],'материалы')
        self.assertEqual(edited['terms']['groups'],self.groups[:2])
        self.assertEqual((edited['terms']['color_date'],edited['terms']['color_source']),(DAY,'пользователь'))
        self.assertTrue(core.block(self.db,self.fixture.root,block['id'])['stale'])
        with self.assertRaises(ValueError): self.save(initial,[],None)
        match=core.search(self.db,self.fixture.root,{'date':DAY,'group':self.groups[:2]})['items'][0]
        self.assertNotIn('group',match['unknown'])
        self.assertEqual(core.search(self.db,self.fixture.root,{'date':DAY,'group':[self.groups[2]]})['items'],[])
        preview=core.common_preview(self.db,self.fixture.root,{'date':DAY,'term_ids':[self.tid],'versions':{str(self.tid):edited['version']},'revisions':{str(self.tid):None}})
        self.assertNotIn(self.groups[0],preview['text'])
        reset=self.save(edited,[],None)
        self.assertNotIn('color',reset['terms']); self.assertNotIn('color_date',reset['terms']); self.assertNotIn('color_source',reset['terms'])
        self.assertIn('group',core.search(self.db,self.fixture.root,{'date':DAY,'group':[self.groups[0]]})['items'][0]['unknown'])
        self.assertNotEqual(initial['version'],reset['version'])
        self.assertTrue(core.block(self.db,self.fixture.root,block['id'])['stale'])
        history=self.db.execute("SELECT data,at FROM history WHERE entity='term' AND entity_id=? AND action='classification' ORDER BY id",(self.tid,)).fetchall()
        self.assertEqual(len(history),2)
        event=json.loads(history[0]['data']); self.assertEqual(event['source'],'пользователь'); self.assertIn('decision_date',event)
        self.assertEqual(event['after']['groups'],self.groups[:2]); self.assertTrue(history[0]['at'])

    def test_group_only_preserves_legacy_color_and_any_category_is_possible(self):
        term=self.item()['terms']; term.update(color='yellow',color_source='старый источник',color_date='2026-11-16',category='Любые товары')
        self.db.execute('UPDATE terms SET data=? WHERE id=?',(json.dumps(term,ensure_ascii=False),self.tid))
        before=self.item()
        changed=self.save(before,[self.groups[0]],'yellow',False)
        self.assertEqual(tuple(changed['terms'][k] for k in ('color','color_source','color_date')),('yellow','старый источник','2026-11-16'))
        result=core.search(self.db,self.fixture.root,{'date':DAY,'group':[self.groups[1]],'category':'шины'})['items']
        self.assertEqual(len(result),1); self.assertEqual(result[0]['match'],'possible')
        self.assertTrue({'group','category'} <= set(result[0]['unknown']))
        confirmed=self.save(changed,[self.groups[0]],'yellow',True)
        self.assertEqual((confirmed['terms']['color_source'],confirmed['terms']['color_date']),('пользователь',DAY))

    def test_legacy_snapshot_and_stale_groups(self):
        task=self.fixture.tasks()[0]
        legacy_version=core.t.sha(core.t.canonical({k:v for k,v in task.items() if k not in ('version','operator_review','acknowledged_reasons')}).encode())
        self.assertEqual(task['version'],legacy_version)
        block=self.fixture.prepare(task)
        # Exercise a synthetic snapshot without review and preview metadata;
        # this does not establish compatibility with every historical format.
        row=self.db.execute('SELECT items FROM blocks WHERE id=?',(block['id'],)).fetchone()
        items=json.loads(row['items'])
        for item in items:
            for key in ('operator_review','acknowledged_reasons','public_snapshot','share_snapshot','share_base_snapshot'):
                item.pop(key,None)
        snapshot=core.t.canonical(items)
        version=core.t.sha(snapshot.encode())
        self.db.execute('UPDATE blocks SET items=?,plan_data=?,plan_hash=? WHERE id=?',(snapshot,snapshot,version,block['id']))
        self.db.execute('UPDATE web_blocks SET version=? WHERE block_id=?',(version,block['id']))
        self.assertFalse(core.block(self.db,self.fixture.root,block['id'])['stale'])
        term=self.item()['terms']; term['groups']=['Старая группа']
        self.db.execute('UPDATE terms SET data=? WHERE id=?',(json.dumps(term,ensure_ascii=False),self.tid))
        before=self.item()
        edited=self.save(before,[], 'green', True, False)
        self.assertTrue(core.block(self.db,self.fixture.root,block['id'])['stale'])
        self.assertEqual(edited['terms']['groups'],['Старая группа'])
        self.assertEqual(edited['terms']['color'],'green')
        with self.assertRaises(ValueError): self.save(edited,['Старая группа'],'yellow',True,True)
        self.assertEqual(self.item(),edited)

    def test_red_color_is_saved_searched_and_rendered_with_provenance(self):
        initial=self.item()
        edited=self.save(initial,[], 'red', True, False)
        self.assertEqual((edited['terms']['color'],edited['terms']['color_source'],edited['terms']['color_date']),('red','пользователь',DAY))
        for color in ('red','красный','красная'):
            result=core.search(self.db,self.fixture.root,{'date':DAY,'color':color})['items']
            self.assertEqual([item['term_id'] for item in result],[self.tid])
        preview=core.common_preview(self.db,self.fixture.root,{'date':DAY,'term_ids':[self.tid],'versions':{str(self.tid):edited['version']},'revisions':{str(self.tid):None}})
        self.assertIn('🔴 красный',preview['text'])
        event=json.loads(self.db.execute("SELECT data FROM history WHERE entity='term' AND entity_id=? AND action='classification' ORDER BY id DESC",(self.tid,)).fetchone()['data'])
        self.assertEqual(event['after']['color'],'red')
        self.assertEqual(event['source'],'пользователь')
        term=edited['terms']; term['color_date']='2026-11-16'
        self.db.execute('UPDATE terms SET data=? WHERE id=?',(json.dumps(term,ensure_ascii=False),self.tid))
        stale=self.item()
        self.assertIn('color',core.search(self.db,self.fixture.root,{'date':DAY,'color':'red'})['items'][0]['unknown'])
        draft,warnings=core.render_public(self.db,stale)
        self.assertIn('Светофор: не указан',draft)
        self.assertTrue(any('светофора' in warning for warning in warnings))

    def test_validation_does_not_mutate(self):
        before=self.item()
        for groups,color in [(['недопустимо'],None),([self.groups[0],self.groups[0]],None),([], 'blue')]:
            with self.assertRaises(ValueError): self.save(before,groups,color)
        with self.assertRaises(ValueError): core.search(self.db,self.fixture.root,{'date':DAY,'group':['устаревшая группа']})
        self.assertEqual(before,self.item())

    def public_save(self,text):
        item=self.item()
        return core.save_public_text(self.db,self.fixture.root,self.tid,{'date':DAY,'version':item['version'],'revision':item['public_text']['revision'],'text':text})

    def test_color_only_updates_current_manual_text_and_preserves_history(self):
        text='Ручные условия\r\nСветофор: не указан\r\nОсобый срок и цена'
        old=self.public_save(text)
        changed=self.save(self.item(),[], 'green', True, False)
        public=changed['public_text']
        self.assertFalse(public['stale'])
        self.assertNotEqual(old['revision'],public['revision'])
        self.assertEqual(public['text'],'Ручные условия\r\nСветофор: 🟢 зелёный\r\nОсобый срок и цена')
        entries=self.db.execute("SELECT data FROM history WHERE entity='public_text' AND entity_id=? ORDER BY id",(self.tid,)).fetchall()
        self.assertEqual([json.loads(x['data'])['text'] for x in entries],[text,public['text']])
        result=core.common_preview(self.db,self.fixture.root,{'date':DAY,'term_ids':[self.tid],'versions':{str(self.tid):changed['version']},'revisions':{str(self.tid):public['revision']}})
        self.assertEqual(result['text'],public['text'])
        cleared=self.save(changed,[],None,True,False)
        self.assertIn('Светофор: не указан',cleared['public_text']['text'])

    def test_ambiguous_or_previously_stale_text_stays_unchanged(self):
        for text in ('Условия без строки', 'Светофор: синий', 'Светофор: не указан\nСветофор: жёлтый'):
            with self.subTest(text=text):
                saved=self.public_save(text)
                changed=self.save(self.item(),[], 'green', True, False)
                self.assertEqual(changed['public_text']['text'],text)
                self.assertEqual(changed['public_text']['revision'],saved['revision'])
                self.assertTrue(changed['public_text']['stale'])
        saved=self.public_save('Условия\nСветофор: не указан')
        term=self.item()['terms']; term['conditions']='изменились'
        self.db.execute('UPDATE terms SET data=? WHERE id=?',(json.dumps(term,ensure_ascii=False),self.tid))
        changed=self.save(self.item(),[], 'yellow', True, False)
        self.assertEqual(changed['public_text']['revision'],saved['revision'])
        self.assertEqual(changed['public_text']['text'],'Условия\nСветофор: не указан')
        self.assertTrue(changed['public_text']['stale'])

    def test_group_change_does_not_confirm_text_and_failure_rolls_back(self):
        saved=self.public_save('Условия\nСветофор: не указан')
        changed=self.save(self.item(),self.groups[:1],'green',True,True)
        self.assertTrue(changed['public_text']['stale'])
        self.assertEqual(changed['public_text']['revision'],saved['revision'])
        self.public_save('Условия\nСветофор: 🟢 зелёный')
        before=self.item()
        with patch.object(core,'save_public_text',side_effect=ValueError('ошибка записи')):
            with self.assertRaises(ValueError): self.save(before,self.groups[:1],'yellow',True,False)
        self.assertEqual(self.item(),before)
