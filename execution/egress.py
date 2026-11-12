#!/usr/bin/env python3
import json
from pathlib import Path
import pwd
import subprocess

PRIVATE = "0.0.0.0/8, 10.0.0.0/8, 100.64.0.0/10, 127.0.0.0/8, 169.254.0.0/16, 172.16.0.0/12, 192.0.0.0/24, 192.0.2.0/24, 192.168.0.0/16, 198.18.0.0/15, 198.51.100.0/24, 203.0.113.0/24, 224.0.0.0/4, 240.0.0.0/4"


def main():
    config = json.loads(Path("/srv/ovh-paas-execution/config.json").read_text())
    uids = [pwd.getpwnam(name).pw_uid for name in ["paasbuild", *[f"paasrt{i}" for i in range(4)]]]
    uid_set = "{ " + ", ".join(str(uid) for uid in uids) + " }"
    controller = pwd.getpwnam("paasvm").pw_uid
    sandbox_ports = ", 23000-23063" if config.get("sandbox_projects") or config.get('personal_previews') else ""
    if config.get("sandbox_sleep_projects") or config.get('personal_previews'):
        sandbox_ports += ", 23100"
    controller_rule = f'   meta skuid {controller} ip daddr 127.0.0.1 tcp dport {{ 21999, 22100-22103, 22200-22203{sandbox_ports} }} accept comment "publisher-execution-control"\n'
    rules = f'''destroy table inet ovh_paas_execution
 table inet ovh_paas_execution {{
  chain qemu_output {{
   type filter hook output priority -10; policy accept;
   meta skuid {uid_set} ct state established,related accept
{controller_rule}   meta skuid {uid_set} ip daddr {{ {PRIVATE}, {config['host_ipv4']}/32 }} counter reject
   meta skuid {uid_set} meta nfproto ipv6 counter reject
  }}
 }}
'''
    subprocess.run(["nft", "-c", "-f", "-"], input=rules, text=True, check=True)
    subprocess.run(["nft", "-f", "-"], input=rules, text=True, check=True)



if __name__ == "__main__":
    main()
