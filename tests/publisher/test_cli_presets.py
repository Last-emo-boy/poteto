import json
from pathlib import Path
import tempfile
import unittest
from publisher.cli_tools import PlanError, plan_deployment


class PresetTests(unittest.TestCase):
    def project(self,root,*,nextjs=False,start=True,lock=True):
        package={"scripts":{"build":"next build" if nextjs else "vite build"},
                 "dependencies":{"next":"16.4.6"} if nextjs else {"vite":"7.0.0"}}
        if start:package["scripts"]["start"]="next start" if nextjs else "node server.mjs"
        (root/"package.json").write_text(json.dumps(package),encoding="utf-8")
        if lock:(root/"package-lock.json").write_text("{}",encoding="utf-8")
        return package

    def test_nextjs_auto_detection_and_custom_overrides(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);self.project(root,nextjs=True)
            plan=plan_deployment(root,None)
            self.assertEqual("nextjs",plan.summary()["preset"])
            self.assertEqual(("node","npm run build","npm start"),
                tuple(plan.config[k] for k in ("runtime","build_command","start_command")))
            self.assertNotIn("memory_mb",plan.config)
            (root/"poteto.json").write_text('{"preset":"nextjs","memory_mb":768,"target":"production"}')
            plan=plan_deployment(root,None,preview=True)
            self.assertEqual(768,plan.config["memory_mb"])
            self.assertEqual("preview",plan.config["target"])
            self.assertNotIn("preset",plan.config)

    def test_existing_next_start_without_build_keeps_generic_node_contract(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);package=self.project(root,nextjs=True)
            del package["scripts"]["build"]
            (root/"package.json").write_text(json.dumps(package))
            plan=plan_deployment(root,None)
            self.assertEqual("node",plan.config["runtime"])
            self.assertNotIn("build_command",plan.config)

    def test_vite_explicit_preset_allows_standard_config_and_output_override(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);self.project(root,start=False)
            (root/"index.html").write_text("source")
            (root/"vite.config.js").write_text("export default {}")
            with self.assertRaises(PlanError):plan_deployment(root,None)
            plan=plan_deployment(root,None,preset="vite")
            self.assertEqual(("static","dist"),(plan.config["runtime"],plan.config["output_directory"]))
            (root/"poteto.json").write_text('{"preset":"vite","output_directory":"public-site"}')
            self.assertEqual("public-site",plan_deployment(root,None).config["output_directory"])

    def test_presets_require_build_lock_and_correct_runtime(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);self.project(root,nextjs=True,start=False)
            with self.assertRaisesRegex(PlanError,"start script"):plan_deployment(root,None)
            self.project(root,nextjs=True,lock=False)
            (root/"package-lock.json").unlink()
            with self.assertRaisesRegex(PlanError,"package-lock"):plan_deployment(root,None)
            self.project(root,nextjs=True)
            for config in ['{"preset":[]}','{"preset":""}','{"preset":"other"}',
                           '{"preset":"nextjs","runtime":"static"}']:
                (root/"poteto.json").write_text(config)
                with self.assertRaises(PlanError):plan_deployment(root,None)


if __name__=="__main__":unittest.main()
