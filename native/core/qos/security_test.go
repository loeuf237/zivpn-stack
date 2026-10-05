package qos

import (
	"context"
	"encoding/binary"
	"golang.org/x/net/dns/dnsmessage"
	"net"
	"testing"
	"time"
)

func securitySession() *Session {
	return NewManager().Attach(context.Background(), "S:alice", func() net.Addr { return &net.UDPAddr{IP: net.ParseIP("192.0.2.1"), Port: 1} }, func() {}, 5667)
}
func dnsQuery(t *testing.T) []byte {
	t.Helper()
	name, e := dnsmessage.NewName("www.youtube.com.")
	if e != nil {
		t.Fatal(e)
	}
	m := dnsmessage.Message{Header: dnsmessage.Header{ID: 1}, Questions: []dnsmessage.Question{{Name: name, Type: dnsmessage.TypeA, Class: dnsmessage.ClassINET}}}
	b, e := m.Pack()
	if e != nil {
		t.Fatal(e)
	}
	return b
}
func TestSecurityAccountAndDNSFraming(t *testing.T) {
	s := securitySession()
	s.destination("example.org:443", "tcp")
	c := &limitedConn{s: s, dnsEnabled: true}
	b := dnsQuery(t)
	frame := make([]byte, 2)
	binary.BigEndian.PutUint16(frame, uint16(len(b)))
	frame = append(frame, b...)
	c.observeDNS(frame[:7])
	c.observeDNS(frame[7:])
	if len(c.dnsBuffer) != 0 {
		t.Fatal("DNS payload retained")
	}
	rr := s.manager.SecuritySnapshot()["records"].([]SecurityRecord)
	if len(rr) != 2 {
		t.Fatal(rr)
	}
	for _, r := range rr {
		if r.Email != "alice" {
			t.Fatal("wrong attribution")
		}
		if r.Kind == "dns" && r.Host != "www.youtube.com" {
			t.Fatal(r)
		}
	}
}
func TestSecurityMalformedAnswerAndExpiry(t *testing.T) {
	s := securitySession()
	s.dns([]byte("not DNS"), "udp")
	b := dnsQuery(t)
	b[2] |= 0x80
	s.dns(b, "udp")
	if len(s.manager.SecuritySnapshot()["records"].([]SecurityRecord)) != 0 {
		t.Fatal("answer recorded")
	}
	o := s.manager.security
	o.records["expired"] = SecurityRecord{FirstMS: time.Now().Add(-31 * time.Minute).UnixMilli(), LastMS: time.Now().UnixMilli()}
	if len(s.manager.SecuritySnapshot()["records"].([]SecurityRecord)) != 0 {
		t.Fatal("retention exceeded")
	}
}
func TestSecurityBoundAndDomainIsolation(t *testing.T) {
	o := newSecurityObserver()
	for i := 0; i < securityCapacity+5; i++ {
		o.record("a", "destination", "tcp", net.IPv4(192, 0, byte(i/256), byte(i%256)).String(), "443")
	}
	if len(o.records) > securityCapacity {
		t.Fatal("unbounded")
	}
	o.record("b", "dns", "udp", "private.example", "53")
	found := false
	for _, r := range o.records {
		if r.Email == "b" && r.Host == "private.example" {
			found = true
		}
	}
	if !found {
		t.Fatal("account not recorded")
	}
}
