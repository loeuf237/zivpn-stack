package qos

import (
	"encoding/binary"
	"golang.org/x/net/dns/dnsmessage"
	"net"
	"strings"
	"sync"
	"time"
)

const securityRetention = 30 * time.Minute
const securityCapacity = 4096

type SecurityRecord struct {
	Email    string `json:"email"`
	Kind     string `json:"kind"`
	Protocol string `json:"protocol"`
	Host     string `json:"host"`
	Port     string `json:"port"`
	Hits     uint64 `json:"hits"`
	LastMS   int64  `json:"last_ms"`
	FirstMS  int64  `json:"first_ms"`
}
type SecurityObserver struct {
	mu      sync.Mutex
	records map[string]SecurityRecord
}

func newSecurityObserver() *SecurityObserver {
	return &SecurityObserver{records: map[string]SecurityRecord{}}
}
func (o *SecurityObserver) record(email, kind, protocol, host, port string) {
	host = strings.TrimSuffix(strings.ToLower(host), ".")
	if len(host) == 0 || len(host) > 253 || strings.ContainsAny(host, "\n\r\x00") {
		return
	}
	now := time.Now().UnixMilli()
	key := email + "\x00" + kind + "\x00" + protocol + "\x00" + host + "\x00" + port
	o.mu.Lock()
	defer o.mu.Unlock()
	for k, r := range o.records {
		if now-r.FirstMS > securityRetention.Milliseconds() {
			delete(o.records, k)
		}
	}
	r, exists := o.records[key]
	if !exists && len(o.records) >= securityCapacity {
		var oldest string
		stamp := now + 1
		for k, v := range o.records {
			if v.LastMS < stamp {
				oldest = k
				stamp = v.LastMS
			}
		}
		delete(o.records, oldest)
	}
	if !exists {
		r = SecurityRecord{Email: email, Kind: kind, Protocol: protocol, Host: host, Port: port, FirstMS: now}
	}
	r.Hits++
	r.LastMS = now
	o.records[key] = r
}
func (s *Session) destination(addr, protocol string) {
	h, p, e := net.SplitHostPort(addr)
	if e != nil {
		return
	}
	s.manager.security.record(s.email, "destination", protocol, h, p)
}
func (s *Session) dns(packet []byte, protocol string) {
	// Only standard, plaintext DNS questions. Never retain payloads or answers.
	if len(packet) > 65535 {
		return
	}
	var p dnsmessage.Parser
	header, e := p.Start(packet)
	if e != nil || header.Response {
		return
	}
	for i := 0; i < 16; i++ {
		q, e := p.Question()
		if e != nil {
			break
		}
		s.manager.security.record(s.email, "dns", protocol, q.Name.String(), "53")
	}
}
func (m *Manager) SecuritySnapshot() map[string]interface{} {
	o := m.security
	o.mu.Lock()
	defer o.mu.Unlock()
	now := time.Now().UnixMilli()
	records := make([]SecurityRecord, 0, len(o.records))
	for k, r := range o.records {
		if now-r.FirstMS > securityRetention.Milliseconds() {
			delete(o.records, k)
		} else {
			records = append(records, r)
		}
	}
	return map[string]interface{}{"records": records, "retention_seconds": 1800, "capacity": securityCapacity, "observed_ms": now, "encrypted_content": false}
}

// TCP DNS uses two-byte message framing and can span multiple writes.
func (c *limitedConn) observeDNS(b []byte) {
	if !c.dnsEnabled {
		return
	}
	c.dnsMu.Lock()
	defer c.dnsMu.Unlock()
	if len(c.dnsBuffer)+len(b) > 131074 {
		c.dnsBuffer = nil
		return
	}
	c.dnsBuffer = append(c.dnsBuffer, b...)
	for len(c.dnsBuffer) >= 2 {
		size := int(binary.BigEndian.Uint16(c.dnsBuffer[:2]))
		if len(c.dnsBuffer) < size+2 {
			break
		}
		c.s.dns(c.dnsBuffer[2:2+size], "tcp")
		c.dnsBuffer = c.dnsBuffer[2+size:]
	}
	if len(c.dnsBuffer) == 0 {
		c.dnsBuffer = nil
	}
}
