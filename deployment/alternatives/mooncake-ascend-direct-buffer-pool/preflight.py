#!/usr/bin/env python3
"""Offline JSON audit; --host adds read-only checks before starting any candidate."""
import argparse
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import shutil
import socket
import subprocess

ROOT = Path(__file__).resolve().parent
POOLS = {'P': 20 * 1024**3, 'D': 0}


def check_configs():
    summary = {}
    for role, count, tp in [('P', 2, 8), ('D', 4, 4)]:
        store = json.loads((ROOT / 'config' / ('mooncake.' + role + '.json')).read_text(encoding='utf-8'))
        connector = json.loads((ROOT / 'config' / ('connector.' + role + '.json')).read_text(encoding='utf-8'))
        assert store['protocol'] == 'ascend' and store['local_buffer_size'] == 0
        assert store['global_segment_size'] == ('20GB' if role == 'P' else 0)
        assert not store['enable_ssd_offload']
        children = connector['kv_connector_extra_config']['connectors']
        assert connector['kv_connector'] == 'MultiConnector'
        assert children[0]['kv_connector'] == 'MooncakeConnectorV2'
        extra = children[1]['kv_connector_extra_config']
        assert extra['backend'] == 'mooncake' and extra['use_layerwise'] is False
        if role == 'D':
            assert not extra['consumer_is_to_load'] and not extra['consumer_is_to_put']
        summary[role] = {'dp': count, 'tp': tp, 'workers': count*tp,
                         'cpu_pool_per_worker_bytes': POOLS[role], 'node_pool_bytes': count*tp*POOLS[role],
                         'local_buffer_size_bytes_per_worker': 0}
    return summary


def host_check(role):
    errors = []
    evidence = {'system': platform.platform(), 'environment_scope': 'No full environment is dumped'}
    if platform.system() != 'Linux':
        return ['--host requires the actual Linux host/container environment'], evidence
    for executable in ('python3', 'vllm', 'mooncake_master' if role == 'P' else 'npu-smi', 'ip', 'npu-smi'):
        if not shutil.which(executable):
            errors.append('Missing executable: ' + executable)
    versions = {}
    for package in ('vllm', 'vllm-ascend', 'torch', 'torch-npu', 'mooncake-transfer-engine'):
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            errors.append('Missing distribution: ' + package)
    evidence['versions'] = versions
    for name in ('ASCEND_ENABLE_USE_FABRIC_MEM', 'ASCEND_USE_ASYNC_TRANSFER'):
        if name in os.environ:
            errors.append('Must be absent, not even =0: ' + name)
    if 'MOONCAKE_GLOBAL_SEGMENT_SIZE' in os.environ:
        errors.append('Must be absent: MOONCAKE_GLOBAL_SEGMENT_SIZE would override per-role JSON pool size')
    if os.environ.get('ASCEND_BUFFER_POOL') != '4:8':
        errors.append('Expected audited default ASCEND_BUFFER_POOL=4:8')
    for name in ('LOCAL_IP', 'NIC_NAME', 'MOONCAKE_MASTER'):
        value = os.environ.get(name, '')
        if not value or value.startswith(('192.0.2.', '198.51.100.', '203.0.113.')):
            errors.append('Set actual private deployment value in local environment: ' + name)
    if shutil.which('ip') and os.environ.get('NIC_NAME') and os.environ.get('LOCAL_IP'):
        result = subprocess.run(['ip', '-j', 'address', 'show', 'dev', os.environ['NIC_NAME']],
                                capture_output=True, text=True, timeout=10)
        try:
            local = {address.get('local') for device in json.loads(result.stdout) for address in device.get('addr_info', [])}
            if os.environ['LOCAL_IP'] not in local:
                errors.append('LOCAL_IP does not belong to NIC_NAME')
        except (ValueError, TypeError):
            errors.append('Could not establish local NIC/IP identity')
    for item in ('/etc/hccn.conf', '/usr/local/Ascend/driver', '/var/ai-model/GLM-5.3-Flash-w8a8/config.json'):
        if not Path(item).exists():
            errors.append('Required path absent: ' + item)
    for number in range(16):
        if not Path('/dev/davinci' + str(number)).exists():
            errors.append('Required logical NPU device absent: /dev/davinci' + str(number))
    available = next(int(line.split()[1])*1024 for line in Path('/proc/meminfo').read_text().splitlines()
                     if line.startswith('MemAvailable:'))
    evidence['mem_available_bytes'] = available
    if role == 'P' and available < (320 + 64) * 1024**3:
        errors.append('Need 320 GiB pool plus local protective 64 GiB host headroom; review actual workload memory')
    ports = [13325] + (list(range(10081, 10083)) + list(range(38680, 38700)) + [39000, 39001, 50088, 9003]
                       if role == 'P' else list(range(10100, 10104)) + list(range(38580, 38600)) + list(range(39100, 39104)))
    for port in ports:
        with socket.socket() as sock:
            try:
                sock.bind(('0.0.0.0', port))
            except OSError:
                errors.append('Candidate TCP port busy: ' + str(port))
    owners = set()
    if os.geteuid() != 0:
        errors.append('Run host preflight as root to establish all NPU owners; no services will be stopped')
    else:
        for process in Path('/proc').iterdir():
            if not process.name.isdigit():
                continue
            try:
                for descriptor in (process / 'fd').iterdir():
                    try:
                        target = os.readlink(descriptor)
                    except (FileNotFoundError, ProcessLookupError):
                        continue
                    if target in {'/dev/davinci' + str(index) for index in range(16)}:
                        owners.add(int(process.name))
                        break
            except (FileNotFoundError, ProcessLookupError):
                continue
        if owners:
            errors.append('NPU devices already held; review an authorized test window. PIDs only: ' + str(sorted(owners)))
    evidence['runtime_acceptance'] = 'Not tested: library ABI, Direct transport build, ordinary host allocation, correctness, performance'
    return errors, evidence


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--host', action='store_true')
    parser.add_argument('--role', choices=['P', 'D'], default='P')
    args = parser.parse_args()
    report = {'configuration': check_configs(), 'errors': []}
    if args.host:
        report['errors'], report['host'] = host_check(args.role)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    raise SystemExit(1 if report['errors'] else 0)
