"""Admission reservations, shared by quota checks and user-facing usage."""
import json

from publisher.store import ARTIFACT_DRIVER, SANDBOX_DRIVER

ACCOUNT = {"memory_mib": 4096, "vcpus": 4}
PLATFORM = {"memory_mib": 20480, "vcpus": 6}
SANDBOX = {"memory_mib": 2048, "vcpus": 1.5}
DATABASE = {"memory_mib": 256, "vcpus": 0.25}
ACTIVE = ("uploading", "queued", "building", "migrating", "deploying", "activating", "cancelling")


def empty():
    return {"memory_mib": 0, "vcpus": 0}


def add(total, amount):
    for key in total:
        total[key] = round(total[key] + amount[key], 6)


def release(row):
    if row["runtime_driver"] == ARTIFACT_DRIVER or (
            row["state"] in {"failed", "cancelled"} and row["backend_id"] is None):
        return empty()
    config = json.loads(row["config"])
    return {"memory_mib": config["memory_mb"], "vcpus": config["cpus"]}


def reservations(db, excluding=""):
    result = {"platform": empty(), "sandbox": empty(), "accounts": {}}
    rows = db.execute("SELECT d.*,p.account_id FROM deployments d JOIN projects p ON p.id=d.project_id WHERE d.id!=?",
                      (excluding,)).fetchall()
    for row in rows:
        amount = release(row)
        add(result["platform"], amount)
        add(result["accounts"].setdefault(row["account_id"], empty()), amount)
        if row["runtime_driver"] == SANDBOX_DRIVER:
            add(result["sandbox"], amount)
    for row in db.execute("SELECT p.account_id FROM data_resources r JOIN projects p ON p.id=r.project_id WHERE r.kind='database'"):
        add(result["platform"], DATABASE)
        add(result["accounts"].setdefault(row["account_id"], empty()), DATABASE)
    return result


def active_count(db, account):
    return db.execute("SELECT count(*) FROM deployments d JOIN projects p ON p.id=d.project_id "
                      "WHERE p.account_id=? AND d.state IN (" + ",".join("?" for _ in ACTIVE) + ")",
                      (account, *ACTIVE)).fetchone()[0]
