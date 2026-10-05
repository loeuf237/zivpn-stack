#!/usr/bin/env python3
import argparse
import json
import os
from pathlib import Path
import platform
import subprocess
from download import archive

ROOT=Path(__file__).resolve().parents[1]

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--arch',choices=['amd64','arm64']);args=parser.parse_args()
    host={'x86_64':'amd64','aarch64':'arm64'}.get(platform.machine())
    if host is None:raise SystemExit('Unsupported build architecture')
    lock=json.loads((ROOT/'config/artifacts.json').read_text());version=lock['go_version']
    cache=ROOT/'.cache'/('go'+version+'-'+host)
    go=cache/'go/bin/go'
    if not go.exists():archive(f'https://go.dev/dl/go{version}.linux-{host}.tar.gz',lock['go'][host],cache)
    output=ROOT/'build';output.mkdir(exist_ok=True)
    env=dict(os.environ,CGO_ENABLED='0',GOOS='linux',GOARCH=args.arch or host,GOTOOLCHAIN='local')
    subprocess.run([str(go),'build','-trimpath','-o',str(output/'zivpn-native'),'./cmd/zivpn-native'],cwd=ROOT/'native/core',env=env,check=True)
    print(output/'zivpn-native')

if __name__=='__main__':main()
