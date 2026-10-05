#!/usr/bin/env python3
"""Download pinned artifacts and validate archives before extraction."""
import hashlib
from pathlib import Path
import shutil
import tarfile
import urllib.request


def archive(url, checksum, destination):
    destination=Path(destination);destination.mkdir(parents=True,exist_ok=True)
    path=destination/'download.tar.gz'
    try:
        with urllib.request.urlopen(url,timeout=60) as response, path.open('wb') as output:
            shutil.copyfileobj(response,output)
        digest=hashlib.sha256(path.read_bytes()).hexdigest()
        if digest!=checksum:raise ValueError('Artifact checksum mismatch')
        with tarfile.open(path) as tar:
            for member in tar.getmembers():
                target=(destination/member.name).resolve()
                if not target.is_relative_to(destination.resolve()) or not (member.isfile() or member.isdir()):
                    raise ValueError('Unsafe archive member')
            tar.extractall(destination)
    finally:path.unlink(missing_ok=True)
