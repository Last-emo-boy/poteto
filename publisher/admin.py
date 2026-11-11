"""Local administrator operations; never exposes an account-creation endpoint."""
import argparse
import json
import os
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["create-account", "issue-owner-token", "retire-account"])
    parser.add_argument("name")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--confirm-account", help="Exact account ID whose empty VM may be destroyed")
    args = parser.parse_args()
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
