import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from publisher.api import create_app
from publisher.backend import Dokploy
from publisher.config import Settings


class AppDomainTests(unittest.TestCase):
    def test_new_domain_does_not_rename_any_existing_project_or_release(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            old_app=create_app(Settings(root,domain='business.test'))
            account=old_app.state.store.create_account('owner')
            headers={'Authorization':'Bearer '+account['token']}
            with TestClient(old_app,headers=headers) as old:
                project=old.post('/v1/projects',json={'name':'legacy'}).json()
                release=old.post(f"/v1/projects/{project['id']}/deployments",json={}).json()
            new_settings=Settings(root,domain='business.test',new_app_domain='user-apps.test')
            application=create_app(new_settings)
            store=application.state.store
            edge_token=root/'edge-token';edge_token.write_text('test-edge-token')
            with TestClient(application,headers=headers) as client:
                incoming=client.post('/v1/projects',json={'name':'new'}).json()
                candidate=client.post(f"/v1/projects/{incoming['id']}/deployments",json={}).json()
                self.assertTrue(incoming['url'].endswith('.user-apps.test'))
                self.assertEqual(project['url'],client.get('/v1/projects/'+project['id']).json()['url'])
                with store.connection() as db:
                    db.execute("UPDATE deployments SET state='ready'")
                    db.execute("UPDATE projects SET current_deployment=? WHERE id=?",(release['id'],project['id']))
                    db.execute("UPDATE projects SET current_deployment=? WHERE id=?",(candidate['id'],incoming['id']))
                old_ready=client.get('/v1/deployments/'+release['id']).json()
                new_ready=client.get('/v1/deployments/'+candidate['id']).json()
                self.assertEqual(project['url'],old_ready['production_url'])
                self.assertTrue(old_ready['url'].endswith('.business.test'))
                self.assertEqual(incoming['url'],new_ready['production_url'])
                self.assertTrue(new_ready['url'].endswith('.user-apps.test'))
                with patch.dict(os.environ,{'PAAS_EDGE_TOKEN_FILE':str(edge_token)}):
                    hosts=client.get('/internal/routes',headers={'X-Paas-Edge-Key':'test-edge-token'}).json()['hosts']
                self.assertEqual({x.removeprefix('https://') for x in [project['url'],old_ready['url'],
                               incoming['url'],new_ready['url']]},set(hosts))
                adapter=Dokploy({'releases':str(root/'releases'),'routes':str(root)},new_settings)
                user=store.authenticate(account['token'])
                with store.connection() as db:
                    runtime_row=store.deployment(db,user,candidate['id'])
                target={'http_port':22201}
                route,_=adapter.forward_route(runtime_row,target,production=True)
                rendered=json.loads(route.read_text())['http']['routers']['publisher-'+incoming['id']]
                self.assertEqual('Host(`'+incoming['url'].removeprefix('https://')+'`)',rendered['rule'])
            # Changing or removing the default for new projects cannot move a
            # project already published on the selected domain.
            with TestClient(create_app(Settings(root,domain='business.test')),headers=headers) as reopened:
                self.assertEqual(incoming['url'],reopened.get('/v1/projects/'+incoming['id']).json()['url'])

    def test_new_domain_cannot_be_the_existing_domain_or_its_subdomain(self):
        for value in ['w33d.xyz','apps.w33d.xyz','https://user-apps.test','*.user-apps.test']:
            with self.assertRaisesRegex(ValueError,'separate DNS domain'):
                Settings(Path('/tmp'),new_app_domain=value)
