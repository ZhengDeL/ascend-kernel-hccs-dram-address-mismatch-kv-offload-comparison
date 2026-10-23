#!/usr/bin/env python3
"""Capture before CANN shell initialization; preserve the actual service status."""
import argparse
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import traceback

from logging_utils import CaptureOwnOutput, exit_code, write_json


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--log-dir', required=True)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command and args.command[0] == '--' else args.command
    if not command:
        parser.error('Missing service command after --')
    folder = Path(args.log_dir)
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    process, code = None, 1
    signalled = [None]
    with CaptureOwnOutput(folder / 'stdout.log') as capture:
        def forward(number, _):
            signalled[0] = number
            if process is not None:
                try:
                    os.killpg(process.pid, number)
                except ProcessLookupError:
                    pass
        for number in (signal.SIGINT, signal.SIGTERM):
            signal.signal(number, forward)
        try:
            if signalled[0] is None:
                process = subprocess.Popen(command, start_new_session=True)
                if signalled[0] is not None:
                    forward(signalled[0], None)
                code = exit_code(process.wait())
            else:
                code = 128 + signalled[0]
        except Exception:
            traceback.print_exc()
        finally:
            write_json(folder / 'exit-status.json', {
                'exit_code': code, 'child_returncode': process.returncode if process else None,
                'received_signal': signalled[0], 'finished_at': time.time(),
                'logging_errors': capture.tee.log.errors,
                'docker_mirror_bytes_dropped_at_child_exit': capture.tee.mirror_bytes_dropped,
            })
    return code


if __name__ == '__main__':
    sys.exit(main())
