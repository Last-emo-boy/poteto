from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
import ssl
import unittest
from unittest.mock import Mock, patch
from urllib.error import URLError

from publisher import cli, cli_logs
from publisher.cli_upload import RequestError

RELEASE='a'*24


def snapshot(cursor=0, entries=(), **extra):
    return {'state':'completed','error':None,'log':'','entries':list(entries),
            'next_cursor':cursor,'truncated':False, **extra}


def line(cursor, text):
    return {'cursor':cursor,'kind':'line','message':text,'collected':1}


class RuntimeCliTests(unittest.TestCase):
    def run_logs(self, responses, **options):
        clock=[0]
        def sleep(seconds):clock[0]+=seconds
        values=iter(responses)
        last=[snapshot()]
        def request(*args,**kw):
            value=next(values,last[0])
            if isinstance(value,Exception):raise value
            last[0]=value
            return value
        client=Mock();client.request.side_effect=request
        out,err=io.StringIO(),io.StringIO()
        with patch.object(cli_logs.time,'monotonic',side_effect=lambda:clock[0]),patch.object(cli_logs.time,'sleep',side_effect=sleep),redirect_stdout(out),redirect_stderr(err):
            result=cli_logs.runtime(client,RELEASE,**options)
        return result,out.getvalue(),err.getvalue(),client

    def test_follow_uses_cursor_without_repeating_snapshots(self):
        _,out,err,client=self.run_logs([snapshot(1,[line(1,'hello\n')]),snapshot(2,[line(2,'world\n')]),snapshot(2)],follow=True,timeout=11)
        self.assertEqual('hello\nworld\n',out);self.assertEqual('',err)
        self.assertNotIn('after=',client.request.call_args_list[0].args[1])
        self.assertIn('after=1',client.request.call_args_list[1].args[1])
        self.assertIn('after=2',client.request.call_args_list[2].args[1])

    def test_resume_jsonl_and_tail_zero_are_machine_readable(self):
        _,out,_,client=self.run_logs([snapshot(8,[line(8,'new\n')])],follow=True,timeout=1,tail=0,after=7,jsonl=True)
        event=json.loads(out);self.assertEqual(RELEASE,event['deployment_id']);self.assertEqual(8,event['cursor'])
        self.assertIn('tail=0&after=7',client.request.call_args.args[1])

    def test_cold_tail_zero_waits_for_a_baseline_before_following(self):
        _,out,_,client=self.run_logs([snapshot(state='queued'),snapshot(5),snapshot(6,[line(6,'new\n')])],follow=True,timeout=7,tail=0)
        self.assertEqual('new\n',out)
        self.assertNotIn('after=',client.request.call_args_list[1].args[1])
        self.assertIn('after=5',client.request.call_args_list[2].args[1])

    def test_gap_is_visible_and_untrusted_terminal_codes_are_escaped(self):
        _,out,err,_=self.run_logs([snapshot(4,[line(4,'\x1b[2Jhello\r\u202etxt\n')],truncated=True)],follow=True,timeout=1)
        self.assertIn('no longer retained',err);self.assertNotIn('\x1b',out);self.assertNotIn('\r',out);self.assertNotIn('\u202e',out)
        self.assertIn('hello',out)

    def test_network_and_transient_http_recover_without_resetting_cursor(self):
        _,out,err,client=self.run_logs([snapshot(1,[line(1,'one\n')]),URLError('offline'),RequestError(503,'temporary'),snapshot(2,[line(2,'two\n')])],follow=True,timeout=10)
        self.assertEqual('one\ntwo\n',out);self.assertEqual(1,err.count('Connection interrupted'))
        self.assertTrue(all('after=1' in call.args[1] for call in client.request.call_args_list[1:]))

    def test_authorization_and_tls_failures_are_not_retried(self):
        for error in [RequestError(401,'expired'),RequestError(404,'removed'),URLError(ssl.SSLCertVerificationError('untrusted'))]:
            with self.assertRaises(type(error)):self.run_logs([error],follow=True,timeout=10)

    def test_snapshot_waits_for_worker_and_legacy_response_still_works(self):
        expected={'state':'completed','error':None,'log':'old server log'}
        result,_,_,client=self.run_logs([snapshot(state='queued'),expected])
        self.assertEqual(expected,result);self.assertEqual(2,client.request.call_count)
        with self.assertRaisesRegex(RuntimeError,'does not support'):
            self.run_logs([expected],follow=True)

    def test_jsonl_gap_and_pause_never_fake_a_log_entry(self):
        _,out,err,_=self.run_logs([snapshot(9,[line(9,'kept')],truncated=True,state='paused')],follow=True,timeout=1,jsonl=True)
        events=[json.loads(row) for row in out.splitlines()]
        self.assertEqual(['gap','line'],[e['kind'] for e in events]);self.assertNotIn('cursor',events[0])
        self.assertIn('without waking',err)

    def test_cli_dispatch_validation_and_keyboard_interrupt(self):
        env={'POTETO_TOKEN':'secret','POTETO_TOKEN_FILE':'','PAAS_TOKEN':'','PAAS_TOKEN_FILE':''}
        out,err=io.StringIO(),io.StringIO()
        with patch.dict(os.environ,env),patch.object(cli,'find_link',return_value=None),patch.object(cli,'Client') as client,patch.object(cli.cli_logs,'runtime',return_value=None) as runtime,redirect_stdout(out),redirect_stderr(err):
            for args in [('logs',RELEASE,'--tail','201','--runtime'),('logs',RELEASE,'--after','-1','--runtime'),('logs',RELEASE,'--jsonl'),('logs',RELEASE,'--runtime','--timeout','0')]:
                with patch('sys.argv',['poteto',*args]),self.assertRaises(SystemExit):cli.main()
            client.assert_not_called()
            with patch('sys.argv',['poteto','logs',RELEASE,'--runtime','--follow','--jsonl','--after','2']):cli.main()
            self.assertEqual(2,runtime.call_args.kwargs['after']);self.assertTrue(runtime.call_args.kwargs['follow'])
            runtime.side_effect=KeyboardInterrupt
            with patch('sys.argv',['poteto','logs',RELEASE,'--runtime','--follow']),self.assertRaises(SystemExit) as stopped:cli.main()
            self.assertEqual(1,stopped.exception.code)
        self.assertNotIn('secret',out.getvalue()+err.getvalue());self.assertIn('Cancelled',err.getvalue())


if __name__=='__main__':unittest.main()
