"""Local root-only maintenance barrier; never exposed as a public API operation."""
import argparse
import json
import os
from pathlib import Path
import re
import sqlite3
from publisher.config import Settings


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('action',choices=['enter','status','leave'])
    parser.add_argument('operation',nargs='?')
    args=parser.parse_args()
    if os.geteuid()!=0:
        raise SystemExit('Root administrator access is required')
    root=Settings.from_env().root;path=root/'maintenance.json'
    if args.action!='status' and (not args.operation or not re.fullmatch('[a-f0-9]{24}',args.operation)):
        raise SystemExit('A 24-character operation ID is required')
    if args.action=='enter':
        if path.exists():
            if json.loads(path.read_text())['operation']!=args.operation:
                raise SystemExit('Another maintenance operation owns the barrier')
        else:
            with open(path,'x',opener=lambda p,f:os.open(p,f,0o644)) as out:
                json.dump({'operation':args.operation},out)
    elif args.action=='leave' and path.exists():
        if json.loads(path.read_text())['operation']!=args.operation:
            raise SystemExit('Refusing to remove another operation’s barrier')
        path.unlink()
    connection=sqlite3.connect(root/'control.sqlite3')
    count=connection.execute("SELECT count(*) FROM jobs WHERE state IN ('queued','running')").fetchone()[0]
    connection.close()
    print(json.dumps({'maintenance':json.loads(path.read_text()) if path.exists() else None,'active_jobs':count}))


if __name__=='__main__':main()
