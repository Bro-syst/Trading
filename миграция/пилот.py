#!/usr/bin/env python3
"""Import the reviewed historical sample into a new isolated pilot workspace."""
import argparse
import json
from pathlib import Path
import subprocess
import sys

PROJECT = Path(__file__).resolve().parent.parent
DATA = PROJECT / 'миграция/2026-11-17.json'
CLI = PROJECT / 'trading.py'


def main():
    parser = argparse.ArgumentParser(description='Import example into a new isolated directory; originals are preserved.')
    parser.add_argument('destination', help='Path to a new or empty pilot directory')
    args = parser.parse_args()
    dest = Path(args.destination).expanduser().resolve()
    if dest == PROJECT or PROJECT in dest.parents or (dest.exists() and (not dest.is_dir() or any(dest.iterdir()))):
        parser.error('destination must be a new or empty directory outside the project')
    dest.mkdir(parents=True, exist_ok=True)
    data = json.loads(DATA.read_text(encoding='utf-8'))

    def run(command, payload):
        proc = subprocess.run([sys.executable, str(CLI), '--root', str(dest), '--db', 'registry.sqlite', command],
                              input=json.dumps(payload, ensure_ascii=False), text=True, capture_output=True)
        try:
            result = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f'{command}: invalid response: {proc.stdout} {proc.stderr}') from exc
        if proc.returncode or not result.get('ok'):
            raise RuntimeError(f'{command}: {result}; pilot remains in {dest} for inspection')
        return result

    run('init', {})
    chats = {}
    records = {}
    terms = requests = offers = 0
    for batch in data['batches']:
        name = batch['chat']
        if name not in chats:
            chats[name] = run('chat', {'name': name})['chat_id']
        payload = {'chat_id': chats[name], 'date': data['date'], 'time': batch['time'],
                   'text': batch['text'], 'source_file': str(PROJECT / batch['source_file']),
                   'source_heading': batch['source_heading']}
        message_id = run('ingest', payload)['message_id']
        result = run('import', {'message_id': message_id, 'offers': batch['offers'], 'requests': batch['requests']})
        offers += len(result['company_ids'])
        terms += len(result['term_ids'])
        requests += len(result['request_ids'])
        for offer, company_id in zip(batch['offers'], result['company_ids']):
            records[offer['company']['inn']] = (company_id, message_id)
    doc = data['document']
    cid, mid = records[doc['inn']]
    document = run('document', {'company_id': cid, 'message_id': mid, 'path': str(PROJECT / doc['source_path'])})
    verification = run('verify', {})
    if not verification['valid']:
        raise RuntimeError(f"verification failed: {verification['errors']}")
    result = {'pilot_root': str(dest), 'chats': len(chats), 'companies': len(records), 'offers': offers,
              'terms': terms, 'requests': requests, 'documents': 1, 'document_id': document['document_id'],
              'note': 'Исходники реконструированы из имеющихся файлов; полнота истории чатов не подтверждена.'}
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
