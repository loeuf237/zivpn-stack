import tempfile
import threading
import time
import unittest
from unittest.mock import Mock
from pathlib import Path
from zivpn_telegram import Transport, Tasks
from zivpn_sqlite import ClosingConnection

class TelegramTests(unittest.TestCase):
    def client(self, responses):
        session = Mock()
        session.post.side_effect = [Mock(status_code=200, json=lambda r=r:r) if isinstance(r,dict) else r for r in responses]
        sleeper = Mock()
        return Transport('https://invalid', lambda:session, sleeper), session, sleeper
    def test_uncertain_send_never_retried(self):
        c,s,_=self.client([TimeoutError()]);r=c.call('sendMessage',{'text':'secret'})
        self.assertTrue(r['delivery_uncertain']);self.assertEqual(s.post.call_count,1)
    def test_definitive_rate_limit_retried(self):
        c,s,sleep=self.client([{'ok':False,'error_code':429,'parameters':{'retry_after':2}},{'ok':True}])
        self.assertTrue(c.call('sendMessage',{})['ok']);self.assertEqual(s.post.call_count,2);sleep.assert_called_once_with(2.1)
    def test_long_rate_limit_not_blocked(self):
        c,s,sleep=self.client([{'ok':False,'error_code':429,'parameters':{'retry_after':60}}]);c.call('sendMessage',{})
        sleep.assert_not_called();self.assertEqual(s.post.call_count,1)
    def test_safe_edit_retries_and_unchanged_success(self):
        c,s,_=self.client([TimeoutError(),{'ok':False,'error_code':400,'description':'message is not modified'}])
        self.assertTrue(c.call('editMessageText',{})['ok']);self.assertEqual(s.post.call_count,2)
    def test_document_not_retried(self):
        c,s,_=self.client([{'ok':False,'error_code':429,'parameters':{'retry_after':1}}]);c.call('sendDocument',{},files={'document':object()})
        self.assertEqual(s.post.call_count,1)
    def test_malformed_result_is_uncertain(self):
        c,s,_=self.client([{'unexpected':'response'}]);self.assertTrue(c.call('sendMessage',{})['delivery_uncertain'])
    def test_rejection_reason_does_not_log_description(self):
        import contextlib, io
        c,s,_=self.client([{'ok':False,'error_code':400,'description':"Bad Request: can't parse entities PASSWORD-PRIVATE"}])
        stream=io.StringIO()
        with contextlib.redirect_stdout(stream): result=c.call('editMessageText',{})
        self.assertEqual(result['failure_reason'],'invalid_format')
        self.assertNotIn('PASSWORD-PRIVATE',stream.getvalue())
        self.assertEqual(c.snapshot()['editMessageText:invalid_format'],1)

    def test_server_error_send_uncertain_without_duplicate(self):
        c,s,_=self.client([{'ok':False,'error_code':502,'description':'Bad Gateway'}])
        self.assertTrue(c.call('sendRichMessage',{})['delivery_uncertain'])
        self.assertEqual(s.post.call_count,1)

    def test_tasks_parallel_order_and_dedup(self):
        with tempfile.TemporaryDirectory() as d:
            gate=threading.Event();other=threading.Event();done=threading.Event();seen=[]
            def handle(u):
                seen.append(u['update_id'])
                if u['update_id']==1: gate.wait(3)
                if u['update_id']==3: other.set()
                if u['update_id']==2: done.set()
            tasks=Tasks(str(Path(d)/'tasks.db'),handle,lambda *args:None)
            def update(i,chat):return {'update_id':i,'message':{'chat':{'id':chat},'text':'PASSWORD-MUST-NOT-PERSIST'}}
            self.assertTrue(tasks.submit(update(1,7)));self.assertTrue(tasks.submit(update(2,7)));self.assertTrue(tasks.submit(update(3,8)))
            self.assertTrue(other.wait(2));self.assertNotIn(2,seen)
            self.assertTrue(tasks.submit(update(1,7)));gate.set();self.assertTrue(done.wait(2));self.assertEqual(seen.count(1),1)
            deadline=time.monotonic()+2
            while 'terminée' not in tasks.status(7).splitlines()[0] and time.monotonic()<deadline: time.sleep(0.01)
            self.assertNotIn(b'PASSWORD-MUST-NOT-PERSIST',Path(d,'tasks.db').read_bytes())
    def test_restart_interrupted_no_replay(self):
        with tempfile.TemporaryDirectory() as d:
            path=str(Path(d)/'tasks.db');tasks=Tasks(path,Mock(),Mock(),workers=0)
            u={'update_id':10,'message':{'chat':{'id':7}}};tasks.submit(u)
            handler=Mock();restarted=Tasks(path,handler,Mock(),workers=0)
            self.assertIn('interrompue',restarted.status(7));self.assertTrue(restarted.submit(u));handler.assert_not_called()
    def test_consistent_backup_and_verified_delivery(self):
        import ast, sqlite3, os
        source=Path(__file__).with_name('zivpn-xui-sync.py').read_text()
        node=next(n for n in ast.parse(source).body if isinstance(n,ast.FunctionDef) and n.name=='send_backup_file')
        with tempfile.TemporaryDirectory() as d:
            path=str(Path(d)/'source.db');db=sqlite3.connect(path)
            db.execute('PRAGMA journal_mode=WAL');db.execute('CREATE TABLE sample(value TEXT)');db.execute("INSERT INTO sample VALUES ('committed-wal')");db.commit()
            transport=Mock();notice=Mock();checked=[]
            def call(method,payload,files):
                copied=sqlite3.connect(files['document'][1].name)
                checked.append(copied.execute('SELECT value FROM sample').fetchone()[0]);copied.close()
                return {'ok':False,'error_code':403}
            transport.call.side_effect=call
            g=dict(sqlite3=sqlite3,ClosingConnection=ClosingConnection,tempfile=tempfile,os=os,time=time,DB_PATH=path,TELEGRAM=transport,send_telegram=notice)
            exec(compile(ast.Module(body=[node],type_ignores=[]),'backup','exec'),g)
            self.assertFalse(g['send_backup_file'](7));self.assertEqual(checked,['committed-wal']);notice.assert_called_once();db.close()

    def test_capacity_no_silent_acceptance(self):
        with tempfile.TemporaryDirectory() as d:
            tasks=Tasks(str(Path(d)/'tasks.db'),Mock(),Mock(),workers=0,capacity=1)
            self.assertTrue(tasks.submit({'update_id':1}));self.assertFalse(tasks.submit({'update_id':2}))

if __name__=='__main__':unittest.main()
