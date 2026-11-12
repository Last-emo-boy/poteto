"""Human-readable admission budgets and explicit preview cleanup."""
import http.client
import json
import sys
import time


def mib(value):
    return f"{value / 1024**2:.2f} MiB"


def show_usage(result):
    storage = result['artifact_storage']
    runtime = result['account_runtime']
    slots = result['deployment_slots']
    print(f"Project: {result['project_name']} ({result['project_id']})")
    print(f"Account artifacts: {mib(storage['reserved_bytes'])} / {mib(storage['limit_bytes'])}")
    print(f"Account runtime: {runtime['reserved']['memory_mib']} / {runtime['limit']['memory_mib']} MiB; "
          f"{runtime['reserved']['vcpus']:g} / {runtime['limit']['vcpus']:g} vCPU reserved")
    print(f"In-flight deployments: {slots['used']} / {slots['limit']}")
    sandbox = result['sandbox_runtime']
    print(f"Shared sandbox pool: {sandbox['reserved']['memory_mib']} / {sandbox['limit']['memory_mib']} MiB; "
          f"{sandbox['reserved']['vcpus']:g} / {sandbox['limit']['vcpus']:g} vCPU reserved")
    print("Reservations are admission budgets, not live CPU/RSS. Sleeping apps keep their reservations.")
    print(result['data_storage'])
    print("\nProject reservations visible to this credential:")
    for row in result['projects']:
        print(f"{row['id']}  {row['name']}  {mib(row['artifact_reserved_bytes'])}  "
              f"{row['runtime_reserved']['memory_mib']} MiB  {row['runtime_reserved']['vcpus']:g} vCPU")
    print("\nVisible releases in this project (largest artifact reservations first):")
    rows = sorted(result['releases'], key=lambda row: row['artifact_reserved_bytes'], reverse=True)
    for row in rows[:10]:
        print(f"{row['id']}  {row['state']:<10} {mib(row['artifact_reserved_bytes']):>13}  "
              f"{row['runtime_reserved']['memory_mib']} MiB  {row['runtime_reserved']['vcpus']:g} vCPU")
    if len(rows) > 10:
        print(f"{len(rows) - 10} more; use --json for the full list.")
    if result['restricted_to_previews']:
        print("This credential sees unpublished previews only; account totals can include other releases.")


def prune(client, args):
    base = f"/v1/projects/{args.project}/previews/prune"
    policy = {'keep': args.keep, 'older_than_days': args.older_than_days}
    if not args.yes:
        query = f"?keep={args.keep}&older_than_days={args.older_than_days}"
        result = client.request('GET', base + query)
        if args.json:
            print(json.dumps(result, indent=2))
            return
        print(f"Dry run: {len(result['candidates'])} previews; would release "
              f"{mib(result['would_release']['artifact_bytes'])} of artifact reservations.")
        for row in result['candidates']:
            print(f"{row['id']}  {row['state']:<10}  {mib(row['artifact_reserved_bytes'])}")
        print(f"Protected releases: {len(result['protected'])}. Production, published history and data-bound previews are excluded.")
        if not result['can_apply']:
            print("Cleanup needs a management credential. Personal users can log in explicitly with --manage-previews.")
        if result['candidates']:
            ids = ' '.join(row['id'] for row in result['candidates'])
            print(f"After confirming these IDs: poteto prune --project {args.project} --keep {args.keep} "
                  f"--older-than-days {args.older_than_days} --yes --ids {ids}")
        return
    try:
        result = client.request('POST', base, policy | {'ids': args.ids})
    except (OSError, http.client.HTTPException):
        print('Cleanup may already be queued. Repeat the same --yes --ids command to recover its status.',
              file=sys.stderr)
        raise
    for row in result['releases']:
        if row['job_id']:
            print(f"Cleanup {row['id']}: job {row['job_id']}", file=sys.stderr, flush=True)
    if not args.no_wait:
        deadline = time.monotonic() + args.timeout
        for row in result['releases']:
            if row['job_id']:
                client.wait_job(row['job_id'], max(0, deadline - time.monotonic()))
                row['state'] = 'removed'
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        for row in result['releases']:
            print(f"{row['id']}  {row['state']}" + (f"  job={row['job_id']}" if row['job_id'] else ''))
