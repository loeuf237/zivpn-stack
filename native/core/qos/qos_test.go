package qos

import (
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"math/big"
	"net"
	"sync"
	"testing"
	"time"

	"github.com/apernet/hysteria/core/client"
	"github.com/apernet/hysteria/core/server"
)

func session(m *Manager, ctx context.Context, id, ip string) *Session {
	return m.Attach(ctx, id, func() net.Addr { return &net.UDPAddr{IP: net.ParseIP(ip), Port: 12345} }, func() {}, 5667)
}
func TestBucketsByAccountAndIP(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	m := NewManager()
	p1 := session(m, ctx, "P:one", "127.0.0.1")
	p2 := session(m, ctx, "P:one", "127.0.0.2")
	p3 := session(m, ctx, "P:two", "127.0.0.1")
	s1 := session(m, ctx, "S:free", "127.0.0.1")
	s2 := session(m, ctx, "S:other-free", "127.0.0.1")
	s3 := session(m, ctx, "S:free", "127.0.0.2")
	if p1.limiter() != p2.limiter() {
		t.Fatal("Shared Premium account has different buckets")
	}
	if p1.limiter() == p3.limiter() {
		t.Fatal("Separate Premium accounts share a bucket")
	}
	if s1.limiter() != s2.limiter() {
		t.Fatal("Same-IP Standards have different buckets")
	}
	if s1.limiter() == s3.limiter() || s1.limiter() == p1.limiter() {
		t.Fatal("IP or tier collision")
	}
	if p1.limiter().Limit() != PremiumRate || s1.limiter().Limit() != StandardRate {
		t.Fatal("Wrong rates")
	}
}
func TestMigrationReconnectAndCancellation(t *testing.T) {
	m := NewManager()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	ip := "127.0.0.1"
	s := m.Attach(ctx, "S:free", func() net.Addr { return &net.UDPAddr{IP: net.ParseIP(ip)} }, func() {}, 5667)
	initial := s.limiter()
	ip = "127.0.0.2"
	if initial == s.limiter() {
		t.Fatal("Migration kept old Standard IP")
	}
	p := session(m, ctx, "P:one", "127.0.0.1")
	bucket := p.limiter()
	if e := p.Wait(ctx, Burst); e != nil {
		t.Fatal(e)
	}
	p2 := session(m, ctx, "P:one", "127.0.0.2")
	if p2.limiter() != bucket {
		t.Fatal("Reconnect reset credits")
	}
	cancelled, c := context.WithCancel(ctx)
	c()
	if e := p2.Wait(cancelled, Chunk); e == nil {
		t.Fatal("Cancelled wait succeeded")
	}
}
func TestRevocationSnapshotAndNewLoginRace(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	m := NewManager()
	closed := 0
	s := m.Attach(ctx, "P:one", func() net.Addr { return &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1), Port: 1234} }, func() { closed++ }, 5667)
	s.Add(100, 200)
	m.RevokeExcept(map[string]bool{"S:other": false})
	if closed != 0 {
		t.Fatal("Fresh login revoked")
	}
	m.RevokeExcept(map[string]bool{"P:one": false})
	if closed != 1 {
		t.Fatal("Revocation missed")
	}
	snap := m.Snapshot()
	if snap.Accounts["one"].Up != 100 || snap.Accounts["one"].Down != 200 || snap.Servers[5667].Down != 200 {
		t.Fatal("Wrong counters")
	}
	if len(snap.Sessions) != 1 {
		t.Fatal("Wrong session count")
	}
	for _, s := range snap.Sessions {
		if s.Email != "one" || !s.VIP || s.RemotePort != 1234 {
			t.Fatal("Wrong metadata")
		}
	}
}

