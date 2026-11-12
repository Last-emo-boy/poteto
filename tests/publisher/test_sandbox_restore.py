import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from execution import platform_backup
from publisher.config import Settings
from publisher.models import DeploymentInput
from publisher.sandbox_restore import recover
from publisher.store import Store


class SandboxRecoveryTests(unittest.TestCase):
    def test_recovery_is_explicit_atomic_and_idempotent_without_altering_the_release(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);store=Store(Settings(root,sandbox_projects=(),prebuilt_image_projects=()))
            owner=store.authenticate(store.create_account('owner')['token']);project=store.create_project(owner,'site')['id']
            release=store.create_deployment(owner,project,DeploymentInput(target='preview').configuration(),None)
            with store.connection() as db:db.execute("UPDATE deployments SET state='ready',runtime_driver='gvisor' WHERE id=?",(release['id'],))
            operation='a'*24
            with self.assertRaisesRegex(ValueError,'source archive'):recover(store,operation,[release['id']])
            (root/'artifacts'/(release['id']+'.tar.gz')).write_bytes(b'owned source snapshot')
            with self.assertRaises(ValueError):recover(store,operation,[release['id'],release['id']])
            failed=store.create_deployment(owner,project,DeploymentInput(target='preview').configuration(),None)
            with store.connection() as db:db.execute("UPDATE deployments SET state='failed',runtime_driver='gvisor' WHERE id=?",(failed['id'],))
            self.assertEqual([release['id']],[r['id'] for r in recover(store,operation,None)['deployments']])
            self.assertTrue(recover(store,operation,[release['id']])['dry_run'])
            with store.connection() as db:self.assertEqual(0,db.execute('SELECT count(*) FROM jobs').fetchone()[0])
            with self.assertRaisesRegex(ValueError,'Enter maintenance'):recover(store,operation,[release['id']],apply=True)
            (root/'maintenance.json').write_text(json.dumps({'operation':operation}))
            result=recover(store,operation,[release['id']],apply=True)
            self.assertEqual('queued',result['deployments'][0]['state'])
            self.assertTrue(recover(store,operation,[release['id']],apply=True)['deployments'][0]['already_requested'])
            with store.connection() as db:
                self.assertEqual(1,db.execute('SELECT count(*) FROM jobs').fetchone()[0])
                row=db.execute('SELECT * FROM deployments WHERE id=?',(release['id'],)).fetchone()
                self.assertEqual(release['config'],row['config'])
                self.assertEqual(release['environment'],row['environment'])
                db.execute("UPDATE deployments SET state='ready' WHERE id=?",(release['id'],))
                db.execute("UPDATE jobs SET state='completed'")
            self.assertTrue(recover(store,operation,[release['id']],apply=True)['deployments'][0]['already_requested'])
            with store.connection() as db:self.assertEqual(1,db.execute('SELECT count(*) FROM jobs').fetchone()[0])

    def test_backup_carries_sandbox_receipts_and_effective_pool_overrides(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);execution=root/'executor';execution.mkdir();(execution/'builds').mkdir()
            for name in ['sandboxes','sandbox-wake','sandbox-retired-ports']:
                (execution/name).mkdir();(execution/name/'fixture.json').write_text('{}')
            code=root/'code';code.mkdir();(code/'runtime.py').write_text('fixture')
            control=root/'control';control.mkdir()
            for name in ['bin','infra','scripts']:(control/name).mkdir()
            units=root/'units';units.mkdir();(units/'paas-sandbox.slice').write_text('[Slice]\nMemoryMax=2G\n')
            override=units/'paas-sandbox.slice.d';override.mkdir();(override/'budget.conf').write_text('[Slice]\nMemoryMax=14G\n')
            stage=root/'stage';stage.mkdir()
            request={'format':'ovh-paas-platform/2','id':'a'*24,'created':0,'code_revision':'fixture','vms':[]}
            with patch.multiple(platform_backup,EXECUTION=execution,CODE=code,CONTROL=control,UNITS=units):
                platform_backup.metadata(stage,request)
            for name in ['sandboxes','sandbox-wake','sandbox-retired-ports']:
                self.assertTrue((stage/'executor/state'/name/'fixture.json').is_file())
            self.assertIn('MemoryMax=14G',(stage/'units/paas-sandbox.slice.d/budget.conf').read_text())
            manifest=json.loads((stage/'manifest.json').read_text())
            self.assertEqual(1,manifest['sandbox_count'])
            self.assertIn('publisher.sandbox_restore',manifest['sandbox_recovery'])
