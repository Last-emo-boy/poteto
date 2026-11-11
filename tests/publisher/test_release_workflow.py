import io
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import shutil
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from publisher.api import create_app
from publisher.config import Settings


def archive():
    buffer=io.BytesIO()
    with tarfile.open(fileobj=buffer,mode="w:gz") as tar:
        item=tarfile.TarInfo("index.html");data=b"<h1>release</h1>"
        item.size=len(data);tar.addfile(item,io.BytesIO(data))
    return buffer.getvalue()


class ReleaseWorkflowTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root=Path(temp.name)
        self.app=create_app(Settings(self.root))
        self.store=self.app.state.store
        self.owner=self.store.create_account("owner")
        self.client=TestClient(self.app,headers={"Authorization":"Bearer "+self.owner["token"]})
        self.addCleanup(self.client.close)
        self.project=self.client.post("/v1/projects",json={"name":"project"}).json()["id"]
        other=self.store.create_account("other")
        self.other=TestClient(self.app,headers={"Authorization":"Bearer "+other["token"]})
        self.addCleanup(self.other.close)
        token=self.client.post("/v1/tokens",json={"name":"preview","scopes":["deployments:preview"],
            "project_ids":[self.project],"expires_in_days":1}).json()["token"]
        self.preview=TestClient(self.app,headers={"Authorization":"Bearer "+token})
        self.addCleanup(self.preview.close)
        self.env=f"/v1/projects/{self.project}/env"

    def original(self,target="preview"):
        created=self.client.post(f"/v1/projects/{self.project}/deployments",json={"target":target}).json()
        response=self.client.put(created["upload_url"],content=archive(),headers={"Content-Type":"application/gzip"})
        self.assertEqual(202,response.status_code,response.text)
        with self.store.connection() as db:
            db.execute("UPDATE deployments SET state='ready' WHERE id=?",(created["id"],))
            db.execute("UPDATE jobs SET state='completed' WHERE deployment_id=?",(created["id"],))
        return created["id"]

    def redeploy(self,source,*,client=None,key="same-request",target="preview"):
        return (client or self.client).post(f"/v1/deployments/{source}/redeploy",
             headers={"Idempotency-Key":key},json={"target":target})

    def variables(self,target="preview"):
        with self.store.connection() as db:
            project=db.execute("SELECT * FROM projects WHERE id=?",(self.project,)).fetchone()
            return self.store.environment(project,target)

    def test_partial_environment_updates_preserve_other_keys_and_stage(self):
        self.client.put(self.env,json={"values":{"PROD_ONLY":"live"}})
        response=self.client.patch(self.env+"?target=preview",json={"values":{"A":"hidden-one","B":"hidden-two"}})
        self.assertEqual(200,response.status_code)
        self.assertNotIn("hidden",response.text)
        self.client.patch(self.env+"?target=preview",json={"values":{"A":"changed"},"remove":["B","NOT_PRESENT"]})
        self.assertEqual({"A":"changed"},self.variables())
        self.assertEqual({"PROD_ONLY":"live"},self.variables("production"))
        self.client.put(self.env+"?target=preview",json={"values":{"REPLACED":"legacy"}})
        self.assertEqual({"REPLACED":"legacy"},self.variables())
        with self.store.connection() as db:
            stored=db.execute("SELECT environment,preview_environment FROM projects WHERE id=?",(self.project,)).fetchone()
            self.assertNotIn("live",str(tuple(stored)))
            self.assertNotIn("legacy",str(tuple(stored)))

    def test_concurrent_partial_updates_do_not_overwrite_each_other(self):
        def update(n):
            return self.client.patch(self.env+"?target=preview",json={"values":{f"KEY_{n}":str(n)}}).status_code
        with ThreadPoolExecutor(max_workers=4) as executor:
            self.assertEqual([200]*8,list(executor.map(update,range(8))))
        self.assertEqual(8,len(self.variables()))

    def test_environment_validation_and_scope_remain_enforced(self):
        for body in [{"values":{"PORT":"1"}},{"remove":["DATABASE_URL"]},{"remove":["PAAS_DATA"]},
                     {"values":{"LOWERcase":"x"}},{"values":{"A":"x"},"remove":["A"]},{}]:
            response=self.client.patch(self.env,json=body)
            self.assertEqual(422,response.status_code,response.text)
        self.assertEqual(404,self.other.patch(self.env,json={"values":{"A":"x"}}).status_code)
        self.assertEqual(403,self.preview.patch(self.env,json={"values":{"A":"x"}}).status_code)
        self.client.patch(self.env+"?target=preview",json={"values":{f"K{n}":"x" for n in range(100)}})
        self.assertEqual(422,self.client.patch(self.env+"?target=preview",json={"values":{"OVER":"secret-value"}}).status_code)
        self.assertEqual(100,len(self.variables()))
        result=self.client.patch(self.env+"?target=preview",json={"values":{"OVER":"ok"},"remove":["K0"]})
        self.assertEqual(200,result.status_code)

    def test_redeploy_has_new_id_same_source_current_env_and_one_idempotent_job(self):
        self.client.patch(self.env+"?target=preview",json={"values":{"RELEASE":"one"}})
        source=self.original()
        self.client.patch(self.env+"?target=preview",json={"values":{"RELEASE":"two"}})
        response=self.redeploy(source)
        self.assertEqual(202,response.status_code,response.text)
        new=response.json()["id"]
        self.assertNotEqual(source,new)
        self.assertEqual("queued",response.json()["state"])
        self.assertEqual(new,self.redeploy(source).json()["id"])
        self.assertEqual((self.root/"artifacts"/(source+".tar.gz")).read_bytes(),
                         (self.root/"artifacts"/(new+".tar.gz")).read_bytes())
        with self.store.connection() as db:
            row=db.execute("SELECT * FROM deployments WHERE id=?",(new,)).fetchone()
            old=db.execute("SELECT * FROM deployments WHERE id=?",(source,)).fetchone()
            self.assertEqual("two",self.store.unseal(row["environment"])["RELEASE"])
            self.assertEqual("one",self.store.unseal(old["environment"])["RELEASE"])
            self.assertEqual(1,db.execute("SELECT count(*) FROM jobs WHERE deployment_id=?",(new,)).fetchone()[0])
            self.assertEqual(2,db.execute("SELECT count(*) FROM deployments").fetchone()[0])

    def test_redeploy_scope_missing_source_and_original_state(self):
        source=self.original()
        self.assertEqual(404,self.redeploy(source,client=self.other).status_code)
        self.assertEqual(403,self.redeploy(source,client=self.preview,target="production").status_code)
        self.assertEqual(202,self.redeploy(source,client=self.preview).status_code)
        self.assertEqual(409,self.redeploy(source,target="production").status_code)
        (self.root/"artifacts"/(source+".tar.gz")).unlink()
        self.assertEqual(409,self.redeploy(source,key="missing").status_code)
        self.assertEqual(422,self.client.post(f"/v1/deployments/{source}/redeploy",json={}).status_code)

    def test_redeploy_source_copy_failure_can_resume_without_another_release(self):
        source=self.original()
        with patch("publisher.redeploy.shutil.copyfileobj",side_effect=OSError("synthetic")):
            response=self.redeploy(source)
        self.assertEqual(503,response.status_code)
        self.assertEqual([],list((self.root/"uploads").iterdir()))
        result=self.redeploy(source)
        self.assertEqual(202,result.status_code,result.text)
        with self.store.connection() as db:
            self.assertEqual(2,db.execute("SELECT count(*) FROM deployments").fetchone()[0])
            self.assertEqual(0,db.execute("SELECT count(*) FROM uploads").fetchone()[0])

    def test_production_source_is_not_visible_to_preview_token(self):
        source=self.original("production")
        self.assertEqual(404,self.redeploy(source,client=self.preview).status_code)
        self.assertEqual("preview",self.redeploy(source).json()["config"]["target"])


if __name__=="__main__":unittest.main()