func TestKickAccountDoesNotKickSameIPStandard(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	m := NewManager()
	premiumClosed, standardClosed := 0, 0
	addr := func() net.Addr { return &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1)} }
	for i := 0; i < 2; i++ {
		m.Attach(ctx, "P:one", addr, func() { premiumClosed++ }, 5667)
	}
	m.Attach(ctx, "S:free", addr, func() { standardClosed++ }, 5667)
	if m.Kick("one") != 2 || premiumClosed != 2 || standardClosed != 0 {
		t.Fatal("Account kick crossed IP/tier boundary")
	}
}

type fixtureAuth struct{}

func (fixtureAuth) Authenticate(_ net.Addr, auth string, _ uint64) (bool, string) {
	_, _, e := ParseID(auth)
	return e == nil, auth
}

type boundFactory struct{ ip string }

func (f boundFactory) New(_ net.Addr) (net.PacketConn, error) {
	return net.ListenUDP("udp4", &net.UDPAddr{IP: net.ParseIP(f.ip)})
}

func TestTunneledSharedCeilingsAndMixedIP(t *testing.T) {
	key, e := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if e != nil {
		t.Fatal(e)
	}
	template := &x509.Certificate{SerialNumber: big.NewInt(1), NotBefore: time.Now().Add(-time.Hour), NotAfter: time.Now().Add(time.Hour)}
	der, e := x509.CreateCertificate(rand.Reader, template, template, &key.PublicKey, key)
	if e != nil {
		t.Fatal(e)
	}
	cert := tls.Certificate{Certificate: [][]byte{der}, PrivateKey: key}
	m := NewManager()
	servers := []server.Server{}
	addresses := []net.Addr{}
	for i := 0; i < 2; i++ {
		pkt, e := net.ListenUDP("udp4", &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1)})
		if e != nil {
			t.Fatal(e)
		}
		port := pkt.LocalAddr().(*net.UDPAddr).Port
		cfg := &server.Config{Conn: pkt, TLSConfig: server.TLSConfig{Certificates: []tls.Certificate{cert}}, IgnoreClientBandwidth: true, Authenticator: fixtureAuth{}, TransportRegistry: m.TransportRegistry}
		cfg.AuthenticatedOutbound = func(ctx context.Context, id string, addr func() net.Addr, close func(), base server.Outbound) server.Outbound {
			return m.Attach(ctx, id, addr, close, port).Wrap(base)
		}
		srv, e := server.NewServer(cfg)
		if e != nil {
			t.Fatal(e)
		}
		servers = append(servers, srv)
		addresses = append(addresses, pkt.LocalAddr())
		go srv.Serve()
	}
	defer func() {
		for _, s := range servers {
			s.Close()
		}
	}()
	target, e := net.Listen("tcp4", "127.0.0.1:0")
	if e != nil {
		t.Fatal(e)
	}
	defer target.Close()
	go func() {
		for {
			c, e := target.Accept()
			if e != nil {
				return
			}
			go func() {
				defer c.Close()
				b := make([]byte, 16384)
				for {
					if _, e = c.Write(b); e != nil {
						return
					}
				}
			}()
		}
	}()
	type flow struct {
		id, ip, group string
		listener      int
	}
	specs := []flow{{"P:vip-one", "127.0.0.1", "vip-shared", 0}, {"P:vip-one", "127.0.0.2", "vip-shared", 1},
		{"P:vip-two", "127.0.0.1", "vip-separate", 0}, {"S:free-one", "127.0.0.1", "std-shared", 0},
		{"S:free-two", "127.0.0.1", "std-shared", 0}, {"S:free-one", "127.0.0.2", "std-separate", 0}}
	clients := []client.Client{}
	streams := []net.Conn{}
	defer func() {
		for _, c := range clients {
			c.Close()
		}
	}()
	for _, spec := range specs {
		c, _, e := client.NewClient(&client.Config{ServerAddr: addresses[spec.listener], Auth: spec.id, ConnFactory: boundFactory{spec.ip}, TLSConfig: client.TLSConfig{InsecureSkipVerify: true}})
		if e != nil {
			t.Fatal(e)
		}
		clients = append(clients, c)
		stream, e := c.TCP(target.Addr().String())
		if e != nil {
			t.Fatal(e)
		}
		streams = append(streams, stream)
	}
	start := time.Now()
	var wg sync.WaitGroup
	var mu sync.Mutex
	totals := map[string]int{}
	for i, stream := range streams {
		i, stream := i, stream
		wg.Add(1)
		go func() {
			defer wg.Done()
			defer stream.Close()
			stream.SetReadDeadline(start.Add(4 * time.Second))
			b := make([]byte, 32768)
			count := 0
			for {
				n, e := stream.Read(b)
				if time.Since(start) >= time.Second {
					count += n
				}
				if e != nil {
					break
				}
			}
			mu.Lock()
			totals[specs[i].group] += count
			mu.Unlock()
		}()
	}
	wg.Wait()
	for group, n := range totals {
		got := float64(n) / 3
		want := float64(StandardRate)
		if group[:3] == "vip" {
			want = PremiumRate
		}
		t.Logf("%s: %.0f B/s, ceiling %.0f", group, got, want)
		if got < want*.80 || got > want*1.08 {
			t.Errorf("%s outside expected range: %.0f", group, got)
		}
	}
	snap := m.Snapshot()
	if snap.Accounts["vip-one"].Down == 0 || snap.Accounts["free-one"].Down == 0 {
		t.Fatal("Missing account telemetry")
	}
	for _, view := range snap.Sessions {
		if view.Transport.Sent == 0 || view.Transport.RTTMS <= 0 || view.LastPayloadMS == 0 {
			t.Fatalf("missing live transport metrics: %+v", view)
		}
	}
	if e := m.ApplyPolicies(Rates{StandardRate, PremiumRate}, map[string]int{"P:vip-one": 2000000, "S:free-one": 250000, "S:free-two": 500000}); e != nil {
		t.Fatal(e)
	}
	var customWG sync.WaitGroup
	for _, spec := range []struct {
		index int
		speed float64
	}{{0, 2000000}, {3, 250000}} {
		spec := spec
		customWG.Add(1)
		go func() {
			defer customWG.Done()
			stream, err := clients[spec.index].TCP(target.Addr().String())
			if err != nil {
				t.Error(err)
				return
			}
			defer stream.Close()
			start := time.Now()
			stream.SetReadDeadline(start.Add(3 * time.Second))
			count := 0
			buf := make([]byte, 32768)
			for {
				n, err := stream.Read(buf)
				if time.Since(start) >= time.Second {
					count += n
				}
				if err != nil {
					break
				}
			}
			got := float64(count) / 2
			t.Logf("custom live rate: %.0f B/s, ceiling %.0f", got, spec.speed)
			if got < spec.speed*.8 || got > spec.speed*1.1 {
				t.Errorf("custom rate outside expected range: %.0f", got)
			}
		}()
	}
	customWG.Wait()
	if e := m.ApplyPolicies(Rates{StandardRate, PremiumRate}, map[string]int{}); e != nil {
		t.Fatal(e)
	}
	// Both UDP forwarding and TCP share the same Session limiter in Wrap.
	udp, e := net.ListenUDP("udp4", &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1)})
	if e != nil {
		t.Fatal(e)
	}
	defer udp.Close()
	go func() {
		b := make([]byte, 2048)
		n, a, e := udp.ReadFromUDP(b)
		if e == nil {
			udp.WriteToUDP(b[:n], a)
		}
	}()
	cu, e := clients[0].UDP()
	if e != nil {
		t.Fatal(e)
	}
	defer cu.Close()
	if e = cu.Send([]byte("udp-verified"), udp.LocalAddr().String()); e != nil {
		t.Fatal(e)
	}
	done := make(chan bool, 1)
	go func() { b, _, e := cu.Receive(); done <- e == nil && string(b) == "udp-verified" }()
	select {
	case ok := <-done:
		if !ok {
			t.Fatal("UDP echo mismatch")
		}
	case <-time.After(3 * time.Second):
		t.Fatal("UDP forwarding timed out")
	}
}
