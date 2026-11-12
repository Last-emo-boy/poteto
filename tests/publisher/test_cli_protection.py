from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from publisher import cli, cli_protection
from publisher.cli_tools import upload_files

PROJECT='a'*24
RELEASE='b'*24
KEY='poteto_preview_'+'x'*43
URL='https://preview-'+RELEASE+'.page.example.test'


class ProtectionCliTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup);self.root=Path(temporary.name)

    def test_key_saved_once_and_excluded_from_uploads_without_printing_it(self):
        client=Mock(url='https://api.example.test')
        client.request.side_effect=[{'access_key':KEY,'project_id':PROJECT,'job_id':'job','enabled':True},{'protected':True}]
        path=self.root/'review.poteto-key'
        args=SimpleNamespace(project=PROJECT,protection_action='enable',output=path,no_wait=False,timeout=60)
        result=cli_protection.configure(client,args)
        self.assertNotIn(KEY,json.dumps(result));self.assertEqual(KEY,json.loads(path.read_text())['access_key'])
        self.assertEqual('https://api.example.test',json.loads(path.read_text())['api'])
        self.assertNotIn(path.name,upload_files(self.root))
        if os.name!='nt':self.assertEqual(0o600,path.stat().st_mode&0o777)
        client.request.reset_mock()
        with self.assertRaises(FileExistsError):cli_protection.configure(client,args)
        client.request.assert_not_called()

    def test_gateway_failure_keeps_the_issued_key_for_retry(self):
        client=Mock(url='https://api.example.test')
        client.request.return_value={'access_key':KEY,'job_id':'job'};client.wait_job.side_effect=RuntimeError('gateway failed')
        path=self.root/'review.poteto-key';args=SimpleNamespace(project=PROJECT,protection_action='enable',output=path,no_wait=False,timeout=60)
        with self.assertRaisesRegex(RuntimeError,'gateway failed'):cli_protection.configure(client,args)
        self.assertEqual(KEY,json.loads(path.read_text())['access_key'])

    def key_file(self,**extra):
        path=self.root/'access.poteto-key';path.write_text(json.dumps({'access_key':KEY,'api':'https://api.example.test','project_id':PROJECT}|extra));return path

    def test_fetch_checks_project_and_never_forwards_a_platform_token(self):
        client=Mock(url='https://api.example.test',token='platform-secret');client.request.return_value={'url':URL,'project_id':PROJECT}
        response=Mock(status=200,headers={'Content-Type':'text/plain'});response.read.return_value=b'hello'
        response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False)
        opener=Mock();opener.open.return_value=response
        with patch.object(cli_protection,'build_opener',return_value=opener):
            result=cli_protection.fetch(client,RELEASE,'/hello',self.key_file())
        self.assertEqual('hello',result['body'])
        headers={k.lower():v for k,v in opener.open.call_args.args[0].header_items()}
        self.assertEqual(KEY,headers['x-poteto-access-key']);self.assertNotIn('authorization',headers)
        with patch.object(cli_protection,'build_opener') as create:
            with self.assertRaisesRegex(ValueError,'another API or project'):
                cli_protection.fetch(client,RELEASE,'/',self.key_file(project_id='c'*24))
            create.assert_not_called()

    def test_fetch_refuses_external_redirects_and_unrelated_preview_urls(self):
        client=Mock(url='https://api.example.test');client.request.return_value={'url':URL,'project_id':PROJECT}
        opener=Mock();opener.open.side_effect=HTTPError(URL,302,'redirect',{'Location':'https://other.test/'},None)
        with patch.object(cli_protection,'build_opener',return_value=opener):
            with self.assertRaisesRegex(ValueError,'outside its origin'):
                cli_protection.fetch(client,RELEASE,'/',self.key_file())
        self.assertEqual(1,opener.open.call_count)
        with self.assertRaisesRegex(ValueError,'must match'):
            cli_protection.fetch(client,URL.replace('page.example.test','malicious.test'),'/',self.key_file())

    def test_cli_key_extension_is_validated_before_authentication(self):
        out=io.StringIO()
        with patch('sys.argv',['poteto','protection','enable','--project',PROJECT,'--output',str(self.root/'key.json')]), \
             patch.object(cli,'Client') as client,redirect_stdout(out),patch('sys.stderr',io.StringIO()):
            with self.assertRaises(SystemExit) as result:cli.main()
        self.assertEqual(2,result.exception.code);client.assert_not_called()
