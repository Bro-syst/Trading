"""Client comments are separately versioned from manually edited public text."""
from contextlib import closing
import unittest
import test_common_preview as fixture
import web_core as core

DAY=fixture.DAY

class ClientComment(fixture.CommonPreview):
    def selection(self,db,ids):
        result=self.selected(db,ids); items=self.items(db)
        result['comment_revisions']={str(i):items[i]['client_comment']['revision'] for i in ids}
        return result

    def comment(self,db,tid,text):
        x=self.items(db)[tid]
        return core.save_client_comment(db,self.root,tid,{'date':DAY,'version':x['version'],'revision':x['client_comment']['revision'],'text':text})

    def test_comment_lifecycle_and_conflicts(self):
        tid=self.old_tid
        with closing(core.connect(self.root)) as db:
            self.save(db,tid,'Ручная редакция')
            x=self.items(db)[tid]; request=self.selection(db,[tid]); old={'date':DAY,'version':x['version'],'revision':None,'text':'Комментарий клиента'}
            saved=core.save_client_comment(db,self.root,tid,old)
            with self.assertRaises(ValueError): core.save_client_comment(db,self.root,tid,old)
            with self.assertRaises(ValueError): core.common_preview(db,self.root,request)
            x=self.items(db)[tid]
            self.assertEqual(x['version'],old['version']); self.assertFalse(x['public_text']['stale'])
            self.assertEqual(x['public_text']['text'],'Ручная редакция')
            self.assertEqual(core.common_preview(db,self.root,self.selection(db,[tid]))['text'],'Ручная редакция\nКомментарий: Комментарий клиента')
            self.assertEqual(self.comment(db,tid,'Комментарий клиента')['revision'],saved['revision'])
            multiline='  первая строка\nвторая строка  '
            self.comment(db,tid,multiline)
            self.assertEqual(self.items(db)[tid]['client_comment']['text'],multiline)
            self.assertEqual(core.common_preview(db,self.root,self.selection(db,[tid]))['text'],'Ручная редакция\nКомментарий: '+multiline)
            self.comment(db,tid,'')
            self.assertEqual(core.common_preview(db,self.root,self.selection(db,[tid]))['text'],'Ручная редакция')
            self.assertEqual(db.execute("SELECT COUNT(*) FROM history WHERE entity='public_text' AND entity_id=?",(tid,)).fetchone()[0],1)

    def test_block_revision_and_race(self):
        tid=self.old_tid
        with closing(core.connect(self.root)) as db:
            self.save(db,tid,'Ручная редакция'); self.comment(db,tid,'Для клиента\nВторая строка')
        task=self.task(tid); block=self.prepare(self.b,[task])
        self.assertEqual(block['text'],'Ручная редакция\nКомментарий: Для клиента\nВторая строка')
        with closing(core.connect(self.root)) as db:
            self.assertFalse(core.block(db,self.root,block['id'])['stale'])
            self.comment(db,tid,'Новый')
            self.assertTrue(core.block(db,self.root,block['id'])['stale'])
            with self.assertRaises(ValueError): core.prepare(db,self.root,{'chat_id':self.b,'date':DAY,'task_ids':[task['id']],'versions':{str(task['id']):task['version']}})

    def test_existing_manual_comment_does_not_duplicate(self):
        with closing(core.connect(self.root)) as db:
            self.save(db,self.old_tid,'Текст\nКомментарий: Да\nещё строка'); self.comment(db,self.old_tid,'Да\nещё строка')
            self.assertEqual(core.common_preview(db,self.root,self.selection(db,[self.old_tid]))['text'],'Текст\nКомментарий: Да\nещё строка')
            self.comment(db,self.old_tid,'Да\nдругая строка')
            with self.assertRaises(ValueError): core.common_preview(db,self.root,self.selection(db,[self.old_tid]))
            self.comment(db,self.old_tid,'Да')
            with self.assertRaises(ValueError): core.common_preview(db,self.root,self.selection(db,[self.old_tid]))

    def test_comment_belongs_to_one_variant_and_rejects_old_data(self):
        _,_,other=self.invoice(self.a,'A-comment')
        with closing(core.connect(self.root)) as db:
            self.comment(db,self.old_tid,'Только первый')
            self.assertEqual(self.items(db)[other]['client_comment'],{'revision':None,'text':''})
            x=self.items(db)[other]
            request={'date':DAY,'version':x['version'],'revision':None,'text':'Второй'}
            db.execute("INSERT INTO pricing(term_id,markup,reason,recorded_at) VALUES(?,?,?,?)",(other,'2','проверка',DAY))
            with self.assertRaises(ValueError): core.save_client_comment(db,self.root,other,request)
