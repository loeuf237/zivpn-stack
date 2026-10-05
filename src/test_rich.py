import ast
import importlib.util
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock
sys.path.insert(0,str(Path(__file__).parent))
import zivpn_rich as rich

class RichTests(unittest.TestCase):
    def transport(self,responses):
        source=Path(__file__).with_name('zivpn-xui-sync.py').read_text()
        names={'deliver_rich_report','send_telegram','edit_telegram_message'}
        nodes=[n for n in ast.parse(source).body if isinstance(n,ast.FunctionDef) and n.name in names]
        request=Mock();request.post.side_effect=[Mock(json=lambda r=r:r) if isinstance(r,dict) else r for r in responses]
        transport=Mock();transport.call.side_effect=lambda method,payload: request.post('https://test.invalid/'+method,json=payload).json()
        g={'requests':request,'TELEGRAM':transport,'API_BASE':'https://test.invalid','PRIMARY_ADMIN_ID':7}
        exec(compile(ast.Module(body=nodes,type_ignores=[]),'transport','exec'),g)
        return g,request
    def report(self):
        return rich.RichReport([rich.heading('Sessions'),rich.table(['Compte','Octets'],[['a_b*<x>', '123']]),rich.details('Détails',[rich.paragraph('Mesure UTC')]),rich.footer('Limites de mesure')])
    def test_native_send_and_keyboard(self):
        g,r=self.transport([{'ok':True}]);report=self.report();g['send_telegram'](report,7,{'inline_keyboard':[]})
        args=r.post.call_args;self.assertTrue(args.args[0].endswith('/sendRichMessage'))
        payload=args.kwargs['json'];self.assertEqual(payload['rich_message'],report.rich_message);self.assertNotIn('parse_mode',payload)
        self.assertIn('reply_markup',payload);json.dumps(payload)
    def test_definite_rejection_plain_fallback_identical_values(self):
        g,r=self.transport([{'ok':False,'error_code':400},{'ok':True}]);report=self.report();g['send_telegram'](report,7)
        self.assertEqual(r.post.call_count,2);payload=r.post.call_args.kwargs['json']
        self.assertTrue(r.post.call_args.args[0].endswith('/sendMessage'));self.assertNotIn('parse_mode',payload)
        self.assertIn('a_b*<x>',payload['text']);self.assertIn('123',payload['text']);self.assertIn('Limites',payload['text'])
    def test_timeout_and_rate_limit_do_not_duplicate(self):
        for response in (TimeoutError(),{'ok':False,'error_code':429}):
            g,r=self.transport([response]);g['send_telegram'](self.report(),7);self.assertEqual(r.post.call_count,1)
    def test_edit_and_unchanged(self):
        g,r=self.transport([{'ok':False,'error_code':400,'description':'Bad Request: message is not modified'}])
        result=g['edit_telegram_message'](self.report(),7,11)
        self.assertTrue(result['ok']);self.assertEqual(r.post.call_count,1)
        payload=r.post.call_args.kwargs['json'];self.assertEqual(payload['message_id'],11);self.assertIn('rich_message',payload);self.assertNotIn('text',payload)
    def test_explicit_classic_mode(self):
        g,r=self.transport([{'ok':True}]);g['send_telegram'](self.report().as_plain(),7)
        self.assertTrue(r.post.call_args.args[0].endswith('/sendMessage'));self.assertNotIn('parse_mode',r.post.call_args.kwargs['json'])
    def test_long_fallback_keeps_caveats_and_limit(self):
        report=rich.RichReport([rich.heading('Rapport'),rich.table(['Hôte'],[['host-'+str(i)+'x'*253] for i in range(100)]),rich.footer('Aucun déchiffrement. Conservation 30 minutes.')])
        self.assertLessEqual(len(report),3500);self.assertIn('Conservation 30 minutes',report);self.assertIn('abrégées',report)
    def test_session_count_not_people_and_sorted_table(self):
        live={str(i):dict(email='alice',ip='192.0.2.1',remote_port=i,port=5667,bytes=i) for i in range(15)}
        report=rich.sessions_report(live,str)
        table=next(b for b in report.rich_message['blocks'] if b['type']=='table')
        self.assertEqual(len(table['cells']),13);self.assertEqual(table['cells'][1][-1]['text'],'14');self.assertIn('15 tunnels',report);self.assertIn('personnes',report)
    def test_empty_consumption_and_raw_markup(self):
        report=rich.consumption_report([],0,None,0,str)
        self.assertIn('Aucun compte',report)
        self.assertEqual(self.report().rich_message['blocks'][1]['cells'][1][0]['text'],'a_b*<x>')
if __name__=='__main__':unittest.main()
