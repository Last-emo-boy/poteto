import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
from publisher.backend import BackendError, Dokploy
from publisher.config import Settings
from publisher.controlflow import ObservationUnavailable
from publisher.models import DeploymentInput
from publisher.store import Store
from publisher.worker import Worker
from test_control import archive


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(Settings(self.root))
        value = self.store.create_account("test")
        self.actor = self.store.authenticate(value["token"])
        self.project = self.store.create_project(self.actor, "example")["id"]
        self.backend = Mock()
        self.backend.target.return_value = None
        self.backend.releases = self.root / "releases"
        self.backend.releases.mkdir()
        self.backend.prepare_static.return_value = ({"image": "nginx"}, 8080)
        self.backend.render.return_value = {"services": {}}
        self.backend.create_release.return_value = "compose-1"
        self.worker = Worker(self.store, self.backend)

    def tearDown(self):
        self.temp.cleanup()

    def queued(self, config=None):
        deployment = self.store.create_deployment(self.actor, self.project, DeploymentInput(**(config or {})).model_dump(), None)
        path = self.root / "input.tar.gz"
        path.write_bytes(archive())
        self.store.enqueue_upload(self.actor, deployment["id"], path, path.stat().st_size, 14)
        return deployment

    def current(self):
        with self.store.connection() as db:
            return db.execute("SELECT current_deployment FROM projects WHERE id=?", (self.project,)).fetchone()[0]

    def state(self, release_id):
        with self.store.connection() as db:
            return db.execute("SELECT state FROM deployments WHERE id=?", (release_id,)).fetchone()[0]

    def test_real_queue_finishes_and_commits_production(self):
        deployment = self.queued({"sleep": True})
        self.assertTrue(self.worker.run_once())
        self.assertEqual("ready", self.state(deployment["id"]))
        self.assertEqual(deployment["id"], self.current())
        self.backend.refresh_wake.assert_called_once()
        self.backend.public_health.assert_called_once()
        self.assertFalse(self.worker.run_once())

    def test_sleeping_sandbox_does_not_reconfigure_the_legacy_wake_proxy(self):
        from dataclasses import replace
        self.store.settings = replace(self.store.settings, sandbox_projects=(self.project,),
                                      sandbox_sleep_projects=(self.project,))
        deployment = self.queued({'runtime': 'node', 'target': 'preview', 'sleep': True})
        self.assertTrue(self.worker.run_once())
        self.assertEqual('ready', self.state(deployment['id']))
        self.backend.publish_sandbox.assert_called_once()
        self.backend.refresh_wake.assert_not_called()

    def test_failed_health_keeps_old_production(self):
        first = self.queued()
        self.worker.run_once()
        second = self.queued()
        self.backend.health.side_effect = BackendError("Failed HTTP check")
        self.worker.run_once()
        self.assertEqual("failed", self.state(second["id"]))
        self.assertEqual(first["id"], self.current())
        self.assertEqual(1, self.backend.promote.call_count)

    def test_preview_does_not_change_production(self):
        deployment = self.queued({"target": "preview"})
        self.worker.run_once()
        self.assertEqual("ready", self.state(deployment["id"]))
        self.assertIsNone(self.current())
        self.backend.promote.assert_not_called()

    def test_recovery_does_not_resubmit_dokploy_operation(self):
        deployment = self.queued()
        with self.store.connection() as db:
            db.execute("UPDATE deployments SET backend_id='existing-compose',state='deploying' WHERE id=?", (deployment["id"],))
        self.worker.run_once()
        self.backend.deploy.assert_not_called()
        self.backend.create_release.assert_not_called()
        self.assertEqual("ready", self.state(deployment["id"]))

    def test_activation_recovery_never_reopens_candidate_cancellation(self):
        release = self.queued()
        with self.store.connection() as db:
            db.execute("UPDATE deployments SET backend_id='existing-compose',state='activating' WHERE id=?",(release['id'],))
        def assert_boundary(*args, **kwargs):
            self.assertEqual('activating',self.state(release['id']))
        self.backend.promote.side_effect=assert_boundary
        self.worker.run_once()
        self.assertEqual('ready',self.state(release['id']))
        self.backend.create_release.assert_not_called()
        self.backend.deploy.assert_not_called()

    def test_migration_recovery_keeps_the_non_interruptible_phase(self):
        from publisher.data_store import DataStore
        resource=DataStore(self.store).create(self.actor,self.project,'database','production')
        with self.store.connection() as db:
            db.execute("UPDATE data_resources SET state='ready' WHERE id=?",(resource['id'],))
            db.execute("UPDATE jobs SET state='completed' WHERE resource_id=?",(resource['id'],))
        release=self.queued({'runtime':'node','database_id':resource['id'],'migration_command':'npm run migrate'})
        self.backend.config={'execution_enabled':True}
        with self.store.connection() as db:
            db.execute("UPDATE deployments SET state='migrating' WHERE id=?",(release['id'],))
        def reconcile(*args,**kwargs):
            self.assertEqual('migrating',self.state(release['id']))
            kwargs['before_migrate']()
            return {'services':{}}
        self.backend.prepare_remote.side_effect=reconcile
        self.worker.run_once()
        self.assertEqual('ready',self.state(release['id']))

    def test_secret_is_redacted_before_persistent_log(self):
        deployment = self.queued()
        deployment["environment"] = self.store.seal({"TOKEN": "real-secret-value"})
        self.worker.log(deployment, "Oops real-secret-value")
        with self.store.connection() as db:
            message = db.execute("SELECT message FROM logs WHERE deployment_id=?", (deployment["id"],)).fetchone()[0]
        self.assertEqual("Oops [REDACTED]", message)

    def test_queued_cancel_cleans_without_building_or_switching_production(self):
        release = self.queued()
        with self.store.connection() as db:
            db.execute("UPDATE deployments SET state='cancelling' WHERE id=?", (release['id'],))
        self.worker.run_once()
        self.assertEqual('cancelled', self.state(release['id']))
        self.backend.cancel.assert_called_once()
        self.backend.create_release.assert_not_called()
        self.backend.promote.assert_not_called()

    def test_cancel_during_health_prevents_activation(self):
        release = self.queued()
        def cancel(*args, **kwargs):
            with self.store.connection() as db:
                db.execute("UPDATE deployments SET state='cancelling' WHERE id=?", (release['id'],))
            kwargs['check']()
        self.backend.health.side_effect = cancel
        self.worker.run_once()
        self.assertEqual('cancelled', self.state(release['id']))
        self.backend.promote.assert_not_called()
        self.assertIsNone(self.current())

    def test_old_worker_cannot_activate_or_overwrite_a_transferred_lease(self):
        release = self.queued()
        def transfer(*args, **kwargs):
            with self.store.connection() as db:
                db.execute("UPDATE jobs SET owner='new-worker' WHERE deployment_id=?", (release['id'],))
                db.execute("UPDATE deployments SET state='building',error='new owner' WHERE id=?", (release['id'],))
        self.backend.health.side_effect = transfer
        self.worker.run_once()
        self.assertEqual('building', self.state(release['id']))
        self.backend.promote.assert_not_called()
        with self.store.connection() as db:
            self.assertEqual('new owner', db.execute('SELECT error FROM deployments WHERE id=?',(release['id'],)).fetchone()[0])

    def test_durable_delete_frees_artifacts_and_targets_but_keeps_job_receipt(self):
        release = self.queued()
        with self.store.connection() as db:
            db.execute("UPDATE jobs SET kind='delete' WHERE deployment_id=?",(release['id'],))
            db.execute("UPDATE deployments SET state='deleting' WHERE id=?",(release['id'],))
            db.execute('INSERT INTO execution_targets VALUES(?,?,?,?,?)',(release['id'],self.actor['account_id'],'server',22200,0))
        self.worker.run_once()
        self.assertFalse((self.root/'artifacts'/(release['id']+'.tar.gz')).exists())
        with self.store.connection() as db:
            self.assertIsNone(db.execute('SELECT id FROM deployments WHERE id=?',(release['id'],)).fetchone())
            self.assertIsNone(db.execute('SELECT deployment_id FROM execution_targets WHERE deployment_id=?',(release['id'],)).fetchone())
            job=db.execute("SELECT * FROM jobs WHERE kind='delete'").fetchone()
            self.assertEqual('completed',job['state'])
            self.assertIsNone(job['deployment_id'])

    def test_interrupted_observation_requeues_same_job_without_resubmitting(self):
        release = self.queued()
        self.backend.config = {'execution_enabled': True}
        self.backend.prepare_remote.side_effect = ObservationUnavailable('observation unavailable')
        self.worker.run_once()
        self.assertEqual('building', self.state(release['id']))
        with self.store.connection() as db:
            job = db.execute('SELECT * FROM jobs WHERE deployment_id=?',(release['id'],)).fetchone()
            self.assertEqual('queued',job['state'])
            self.assertIsNone(job['owner'])
            db.execute('UPDATE jobs SET lease_until=0 WHERE id=?',(job['id'],))
        self.backend.prepare_remote.side_effect = None
        self.worker.run_once()
        self.assertEqual('ready',self.state(release['id']))
        self.backend.create_release.assert_called_once()
        with self.store.connection() as db:
            self.assertEqual(1,db.execute('SELECT count(*) FROM jobs WHERE deployment_id=?',(release['id'],)).fetchone()[0])

    def test_public_probe_checks_binding_without_using_reserved_healthz(self):
        from unittest.mock import patch
        deployment = self.queued({"health_check": "/healthz"})
        backend = Dokploy({"releases": str(self.backend.releases)}, self.store.settings)
        connection = Mock()
        connection.getresponse.return_value.status = 404
        connection.getresponse.return_value.getheader.return_value = deployment["id"]
        with patch("publisher.backend.http.client.HTTPSConnection", return_value=connection):
            backend.public_health(deployment)
        self.assertEqual("/__paas_route_probe__", connection.request.call_args.args[1])

    def test_rendered_service_cannot_access_host_privileges(self):
        deployment = self.queued()
        adapter = Dokploy({"releases": str(self.backend.releases)}, self.store.settings)
        document = adapter.render(deployment, {"image": "nginx"}, 8080)
        service = document["services"]["app"]
        self.assertEqual(["ALL"], service["cap_drop"])
        self.assertEqual(["no-new-privileges:true"], service["security_opt"])
        self.assertEqual(128, service["pids_limit"])
        self.assertNotIn("privileged", service)
        self.assertNotIn("ports", service)


if __name__ == "__main__":
    unittest.main()
