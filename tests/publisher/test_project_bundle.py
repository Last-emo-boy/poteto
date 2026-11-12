import io
import json
from pathlib import Path
import stat
import tempfile
import unittest
import zipfile
from publisher.project_bundle import BoundedZipInput,BundleError,unpack_bundle
from test_control import archive


class BundleTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.destination=self.root/'staging';self.destination.mkdir()
        self.project=self.root/'project.zip';self.database='c'*24;self.volume='d'*24
        self.manifest={'format':'ovh-paas-project/1','bundle_id':'a'*24,'source_release':'b'*24,'name':'portable',
            'image':'paas-release:'+'b'*24,'configuration':{'runtime':'node','database_id':self.database,'volume_id':self.volume},
            'project_environment':{'APP_VERSION':'future'},'release_environment':{'APP_VERSION':'published'},
            'resources':[{'id':self.database,'kind':'database','stage':'production'},{'id':self.volume,'kind':'volume','stage':'production'}]}

    def tearDown(self):self.temp.cleanup()

    def write(self,*,extra=None,compression=zipfile.ZIP_STORED):
        data=io.BytesIO();prefix='volumes/'+self.volume+'/'
        with zipfile.ZipFile(data,'w') as entries:
            entries.writestr('databases/'+self.database+'.dump',b'PGDMP'+b'x'*20)
            entries.writestr(prefix,b'');entries.writestr(prefix+'note.txt',b'persisted')
            if extra:entries.writestr(*extra)
        with zipfile.ZipFile(self.project,'w',compression=compression) as result:
            result.writestr('manifest.json',json.dumps(self.manifest))
            result.writestr('image.tar',b'docker archive')
            result.writestr('source.tar.gz',archive())
            result.writestr('data.zip',data.getvalue())

    def test_versioned_bundle_keeps_project_and_published_environments_distinct(self):
        self.write();manifest=unpack_bundle(self.project,self.destination)
        self.assertEqual('future',manifest.project_environment['APP_VERSION'])
        self.assertEqual('published',manifest.release_environment['APP_VERSION'])
        self.assertEqual(self.database,manifest.configuration.database_id)
        self.assertEqual(0o600,(self.destination/'image.tar').stat().st_mode&0o777)

    def test_managed_credentials_and_invalid_binding_are_rejected_before_staging(self):
        self.manifest['release_environment']['DATABASE_URL']='synthetic-secret'
        self.write()
        with self.assertRaises(BundleError) as error:unpack_bundle(self.project,self.destination)
        self.assertNotIn('synthetic-secret',str(error.exception));self.assertEqual([],list(self.destination.iterdir()))
        del self.manifest['release_environment']['DATABASE_URL'];self.manifest['resources'][0]['stage']='preview';self.write()
        with self.assertRaises(BundleError):unpack_bundle(self.project,self.destination)

    def test_volume_entries_cannot_escape_or_alias_declared_resources(self):
        for path in ['../outside','a//b','/absolute']:
            with self.subTest(path=path),tempfile.TemporaryDirectory() as directory:
                self.write(extra=('volumes/'+self.volume+'/'+path,b'bad'))
                with self.assertRaises(BundleError):unpack_bundle(self.project,Path(directory))

    def test_volume_symbolic_links_are_never_followed(self):
        link=zipfile.ZipInfo('volumes/'+self.volume+'/link');link.external_attr=(stat.S_IFLNK|0o777)<<16
        self.write(extra=(link,b'../../elsewhere'))
        with self.assertRaisesRegex(BundleError,'unsafe entries'):unpack_bundle(self.project,self.destination)

    def test_compressed_outer_zip_is_not_the_exported_format(self):
        self.write(compression=zipfile.ZIP_DEFLATED)
        with self.assertRaises(BundleError):unpack_bundle(self.project,self.destination)
        self.assertEqual([],list(self.destination.iterdir()))

    def test_metadata_reads_are_bounded_before_allocation(self):
        self.project.write_bytes(b'example')
        with self.project.open('rb') as source:
            bounded=BoundedZipInput(source)
            with self.assertRaisesRegex(BundleError,'8 MiB'):bounded.read(8*1024**2+1)
            self.assertEqual(b'example',bounded.read())

    def test_legacy_static_requires_source_assets_not_a_bare_runtime_image(self):
        self.manifest.update(configuration={'runtime':'static'},resources=[],image='sha256:'+'e'*64)
        with zipfile.ZipFile(self.project,'w') as result:
            result.writestr('manifest.json',json.dumps(self.manifest));result.writestr('image.tar',b'nginx base')
            result.writestr('source.tar.gz',archive());result.writestr('nginx.conf',b'events {}')
        self.assertTrue(unpack_bundle(self.project,self.destination).legacy_static)

    def test_prebuilt_static_bundle_needs_only_the_validated_source(self):
        self.manifest.update(configuration={'runtime':'static'},resources=[],image='static-artifact')
        with zipfile.ZipFile(self.project,'w') as result:
            result.writestr('manifest.json',json.dumps(self.manifest))
            result.writestr('source.tar.gz',archive())
        self.assertTrue(unpack_bundle(self.project,self.destination).static_artifact)
        self.assertEqual(['source.tar.gz'],[p.name for p in self.destination.iterdir()])
