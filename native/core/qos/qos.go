// Package qos enforces payload ceilings after QUIC account authentication.
package qos

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"fmt"
	"net"
	"strings"
	"sync"
	"time"

	"github.com/apernet/hysteria/core/server"
	"golang.org/x/time/rate"
)

const StandardRate = 500000
const PremiumRate = 4000000
const Burst = 65536
const Chunk = 16384

type Counter struct {
	Up   uint64 `json:"up"`
	Down uint64 `json:"down"`
}
type SessionView struct {
	Counter
	Email           string               `json:"email"`
	IP              string               `json:"ip"`
	RemotePort      int                  `json:"remote_port"`
	Port            int                  `json:"port"`
	VIP             bool                 `json:"vip"`
	AuthenticatedMS int64                `json:"authenticated_ms"`
	LastPayloadMS   int64                `json:"last_payload_ms"`
	Transport       server.TransportView `json:"transport"`
}
type Snapshot struct {
	Epoch    string                 `json:"epoch"`
	Accounts map[string]Counter     `json:"accounts"`
	Servers  map[int]Counter        `json:"servers"`
	Sessions map[string]SessionView `json:"sessions"`
}
type bucket struct {
	limiter                         *rate.Limiter
	last                            time.Time
	counter                         Counter
	waitNS, waitCalls, delayedCalls uint64
}
type Manager struct {
	security          *SecurityObserver
	TransportRegistry *server.TransportRegistry
	started           int64
	auth              map[string]uint64
	closes            [closeCapacity]CloseView
	closeSequence     uint64
	mu                sync.Mutex
	epoch             string
	buckets           map[string]*bucket
	accounts          map[string]Counter
	servers           map[int]Counter
	sessions          map[string]*Session
}
type Session struct {
	manager        *Manager
	ctx            context.Context
	id, email, sid string
	vip            bool
	port           int
	addr           func() net.Addr
	close          func()
	started        int64
	counter        Counter
	lastPayload    int64
	transport      *server.TransportStats
}

func NewManager() *Manager {
	b := make([]byte, 16)
	if _, e := rand.Read(b); e != nil {
		panic(e)
	}
	return &Manager{security: newSecurityObserver(), TransportRegistry: &server.TransportRegistry{}, started: time.Now().UnixMilli(), auth: map[string]uint64{}, epoch: hex.EncodeToString(b), buckets: map[string]*bucket{},
		accounts: map[string]Counter{}, servers: map[int]Counter{}, sessions: map[string]*Session{}}
}
func ParseID(id string) (string, bool, error) {
	prefix, email, ok := strings.Cut(id, ":")
	if !ok || email == "" || (prefix != "P" && prefix != "S") {
		return "", false, fmt.Errorf("invalid authenticated ID")
	}
	return email, prefix == "P", nil
}
func host(addr net.Addr) string {
	h, _, e := net.SplitHostPort(addr.String())
	if e != nil {
		return addr.String()
	}
	return h
}
func (m *Manager) Attach(ctx context.Context, id string, addr func() net.Addr, close func(), port int) *Session {
	email, vip, e := ParseID(id)
	if e != nil {
		panic(e)
	}
	b := make([]byte, 16)
	if _, e = rand.Read(b); e != nil {
		panic(e)
	}
	s := &Session{manager: m, ctx: ctx, id: id, email: email, vip: vip, sid: hex.EncodeToString(b), port: port,
		addr: addr, close: close, started: time.Now().UnixMilli(), transport: m.TransportRegistry.Lookup(ctx)}
	m.mu.Lock()
	m.sessions[s.sid] = s
	m.mu.Unlock()
	go func() { <-ctx.Done(); m.finish(s) }()
	return s
}
func (s *Session) limiter() *rate.Limiter {
	key := s.bucketKey()
	speed := StandardRate
	if s.vip {
		key = "P:" + s.email
		speed = PremiumRate
	}
	m := s.manager
	m.mu.Lock()
	defer m.mu.Unlock()
	b := m.buckets[key]
	if b == nil {
		b = &bucket{limiter: rate.NewLimiter(rate.Limit(speed), Burst)}
		m.buckets[key] = b
	}
	b.last = time.Now()
	return b.limiter
}
func (s *Session) Wait(ctx context.Context, n int) error {
	limiter := s.limiter()
	start := time.Now()
	err := limiter.WaitN(ctx, n)
	elapsed := time.Since(start)
	m := s.manager
	m.mu.Lock()
	// Match the actual limiter, even if the connection changes its public IP.
	if b := m.buckets[s.bucketKey()]; b != nil && b.limiter == limiter {
		b.waitCalls++
		b.waitNS += uint64(elapsed)
		if elapsed > time.Millisecond {
			b.delayedCalls++
		}
	}
	m.mu.Unlock()
	return err
}
func (s *Session) Add(up, down int) {
	m := s.manager
	m.mu.Lock()
	defer m.mu.Unlock()
	if up+down > 0 {
		s.lastPayload = time.Now().UnixMilli()
	}
	if b := m.buckets[s.bucketKey()]; b != nil {
		b.counter.Up += uint64(up)
		b.counter.Down += uint64(down)
		b.last = time.Now()
	}
	s.counter.Up += uint64(up)
	s.counter.Down += uint64(down)
	a := m.accounts[s.email]
	a.Up += uint64(up)
	a.Down += uint64(down)
	m.accounts[s.email] = a
	b := m.servers[s.port]
	b.Up += uint64(up)
	b.Down += uint64(down)
	m.servers[s.port] = b
}
func (m *Manager) Snapshot() Snapshot {
	m.mu.Lock()
	defer m.mu.Unlock()
	out := Snapshot{Epoch: m.epoch, Accounts: map[string]Counter{}, Servers: map[int]Counter{}, Sessions: map[string]SessionView{}}
	for k, v := range m.accounts {
		out.Accounts[k] = v
	}
	for k, v := range m.servers {
		out.Servers[k] = v
	}
	for k, s := range m.sessions {
		a := s.addr()
		p := 0
		if u, ok := a.(*net.UDPAddr); ok {
			p = u.Port
		}
		out.Sessions[k] = SessionView{Counter: s.counter, Email: s.email, IP: host(a), RemotePort: p, Port: s.port, VIP: s.vip, AuthenticatedMS: s.started, LastPayloadMS: s.lastPayload, Transport: s.transport.View()}
	}
	// Idle buckets retain credits across quick reconnects, then expire only
	// when their existing bucket would be fully refilled anyway.
	for key, b := range m.buckets {
		if time.Since(b.last) > time.Minute {
			delete(m.buckets, key)
		}
	}
	return out
}
func (m *Manager) IDs() []string {
	m.mu.Lock()
	defer m.mu.Unlock()
	ids := map[string]bool{}
	for _, s := range m.sessions {
		ids[s.id] = true
	}
	out := []string{}
	for id := range ids {
		out = append(out, id)
	}
	return out
}
func (m *Manager) RevokeExcept(allowed map[string]bool) {
	m.mu.Lock()
	closes := []func(){}
	for _, s := range m.sessions {
		if permitted, checked := allowed[s.id]; checked && !permitted {
			closes = append(closes, s.close)
		}
	}
	m.mu.Unlock()
	for _, close := range closes {
		close()
	}
}

