from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from publisher import cli, cli_login
from publisher.login_contract import ISSUER

P='a'*24
R='b'*24


class UsageCliTests(unittest.TestCase):
    def command(self,client,*args,status=0):
        out,err=io.StringIO(),io.StringIO()
        env={'POTETO_TOKEN':'test-secret','POTETO_TOKEN_FILE':'','PAAS_TOKEN':'','PAAS_TOKEN_FILE':'','POTETO_URL':''}
        with patch.dict(os.environ,env),patch.object(cli,'find_link',return_value=None),patch.object(cli,'Client',return_value=client), \
             patch('sys.argv',['poteto','--url','https://api.test',*args]),redirect_stdout(out),redirect_stderr(err):
            if status:
                with self.assertRaises(SystemExit) as error:cli.main()
                self.assertEqual(status,error.exception.code)
            else:cli.main()
        self.assertNotIn('test-secret',out.getvalue()+err.getvalue())
        return out.getvalue(),err.getvalue()

    def test_prune_is_read_only_by_default_and_apply_requires_exact_ids(self):
        result={'candidates':[{'id':R,'state':'ready','artifact_reserved_bytes':1024}], 'protected':[],
                'can_apply':False,'would_release':{'artifact_bytes':1024}}
        client=Mock();client.request.return_value=result
        out,_=self.command(client,'prune','--project',P)
        client.request.assert_called_once_with('GET',f'/v1/projects/{P}/previews/prune?keep=3&older_than_days=7')
        self.assertIn('Dry run: 1 previews',out);self.assertIn('--yes --ids '+R,out)
        for args in [('--yes',),('--ids',R),('--keep','-1'),('--yes','--ids','invalid'),('--no-wait',)]:
            client.reset_mock()
            self.command(client,'prune','--project',P,*args,status=2)
            client.request.assert_not_called()

    def test_apply_uses_existing_jobs_and_json_preserves_receipts(self):
        result={'project_id':P,'dry_run':False,'releases':[{'id':R,'state':'deleting','job_id':'job'}]}
        client=Mock();client.request.side_effect=lambda *a,**kw:deepcopy(result)
        out,_=self.command(client,'prune','--project',P,'--older-than-days','0','--keep','0','--yes','--ids',R,'--json')
        client.request.assert_called_once_with('POST',f'/v1/projects/{P}/previews/prune',{'keep':0,'older_than_days':0,'ids':[R]})
        self.assertEqual('removed',json.loads(out)['releases'][0]['state'])
        self.assertEqual('job',client.wait_job.call_args.args[0])
        client.reset_mock()
        out,_=self.command(client,'prune','--project',P,'--yes','--ids',R,'--no-wait','--json')
        client.wait_job.assert_not_called();self.assertEqual('deleting',json.loads(out)['releases'][0]['state'])

    def test_usage_json_and_human_output_are_reservations_not_live_metrics(self):
        result={'project_id':P,'project_name':'sample','restricted_to_previews':True,'releases':[],'projects':[],
                'artifact_storage':{'reserved_bytes':1024**2,'limit_bytes':512*1024**2},
                'account_runtime':{'reserved':{'memory_mib':256,'vcpus':0.5},'limit':{'memory_mib':4096,'vcpus':4}},
                'sandbox_runtime':{'reserved':{'memory_mib':256,'vcpus':0.5},'limit':{'memory_mib':2048,'vcpus':1.5}},
                'deployment_slots':{'used':1,'limit':2},'data_storage':'Persistent data remains separate.'}
        client=Mock();client.request.return_value=result
        out,_=self.command(client,'usage','--project',P,'--json')
        self.assertEqual(result,json.loads(out))
        out,_=self.command(client,'usage','--project',P)
        self.assertIn('1.00 MiB / 512.00 MiB',out)
        self.assertIn('not live CPU/RSS',out)
        self.assertIn('Sleeping apps keep their reservations',out)

    def test_login_management_is_opt_in_and_server_capability_is_checked(self):
        with tempfile.TemporaryDirectory() as folder:
            client=Mock(url='https://api.test')
            client.request.side_effect=[{'issuer':ISSUER,'client_id':'client','preview_management':True},
                                        {'token':'never-print','project_id':P}]
            with patch.object(cli_login,'authorize',return_value={'code':'one','verifier':'v','nonce':'n'}):
                output=cli_login.login(client,Path(folder)/'token.json',manage_previews=True)
            self.assertEqual({'code':'one','verifier':'v','nonce':'n','manage_previews':True},client.request.call_args.args[2])
            self.assertNotIn('never-print',str(output))
            client=Mock(url='https://api.test');client.request.return_value={'issuer':ISSUER,'client_id':'old'}
            with patch.object(cli_login,'authorize') as browser:
                with self.assertRaisesRegex(RuntimeError,'does not support'):
                    cli_login.login(client,Path(folder)/'other.json',manage_previews=True)
                browser.assert_not_called()


class PreviewLoginOptionTests(unittest.TestCase):
    def test_explicit_options_are_sent_only_after_capability_check(self):
        with tempfile.TemporaryDirectory() as directory:
            client = Mock(url='https://deploy.example.test')
            client.request.side_effect = [
                {'issuer':ISSUER,'client_id':'client','preview_environment':True,'web_egress':True},
                {'token':'synthetic-token','project_id':'a'*24}]
            with patch.object(cli_login,'authorize',return_value={'code':'code','verifier':'v','nonce':'n'}):
                cli_login.login(client,Path(directory)/'token.json',manage_env=True,allow_web=True)
            self.assertEqual({'code':'code','verifier':'v','nonce':'n','manage_env':True,'allow_web':True},client.request.call_args.args[2])
            client.request.side_effect = [{'issuer':ISSUER,'client_id':'client'}]
            with patch.object(cli_login,'authorize') as browser, self.assertRaisesRegex(RuntimeError,'permission'):
                cli_login.login(client,Path(directory)/'unsupported.json',allow_web=True)
            browser.assert_not_called()
            self.assertFalse((Path(directory)/'unsupported.json').exists())
