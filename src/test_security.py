import ast
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).parent))
p=Path(__file__).with_name('zivpn_security.py')
spec=importlib.util.spec_from_file_location('security_under_test',p);security=importlib.util.module_from_spec(spec);spec.loader.exec_module(security)

class SecurityTests(unittest.TestCase):
    def test_domain_suffix_not_lookalike(self):
        self.assertEqual(security.service('www.youtube.com'),'YouTube probable')
        self.assertIsNone(security.service('youtube.com.evil.example'))
        self.assertIsNone(security.service('notyoutube.com'))
        self.assertIsNone(security.service('192.0.2.1'))
    def test_account_filter_and_encryption_limit(self):
        records=[dict(email='fixture-user',kind='dns',protocol='udp',host='youtube.com',port='53',hits=1,last_ms=1000),dict(email='alice',kind='destination',protocol='tcp',host='private.example',port='443',hits=2,last_ms=1000)]
        with patch.object(security,'request',return_value={'records':records,'observed_ms':1000}),patch.object(security,'snapshot',return_value={'sessions':{}}):
            report=security.build_security_text('securite','michel')
        self.assertNotIn('private.example',report)
        self.assertIn('non accessibles',report)
        self.assertIn('confiance moyenne',report)
    def command(self,user,chat,kind,cmd):
        tree=ast.parse(Path(__file__).with_name('zivpn-xui-sync.py').read_text())
        fn=next(x for x in tree.body if isinstance(x,ast.FunctionDef) and x.name=='handle_telegram_command')
        sent=[];module=types.ModuleType('zivpn_security');module.build_security_text=lambda *a:'sensitive-report'
        g={'PRIMARY_ADMIN_ID':7,'get_admin_ids':lambda:[7,8],'send_telegram':lambda txt,**kw:sent.append(txt)}
        exec(compile(ast.Module(body=[fn],type_ignores=[]),'commands','exec'),g)
        with patch.dict(sys.modules,{'zivpn_security':module}):g['handle_telegram_command'](chat,user,cmd,kind)
        return sent
    def test_commands_restricted_to_private_primary(self):
        for cmd in ('/securite','/apps','/dns','/destinations'):
            self.assertNotIn('sensitive-report',self.command(8,8,'private',cmd))
            self.assertNotIn('sensitive-report',self.command(7,-10,'group',cmd))
            self.assertEqual(self.command(7,7,'private',cmd),['sensitive-report'])
    def test_callback_restricted_before_report(self):
        tree=ast.parse(Path(__file__).with_name('zivpn-xui-sync.py').read_text())
        fn=next(x for x in tree.body if isinstance(x,ast.FunctionDef) and x.name=='handle_telegram_callback')
        calls=[];g={'PRIMARY_ADMIN_ID':7,'answer_callback':lambda *a:calls.append(a)}
        exec(compile(ast.Module(body=[fn],type_ignores=[]),'callback','exec'),g)
        g['handle_telegram_callback']({'id':'x','data':'menu_apps','from':{'id':7},'message':{'chat':{'id':-1,'type':'group'}}})
        self.assertEqual(len(calls),1)
        self.assertIn('réservé',calls[0][1])
if __name__=='__main__':unittest.main()
