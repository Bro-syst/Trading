"""End-to-end tests of the trading CLI; no external services or project data changes."""
import json
import hashlib
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

CLI = Path(__file__).resolve().parents[1] / 'trading.py'
PROJECT = CLI.parent
DAY = '2026-11-17'


class TradingWorkflow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = self.root / 'test.sqlite'
        self.run_cli('init')
        self.a = self.run_cli('chat', {'name': 'Source A'})['chat_id']
        self.b = self.run_cli('chat', {'name': 'Source B'})['chat_id']
        self.c = self.run_cli('chat', {'name': 'Recipient C'})['chat_id']

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, command, payload=None, ok=True):
        proc = subprocess.run([sys.executable, str(CLI), '--root', str(self.root),
                               '--db', str(self.db), command],
                              input=json.dumps(payload or {}, ensure_ascii=False),
                              text=True, capture_output=True)
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError:
            self.fail(f'{command}: invalid JSON: stdout={proc.stdout!r} stderr={proc.stderr!r}')
        self.assertEqual(data.get('ok'), ok, (command, data, proc.stderr))
        if ok:
            self.assertEqual(proc.returncode, 0, (command, data, proc.stderr))
        return data

    def ingest(self, chat, text='source text', **extra):
        return self.run_cli('ingest', {'chat_id': chat, 'date': DAY, 'text': text, **extra})

    def offer(self, chat, company=None, company_id=None, terms=None, source_id=None):
        message = self.ingest(chat, source_id=source_id) if source_id else self.ingest(chat)
        record = {'terms': terms or [self.term()]}
        if company_id is None:
            record['company'] = company or {'name': 'Test Company', 'inn': '7733446222', 'inn_status': 'verified'}
        else:
            record['company_id'] = company_id
        result = self.run_cli('import', {'message_id': message['message_id'], 'offers': [record]})
        return message, result

    @staticmethod
    def term(category='строительные материалы', rate='15.5', **extra):
        return {'category': category, 'rate': rate, 'duration': 2, 'duration_unit': 'day',
                'vat': None, 'color': None, 'conditions': '', 'scope_confirmed': True,
                'review': False, **extra}

    def test_original_survives_failed_structured_import_and_idempotency(self):
        text = 'Компания, исходное сообщение: 15,5%\nНе изменять!'
        first = self.ingest(self.a, text, source_id='telegram-42')
        repeated = self.ingest(self.a, text, source_id='telegram-42')
        self.assertEqual(first['message_id'], repeated['message_id'])
        self.run_cli('import', {'message_id': first['message_id'], 'offers': [
            {'company': {'name': 'А', 'inn': '7733446222', 'inn_status': 'verified'},
             'terms': [self.term()]},
            {'company': {'name': 'Б', 'inn': '123', 'inn_status': 'verified'},
             'terms': [self.term(rate='не число')]},
        ]}, ok=False)
        with sqlite3.connect(f'file:{self.db}?mode=ro', uri=True) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM offers').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM companies').fetchone()[0], 0)
        matches = [p for p in self.root.rglob('*') if p.is_file() and p.suffix in ('.txt', '.md', '.json')
                   and text in p.read_text(encoding='utf-8', errors='ignore')]
        self.assertTrue(matches, 'Исходное сообщение должно сохраниться при ошибке разбора')
        # Retrying a valid import after failure must succeed (no orphaned first company).
        result = self.run_cli('import', {'message_id': first['message_id'], 'offers': [
            {'company': {'name': 'А', 'inn': '7733446222', 'inn_status': 'verified'},
             'terms': [self.term()]}]})
        self.assertEqual(len(result['term_ids']), 1)

    def test_original_company_is_never_returned_to_its_source(self):
        _, first = self.offer(self.a)
        company_id = first['company_ids'][0]
        _, other = self.offer(self.b, company_id=company_id, terms=[self.term(rate='17')])
        plan = self.run_cli('plan', {'chat_id': self.a, 'date': DAY})
        by_id = {i['term_id']: i for i in plan['items']}
        # No matter which source supplied it, company cannot go back to Source A.
        for term_id in (first['term_ids'][0], other['term_ids'][0]):
            self.assertIn(term_id, by_id)
            self.assertNotEqual(by_id[term_id]['reason'], 'ok')
        draft = self.run_cli('draft', {'chat_id': self.a, 'date': DAY})
        self.assertNotIn(other['term_ids'][0], [i['term_id'] for i in draft['items']])

    def test_unverified_valid_inn_matches_separate_ids_for_source_and_sent(self):
        company = {'name': 'Одинаковая', 'inn': '7733446222', 'inn_status': 'unverified'}
        _, first = self.offer(self.a, company=company)
        tid = first['term_ids'][0]
        item = next(x for x in self.run_cli('plan', {'chat_id': self.c, 'date': DAY})['items'] if x['term_id'] == tid)
        self.assertEqual(item['reason'], 'ok')
        self.assertEqual(item['inn_status'], 'unverified')
        self.assertEqual(next(x for x in self.run_cli('search', {'date': DAY})['items'] if x['term_id'] == tid)['match'], 'full')
        self.assertNotIn(tid, [x['term_id'] for x in self.run_cli('review')['items']])
        draft = self.run_cli('draft', {'chat_id': self.c, 'date': DAY})
        self.run_cli('confirm', {'block_id': draft['block_id'], 'term_ids': [tid]})
        _, second = self.offer(self.b, company=company, terms=[self.term(rate='19')])
        self.assertNotEqual(first['company_ids'][0], second['company_ids'][0])
        next_item = next(x for x in self.run_cli('plan', {'chat_id': self.c, 'date': DAY})['items'] if x['term_id'] == second['term_ids'][0])
        self.assertIn('company_previously_sent_review', next_item['reasons'])
        _, own = self.offer(self.c, company=company, terms=[self.term(rate='22')])
        plan = self.run_cli('plan', {'chat_id': self.c, 'date': DAY})
        for item in plan['items']:
            if item['term_id'] in (tid, second['term_ids'][0], own['term_ids'][0]):
                self.assertIn('recipient_source_today', item['reasons'])
                self.assertNotIn('possible_identity_conflict_review', item['reasons'])

    def test_conflicting_identity_still_blocks_own_and_prior_send_by_valid_inn(self):
        company={'name':'Совпадающий ИНН','inn':'7733446222','inn_status':'unverified'}
        _, original=self.offer(self.a, company=company)
        draft=self.run_cli('draft', {'chat_id':self.c,'date':DAY})
        self.run_cli('confirm', {'block_id':draft['block_id'],'term_ids':original['term_ids']})
        _, conflicting=self.offer(self.b, company={**company,'inn_status':'conflict'})
        tid=conflicting['term_ids'][0]
        item=next(x for x in self.run_cli('plan', {'chat_id':self.c,'date':DAY})['items'] if x['term_id']==tid)
        self.assertIn('company_previously_sent_review',item['reasons'])
        self.assertIn('identity_review',item['reasons'])
        self.offer(self.c, company={**company,'inn_status':'conflict'})
        item=next(x for x in self.run_cli('plan', {'chat_id':self.c,'date':DAY})['items'] if x['term_id']==original['term_ids'][0])
        self.assertIn('recipient_source_today',item['reasons'])
        self.assertIn('possible_identity_conflict_review',item['reasons'])
        self.run_cli('update', {'action':'risk_decision','chat_id':self.c,'date':DAY,'term_id':item['term_id'],'reason':'attempt'},ok=False)

    def test_duplicate_sources_cannot_enter_one_draft_after_review(self):
        company={'name':'Одна компания','inn':'7733446222','inn_status':'unverified'}
        _, one=self.offer(self.a, company=company)
        _, two=self.offer(self.b, company=company, terms=[self.term(rate='19')])
        plan=self.run_cli('plan', {'chat_id':self.c,'date':DAY})
        for item in plan['items']:
            self.assertIn('multiple_sources_review', item['reasons'])
            self.run_cli('update', {'action':'risk_decision','chat_id':self.c,'date':DAY,
                                    'term_id':item['term_id'],'reason':'reviewed'})
        self.run_cli('draft', {'chat_id':self.c,'date':DAY}, ok=False)

    def test_invalid_inn_and_real_identity_conflicts_remain_reviewed(self):
        _, invalid = self.offer(self.a, company={'name':'Плохой ИНН','inn':'123','inn_status':'unverified'})
        _, one = self.offer(self.a, company={'name':'Разное название','inn':'7733446222','inn_status':'unverified'})
        _, other = self.offer(self.b, company={'name':'Другое название','inn':'7733446222','inn_status':'unverified'})
        _, conflict = self.offer(self.a, company={'name':'Конфликт','inn':'7707083893','inn_status':'conflict'})
        plan = {x['term_id']: x for x in self.run_cli('plan', {'chat_id': self.c, 'date': DAY})['items']}
        self.assertIn('identity_review', plan[invalid['term_ids'][0]]['reasons'])
        self.assertIn('identity_review', plan[conflict['term_ids'][0]]['reasons'])
        for result in (one, other):
            self.assertIn('possible_identity_conflict_review', plan[result['term_ids'][0]]['reasons'])

    def test_unknown_common_scope_and_missing_identity_do_not_enter_draft(self):
        _, ambiguous = self.offer(self.a, company={'name': 'Без ИНН', 'inn': None, 'inn_status': 'missing'},
                                  terms=[self.term(scope_confirmed=False, review=True)])
        plan = self.run_cli('plan', {'chat_id': self.c, 'date': DAY})
        item = next(i for i in plan['items'] if i['term_id'] == ambiguous['term_ids'][0])
        self.assertNotEqual(item['reason'], 'ok')
        draft = self.run_cli('draft', {'chat_id': self.c, 'date': DAY})
        self.assertNotIn(ambiguous['term_ids'][0], [i['term_id'] for i in draft['items']])

    def test_partial_send_excludes_only_confirmed_term_and_snapshot_not_assumed(self):
        _, result = self.offer(self.a, terms=[self.term(category='строительные материалы'),
                                              self.term(category='оборудование', rate='16')])
        first, second = result['term_ids']
        draft = self.run_cli('draft', {'chat_id': self.c, 'date': DAY})
        self.assertEqual({i['term_id'] for i in draft['items']}, {first, second})
        sent = self.run_cli('confirm', {'block_id': draft['block_id'], 'term_ids': [first], 'document_ids': []})
        self.assertFalse(sent['actual_text_known'])
        with sqlite3.connect(f'file:{self.db}?mode=ro', uri=True) as db:
            self.assertEqual(db.execute('SELECT actual_text, actual_hash FROM sends').fetchone(), (None, None))
        plan = self.run_cli('plan', {'chat_id': self.c, 'date': DAY})
        by_id = {i['term_id']: i for i in plan['items']}
        self.assertNotEqual(by_id[first]['reason'], 'ok')
        self.assertEqual(by_id[second]['reason'], 'ok')
        second_draft = self.run_cli('draft', {'chat_id': self.c, 'date': DAY})
        self.assertEqual([i['term_id'] for i in second_draft['items']], [second])

    def test_draft_becomes_stale_when_recipient_sends_same_company(self):
        _, original = self.offer(self.a)
        draft = self.run_cli('draft', {'chat_id': self.c, 'date': DAY})
        self.assertTrue(draft['items'])
        self.offer(self.c, company_id=original['company_ids'][0])
        check = self.run_cli('check-draft', {'block_id': draft['block_id']})
        self.assertTrue(check['stale'])
        self.run_cli('confirm', {'block_id': draft['block_id'], 'term_ids': original['term_ids'],
                                 'document_ids': []}, ok=False)

    def test_actual_stale_send_requires_explicit_fact_and_is_audited(self):
        _, original = self.offer(self.a)
        draft = self.run_cli('draft', {'chat_id': self.c, 'date': DAY})
        self.offer(self.c, company_id=original['company_ids'][0])
        payload = {'block_id': draft['block_id'], 'term_ids': original['term_ids'],
                   'actual_text': 'Действительно отправленный текст'}
        self.run_cli('confirm', payload, ok=False)
        self.assertFalse(self.run_cli('history')['sends'])
        confirmation = self.run_cli('confirm', {**payload, 'stale_send_confirmed': True})
        self.assertTrue(confirmation['actual_text_known'])
        history = self.run_cli('history')
        self.assertEqual(history['sends'][0]['actual_text'], payload['actual_text'])
        self.assertTrue(any(x['entity'] == 'block' and x['entity_id'] == draft['block_id']
                            and x['action'] == 'confirm_stale_send' for x in history['history']))

    def test_source_ids_are_scoped_to_chat_and_changed_content_is_versioned(self):
        a1 = self.ingest(self.a, 'Первоначальный текст', source_id='42')
        b1 = self.ingest(self.b, 'Другой чат, тот же ID', source_id='42')
        self.assertNotEqual(a1['message_id'], b1['message_id'])
        changed = self.ingest(self.a, 'Исправленный текст', source_id='42')
        repeated = self.ingest(self.a, 'Исправленный текст', source_id='42')
        self.assertEqual(changed['message_id'], repeated['message_id'])
        contents = [p.read_text(encoding='utf-8', errors='ignore') for p in self.root.rglob('*')
                    if p.is_file() and p.suffix in ('.txt', '.md', '.json')]
        self.assertTrue(any('Первоначальный текст' in x for x in contents))
        self.assertTrue(any('Исправленный текст' in x for x in contents))

    def test_competing_message_versions_block_both_until_resolved(self):
        first = self.ingest(self.a, 'Исходная ставка 15,5%', source_id='msg-1')
        old = self.run_cli('import', {'message_id': first['message_id'], 'offers': [{
            'company': {'name': 'Test Company', 'inn': '7733446222', 'inn_status': 'verified'},
            'terms': [self.term()]}]})
        second = self.ingest(self.a, 'Исправленная ставка 17%', source_id='msg-1')
        prior = self.run_cli('plan', {'chat_id': self.c, 'date': DAY})
        self.assertNotEqual(next(x for x in prior['items'] if x['term_id'] == old['term_ids'][0])['reason'], 'ok')
        prior_search = next(x for x in self.run_cli('search', {'date': DAY})['items']
                            if x['term_id'] == old['term_ids'][0])
        self.assertEqual(prior_search['match'], 'possible')
        self.assertIn('message_versions_review', prior_search['unknown'])
        new = self.run_cli('import', {'message_id': second['message_id'], 'offers': [{
            'company_id': old['company_ids'][0], 'terms': [self.term(rate='17')]}]})
        old_id, new_id = old['term_ids'][0], new['term_ids'][0]
        plan = self.run_cli('plan', {'chat_id': self.c, 'date': DAY})
        items = {x['term_id']: x for x in plan['items']}
        self.assertNotEqual(items[old_id]['reason'], 'ok')
        self.assertNotEqual(items[new_id]['reason'], 'ok')
        search = {x['term_id']: x for x in self.run_cli('search', {'date': DAY})['items']}
        for tid in (old_id, new_id):
            self.assertEqual(search[tid]['match'], 'possible')
            self.assertIn('message_versions_review', search[tid]['unknown'])
        draft = self.run_cli('draft', {'chat_id': self.c, 'date': DAY})
        self.assertFalse({old_id, new_id} & {x['term_id'] for x in draft['items']})
        self.run_cli('update', {'action': 'withdraw', 'term_id': old_id,
                                'reason': 'Заменено исправленной версией источника'})
        plan = self.run_cli('plan', {'chat_id': self.c, 'date': DAY})
        self.assertEqual(next(x for x in plan['items'] if x['term_id'] == new_id)['reason'], 'ok')
        resolved = next(x for x in self.run_cli('search', {'date': DAY})['items'] if x['term_id'] == new_id)
        self.assertEqual(resolved['match'], 'full')
        self.assertEqual((self.root / first['path']).read_text(encoding='utf-8'), 'Исходная ставка 15,5%')
        self.assertEqual((self.root / second['path']).read_text(encoding='utf-8'), 'Исправленная ставка 17%')

    def test_file_provenance_and_original_survive_backup_restore(self):
        original = self.root / 'legacy.md'
        content = '# История\n\n## Секция компании\nУсловия 15,5%\n\nДругие данные\n'
        original.write_text(content, encoding='utf-8')
        msg = self.ingest(self.a, 'Условия 15,5%', source_file='legacy.md',
                          source_heading='Секция компании')
        self.assertEqual(msg['source_file'], 'legacy.md')
        self.assertEqual(msg['source_heading'], 'Секция компании')
        copy_path = msg['source_copy_path']
        self.assertEqual((self.root / copy_path).read_text(encoding='utf-8'), content)
        original.write_text('Изменённое исходное расположение', encoding='utf-8')
        self.assertEqual((self.root / copy_path).read_text(encoding='utf-8'), content)
        backup = self.run_cli('backup', {'directory': 'provenance-backup'})
        restored = self.run_cli('restore', {'backup_directory': backup['path'],
                                             'destination': 'provenance-restored'})
        restore_root = self.root / restored['path']
        self.assertEqual((restore_root / copy_path).read_text(encoding='utf-8'), content)
        with sqlite3.connect(f'file:{restore_root / "registry.sqlite"}?mode=ro', uri=True) as db:
            row = db.execute('SELECT source_file, source_heading, source_copy_path, source_copy_sha256 '
                             'FROM messages WHERE id=?', (msg['message_id'],)).fetchone()
        self.assertEqual(row, ('legacy.md', 'Секция компании', copy_path,
                               hashlib.sha256(content.encode()).hexdigest()))

    def test_identity_decision_records_reason_and_preserves_history(self):
        _, result = self.offer(self.a, company={'name': 'Неясная компания', 'inn': None,
                                                'inn_status': 'missing'})
        cid, tid = result['company_ids'][0], result['term_ids'][0]
        before = self.run_cli('plan', {'chat_id': self.c, 'date': DAY})
        self.assertNotEqual(next(x for x in before['items'] if x['term_id'] == tid)['reason'], 'ok')
        self.run_cli('update', {'action': 'company_identity', 'company_id': cid,
                                'inn': '7733446222', 'inn_status': 'verified',
                                'reason': 'Сверено с карточкой компании'})
        after = self.run_cli('plan', {'chat_id': self.c, 'date': DAY})
        self.assertEqual(next(x for x in after['items'] if x['term_id'] == tid)['reason'], 'ok')
        with sqlite3.connect(f'file:{self.db}?mode=ro', uri=True) as db:
            raw = db.execute("SELECT data FROM history WHERE entity='company' AND entity_id=? "
                             "AND action='identity_decision'", (cid,)).fetchone()
        self.assertIsNotNone(raw)
        decision = json.loads(raw[0])
        self.assertEqual(decision['reason'], 'Сверено с карточкой компании')
        self.assertEqual(decision['before']['inn_status'], 'missing')
        self.assertIsNone(decision['before']['inn'])

    def test_message_can_reference_the_block_it_answers(self):
        self.offer(self.a)
        block = self.run_cli('draft', {'chat_id': self.c, 'date': DAY})
        reply = self.ingest(self.c, 'Предлагаем другую ставку', reply_block_id=block['block_id'])
        with sqlite3.connect(f'file:{self.db}?mode=ro', uri=True) as db:
            linked = db.execute('SELECT reply_block_id FROM messages WHERE id=?',
                                (reply['message_id'],)).fetchone()[0]
        self.assertEqual(linked, block['block_id'])
        self.run_cli('ingest', {'chat_id': self.c, 'date': DAY, 'text': 'Ответ',
                                'reply_block_id': 999999}, ok=False)

    def test_same_text_without_source_id_does_not_discard_a_message(self):
        first = self.ingest(self.a, 'Возможный повтор')
        second = self.ingest(self.a, 'Возможный повтор')
        self.assertNotEqual(first['message_id'], second['message_id'])
        self.assertTrue(second.get('warnings'), 'Вероятный повтор должен быть обозначен')

    def test_preparation_is_not_send_and_confirmation_is_per_recipient(self):
        _, result = self.offer(self.a)
        term_id = result['term_ids'][0]
        first = self.run_cli('draft', {'chat_id': self.c, 'date': DAY})
        again = self.run_cli('draft', {'chat_id': self.c, 'date': DAY})
        self.assertIn(term_id, [i['term_id'] for i in again['items']])
        self.run_cli('confirm', {'block_id': first['block_id'], 'term_ids': [term_id],
                                 'document_ids': []})
        other_recipient = self.run_cli('chat', {'name': 'Recipient D'})['chat_id']
        plan = self.run_cli('plan', {'chat_id': other_recipient, 'date': DAY})
        item = next(i for i in plan['items'] if i['term_id'] == term_id)
        self.assertEqual(item['reason'], 'ok')

    def test_prior_day_offer_is_not_current(self):
        _, result = self.offer(self.a)
        tomorrow = self.run_cli('draft', {'chat_id': self.c, 'date': '2026-11-18'})
        self.assertNotIn(result['term_ids'][0], [i['term_id'] for i in tomorrow['items']])

    def test_two_partial_confirmations_of_same_block(self):
        _, result = self.offer(self.a, terms=[self.term(), self.term(category='оборудование')])
        first, second = result['term_ids']
        draft = self.run_cli('draft', {'chat_id': self.c, 'date': DAY})
        self.run_cli('confirm', {'block_id': draft['block_id'], 'term_ids': [first]})
        self.run_cli('confirm', {'block_id': draft['block_id'], 'term_ids': [second]})
        self.run_cli('confirm', {'block_id': draft['block_id'], 'term_ids': [first]}, ok=False)

    def test_changed_sent_terms_are_reviewed_not_silently_resent(self):
        _, result = self.offer(self.a)
        original = result['term_ids'][0]
        draft = self.run_cli('draft', {'chat_id': self.c, 'date': DAY})
        self.run_cli('confirm', {'block_id': draft['block_id'], 'term_ids': [original]})
        changed = self.run_cli('update', {'action': 'replace', 'term_id': original,
                                          'reason': 'Уточнена ставка источником',
                                          'terms': self.term(rate='17')})['new_term_id']
        plan = self.run_cli('plan', {'chat_id': self.c, 'date': DAY})
        item = next(x for x in plan['items'] if x['term_id'] == changed)
        self.assertNotEqual(item['reason'], 'ok')
        new_draft = self.run_cli('draft', {'chat_id': self.c, 'date': DAY})
        self.assertNotIn(changed, [x['term_id'] for x in new_draft['items']])

    def test_document_added_after_draft_makes_it_stale(self):
        message, result = self.offer(self.a)
        draft = self.run_cli('draft', {'chat_id': self.c, 'date': DAY})
        card = self.root / 'new-card.pdf'
        card.write_bytes(b'new card')
        self.run_cli('document', {'message_id': message['message_id'],
                                  'company_id': result['company_ids'][0], 'path': str(card)})
        self.assertTrue(self.run_cli('check-draft', {'block_id': draft['block_id']})['stale'])
        self.run_cli('confirm', {'block_id': draft['block_id'], 'term_ids': result['term_ids']}, ok=False)

    def test_changing_company_inn_invalidates_verified_document(self):
        message, result = self.offer(self.a)
        card = self.root / 'card.pdf'
        card.write_bytes(b'original card')
        doc = self.run_cli('document', {'message_id': message['message_id'],
                                         'company_id': result['company_ids'][0], 'path': str(card)})
        did = doc['document_id']
        self.run_cli('update', {'action': 'document_review', 'document_id': did, 'status': 'verified',
                                'document_inn': '7733446222', 'reason': 'Карточка сверена'})
        self.run_cli('update', {'action': 'company_identity', 'company_id': result['company_ids'][0],
                                'inn': '7707083893', 'inn_status': 'verified',
                                'reason': 'Исправлен ошибочный ИНН'})
        review = self.run_cli('review')
        item = next(x for x in review['documents'] if x['id'] == did)
        self.assertNotEqual(item['status'], 'verified')
        self.run_cli('update', {'action': 'document_review', 'document_id': did, 'status': 'verified',
                                'document_inn': '7733446222', 'reason': 'Старый ИНН'}, ok=False)

    def test_corrupt_document_cannot_be_verified_or_sent(self):
        message, result = self.offer(self.a)
        card = self.root / 'card.pdf'
        card.write_bytes(b'original card')
        doc = self.run_cli('document', {'message_id': message['message_id'],
                                         'company_id': result['company_ids'][0], 'path': str(card)})
        draft = self.run_cli('draft', {'chat_id': self.c, 'date': DAY})
        (self.root / doc['path']).write_bytes(b'changed card')
        self.run_cli('update', {'action': 'document_review', 'document_id': doc['document_id'],
                                'status': 'verified', 'document_inn': '7733446222',
                                'reason': 'Попытка сверки'}, ok=False)
        self.assertTrue(self.run_cli('check-draft', {'block_id': draft['block_id']})['stale'])
        self.run_cli('confirm', {'block_id': draft['block_id'], 'term_ids': result['term_ids'],
                                 'document_ids': [doc['document_id']]}, ok=False)
        self.run_cli('draft', {'chat_id': self.c, 'date': DAY}, ok=False)

    def test_mention_in_request_is_a_risk_not_own_offer(self):
        _, result = self.offer(self.a)
        inquiry = self.ingest(self.c, 'Есть ли Test Company?')
        self.run_cli('import', {'message_id': inquiry['message_id'],
                                'requests': [{'text': 'Есть ли Test Company?'}]})
        plan = self.run_cli('plan', {'chat_id': self.c, 'date': DAY})
        item = next(x for x in plan['items'] if x['term_id'] == result['term_ids'][0])
        self.assertNotEqual(item['reason'], 'ok')
        self.assertNotEqual(item['reason'], 'recipient_source_today')

    def test_risk_decision_is_scoped_and_invalidated_by_new_context(self):
        _, result = self.offer(self.a)
        tid = result['term_ids'][0]
        inquiry = self.ingest(self.c, 'Есть ли Test Company?')
        self.run_cli('import', {'message_id': inquiry['message_id'],
                                'requests': [{'text': 'Есть ли Test Company?'}]})
        decision = {'action': 'risk_decision', 'chat_id': self.c, 'date': DAY,
                    'term_id': tid, 'reason': 'Уточнено у получателя'}
        self.run_cli('update', decision)
        item = next(x for x in self.run_cli('plan', {'chat_id': self.c, 'date': DAY})['items']
                    if x['term_id'] == tid)
        self.assertEqual(item['reason'], 'ok')
        self.ingest(self.c, 'Новая информация')
        item = next(x for x in self.run_cli('plan', {'chat_id': self.c, 'date': DAY})['items']
                    if x['term_id'] == tid)
        self.assertNotEqual(item['reason'], 'ok')
        self.run_cli('update', decision)
        self.offer(self.c, company_id=result['company_ids'][0])
        self.run_cli('update', decision, ok=False)
        self.assertNotIn(tid, [x['term_id'] for x in self.run_cli('draft',
                              {'chat_id': self.c, 'date': DAY})['items']])

    def test_risk_decision_cannot_override_already_sent(self):
        _, result = self.offer(self.a)
        tid = result['term_ids'][0]
        draft = self.run_cli('draft', {'chat_id': self.c, 'date': DAY})
        self.run_cli('confirm', {'block_id': draft['block_id'], 'term_ids': [tid]})
        self.run_cli('update', {'action': 'risk_decision', 'chat_id': self.c, 'date': DAY,
                                'term_id': tid, 'reason': 'Повтор'}, ok=False)

    def test_partial_confirmations_remain_valid_after_approved_risks(self):
        _, result = self.offer(self.a, terms=[self.term(), self.term(category='оборудование')])
        inquiry = self.ingest(self.c, 'Есть ли Test Company?')
        self.run_cli('import', {'message_id': inquiry['message_id'],
                                'requests': [{'text': 'Есть ли Test Company?'}]})
        first, second = result['term_ids']
        for term in (first, second):
            self.run_cli('update', {'action': 'risk_decision', 'chat_id': self.c,
                                    'date': DAY, 'term_id': term, 'reason': 'Уточнено'})
        draft = self.run_cli('draft', {'chat_id': self.c, 'date': DAY})
        self.assertEqual({x['term_id'] for x in draft['items']}, {first, second})
        self.run_cli('confirm', {'block_id': draft['block_id'], 'term_ids': [first]})
        self.run_cli('confirm', {'block_id': draft['block_id'], 'term_ids': [second]})

    def test_search_marks_unverified_identity_and_scope_as_possible(self):
        _, result = self.offer(self.a, company={'name': 'Непроверено', 'inn': '7733446222',
                                                'inn_status': 'unverified'},
                               terms=[self.term(scope_confirmed=False, review=True)])
        item = next(x for x in self.run_cli('search', {'date': DAY})['items']
                    if x['term_id'] == result['term_ids'][0])
        self.assertEqual(item['match'], 'possible')
        self.assertIn('terms_review', item['unknown'])
        self.assertNotIn('identity_review', item['unknown'])

    def test_search_separates_unknown_fields_from_confirmed_matches(self):
        _, result = self.offer(self.a, terms=[
            self.term(color='green'),
            self.term(rate='16', duration=None, duration_unit=None),
            self.term(rate='17', color='yellow'),
        ])
        found = self.run_cli('search', {'date': DAY, 'category': 'строительные',
                                        'color': 'green', 'max_duration': 2})
        matches = {x['term_id']: x for x in found['items']}
        first, uncertain, mismatch = result['term_ids']
        self.assertEqual(matches[first]['match'], 'full')
        self.assertEqual(matches[uncertain]['match'], 'possible')
        self.assertTrue({'color', 'duration'}.issubset(matches[uncertain]['unknown']))
        self.assertNotIn(mismatch, matches)

    def test_search_supports_category_and_color_synonyms(self):
        _, result = self.offer(self.a, terms=[self.term(category='стройматериалы', color='зелёный'),
                                              self.term(category='услуги', color='yellow')])
        for category in ('строительные материалы', 'стромат', 'стройматериалы'):
            for color in ('green', 'зеленая', 'зелёный'):
                items = self.run_cli('search', {'date': DAY, 'category': category, 'color': color})['items']
                self.assertEqual([x['term_id'] for x in items], [result['term_ids'][0]])
        yellow = self.run_cli('search', {'date': DAY, 'category': 'услуги', 'color': 'жёлтый'})['items']
        self.assertEqual([x['term_id'] for x in yellow], [result['term_ids'][1]])

    def test_search_red_color_synonyms(self):
        _, result = self.offer(self.a, terms=[self.term(color='red'), self.term(color='красный')])
        for color in ('red', 'красный', 'красная'):
            items = self.run_cli('search', {'date': DAY, 'color': color})['items']
            self.assertEqual({x['term_id'] for x in items}, set(result['term_ids']))

    def test_coverage_records_do_not_imply_completeness(self):
        self.ingest(self.a, 'Первое сообщение')
        self.ingest(self.a, 'Последнее сообщение')
        coverage = self.run_cli('coverage', {'chat_id': self.a})
        self.assertEqual(coverage['records'], [])
        self.assertTrue(coverage['warning'])
        record = {'from': DAY, 'to': DAY, 'complete': False, 'source': 'ручная загрузка'}
        self.run_cli('coverage', {'chat_id': self.a, 'record': record})
        self.assertEqual(self.run_cli('coverage', {'chat_id': self.a})['records'][0]['data'], record)

    def test_review_plan_and_search_include_source_context(self):
        message, result = self.offer(self.a, company={'name': 'Нужна проверка', 'inn': '123',
                                                       'inn_status': 'unverified'},
                                     terms=[self.term(review=True, scope_confirmed=False)])
        tid = result['term_ids'][0]
        for command, payload in [('review', {}), ('plan', {'chat_id': self.c, 'date': DAY}),
                                 ('search', {'date': DAY})]:
            item = next(x for x in self.run_cli(command, payload)['items'] if x['term_id'] == tid)
            self.assertEqual(item['company'], 'Нужна проверка')
            self.assertEqual(item['inn'], '123')
            self.assertEqual(item['source_chat'], 'Source A')
            self.assertEqual(item['source_chat_id'], self.a)
            self.assertEqual(item['message_id'], message['message_id'])
            self.assertEqual(item['source_path'], message['path'])
            self.assertEqual((self.root / item['source_path']).read_text(encoding='utf-8'), 'source text')
            self.assertEqual(item['terms']['rate'], '15.5')
        review = next(x for x in self.run_cli('review')['items'] if x['term_id'] == tid)
        self.assertEqual(review['inn_status'], 'unverified')
        self.assertTrue({'identity_review', 'terms_review'}.issubset(review['reasons']))

    def test_empty_draft_has_warning_and_summary_without_persisted_block(self):
        _, result = self.offer(self.a, company={'name': 'Без ИНН', 'inn': None, 'inn_status': 'missing'})
        draft = self.run_cli('draft', {'chat_id': self.c, 'date': DAY})
        self.assertIsNone(draft['block_id'])
        self.assertEqual(draft['text'], '')
        self.assertEqual(draft['items'], [])
        self.assertTrue(draft['warning'])
        self.assertEqual(draft['warning_code'], 'NO_ELIGIBLE_ITEMS')
        self.assertTrue(draft['reason_summary'])
        self.assertIn(result['term_ids'][0], [x['term_id'] for x in draft['exclusions']])
        with sqlite3.connect(f'file:{self.db}?mode=ro', uri=True) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM blocks').fetchone()[0], 0)

    def test_unverified_document_warned_but_actual_send_can_be_recorded(self):
        message, result = self.offer(self.a)
        card = self.root / 'unchecked.pdf'
        card.write_bytes(b'card')
        doc = self.run_cli('document', {'message_id': message['message_id'],
                                       'company_id': result['company_ids'][0], 'path': str(card)})
        draft = self.run_cli('draft', {'chat_id': self.c, 'date': DAY})
        self.assertNotIn(doc['document_id'], [x['id'] for x in draft['documents']])
        self.assertIn(doc['document_id'], [x['id'] for x in draft['document_warnings']])
        confirmation = self.run_cli('confirm', {'block_id': draft['block_id'],
                                                  'term_ids': result['term_ids'],
                                                  'document_ids': [doc['document_id']]})
        self.assertEqual(confirmation['confirmed_document_ids'], [doc['document_id']])
        self.assertFalse(confirmation['actual_text_known'])
        self.assertTrue(confirmation['warnings'])

    def test_document_added_after_draft_can_be_recorded_with_reason(self):
        message, result = self.offer(self.a)
        draft = self.run_cli('draft', {'chat_id': self.c, 'date': DAY})
        card = self.root / 'later.pdf'
        card.write_bytes(b'later card')
        doc = self.run_cli('document', {'message_id': message['message_id'],
                                       'company_id': result['company_ids'][0], 'path': str(card)})
        payload = {'block_id': draft['block_id'], 'term_ids': result['term_ids'],
                   'document_ids': [doc['document_id']]}
        self.run_cli('confirm', payload, ok=False)
        self.run_cli('confirm', {**payload, 'stale_send_confirmed': True}, ok=False)
        reason = 'Карточка прикреплена человеком после подготовки'
        confirmation = self.run_cli('confirm', {**payload, 'stale_send_confirmed': True,
                                                 'extra_document_reason': reason})
        self.assertEqual(confirmation['confirmed_document_ids'], [doc['document_id']])
        self.assertTrue(confirmation['warnings'])
        history = self.run_cli('history')
        event = next(x for x in history['history'] if x['action'] == 'confirm_stale_send')
        details = json.loads(event['data'])
        self.assertEqual(details['extra_document_reason'], reason)
        self.assertEqual(details['extra_document_ids'], [doc['document_id']])

    def test_draft_renders_known_conditions_in_russian(self):
        self.offer(self.a, terms=[self.term(duration=2, duration_unit='day', vat='с НДС',
                                            color='green', amount='500000', minimum='100000',
                                            currency='RUB', conditions='Без предоплаты',
                                            restrictions='Только РФ')])
        text = self.run_cli('draft', {'chat_id': self.c, 'date': DAY})['text']
        for value in ('Test Company', '7733446222', 'строительные материалы', '15.5', '2',
                      'НДС', '500000', '100000', 'Без предоплаты', 'Только РФ'):
            self.assertIn(value, text)
        self.assertNotIn('day', text)
        self.assertNotIn('green', text)
        self.assertNotIn('Source A', text)

    def test_batch_decisions_are_explicit_atomic_and_keep_identity_policy(self):
        _, first = self.offer(self.a, company={'name': 'Первая', 'inn': '7733446222',
                                               'inn_status': 'unverified'})
        _, second = self.offer(self.a, company={'name': 'Вторая', 'inn': '7707083893',
                                                'inn_status': 'unverified'})
        decisions = [{'action': 'company_identity', 'company_id': first['company_ids'][0],
                      'inn': '7733446222', 'inn_status': 'verified', 'reason': 'Сверено',
                      'decision_confirmed': True},
                     {'action': 'company_identity', 'company_id': second['company_ids'][0],
                      'inn': '7707083893', 'inn_status': 'verified', 'reason': 'Сверено',
                      'decision_confirmed': True}]
        self.run_cli('update', {'action': 'batch', 'decisions': [decisions[0],
                      {**decisions[1], 'decision_confirmed': False}]}, ok=False)
        with sqlite3.connect(f'file:{self.db}?mode=ro', uri=True) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM companies WHERE inn_status='verified'").fetchone()[0], 0)
        self.run_cli('update', {'action': 'batch', 'decisions': decisions})
        with sqlite3.connect(f'file:{self.db}?mode=ro', uri=True) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM companies WHERE inn_status='verified'").fetchone()[0], 2)
        # The grouped interface must not make a recipient's own offer eligible.
        tid = first['term_ids'][0]
        self.run_cli('update', {'action': 'batch', 'decisions': [
            {'action': 'risk_decision', 'chat_id': self.a, 'date': DAY, 'term_id': tid,
             'reason': 'Попытка обойти исключение', 'decision_confirmed': True}]}, ok=False)
        item = next(x for x in self.run_cli('plan', {'chat_id': self.a, 'date': DAY})['items']
                    if x['term_id'] == tid)
        self.assertEqual(item['reason'], 'recipient_source_today')

    def test_status_and_list_expose_registry_and_confirmation(self):
        _, result = self.offer(self.a)
        block = self.run_cli('draft', {'chat_id': self.c, 'date': DAY})
        self.run_cli('confirm', {'block_id': block['block_id'], 'term_ids': result['term_ids']})
        status = self.run_cli('status')
        listing = self.run_cli('list')
        for key in ('chats', 'companies', 'blocks', 'sends', 'coverage'):
            self.assertIsInstance(status[key], list)
            self.assertEqual(status[key], listing[key])
        self.assertIn(block['block_id'], [x['id'] for x in status['blocks']])
        self.assertIn(result['term_ids'][0], [x['term_id'] for x in status['sends']])
        self.offer(self.b, company={'name': 'Непроверенная', 'inn': None, 'inn_status': 'missing'})
        review = self.run_cli('review')
        summary = self.run_cli('status')['open_reviews']
        self.assertEqual(summary['items'], len(review['items']))
        self.assertEqual(summary['documents'], len(review['documents']))
        self.assertEqual(summary['total'], len(review['items']) + len(review['documents']))

    def test_confirm_records_only_selected_documents_and_actual_text_snapshot(self):
        message, result = self.offer(self.a)
        card1, card2 = self.root / 'card1.pdf', self.root / 'card2.pdf'
        card1.write_bytes(b'card one')
        card2.write_bytes(b'card two')
        documents = [self.run_cli('document', {'message_id': message['message_id'],
                                                'company_id': result['company_ids'][0],
                                                'path': str(path)})['document_id']
                     for path in (card1, card2)]
        draft = self.run_cli('draft', {'chat_id': self.c, 'date': DAY})
        actual = 'Изменённый текст, действительно отправленный получателю'
        confirmation = self.run_cli('confirm', {'block_id': draft['block_id'],
                                                 'term_ids': result['term_ids'],
                                                 'document_ids': documents[:1],
                                                 'actual_text': actual})
        self.assertTrue(confirmation['actual_text_known'])
        with sqlite3.connect(f'file:{self.db}?mode=ro', uri=True) as db:
            rows = db.execute('SELECT term_id, document_id, actual_text, actual_hash FROM sends').fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual({r[1] for r in rows if r[1] is not None}, {documents[0]})
        self.assertTrue(all(r[2] == actual and r[3] == hashlib.sha256(actual.encode()).hexdigest()
                            for r in rows))

    def test_backup_contains_database_originals_and_verified_manifest(self):
        message, result = self.offer(self.a)
        attachment = self.root / 'source-card.pdf'
        attachment.write_bytes(b'%PDF test original\n')
        document = self.run_cli('document', {'message_id': message['message_id'],
                                              'company_id': result['company_ids'][0],
                                              'path': str(attachment)})
        self.assertEqual(document['status'], 'unverified')
        backup = self.run_cli('backup', {'directory': 'backup-test'})
        directory = self.root / backup['path']
        manifest = json.loads((directory / 'manifest.json').read_text(encoding='utf-8'))
        self.assertEqual(hashlib.sha256((directory / 'registry.sqlite').read_bytes()).hexdigest(),
                         manifest['database_sha256'])
        entries = {item['path']: item['sha256'] for item in manifest['files']}
        for path in (message['path'], document['path']):
            content = (directory / 'files' / path).read_bytes()
            self.assertEqual(hashlib.sha256(content).hexdigest(), entries[path])
        with sqlite3.connect(f'file:{directory / "registry.sqlite"}?mode=ro', uri=True) as db:
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            self.assertEqual(db.execute('SELECT COUNT(*) FROM documents').fetchone()[0], 1)
        restored = self.run_cli('restore', {'backup_directory': backup['path'],
                                             'destination': 'restored'})
        restored_root = self.root / restored['path']
        proc = subprocess.run([sys.executable, str(CLI), '--root', str(restored_root),
                               '--db', 'registry.sqlite', 'verify'], input='{}', text=True,
                              capture_output=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(json.loads(proc.stdout)['valid'])
        self.assertEqual((restored_root / document['path']).read_bytes(), attachment.read_bytes())
        (directory / 'files' / document['path']).write_bytes(b'tampered backup')
        self.run_cli('restore', {'backup_directory': backup['path'],
                                 'destination': 'bad-restore'}, ok=False)
        self.assertFalse((self.root / 'bad-restore').exists())
        (self.root / document['path']).write_bytes(b'changed')
        self.assertFalse(self.run_cli('verify')['valid'])
        self.run_cli('backup', {'directory': 'invalid-backup'}, ok=False)

    def test_field_provenance_checks_quotes_and_original_integrity(self):
        message = self.ingest(self.a, 'Условия: ставка 15,5%; срок два дня. Служебная цитата.')
        term = self.term(field_provenance={'rate': {'message_id': message['message_id'],
                                                   'quote': 'ставка 15,5%'},
                                           'duration': {'message_id': message['message_id'],
                                                        'quote': 'срок два дня'}})
        payload = {'message_id': message['message_id'], 'offers': [{
            'company': {'name': 'Test Company', 'inn': '7733446222', 'inn_status': 'verified'},
            'terms': [term]}]}
        bad = json.loads(json.dumps(payload))
        bad['offers'][0]['terms'][0]['field_provenance']['rate']['quote'] = 'ставка 99%'
        self.run_cli('import', bad, ok=False)
        bad['offers'][0]['terms'][0]['field_provenance']['rate']['quote'] = ''
        self.run_cli('import', bad, ok=False)
        bad['offers'][0]['terms'][0]['field_provenance']['rate'] = {'message_id': 999999, 'quote': 'ставка 15,5%'}
        self.run_cli('import', bad, ok=False)
        result = self.run_cli('import', payload)
        item = next(x for x in self.run_cli('search', {'date': DAY})['items']
                    if x['term_id'] == result['term_ids'][0])
        self.assertEqual(item['terms']['field_provenance']['rate']['quote'], 'ставка 15,5%')
        draft = self.run_cli('draft', {'chat_id': self.c, 'date': DAY})
        self.assertIn(result['term_ids'][0], [x['term_id'] for x in draft['items']])
        self.assertNotIn('ставка 15,5%', draft['text'])
        self.assertNotIn('field_provenance', draft['text'])
        (self.root / message['path']).write_text('Исходник изменён', encoding='utf-8')
        self.assertTrue(self.run_cli('check-draft', {'block_id': draft['block_id']})['stale'])
        plan = self.run_cli('plan', {'chat_id': self.c, 'date': DAY})
        item = next(x for x in plan['items'] if x['term_id'] == result['term_ids'][0])
        self.assertIn('invalid_field_provenance_review', item['reasons'])
        search_item = next(x for x in self.run_cli('search', {'date': DAY})['items']
                           if x['term_id'] == result['term_ids'][0])
        self.assertEqual(search_item['match'], 'possible')
        self.assertIn('invalid_field_provenance_review', search_item['unknown'])
        review_item = next(x for x in self.run_cli('review')['items'] if x['term_id'] == result['term_ids'][0])
        self.assertIn('invalid_field_provenance_review', review_item['reasons'])
        other = self.ingest(self.b, 'Реальная ставка 17%')
        (self.root / other['path']).write_text('Подменённый исходник', encoding='utf-8')
        invalid = {'message_id': other['message_id'], 'offers': [{
            'company': {'name': 'Другая', 'inn': '7707083893', 'inn_status': 'verified'},
            'terms': [self.term(rate='17', field_provenance={'rate': {
                'message_id': other['message_id'], 'quote': 'Реальная ставка 17%'}})]}]}
        self.run_cli('import', invalid, ok=False)

    def test_reply_classification_preserves_mixed_negotiation_without_offers(self):
        _, original = self.offer(self.a)
        block = self.run_cli('draft', {'chat_id': self.c, 'date': DAY})
        reply = self.ingest(self.c, 'Можем 14%; отдельно новая компания', reply_block_id=block['block_id'])
        new_company = {'name': 'Другая компания', 'inn': '7707083893', 'inn_status': 'verified'}
        offer = {'company': new_company, 'terms': [self.term(rate='16')]}
        self.run_cli('import', {'message_id': reply['message_id'], 'offers': [offer]}, ok=False)
        self.run_cli('import', {'message_id': reply['message_id'], 'offers': [{**offer, 'kind': 'counteroffer'}]}, ok=False)
        request = {'text': 'Можем 14%', 'kind': 'counteroffer',
                   'term_id': original['term_ids'][0], 'block_id': block['block_id']}
        self.run_cli('import', {'message_id': reply['message_id'], 'requests': [{**request, 'term_id': 999999}]}, ok=False)
        self.run_cli('import', {'message_id': reply['message_id'], 'requests': [{**request, 'block_id': 999999}]}, ok=False)
        result = self.run_cli('import', {'message_id': reply['message_id'],
            'offers': [{**offer, 'kind': 'offer'}], 'requests': [request,
                {'text': 'Обсуждаем сроки', 'kind': 'negotiation', 'block_id': block['block_id']}]})
        self.assertEqual(len(result['term_ids']), 1)
        self.assertEqual(len(result['request_ids']), 2)
        items = self.run_cli('search', {'date': DAY})['items']
        self.assertEqual({x['term_id'] for x in items}, set(original['term_ids'] + result['term_ids']))
        self.assertNotIn('14', [x['terms']['rate'] for x in items])

    def test_cli_help_is_readable_without_input_or_database(self):
        db = self.root / 'help-only.sqlite'
        proc = subprocess.run([sys.executable, str(CLI), '--root', str(self.root), '--db', str(db), '--help'],
                              input='', text=True, capture_output=True, timeout=10)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for word in ('import', 'draft', 'review', 'confirm'):
            self.assertIn(word, proc.stdout)
        self.assertFalse(db.exists())

    def test_negotiation_risk_follows_replaced_term_with_explicit_links(self):
        _, original = self.offer(self.a)
        block = self.run_cli('draft', {'chat_id': self.c, 'date': DAY})
        request = self.ingest(self.c, 'Обсудим цену')  # no reply_block_id
        self.run_cli('import', {'message_id': request['message_id'], 'requests': [
            {'text': 'Обсудим цену', 'kind': 'negotiation', 'block_id': block['block_id']}]})
        updated = self.run_cli('update', {'action': 'replace', 'term_id': original['term_ids'][0],
                                          'reason': 'Новые условия источника',
                                          'terms': self.term(rate='17')})['new_term_id']
        item = next(x for x in self.run_cli('plan', {'chat_id': self.c, 'date': DAY})['items']
                    if x['term_id'] == updated)
        self.assertIn('negotiation_review', item['reasons'])
        self.assertNotIn(updated, [x['term_id'] for x in self.run_cli('draft',
                         {'chat_id': self.c, 'date': DAY})['items']])

    def test_ambiguous_request_reclassification_preserves_audit_and_validates_links(self):
        _, original = self.offer(self.a)
        block = self.run_cli('draft', {'chat_id': self.c, 'date': DAY})
        msg = self.ingest(self.c, 'Сможем обсудить?', reply_block_id=block['block_id'])
        result = self.run_cli('import', {'message_id': msg['message_id'], 'requests': [
            {'text': 'Сможем обсудить?', 'kind': 'ambiguous'}]})
        rid = result['request_ids'][0]
        self.assertIn(rid, [x['request_id'] for x in self.run_cli('review')['requests']])
        decision = {'action': 'request_classification', 'request_id': rid, 'kind': 'negotiation',
                    'term_id': original['term_ids'][0], 'block_id': block['block_id'],
                    'reason': 'Уточнено содержание ответа'}
        self.run_cli('update', {**decision, 'term_id': 999999}, ok=False)
        self.run_cli('update', {**decision, 'block_id': 999999}, ok=False)
        self.assertIn(rid, [x['request_id'] for x in self.run_cli('review')['requests']])
        self.run_cli('update', decision)
        self.assertNotIn(rid, [x['request_id'] for x in self.run_cli('review')['requests']])
        history = self.run_cli('history')
        entry = next(x for x in history['history'] if x['entity'] == 'request'
                     and x['entity_id'] == rid and x['action'] == 'request_classification')
        data = json.loads(entry['data'])
        self.assertEqual(data['before']['kind'], 'ambiguous')
        self.assertEqual(data['after']['kind'], 'negotiation')
        self.assertEqual(data['reason'], decision['reason'])
        self.assertEqual(len(self.run_cli('search', {'date': DAY})['items']), 1)
        item = next(x for x in self.run_cli('plan', {'chat_id': self.c, 'date': DAY})['items']
                    if x['term_id'] == original['term_ids'][0])
        self.assertIn('negotiation_review', item['reasons'])

    def test_export_has_readable_conditions_unknowns_and_sent_snapshots(self):
        _, original = self.offer(self.a, terms=[self.term(vat=None, color=None, conditions='Только по договору')])
        block = self.run_cli('draft', {'chat_id': self.c, 'date': DAY})
        sent_text = 'Фактический текст: изменено человеком'
        self.run_cli('confirm', {'block_id': block['block_id'], 'term_ids': original['term_ids'],
                                 'actual_text': sent_text})
        self.offer(self.b, company={'name': 'Дополнительная', 'inn': '7707083893', 'inn_status': 'verified'},
                   terms=[self.term(category='услуги', extra_condition='Нестандартное условие')])
        report = self.run_cli('export', {'date': DAY, 'directory': 'reports'})
        contents = {key: (self.root / report[key]).read_text(encoding='utf-8')
                    for key in ('path', 'index', 'requests', 'plans', 'sends')}
        self.assertIn('Только по договору', contents['path'])
        self.assertIn('Нестандартное условие', contents['path'])
        self.assertIn('неизвестно', contents['path'].lower())
        self.assertIn('Ставка', contents['path'])
        self.assertIn('дн', contents['path'].lower())
        self.assertNotIn('"category"', contents['path'])
        self.assertIn(block['text'], contents['plans'])
        self.assertIn(sent_text, contents['sends'])


class PilotMigration(unittest.TestCase):
    def test_pilot_counts_original_provenance_and_review(self):
        with tempfile.TemporaryDirectory() as temporary:
            dest = Path(temporary) / 'pilot'
            proc = subprocess.run([sys.executable, str(PROJECT / 'миграция/пилот.py'), str(dest)],
                                  text=True, capture_output=True, timeout=120)
            self.assertEqual(proc.returncode, 0, (proc.stdout, proc.stderr))
            result = json.loads(proc.stdout)
            for key, expected in {'companies': 26, 'chats': 2, 'documents': 1,
                                  'requests': 1, 'terms': 52}.items():
                self.assertEqual(result[key], expected, key)
            data = json.loads((PROJECT / 'миграция/2026-11-17.json').read_text(encoding='utf-8'))
            with sqlite3.connect(f'file:{dest / "registry.sqlite"}?mode=ro', uri=True) as db:
                for table, expected in {'companies': 26, 'chats': 2, 'documents': 1,
                                        'requests': 1, 'terms': 52}.items():
                    self.assertEqual(db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0], expected, table)
                uni = db.execute('SELECT t.id,t.data FROM terms t JOIN offers o ON o.id=t.offer_id '
                                 'JOIN companies c ON c.id=o.company_id WHERE c.inn=?',
                                 ('5028037514',)).fetchall()
                six = [(tid, json.loads(raw)) for tid, raw in uni if json.loads(raw).get('rate') == '6']
                self.assertEqual(len(six), 1)
                self.assertTrue(six[0][1]['review'])
                self.assertFalse(six[0][1]['scope_confirmed'])
                messages = db.execute('SELECT source_file,source_heading,source_copy_path,source_copy_sha256 '
                                      'FROM messages ORDER BY id').fetchall()
                self.assertEqual(len(messages), len(data['batches']))
                for row, batch in zip(messages, data['batches']):
                    source, heading, copied, digest = row
                    self.assertEqual(Path(source), PROJECT / batch['source_file'])
                    self.assertEqual(heading, batch['source_heading'])
                    original = (PROJECT / batch['source_file']).read_bytes()
                    self.assertEqual((dest / copied).read_bytes(), original)
                    self.assertEqual(digest, hashlib.sha256(original).hexdigest())
            review = subprocess.run([sys.executable, str(CLI), '--root', str(dest), '--db',
                                     'registry.sqlite', 'review'], input='{}', text=True,
                                    capture_output=True)
            self.assertEqual(review.returncode, 0, review.stderr)
            item = next(x for x in json.loads(review.stdout)['items'] if x['term_id'] == six[0][0])
            self.assertIn('terms_review', item['reasons'])


if __name__ == '__main__':
    unittest.main()
