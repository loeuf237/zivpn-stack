#!/usr/bin/env python3
"""Check Git blobs without printing matched credentials. --live is root-only."""
import argparse
from pathlib import Path
import re
import sqlite3
import subprocess

ROOT=Path(__file__).resolve().parents[1]


def blobs(staged):
    command=['git','-c','safe.directory='+str(ROOT),'ls-files','-z'] if not staged else ['git','-c','safe.directory='+str(ROOT),'diff','--cached','--name-only','--diff-filter=ACMR','-z']
    names=subprocess.check_output(command,cwd=ROOT).decode().split('\0')
    for name in filter(None,names):
        if staged:
            data=subprocess.check_output(['git','-c','safe.directory='+str(ROOT),'show',':'+name],cwd=ROOT)
        else:data=(ROOT/name).read_bytes()
        yield name,data


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--staged',action='store_true');parser.add_argument('--live',action='store_true');args=parser.parse_args()
    private=[]
    if args.live:
        for path in ['/etc/zivpn/telegram.token','/etc/zivpn/zivpn.key']:
            private.append(Path(path).read_bytes().strip())
        with sqlite3.connect('file:/etc/x-ui/x-ui.db?mode=ro',uri=True) as conn:
            private += [row[0].encode() for row in conn.execute("SELECT password FROM clients WHERE password IS NOT NULL AND password != ''")]
            private += [row[0].encode() for row in conn.execute("SELECT value FROM settings WHERE key='tgBotToken' AND value != ''")]
    rejected=[];count=0
    for name,data in blobs(args.staged):
        count+=1
        blocked=bool(re.search(r'(^|/)(\.env(?:\..*)?|clients\.csv|hosts\.yml)$|\.(db(?:-.*)?|sqlite\w*|token|key|pem|crt)$',name))
        token=bool(re.search(rb'\b[0-9]{6,}:[A-Za-z0-9_-]{30,}\b',data))
        key=(b'-----BEGIN '+b'PRIVATE KEY-----') in data or (b'-----BEGIN '+b'RSA PRIVATE KEY-----') in data
        live=any(value and value in data for value in private)
        if blocked or token or key or live:rejected.append(name)
    if rejected:
        for name in rejected:print('BLOCKED sensitive material: '+name)
        raise SystemExit(1)
    print(f'Secret scan passed: {count} files; live comparison={args.live}.')

if __name__=='__main__':main()
