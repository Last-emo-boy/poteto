"""Local administrator operations; never exposes an account-creation endpoint."""
import argparse
import json
import os
import re
from pathlib import Path
from publisher.config import Settings
from publisher.store import Problem, Store
from publisher.backend import BackendError, Dokploy
from publisher.controlflow import ObservationUnavailable
from publisher.executor import ExecutionFailure
from publisher.runtime import RuntimeRegistry


def retire_account(store, name, account_id):
    config=Path(os.environ.get('PAAS_BACKEND_CONFIG','/etc/paas-publisher/backend.json'))
    settings=json.loads(config.read_text())
    if settings.get('execution_enabled') is not True:
        raise ValueError('Retirement requires the isolated execution supervisor')
    completed=store.begin_account_retirement(name,account_id)
    if completed is not None:
        return completed
    backend=Dokploy(settings,store.settings)
    result=RuntimeRegistry(backend).retire(account_id)
    return store.complete_account_retirement(account_id,result)


def set_storage_limit(store, name, account_id, mib):
    if not isinstance(account_id, str) or not re.fullmatch(r"[a-f0-9]{24}", account_id):
        raise ValueError("Confirm the exact account ID")
    if type(mib) is not int or not 1 <= mib <= 1048576:
        raise ValueError("Storage limit must be between 1 MiB and 1 TiB")
    size = mib * 1024**2
    with store.connection() as db:
        row = db.execute("SELECT id,max_storage FROM accounts WHERE name=? AND id=? AND enabled=1",
                         (name, account_id)).fetchone()
        if row is None:
            raise ValueError("Account name and ID do not match an active account")
        used = store.storage_used(db, account_id)
        if size < used:
            raise ValueError("New storage limit is below the account's current reservation")
        db.execute("UPDATE accounts SET max_storage=? WHERE id=?", (size, account_id))
        store.audit(db, {"account_id": account_id, "id": None}, "account.storage-limit", account_id)
    return {"account_id": account_id, "previous_bytes": row["max_storage"], "limit_bytes": size, "reserved_bytes": used}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["create-account", "issue-owner-token", "retire-account", "set-storage-limit"])
    parser.add_argument("name")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--confirm-account", help="Exact account ID whose empty VM may be destroyed")
    parser.add_argument("--mib", type=int, help="Account artifact storage limit in MiB")
    args = parser.parse_args()
    if args.command == "set-storage-limit":
        if os.geteuid() != 0 or not args.confirm_account or args.output or args.mib is None:
            parser.error("set-storage-limit requires controller root, --confirm-account ACCOUNT_ID and --mib N")
        try:
            print(json.dumps(set_storage_limit(Store(Settings.from_env()), args.name, args.confirm_account, args.mib)))
        except (ValueError, Problem) as error:
            raise SystemExit(str(error)) from None
        return
    if args.mib is not None:
        parser.error("--mib is only used with set-storage-limit")
    if args.command=='retire-account':
        if os.geteuid()!=0 or not args.confirm_account or args.output:
            parser.error('retire-account requires controller root and --confirm-account ACCOUNT_ID')
        try:
            print(json.dumps(retire_account(Store(Settings.from_env()),args.name,args.confirm_account)))
        except (Problem,ValueError,BackendError,ExecutionFailure,ObservationUnavailable) as error:
            raise SystemExit(str(error)) from None
        return
    if not args.output or args.confirm_account:
        parser.error('Use --output FILE to save the owner credential')
    # Reserve the private output before changing account state.
    with open(args.output, "x", opener=lambda p, f: os.open(p, f, 0o600)) as out:
        try:
            store = Store(Settings.from_env())
            result = store.create_account(args.name) if args.command == "create-account" else store.issue_owner_token(args.name)
            json.dump(result, out, indent=2)
            out.write("\n")
        except BaseException:
            args.output.unlink(missing_ok=True)
            raise
    print(f"Owner token saved to {args.output}")


if __name__ == "__main__":
    main()
