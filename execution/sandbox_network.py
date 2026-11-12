"""Reconcile opt-in web egress without exposing host or private networks."""
import ipaddress
import json
from pathlib import Path
import re
import subprocess

ROOT = Path('/srv/ovh-paas-execution')
DNS = ('1.1.1.1', '8.8.8.8')
PRIVATE = ('0.0.0.0/8', '10.0.0.0/8', '100.64.0.0/10', '127.0.0.0/8',
           '169.254.0.0/16', '172.16.0.0/12', '192.0.0.0/24', '192.0.2.0/24',
           '192.88.99.0/24', '192.168.0.0/16', '198.18.0.0/15', '198.51.100.0/24',
           '203.0.113.0/24', '224.0.0.0/4', '240.0.0.0/4')


def dns_servers(config):
    values = config.get('sandbox_dns', DNS)
    if not isinstance(values, (list, tuple)) or not 1 <= len(values) <= 3:
        raise ValueError('Choose one to three public IPv4 sandbox DNS resolvers')
    try:
        addresses = [ipaddress.IPv4Address(value) for value in values if isinstance(value, str)]
    except ipaddress.AddressValueError:
        raise ValueError('Sandbox DNS resolvers must be public IPv4 addresses') from None
    if len(addresses) != len(values) or any(not value.is_global for value in addresses):
        raise ValueError('Sandbox DNS resolvers must be public IPv4 addresses')
    return [str(value) for value in addresses]


def render(root, config, *, exclude=None):
    if type(config.get('sandbox_web', False)) is not bool:
        raise ValueError('sandbox_web must be a boolean')
    addresses = dns_servers(config)
    blocked = list(PRIVATE)
    host = ipaddress.IPv4Address(config['host_ipv4'])
    if host.is_global:
        blocked.append(str(host) + '/32')
    bridges = set()
    if config.get('sandbox_web'):
        for path in (root / 'sandboxes').glob('*.json'):
            if not re.fullmatch(r'[a-f0-9]{24}', path.stem):
                raise ValueError('Invalid sandbox receipt name')
            if path.stem == exclude:
                continue
            receipt = json.loads(path.read_text())
            if receipt.get('outbound_network', 'none') == 'none':
                continue
            if receipt.get('outbound_network') != 'web':
                raise ValueError('Invalid sandbox network policy')
            port = receipt.get('host_port')
            if type(port) is not int or not 23000 <= port <= 23063:
                raise ValueError('Invalid sandbox bridge port')
            bridge = 'psbx' + str(port)
            if bridge in bridges:
                raise ValueError('Sandbox bridge has multiple egress owners')
            bridges.add(bridge)
    elements = ', '.join('"' + value + '"' for value in sorted(bridges))
    members = 'elements = { ' + elements + ' };' if elements else ''
    return f'''destroy table inet poteto_sandbox
 table inet poteto_sandbox {{
  set web_bridges {{ type ifname; {members} }}
  set denied_ipv4 {{ type ipv4_addr; flags interval; elements = {{ {', '.join(blocked)} }}; }}
  set dns_servers {{ type ipv4_addr; elements = {{ {', '.join(addresses)} }}; }}
  chain input {{
   type filter hook input priority -20; policy accept;
   iifname "psbx*" ct state established,related accept
   iifname "psbx*" counter reject
  }}
  chain forward {{
   type filter hook forward priority -20; policy accept;
   iifname @web_bridges ip daddr @denied_ipv4 counter reject
   iifname @web_bridges ip daddr @dns_servers udp dport 53 accept
   iifname @web_bridges ip daddr @dns_servers tcp dport 53 accept
   iifname @web_bridges meta nfproto ipv4 tcp dport {{ 80, 443 }} accept
   oifname @web_bridges ct state established,related accept
   iifname "psbx*" counter drop
   oifname "psbx*" counter drop
  }}
 }}
'''


def configure(root, config, *, exclude=None):
    rules = render(root, config, exclude=exclude)
    subprocess.run(['nft', '-c', '-f', '-'], input=rules, text=True, check=True, timeout=15)
    subprocess.run(['nft', '-f', '-'], input=rules, text=True, check=True, timeout=15)


def main():
    configure(ROOT, json.loads((ROOT / 'config.json').read_text()))


if __name__ == '__main__':
    main()
