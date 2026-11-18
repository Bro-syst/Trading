import json
import unittest
import test_web_independent as fixture_module
import web_core as core

DAY=fixture_module.DAY

class VatCondition(unittest.TestCase):
    def setUp(self):
        self.fixture=fixture_module.LocalWebIndependent('test_chat_platform_preserves_provenance_and_roles')
        self.fixture.setUp(); self.addCleanup(self.fixture.tearDown)
        _,offer=self.fixture.offer(); self.tid=offer['term_ids'][0]
        self.fixture.cli('pricing',{'term_id':self.tid,'markup':'0','reason':'тест'})
        self.db=core.connect(self.fixture.root); self.addCleanup(self.db.close)

    def item(self, **filters):
        return next(x for x in core.search(self.db,self.fixture.root,{'date':DAY,**filters})['items'] if x['term_id']==self.tid)

    def set_raw(self, vat, payment):
        term=self.item()['terms']; term.update(vat=vat,payment=payment)
        self.db.execute('UPDATE terms SET data=? WHERE id=?',(json.dumps(term,ensure_ascii=False),self.tid))

    def choose(self,item,value):
        return core.classify_vat(self.db,self.fixture.root,self.tid,{'date':DAY,'version':item['version'],'vat_condition':value})['item']

    def test_generated_signal_provenance_survives_unchanged_saves_only(self):
        initial=self.item(); public=initial['public_text']
        self.assertTrue(public['generated_signal'])
        original=public['text']; version=initial['version']
        saved=core.save_public_text(self.db,self.fixture.root,self.tid,{'date':DAY,'version':version,'revision':None,'text':original})
        self.assertIsInstance(saved['revision'],int); self.assertTrue(saved['generated_signal'])
        unchanged=core.save_public_text(self.db,self.fixture.root,self.tid,{'date':DAY,'version':version,'revision':saved['revision'],'text':original})
        self.assertTrue(unchanged['generated_signal']); self.assertEqual(unchanged['text'],original)
        current=self.item(); self.assertEqual(current['version'],version)
        selection={'date':DAY,'term_ids':[self.tid],'versions':{str(self.tid):version},'revisions':{str(self.tid):unchanged['revision']}}
        self.assertEqual(core.common_preview(self.db,self.fixture.root,selection)['text'],unchanged['preview'])
        manual='Комментарий: первая строка\nСветофор: 🟢 зелёный'
        edited=core.save_public_text(self.db,self.fixture.root,self.tid,{'date':DAY,'version':version,'revision':unchanged['revision'],'text':manual})
        self.assertFalse(edited['generated_signal']); self.assertEqual(edited['preview'],manual)
        selection['revisions'][str(self.tid)]=edited['revision']
        self.assertEqual(core.common_preview(self.db,self.fixture.root,selection)['text'],manual)

    def test_exact_normalization_and_ambiguity(self):
        for raw,expected in [('22%','НДС 22%'),('с НДС 22','НДС 22%'),('НДС 22','НДС 22%'),('НДС22%','НДС 22%'),('с ндс 22%','НДС 22%'),('НДС 7%','НДС 7%'),('без ндс','Без НДС'),('0%','НДС 0%'),('Безотчет','Безотчёт')]:
            self.set_raw(raw,None); self.assertEqual(self.item()['vat_condition']['value'],expected)
        self.set_raw('НДС 20%','Без НДС')
        self.assertEqual(self.item(vat_condition='НДС 20%')['vat_condition']['status'],'conflict')
        self.assertIn('vat_condition',self.item(vat_condition='Без НДС')['unknown'])
        self.set_raw('НДС 20%','цена 20%')
        self.assertEqual(self.item()['vat_condition']['status'],'unknown')
        self.assertIn('vat_condition',self.item(vat_condition='НДС 20%')['unknown'])

    def test_choice_history_stale_and_search(self):
        self.set_raw('20%',None); initial=self.item()
        saved=core.save_public_text(self.db,self.fixture.root,self.tid,{'date':DAY,'version':initial['version'],'revision':None,'text':'Ручной текст'})
        changed=self.choose(initial,'Без НДС')
        self.assertEqual(changed['terms']['vat'],'20%'); self.assertEqual(changed['vat_condition']['value'],'Без НДС')
        self.assertFalse(changed['public_text']['stale']); self.assertEqual(changed['public_text']['text'],'Ручной текст\nНДС предложения: Без НДС')
        self.assertNotEqual(changed['public_text']['revision'],saved['revision'])
        self.assertNotIn('vat_condition',self.item(vat_condition='Без НДС')['unknown'])
        self.assertFalse(core.search(self.db,self.fixture.root,{'date':DAY,'vat_condition':'НДС 0%'})['items'])
        with self.assertRaises(ValueError): self.choose(initial,'НДС 0%')
        history=self.db.execute("SELECT data FROM history WHERE entity='term' AND entity_id=? AND action='classification'",(self.tid,)).fetchall()
        event=json.loads(history[-1]['data']); self.assertEqual(event['source'],'пользователь'); self.assertTrue(event['decision_date'])
        self.set_raw('НДС 22%',None)
        self.assertEqual(self.item()['vat_condition']['status'],'conflict')
        self.assertIn('vat_condition',self.item(vat_condition='НДС 20%')['unknown'])
        updated=self.choose(self.item(),'НДС 22%'); self.assertEqual(updated['vat_condition']['status'],'explicit')
        self.assertEqual(updated['terms']['vat'],'НДС 22%')

    def test_ambiguous_text_and_unknown_removal(self):
        initial=self.item()
        core.save_public_text(self.db,self.fixture.root,self.tid,{'date':DAY,'version':initial['version'],'revision':None,'text':'Ручной НДС в цене'})
        ambiguous=self.choose(initial,'НДС 20%')
        self.assertTrue(ambiguous['public_text']['stale'])
        self.assertEqual(ambiguous['public_text']['text'],'Ручной НДС в цене')
        core.save_public_text(self.db,self.fixture.root,self.tid,{'date':DAY,'version':ambiguous['version'],'revision':ambiguous['public_text']['revision'],'text':'Условия\nНДС компании: 20%'})
        added=self.choose(self.item(),'НДС 22%')
        self.assertIn('НДС предложения: НДС 22%',added['public_text']['text'])
        cleared=self.choose(added,'не указан')
        self.assertFalse(cleared['public_text']['stale'])
        self.assertNotIn('НДС предложения:',cleared['public_text']['text'])
        self.assertIn('НДС компании: 20%',cleared['public_text']['text'])

    def test_existing_company_vat_line_and_explicit_unspecified_filter(self):
        initial=self.item()
        text='Компания\nНДС: не указан\nНазначение: материалы'
        core.save_public_text(self.db,self.fixture.root,self.tid,{'date':DAY,'version':initial['version'],'revision':None,'text':text})
        chosen=self.choose(initial,'НДС 22%')
        self.assertEqual(chosen['public_text']['text'],text+'\nНДС предложения: НДС 22%')
        self.assertFalse(chosen['public_text']['stale'])
        unspecified=self.choose(chosen,'не указан')
        self.assertEqual(unspecified['public_text']['text'],text+'\n')
        self.assertFalse(unspecified['public_text']['stale'])
        possible=self.item(vat_condition='НДС 22%')
        self.assertIn('vat_condition',possible['unknown'])
        exact=self.item(vat_condition='не указан')
        self.assertNotIn('vat_condition',exact['unknown'])

    def test_common_and_recipient_previews_project_saved_vat_without_losing_text(self):
        initial=self.item()
        original=f"{initial['company']} — ИНН {initial['inn']}\nНДС: не указан\nНазначение: материалы, полный срок оплаты до 30 ноября"
        core.save_public_text(self.db,self.fixture.root,self.tid,{'date':DAY,'version':initial['version'],'revision':None,'text':original})
        chosen=self.choose(self.item(),'Безотчёт')
        self.assertFalse(chosen['public_text']['stale'])
        selection={'date':DAY,'term_ids':[self.tid], 'versions':{str(self.tid):chosen['version']}, 'revisions':{str(self.tid):chosen['public_text']['revision']}}
        expected=f"{initial['company']} — ИНН {initial['inn']}\nНДС: Безотчёт\nНазначение: материалы, полный срок оплаты до 30 ноября\n"
        self.assertEqual(core.common_preview(self.db,self.fixture.root,selection)['text'],expected)
        task=next(x for x in core.queue(self.db,self.fixture.root,self.fixture.b,DAY) if x['term_id']==self.tid)
        block=core.prepare(self.db,self.fixture.root,{'date':DAY,'chat_id':self.fixture.b,'task_ids':[task['id']],'versions':{str(task['id']):task['version']}})
        self.assertEqual(block['text'],expected)
        self.assertEqual(self.item()['public_text']['text'],original+'\nНДС предложения: Безотчёт')
        self.assertEqual(self.item()['public_text']['preview'],expected)
        core.t.hist(self.db,'public_text',self.tid,'save',{'version':chosen['version'],'text':chosen['public_text']['text'],'reason':'document_upload','previous_revision':chosen['public_text']['revision']})
        self.assertEqual(self.item()['public_text']['preview'],expected)
        current=self.item()
        core.save_public_text(self.db,self.fixture.root,self.tid,{'date':DAY,'version':current['version'],'revision':current['public_text']['revision'],'text':current['public_text']['text']})
        self.assertEqual(self.item()['public_text']['preview'],expected)
        current=self.item()
        manual='Компания\nНДС: не указан\nНазначение: строка 1\nНДС предложения: Безотчёт'
        core.save_public_text(self.db,self.fixture.root,self.tid,{'date':DAY,'version':current['version'],'revision':current['public_text']['revision'],'text':manual})
        self.assertEqual(self.item()['public_text']['preview'],manual)
        self.assertTrue(any('строки НДС' in w for w in self.item()['public_text']['warnings']))

    def test_vat_projection_preserves_known_and_ambiguous_values(self):
        self.assertEqual(core.client_vat_projection('Компания\nНДС: не указан\nНДС предложения: НДС 20%', 'auto','Компания'), 'Компания\nНДС: 20%\n')
        self.assertEqual(core.client_vat_projection('Компания\nНДС компании: 20%\nНДС предложения: НДС 20%', 'auto','Компания'), 'Компания\nНДС: 20%\n')
        self.assertEqual(core.client_vat_projection('Компания\nНДС компании: 22%\nНДС предложения: НДС 20%', 'auto','Компания'), 'Компания\nНДС: компании: 22%; предложения: 20%\n')
        self.assertEqual(core.client_vat_projection('Компания\nНДС компании: 22%\nНДС предложения: не указан', 'auto','Компания'), 'Компания\nНДС: 22%\n')
        self.assertEqual(core.client_vat_projection('Назначение\nНДС: не указан\nНДС предложения: Безотчёт', 'classified','Компания'), 'Назначение\nНДС: не указан\nНДС предложения: Безотчёт')
        self.assertEqual(core.client_vat_projection('Компания\nНДС компании: 20%\nНДС предложения: Безотчёт\nУсловия: НДС включён', 'auto','Компания'),
                         'Компания\nНДС: компании: 20%; предложения: Безотчёт\nУсловия: НДС включён')
        self.assertEqual(core.client_vat_projection('Компания\nНДС компании: на согласовании\nНДС предложения: Без НДС', 'auto','Компания'),
                         'Компания\nНДС: компании: на согласовании; предложения: Без НДС\n')
        ambiguous='НДС: не указан\nНДС предложения: Безотчёт\nНДС предложения: Без НДС'
        self.assertEqual(core.client_vat_projection(ambiguous,'auto','Компания'),ambiguous)

    def test_unchanged_auto_save_and_reclassification(self):
        self.set_raw('НДС 20%',None)
        initial=self.item()
        core.save_public_text(self.db,self.fixture.root,self.tid,{'date':DAY,'version':initial['version'],'revision':None,'text':initial['public_text']['text']})
        saved=self.item()
        self.assertEqual(saved['public_text']['preview'],initial['public_text']['preview'])
        changed=self.choose(saved,'НДС 20%')
        self.assertFalse(changed['public_text']['stale'])
        self.assertIn('НДС: 20%',changed['public_text']['preview'])
        cleared=self.choose(changed,'не указан')
        self.assertFalse(cleared['public_text']['stale'])
        self.assertEqual(cleared['public_text']['preview'].count('НДС'),1)

    def test_neutral_payment_and_reviewed_public_vat(self):
        for payment in ('перевод','без предоплаты'):
            self.set_raw('НДС 22%',payment)
            self.assertEqual(self.item()['vat_condition']['value'],'НДС 22%')
        self.set_raw('Безотчёт',None)
        inferred=self.item()
        self.assertNotIn('Безотчёт',inferred['public_text']['text'])
        explicit=self.choose(inferred,'Безотчёт')
        self.assertIn('Безотчёт',explicit['public_text']['text'])

    def test_comparison_accounts_for_explicit_vat_and_unknown(self):
        initial=self.item(); company_id=initial['company_id']
        source=self.fixture.cli('chat',{'name':'Другой источник'})['chat_id']
        _,other=self.fixture.offer(source=source,company_id=company_id); other_id=other['term_ids'][0]
        self.fixture.cli('pricing',{'term_id':other_id,'markup':'0','reason':'тест'})
        for tid,rate in ((self.tid,'10'),(other_id,'12')):
            term=json.loads(self.db.execute('SELECT data FROM terms WHERE id=?',(tid,)).fetchone()[0])
            term.update(category='материалы',rate=rate,rate_unit='%',rate_type='комиссия',rate_basis='платёж',vat='22%',payment='перевод',duration_text='2 дня',duration=2,duration_unit='day',duration_day_type='calendar',color='green',color_source='тест',color_date=DAY,conditions='нет',restrictions='нет',amount='нет',minimum='нет',currency='нет',scope_confirmed=True,review=False)
            self.db.execute('UPDATE terms SET data=? WHERE id=?',(json.dumps(term,ensure_ascii=False),tid))
        def tasks(): return core.queue(self.db,self.fixture.root,self.fixture.b,DAY)
        self.assertTrue(all(x['comparison']['comparable'] for x in tasks()))
        chosen=self.choose(self.item(),'Без НДС')
        self.assertTrue(all(not x['comparison']['comparable'] and not x['comparison']['lowest'] for x in tasks()))
        self.choose(chosen,'не указан')
        self.assertTrue(all(not x['comparison']['comparable'] and not x['comparison']['lowest'] for x in tasks()))

    def test_invalid_values(self):
        initial=self.item()
        with self.assertRaises(ValueError): self.choose(initial,'С НДС')
        with self.assertRaises(ValueError): core.search(self.db,self.fixture.root,{'date':DAY,'vat_condition':'С НДС'})
        self.assertEqual(initial,self.item())
