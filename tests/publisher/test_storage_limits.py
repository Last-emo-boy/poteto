from pathlib import Path
import tempfile
import unittest

from publisher.admin import set_storage_limit
from publisher.config import Settings
from publisher.store import Store


class StorageLimitTests(unittest.TestCase):
    def test_explicit_account_only_and_reservations_prevent_unsafe_reduction(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = Store(Settings(Path(temporary)))
            account = store.create_account("one")
            other = store.create_account("two")
            result = set_storage_limit(store, "one", account["account_id"], 2048)
            self.assertEqual(512 * 1024**2, result["previous_bytes"])
            self.assertEqual(2048 * 1024**2, result["limit_bytes"])
            with self.assertRaisesRegex(ValueError, "do not match"):
                set_storage_limit(store, "one", other["account_id"], 2048)
            with store.connection() as db:
                self.assertEqual(512 * 1024**2, db.execute("SELECT max_storage FROM accounts WHERE id=?",
                                                       (other["account_id"],)).fetchone()[0])
            actor = store.authenticate(account["token"])
            project = store.create_project(actor, "site")["id"]
            from publisher.models import DeploymentInput
            release = store.create_deployment(actor, project, DeploymentInput().configuration(), None)
            with store.connection() as db:
                db.execute("UPDATE deployments SET artifact_bytes=? WHERE id=?", (3 * 1024**2, release["id"]))
            with self.assertRaisesRegex(ValueError, "current reservation"):
                set_storage_limit(store, "one", account["account_id"], 1)
            self.assertEqual(3 * 1024**2, set_storage_limit(store, "one", account["account_id"], 4)["reserved_bytes"])

    def test_image_and_source_limits_remain_distinct(self):
        settings = Settings(Path("/unused"))
        self.assertEqual((50 * 1024**2, 200 * 1024**2, 10000), settings.upload_limits("static"))
        self.assertEqual(settings.upload_limits("static"), settings.upload_limits("dockerfile"))
        self.assertEqual((128 * 1024**2, 512 * 1024**2, 1024), settings.upload_limits("image"))
