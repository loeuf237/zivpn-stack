import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import hashlib
from zivpn_settings import load_settings,default_interface
from download import archive
from install import validate_private

class DeploymentTests(unittest.TestCase):
    def test_settings_accept_ip_and_auto_interface(self):
        with tempfile.TemporaryDirectory() as directory:
            p=Path(directory)/'settings.json';p.write_text(json.dumps({'server_address':'203.0.113.10','primary_admin_id':123}))
            self.assertEqual(load_settings(p)['primary_admin_id'],123)
            with patch('zivpn_settings.subprocess.check_output',return_value=b'[{"dev":"ens5"}]'):
                self.assertEqual(default_interface(),'ens5')
    def test_settings_reject_invalid_identity_and_interface(self):
        with tempfile.TemporaryDirectory() as directory:
            p=Path(directory)/'settings.json'
            for settings in [dict(server_address='vpn.example.com',primary_admin_id=0),dict(server_address='$(id)',primary_admin_id=1),dict(server_address='vpn.example.com',primary_admin_id=True),dict(server_address='vpn.example.com',primary_admin_id=1,network_interface='../etc')]:
                p.write_text(json.dumps(settings))
                with self.assertRaises(ValueError):load_settings(p)
    def test_secret_file_permissions_required(self):
        with tempfile.TemporaryDirectory() as directory:
            p=Path(directory)/'secrets.json';p.write_text('{}');p.chmod(0o644)
            with self.assertRaises(ValueError):validate_private(p)
    def test_archive_rejects_traversal_even_with_valid_checksum(self):
        data=io.BytesIO()
        with tarfile.open(fileobj=data,mode='w:gz') as tar:
            member=tarfile.TarInfo('../escape');member.size=1;tar.addfile(member,io.BytesIO(b'x'))
        payload=data.getvalue()
        with tempfile.TemporaryDirectory() as directory,patch('download.urllib.request.urlopen',return_value=io.BytesIO(payload)):
            with self.assertRaises(ValueError):archive('https://example.invalid',hashlib.sha256(payload).hexdigest(),directory)
            self.assertFalse((Path(directory)/'download.tar.gz').exists())
    def test_archive_checksum_rejected(self):
        with tempfile.TemporaryDirectory() as directory,patch('download.urllib.request.urlopen',return_value=io.BytesIO(b'bad')):
            with self.assertRaises(ValueError):archive('https://example.invalid','0'*64,directory)

if __name__=='__main__':unittest.main()
