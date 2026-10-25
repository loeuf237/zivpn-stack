"""Probe the private native API and current account-policy access."""
import json
import subprocess

from zivpn_native_accounting import request


def main():
    request('/health')
    result = subprocess.run(
        ['/usr/local/sbin/zivpn-native-auth'],
        input=json.dumps({'check': True, 'ids': []}),
        text=True, capture_output=True, timeout=8, check=True,
    )
    if json.loads(result.stdout).get('ok') is not True:
        raise SystemExit('Account policy is unavailable')


if __name__ == '__main__':
    main()
