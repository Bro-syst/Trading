#!/usr/bin/env python3
"""Create an isolated, wholly fictional v2 instance. Never overwrites a directory."""
import argparse, sqlite3, tempfile
from pathlib import Path
import trading as t
import web_core as core

def inn(i):
    digits=[1,0,0,0,0,0,0,0,i]; digits.append(sum(a*b for a,b in zip(digits,[2,4,10,3,5,9,4,6,8]))%11%10); return ''.join(map(str,digits))

def create(dest, extra_markup_review=False):
    with tempfile.TemporaryDirectory(prefix='trading-fiction-') as tmp:
        root=Path(tmp).resolve(); db=sqlite3.connect(root/'registry.sqlite'); db.row_factory=sqlite3.Row; db.execute('PRAGMA foreign_keys=ON'); db.executescript(t.SCHEMA)
        with db:
            a=t.execute(db,root,'chat',{'name':'ТЕСТ Источник A'})['chat_id']; b=t.execute(db,root,'chat',{'name':'ТЕСТ Получатель B'})['chat_id']
            terms=[]
            for i in range(1,6 if extra_markup_review else 5):
                raw=f'ТЕСТ вымышленное сообщение {i}: компания ТЕСТ {i}, цена {10+i}%, запчасти, срок 2 дня.'
                mid=t.execute(db,root,'ingest',{'chat_id':a,'date':'2026-11-17','text':raw})['message_id']
                term={'category':'запчасти','rate':str(10+i),'rate_unit':'%','rate_text':f'цена {10+i}%','duration':2,'duration_unit':'day','duration_text':'2 дня','scope_confirmed':True,'review':False}
                status='unverified' if i==4 else 'verified'
                result=t.execute(db,root,'import',{'message_id':mid,'offers':[{'company':{'name':f'ТЕСТ Компания {i}','inn':inn(i),'inn_status':status},'terms':[term]}]}); terms.append((mid,result['company_ids'][0],result['term_ids'][0]))
            # X is offered by A and B; both recipients must exclude it as their own.
            shared=None
            for source in (a,b):
                raw='ТЕСТ общая компания X, запчасти, цена 16%, два дня.'
                mid=t.execute(db,root,'ingest',{'chat_id':source,'date':'2026-11-17','text':raw})['message_id']
                offer={'terms':[{'category':'запчасти','rate':'16','rate_unit':'%','duration':2,'duration_unit':'day','duration_text':'2 дня','scope_confirmed':True,'review':False}]}
                if shared is None: offer['company']={'name':'ТЕСТ Компания X','inn':inn(8),'inn_status':'verified'}
                else: offer['company_id']=shared
                imported=t.execute(db,root,'import',{'message_id':mid,'offers':[offer]}); shared=imported['company_ids'][0]
            sample=root/'ТЕСТ-карточка.txt'; sample.write_text('ТЕСТ. Вымышленная карточка, не использовать для реальных передач.\n')
            did=t.execute(db,root,'document',{'message_id':terms[0][0],'company_id':terms[0][1],'path':str(sample)})['document_id']
            t.execute(db,root,'update',{'action':'document_review','document_id':did,'status':'verified','document_inn':inn(1),'reason':'ТЕСТ: подтверждена связь вымышленного файла','confirmation_kind':'user','confirmation_source':'ТЕСТ: слова пользователя'})
            unverified=root/'ТЕСТ-карточка-непроверенная.txt'; unverified.write_text('ТЕСТ. Вымышленная непроверенная карточка.\n')
            t.execute(db,root,'document',{'message_id':terms[1][0],'company_id':terms[1][1],'path':str(unverified)})
        db.close(); core.migrate_copy(root,dest)
    db=core.connect(dest)
    try:
        db.execute('BEGIN IMMEDIATE'); t.execute(db,dest,'recipient',{'chat_id':a,'enabled':True,'reason':'ТЕСТ: вымышленный получатель'}); t.execute(db,dest,'recipient',{'chat_id':b,'enabled':True,'reason':'ТЕСТ: вымышленный получатель'})
        for i,(_,cid,tid) in enumerate(terms,1):
            t.execute(db,dest,'company-vat',{'company_id':cid,'date':'2026-11-17','value':'22%','source':'ТЕСТ вымышленный источник','status':'user_confirmed'})
            if i!=5: t.execute(db,dest,'pricing',{'term_id':tid,'markup':'0','reason':'ТЕСТ: без наценки','source':'ТЕСТ: вымышленное указание пользователя'})
        core.sync_tasks(db,b,'2026-11-17'); db.commit()
    finally: db.close()
    return {'recipient_id':b,'date':'2026-11-17','note':'Все данные вымышленные, только для локального теста.'}

if __name__=='__main__':
    parser=argparse.ArgumentParser(); parser.add_argument('destination'); parser.add_argument('--extra-markup-review',action='store_true'); args=parser.parse_args(); print(t.canonical(create(Path(args.destination).expanduser().resolve(),args.extra_markup_review)))
