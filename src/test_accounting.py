import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import zivpn_accounting as accounting


class AccountingTests(unittest.TestCase):
    def setUp(self):
        native = patch.object(accounting, 'native_enabled', return_value=False)
        native.start()
        self.addCleanup(native.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = str(Path(self.tmp.name)/"test.db")
        self.conn = sqlite3.connect(self.db)
        self.addCleanup(self.conn.close)
        self.conn.executescript("""
            CREATE TABLE clients(email TEXT PRIMARY KEY);
            CREATE TABLE client_traffics(email TEXT PRIMARY KEY, inbound_id INTEGER,
                up INTEGER, down INTEGER, last_online INTEGER);
            CREATE TABLE inbounds(tag TEXT PRIMARY KEY, up INTEGER, down INTEGER);
            INSERT INTO clients VALUES ('alice');
            INSERT INTO clients VALUES ('bob');
            INSERT INTO client_traffics VALUES ('alice',1,0,0,0);
            INSERT INTO client_traffics VALUES ('bob',2,0,0,0);
            INSERT INTO inbounds VALUES ('inbound-zivpn',0,0);
            INSERT INTO inbounds VALUES ('inbound-zivpn-limited',0,0);
        """)
        accounting.ensure_schema(self.conn)
        self.conn.commit()
        self.epochs = {5667: "server-A", 5668: "server-B"}
        self.now = 1000

    def flow(self, key="one", source_port=1234, server_port=5667, up=100, down=200):
        return {"key": key, "ip": "203.0.113.10", "port": source_port,
                "server_port": server_port, "up": up, "down": down}

    def sample(self, flows, baseline=False):
        self.now += 1
        with self.conn:
            return accounting.account_snapshot(self.conn,flows,self.epochs,self.now,baseline)

    def bind(self, flow, email):
        with patch.object(accounting,"read_flows",return_value=[flow]), \
             patch.object(accounting,"process_epoch",return_value=self.epochs[flow["server_port"]]), self.conn:
            accounting.record_session(self.conn, f"{flow['ip']}:{flow['port']}",
                                      flow["server_port"], email, self.now)

    def counters(self, email):
        return self.conn.execute("SELECT up,down FROM client_traffics WHERE email=?",(email,)).fetchone()

    def test_repeated_snapshot_counts_only_difference(self):
        initial = self.flow()
        self.sample([initial],baseline=True)
        self.bind(initial,"alice")
        next_flow = self.flow(up=140,down=270)
        result=self.sample([next_flow])
        self.assertEqual((result["up"],result["down"]),(40,70))
        self.sample([next_flow])
        self.assertEqual(self.counters("alice"),(40,70))

    def test_same_public_ip_two_accounts_remain_distinct(self):
        a,b=self.flow(),self.flow("two",5678)
        self.sample([a,b],baseline=True)
        self.bind(a,"alice");self.bind(b,"bob")
        self.sample([self.flow(up=130,down=250),self.flow("two",5678,up=110,down=220)])
        self.assertEqual(self.counters("alice"),(30,50))
        self.assertEqual(self.counters("bob"),(10,20))

    def test_vip_account_on_standard_port_credited_to_vip_account(self):
        f=self.flow();self.sample([f],baseline=True);self.bind(f,"bob")
        self.sample([self.flow(up=120,down=240)])
        self.assertEqual(self.counters("bob"),(20,40))
        self.assertEqual(self.conn.execute("SELECT up,down FROM inbounds WHERE tag='inbound-zivpn'").fetchone(),(20,40))

    def test_unknown_socket_not_guessed_from_ip(self):
        f=self.flow();self.sample([f],baseline=True);self.bind(f,"alice")
        self.sample([f,self.flow("unknown",9876,up=10,down=20)])
        self.assertEqual(self.counters("alice"),(0,0))
        meta=dict(self.conn.execute("SELECT key,value FROM zivpn_accounting_meta"))
        self.assertEqual((int(meta["unattributed_up"]),int(meta["unattributed_down"])),(10,20))

    def test_daemon_restart_does_not_recount(self):
        f=self.flow();self.sample([f],baseline=True);self.bind(f,"alice")
        f=self.flow(up=150,down=300);self.sample([f])
        second=sqlite3.connect(self.db)
        try:
            with second:accounting.account_snapshot(second,[f],self.epochs,9999)
        finally:second.close()
        self.assertEqual(self.counters("alice"),(50,100))

    def test_server_restart_invalidates_old_binding(self):
        f=self.flow();self.sample([f],baseline=True);self.bind(f,"alice")
        self.epochs[5667]="server-new"
        self.sample([self.flow(up=120,down=240)])
        self.assertEqual(self.counters("alice"),(0,0))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM zivpn_sessions").fetchone()[0],0)

    def test_socket_reassigned_keeps_previous_bytes_on_previous_account(self):
        f=self.flow();self.sample([f],baseline=True);self.bind(f,"alice")
        later=self.flow(up=120,down=240)
        self.bind(later,"bob")
        self.sample([self.flow(up=150,down=300)])
        self.assertEqual(self.counters("alice"),(0,0))
        self.assertEqual(self.counters("bob"),(0,0))
        self.sample([self.flow(up=160,down=320)])
        self.assertEqual(self.counters("bob"),(10,20))

    def test_transaction_rollback_retries_once(self):
        f=self.flow();self.sample([f],baseline=True);self.bind(f,"alice")
        later=self.flow(up=120,down=240)
        try:
            with self.conn:
                accounting.account_snapshot(self.conn,[later],self.epochs,2000)
                raise RuntimeError("simulated interruption")
        except RuntimeError:pass
        self.assertEqual(self.counters("alice"),(0,0))
        self.sample([later]);self.sample([later])
        self.assertEqual(self.counters("alice"),(20,40))

    def test_conntrack_failure_keeps_counters_and_checkpoints(self):
        f=self.flow();self.sample([f],baseline=True);self.bind(f,"alice")
        before=self.conn.execute("SELECT * FROM zivpn_traffic_checkpoints").fetchall()
        with patch.object(accounting,"read_flows",side_effect=subprocess.CalledProcessError(1,"conntrack")):
            with self.assertRaises(subprocess.CalledProcessError):accounting.sync_traffic(self.db)
        self.assertEqual(self.conn.execute("SELECT * FROM zivpn_traffic_checkpoints").fetchall(),before)
        self.assertEqual(self.counters("alice"),(0,0))

    def test_new_flow_identity_counts_new_lifetime_once(self):
        f=self.flow();self.sample([f],baseline=True);self.bind(f,"alice")
        fresh=self.flow("new-id",up=10,down=20)
        self.sample([fresh]);self.sample([fresh])
        self.assertEqual(self.counters("alice"),(10,20))

    def test_counter_reset_does_not_add_previous_total_again(self):
        f=self.flow();self.sample([f],baseline=True);self.bind(f,"alice")
        fresh=self.flow(up=10,down=20)
        self.sample([fresh]);self.sample([fresh])
        self.assertEqual(self.counters("alice"),(10,20))

    def test_live_view_uses_authenticated_socket(self):
        f=self.flow();self.sample([f],baseline=True);self.bind(f,"bob")
        unknown=self.flow("unknown",5678)
        with patch.object(accounting,"read_flows",return_value=[f,unknown]), \
             patch.object(accounting,"server_epochs",return_value=self.epochs):
            rows=list(accounting.live_connections(self.db).values())
        self.assertEqual([r["email"] for r in rows],["bob",None])


class ParserTests(unittest.TestCase):
    def line(self, destination="172.16.0.4", source="203.0.113.10", remote_port=1234,
             server_port=5667, original_port=10000, flow_id=123, up=100, down=200):
        return (f"ipv4 2 udp 17 30 src={source} dst={destination} sport={remote_port} dport={original_port} packets=2 bytes={up} "
                f"src={destination} dst={source} sport={server_port} dport={remote_port} packets=3 bytes={down} mark=0 id={flow_id}")

    def parse(self,line):
        return accounting.parse_flows(line,{"172.16.0.4","::1"},"boot-test")

    def test_redirected_udp_port_and_direction(self):
        f=self.parse(self.line())[0]
        self.assertEqual((f["port"],f["server_port"],f["up"],f["down"],f["packets"]),(1234,5667,100,200,5))

    def test_vip_port_and_ipv6(self):
        f=self.parse(self.line(destination="::1",source="2001:db8::1",server_port=5668))[0]
        self.assertEqual(f["ip"],"2001:db8::1")
        self.assertEqual(f["server_port"],5668)

    def test_outgoing_or_similar_port_excluded(self):
        self.assertEqual(self.parse(self.line(destination="203.0.113.50")),[])
        self.assertEqual(self.parse(self.line(server_port=56670)),[])

    def test_missing_counter_or_id_rejects_sample(self):
        for line in (self.line().replace(" bytes=200",""),self.line().replace(" id=123","")):
            with self.assertRaises(ValueError):self.parse(line)

    def test_flow_id_and_boot_separate_reused_socket(self):
        first=self.parse(self.line())[0]["key"]
        self.assertNotEqual(first,self.parse(self.line(flow_id=124))[0]["key"])
        self.assertNotEqual(first,accounting.parse_flows(self.line(),{"172.16.0.4"},"new-boot")[0]["key"])

    def test_ipv4_and_ipv6_socket_extraction(self):
        self.assertEqual(accounting.socket_address("203.0.113.10:1234"),("203.0.113.10",1234))
        self.assertEqual(accounting.socket_address("[2001:db8::1]:1234"),("2001:db8::1",1234))


if __name__=="__main__":
    unittest.main()
