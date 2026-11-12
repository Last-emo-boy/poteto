import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from execution import sandbox, sandbox_network as network


class SandboxNetworkTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name); (self.root/'sandboxes').mkdir()
        self.config = {'host_ipv4':'8.8.4.4','sandbox_web':True,'sandbox_dns':['1.1.1.1']}
        self.release = 'a'*24

    def receipt(self, release=None, **values):
        (self.root/'sandboxes'/((release or self.release)+'.json')).write_text(json.dumps(
            {'host_port':23000,'outbound_network':'web'} | values))

    def test_web_bridge_is_explicit_and_private_denial_precedes_web_ports(self):
        self.receipt(); rules = network.render(self.root,self.config)
        self.assertIn('"psbx23000"',rules)
        self.assertIn('8.8.4.4/32',rules)
        for prefix in ['10.0.0.0/8','127.0.0.0/8','169.254.0.0/16','172.16.0.0/12','192.168.0.0/16','100.64.0.0/10']:
            self.assertIn(prefix,rules)
        self.assertLess(rules.index('ip daddr @denied_ipv4 counter reject'),rules.index('tcp dport { 80, 443 } accept'))
        self.assertIn('meta nfproto ipv4 tcp dport { 80, 443 } accept',rules)
        self.assertIn('iifname "psbx*" counter drop',rules)
        self.assertIn('oifname "psbx*" counter drop',rules)

    def test_old_receipts_disabled_flag_and_deletion_never_keep_web_access(self):
        self.receipt(outbound_network='none')
        self.assertNotIn('"psbx23000"',network.render(self.root,self.config))
        self.receipt(); self.assertNotIn('"psbx23000"',network.render(self.root,self.config,exclude=self.release))
        self.assertNotIn('"psbx23000"',network.render(self.root,self.config|{'sandbox_web':False}))

    def test_invalid_receipts_and_duplicate_bridges_fail_before_applying_rules(self):
        for values in [{'host_port':0},{'host_port':'23000'},{'outbound_network':'all'}]:
            with self.subTest(values=values):
                self.receipt(**values)
                with self.assertRaises(ValueError):network.render(self.root,self.config)
        self.receipt(); self.receipt('b'*24)
        with self.assertRaisesRegex(ValueError,'multiple egress owners'):network.render(self.root,self.config)

    def test_dns_is_limited_to_explicit_public_ipv4_resolvers(self):
        for values in [[],['127.0.0.1'],['10.0.0.2'],['169.254.169.254'],['100.100.100.200'],['::1'],['1.1.1.1; flush ruleset'],[1234]]:
            with self.subTest(values=values),self.assertRaises(ValueError):network.dns_servers({'sandbox_dns':values})
        self.assertEqual(['1.1.1.1'],network.dns_servers(self.config))

    def test_nft_syntax_failure_does_not_apply_or_replace_existing_rules(self):
        with patch.object(network.subprocess,'run',side_effect=subprocess.CalledProcessError(1,'nft')) as run:
            with self.assertRaises(subprocess.CalledProcessError):network.configure(self.root,self.config)
        run.assert_called_once()
        self.assertEqual(['nft','-c','-f','-'],run.call_args.args[0])

    def test_container_dns_is_explicit_but_offline_default_remains_loopback(self):
        socket = Mock(); socket.getresponse.return_value = Mock(status=201)
        with patch.object(sandbox,'DockerSocket',return_value=socket):
            sandbox.create('app','image',3000,23000,256,.1,{}, {},dns=['1.1.1.1'])
            self.assertEqual(['1.1.1.1'],json.loads(socket.request.call_args.args[2])['HostConfig']['Dns'])
            sandbox.create('app','image',3000,23000,256,.1,{}, {})
            self.assertEqual(['127.0.0.1'],json.loads(socket.request.call_args.args[2])['HostConfig']['Dns'])

    def test_executor_rejects_web_without_switch_or_matching_owned_build(self):
        body = {'action':'start','account_id':'b'*24,'project_id':'c'*24,'personal_preview':True,
                'outbound_network':'web','port':3000,'memory_mb':256,'cpus':.1,'environment':{}}
        config = self.config|{'personal_previews':True}
        build = self.root/'builds'/self.release; build.mkdir(parents=True)
        (build/'spec.json').write_text(json.dumps({'runtime':'image','account_id':'b'*24,'project_id':'c'*24,'personal_preview':True}))
        status = Mock(return_value={'state':'succeeded','driver':'prebuilt','image':'paas-release:'+self.release})
        with patch.object(sandbox,'docker') as docker:
            with self.assertRaisesRegex(ValueError,'not enabled'):
                sandbox.operation(self.root,config|{'sandbox_web':False},self.release,body,status)
            with self.assertRaisesRegex(ValueError,'owned build receipt'):
                sandbox.operation(self.root,config,self.release,body,status)
            docker.assert_not_called()
