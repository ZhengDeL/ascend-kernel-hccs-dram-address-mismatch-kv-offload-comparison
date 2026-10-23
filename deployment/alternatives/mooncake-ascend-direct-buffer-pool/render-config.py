#!/usr/bin/env python3
"""Render only DP identity/lookup port; never changes model or transfer flags."""
import argparse
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def render(role, rank, output):
    maximum = 2 if role == 'P' else 4
    if rank not in range(maximum):
        raise ValueError('Invalid rank for ' + role)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    connector = json.loads((ROOT / 'config' / ('connector.' + role + '.json')).read_text(encoding='utf-8'))
    connector['engine_id'] = 'glm53flash-buffer-{}-dp{}'.format('prefill' if role == 'P' else 'decode', rank)
    connector['kv_connector_extra_config']['connectors'][1]['kv_connector_extra_config']['lookup_rpc_port'] += rank
    store = json.loads((ROOT / 'config' / ('mooncake.' + role + '.json')).read_text(encoding='utf-8'))
    if os.environ.get('MOONCAKE_MASTER'):
        master = os.environ['MOONCAKE_MASTER']
        host, port = master.rsplit(':', 1)
        if not host or not port.isdigit() or not 1 <= int(port) <= 65535:
            raise ValueError('MOONCAKE_MASTER must be host:port')
        if host.startswith(('192.0.2.', '198.51.100.', '203.0.113.')):
            raise ValueError('Replace the documentation Master address in local environment first')
        store['master_server_address'] = master
    for name, data in [('connector.json', connector), ('mooncake.json', store)]:
        (output / name).write_text(json.dumps(data, indent=2) + '\n', encoding='utf-8', newline='\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--role', required=True, choices=['P', 'D'])
    parser.add_argument('--rank', required=True, type=int)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    render(args.role, args.rank, args.output)
