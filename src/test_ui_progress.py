import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parent))
import threading
import unittest
from zivpn_ui import style_keyboard
from zivpn_progress import Diagnostics

class UITests(unittest.TestCase):
    def test_colors_and_disabled_action_schema(self):
        original={'inline_keyboard':[[{'text':'Restart','callback_data':'menu_restart'},{'text':'P2P','callback_data':'p2p_on'},{'text':'Apps','callback_data':'menu_apps'}]]}
        decorated=style_keyboard(original,False)
        row=decorated['inline_keyboard'][0]
        self.assertEqual(row[0]['style'],'danger');self.assertEqual(row[1]['style'],'success')
        self.assertEqual(row[2]['disabled'],{});self.assertNotIn('callback_data',row[2])
        self.assertIn('callback_data',original['inline_keyboard'][0][2])
    def test_running_diagnostic_disabled_but_primary_apps_enabled(self):
        keyboard={'inline_keyboard':[[{'text':'D','callback_data':'menu_diagnostic'},{'text':'Apps','callback_data':'menu_apps'}]]}
        buttons=style_keyboard(keyboard,True,True)['inline_keyboard'][0]
        self.assertIn('disabled',buttons[0]);self.assertIn('callback_data',buttons[1])

class ProgressTests(unittest.TestCase):
    def test_nonblocking_cancel_owner_and_no_remaining_steps(self):
        started=threading.Event();done=threading.Event();calls=[];reports=[];second=[]
        def provider(cancel):started.set();cancel.wait(2);return 'partial'
        def send(report,**kw):reports.append(report);done.set()
        engine=Diagnostics(lambda m,p:calls.append((m,p)) or {'ok':True},send,[('First',provider),('Second',lambda c:second.append(1))],lambda u:u in (7,8))
        self.assertIsNone(engine.start(7,7,'private'));self.assertTrue(started.wait(1))
        self.assertTrue(engine.running(7));self.assertIsNotNone(engine.start(7,7,'private'))
        self.assertFalse(engine.cancel(7,8));self.assertTrue(engine.cancel(7,7));self.assertTrue(done.wait(2))
        self.assertEqual(second,[]);self.assertIn('arrêté',reports[0]);self.assertTrue(hasattr(reports[0],'rich_message'))
        self.assertEqual(calls[0][0],'sendMessageDraft');self.assertTrue(calls[0][1]['can_stop'])
    def test_group_and_unauthorized_never_start(self):
        calls=[];engine=Diagnostics(lambda *a:calls.append(a),lambda *a,**k:None,[],lambda u:u==7)
        self.assertIsNotNone(engine.start(-1,7,'group'));self.assertIsNotNone(engine.start(8,8,'private'));self.assertEqual(calls,[])
    def test_stopped_update_matches_private_draft(self):
        engine=Diagnostics(None,None,[],lambda u:True);cancel=threading.Event();engine.jobs[7]={'id':12,'user':7,'cancel':cancel}
        self.assertFalse(engine.stopped({'chat':{'id':7,'type':'private'},'draft_id':11}))
        self.assertFalse(cancel.is_set());self.assertTrue(engine.stopped({'chat':{'id':7,'type':'private'},'draft_id':12}));self.assertTrue(cancel.is_set())
    def test_fallback_progress_edited_and_final_saved(self):
        calls=[];done=threading.Event();reports=[]
        def api(m,p):
            calls.append((m,p))
            if m=='sendMessageDraft':return {'ok':False,'error_code':400}
            return {'ok':True,'result':{'message_id':42}}
        def send(r,**k):reports.append(r);done.set()
        engine=Diagnostics(api,send,[('A',lambda c:'a'),('B',lambda c:'b')],lambda u:True)
        engine.start(7,7,'private');self.assertTrue(done.wait(2))
        self.assertEqual(sum(m=='sendMessage' for m,p in calls),1)
        self.assertTrue(any(m=='editMessageText' and p.get('message_id')==42 for m,p in calls))
        self.assertIn('terminé',reports[0]);self.assertIn('a',reports[0]);self.assertIn('b',reports[0])
    def test_revoked_authorization_stops_without_report(self):
        entered=threading.Event();release=threading.Event();allowed=[True];sent=[]
        def provider(c):entered.set();release.wait(1);return 'sensitive'
        engine=Diagnostics(lambda *a:{'ok':True},lambda *a,**k:sent.append(a),[('A',provider)],lambda u:allowed[0])
        engine.start(7,7,'private');self.assertTrue(entered.wait(1));allowed[0]=False;release.set()
        import time
        deadline=time.monotonic()+1
        while engine.running(7) and time.monotonic()<deadline:time.sleep(.01)
        self.assertEqual(sent,[])
if __name__=='__main__':unittest.main()
