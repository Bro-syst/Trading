"""Independent checks of search and validation after correcting an offer type."""
import unittest
import test_invoice_independent as fixtures


class TypeReclassification(unittest.TestCase):
    setUp = fixtures.InvoiceIndependent.setUp
    cli = fixtures.InvoiceIndependent.cli
    snapshot = fixtures.InvoiceIndependent.snapshot
    message = fixtures.InvoiceIndependent.message
    company_offer = fixtures.InvoiceIndependent.company_offer
    invoice = fixtures.InvoiceIndependent.invoice

    def search_item(self, term_id):
        items = self.cli('search', {'date': fixtures.DAY})['items']
        return next(item for item in items if item['term_id'] == term_id)

    def test_open_document_issue_remains_visible_in_search(self):
        mid, invoice_id, term_id = self.invoice(self.a, 'SEARCH-TYPE')
        issue = self.cli('document', {'message_id': mid, 'company_id': self.cid,
                         'path': str(self.root / 'missing.pdf'), 'classification': 'invoice',
                         'invoice_id': invoice_id})['document_issue_id']
        self.cli('invoice-offer-to-company', {'term_id': term_id, 'reason': 'correct type', 'source': 'user'})
        item = self.search_item(term_id)
        self.assertIn('reclassified_invoice_document_review', item['unknown'])
        self.assertEqual(item['match'], 'possible')
        supplied = self.root / 'supplied.pdf'
        supplied.write_bytes(b'test document')
        self.cli('document-issue-resolve', {'issue_id': issue, 'path': str(supplied), 'reason': 'file supplied'})
        self.assertNotIn('reclassified_invoice_document_review', self.search_item(term_id)['unknown'])

    def test_excerpt_must_belong_to_source_and_failure_is_atomic(self):
        _, invoice_id, term_id = self.invoice(self.a, 'EXCERPT')
        self.cli('invoice-offer-to-company', {'term_id': term_id, 'reason': 'correct type',
                 'source': 'user', 'source_condition': 'invented excerpt'}, ok=False)
        item = self.search_item(term_id)
        self.assertEqual(item['offer_type'], 'invoice')
        self.assertEqual(item['invoice']['id'], invoice_id)
