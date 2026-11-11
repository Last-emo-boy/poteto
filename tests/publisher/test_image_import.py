import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch
from pydantic import ValidationError
from execution import guest_build
from publisher.executor import ExecutionClient
from publisher.models import DeploymentInput


class ImageImportTests(unittest.TestCase):
    def test_builder_emits_only_the_new_canonical_tag(self):
        original='paas-release:'+'a'*24;release='b'*24;canonical='paas-release:'+release
        with tempfile.TemporaryDirectory() as temp,patch.object(guest_build,'WORK',Path(temp)),patch.object(guest_build,'logged',return_value=True) as logged,patch.object(guest_build.subprocess,'run') as run,patch.object(guest_build,'build_source') as source:
            (Path(temp)/'spec.json').write_text(json.dumps({'runtime':'image-import','image_reference':original,'id':release}))
            guest_build.main()
            source.assert_not_called()
            self.assertEqual(['docker','load','--platform=linux/amd64','--input',str(Path(temp)/'source.tar.gz')],logged.call_args.args[0])
            self.assertEqual([['docker','tag',original,canonical],['docker','save','--output',str(Path(temp)/'image.tar'),canonical]],[call.args[0] for call in run.call_args_list])

    def test_unexpected_image_reference_never_reaches_docker(self):
        with patch.object(guest_build,'logged') as command:
            with self.assertRaises(ValueError):guest_build.normalize_image({'image_reference':'postgres:16-alpine'},'paas-release:'+'a'*24)
            command.assert_not_called()

    def test_image_intake_is_not_a_public_source_runtime(self):
        with self.assertRaises(ValidationError):DeploymentInput(runtime='image-import')

    def test_client_preserves_source_media_type_and_selects_raw_image_for_import(self):
        client=ExecutionClient({'url':'http://10.201.0.2:21999','token':'test-only'})
        with tempfile.TemporaryDirectory() as temp,patch.object(client,'request',return_value={}) as request:
            path=Path(temp)/'input';path.write_bytes(b'body')
            client.start_build('a'*24,{'runtime':'dockerfile'},path)
            self.assertEqual('application/gzip',request.call_args.args[3]['Content-Type'])
            client.start_build('b'*24,{'runtime':'image-import'},path)
            self.assertEqual('application/x-tar',request.call_args.args[3]['Content-Type'])