func (m *Manager) Kick(target string) int {
	m.mu.Lock()
	closes := []func(){}
	for _, s := range m.sessions {
		if target == "all" || s.email == target || host(s.addr()) == target {
			closes = append(closes, s.close)
		}
	}
	m.mu.Unlock()
	for _, close := range closes {
		close()
	}
	return len(closes)
}
func (s *Session) Wrap(base server.Outbound) server.Outbound {
	return &outbound{base: base, session: s}
}

type outbound struct {
	base    server.Outbound
	session *Session
}

func (o *outbound) TCP(addr string) (net.Conn, error) {
	c, e := o.base.TCP(addr)
	if e != nil {
		return nil, e
	}
	o.session.destination(addr, "tcp")
	_, dnsPort, _ := net.SplitHostPort(addr)
	ctx, cancel := context.WithCancel(o.session.ctx)
	return &limitedConn{Conn: c, s: o.session, ctx: ctx, cancel: cancel, dnsEnabled: dnsPort == "53"}, nil
}
func (o *outbound) UDP(addr string) (server.UDPConn, error) {
	c, e := o.base.UDP(addr)
	if e != nil {
		return nil, e
	}
	ctx, cancel := context.WithCancel(o.session.ctx)
	return &limitedUDP{UDPConn: c, s: o.session, ctx: ctx, cancel: cancel}, nil
}

type limitedConn struct {
	dnsEnabled bool
	dnsMu      sync.Mutex
	dnsBuffer  []byte
	net.Conn
	s      *Session
	ctx    context.Context
	cancel context.CancelFunc
}

func (c *limitedConn) Close() error { c.cancel(); return c.Conn.Close() }
func (c *limitedConn) Read(b []byte) (int, error) {
	if len(b) > Chunk {
		b = b[:Chunk]
	}
	n, e := c.Conn.Read(b)
	if n > 0 {
		if w := c.s.Wait(c.ctx, n); w != nil {
			return 0, w
		}
		c.s.Add(0, n)
	}
	return n, e
}
func (c *limitedConn) Write(b []byte) (int, error) {
	total := 0
	for len(b) > 0 {
		chunk := b
		if len(chunk) > Chunk {
			chunk = chunk[:Chunk]
		}
		if e := c.s.Wait(c.ctx, len(chunk)); e != nil {
			return total, e
		}
		n, e := c.Conn.Write(chunk)
		if n > 0 {
			c.observeDNS(chunk[:n])
		}
		c.s.Add(n, 0)
		total += n
		b = b[n:]
		if e != nil {
			return total, e
		}
		if n == 0 {
			return total, fmt.Errorf("zero-length write")
		}
	}
	return total, nil
}

type limitedUDP struct {
	server.UDPConn
	s      *Session
	ctx    context.Context
	cancel context.CancelFunc
}

func (c *limitedUDP) Close() error { c.cancel(); return c.UDPConn.Close() }
func (c *limitedUDP) ReadFrom(b []byte) (int, string, error) {
	n, a, e := c.UDPConn.ReadFrom(b)
	if n > 0 {
		if w := c.s.Wait(c.ctx, n); w != nil {
			return 0, "", w
		}
		c.s.Add(0, n)
	}
	return n, a, e
}
func (c *limitedUDP) WriteTo(b []byte, a string) (int, error) {
	if e := c.s.Wait(c.ctx, len(b)); e != nil {
		return 0, e
	}
	n, e := c.UDPConn.WriteTo(b, a)
	if n > 0 {
		c.s.destination(a, "udp")
		_, port, _ := net.SplitHostPort(a)
		if port == "53" {
			c.s.dns(b[:n], "udp")
		}
	}
	c.s.Add(n, 0)
	return n, e
}
