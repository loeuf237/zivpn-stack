package qos

import (
	"context"
	"testing"
)

func TestDynamicRateScopesAndMostRestrictiveSharedIP(t *testing.T) {
	m := NewManager()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	a := session(m, ctx, "S:one", "127.0.0.1")
	b := session(m, ctx, "S:two", "127.0.0.1")
	c := session(m, ctx, "S:one", "127.0.0.2")
	p := session(m, ctx, "P:vip", "127.0.0.1")
	original := a.limiter()
	if e := m.ApplyPolicies(Rates{StandardRate, PremiumRate}, map[string]int{"S:one": 2000000, "S:two": 750000, "P:vip": 6000000}); e != nil {
		t.Fatal(e)
	}
	if a.limiter() != original || a.limiter() != b.limiter() || a.limiter().Limit() != 750000 || c.limiter().Limit() != 2000000 || p.limiter().Limit() != 6000000 {
		t.Fatal("wrong scope or live rate update")
	}
	if m.Health().Buckets["S:127.0.0.1"].Limit != 750000 {
		t.Fatal("misleading effective ceiling")
	}
	before := m.Health().Buckets["S:127.0.0.1"].Generation
	if e := m.ApplyPolicies(Rates{StandardRate, PremiumRate}, map[string]int{"S:one": 250000, "S:two": 750000, "P:vip": 3000000}); e != nil {
		t.Fatal(e)
	}
	if a.limiter().Limit() != 250000 || p.limiter().Limit() != 3000000 || m.Health().Buckets["S:127.0.0.1"].Generation == before {
		t.Fatal("live policy change not applied")
	}
	m.mu.Lock()
	delete(m.sessions, a.sid)
	m.refreshRatesLocked()
	m.mu.Unlock()
	if b.limiter().Limit() != 750000 {
		t.Fatal("departed account still restricts shared IP")
	}
	if e := m.ApplyPolicies(Rates{0, 4000000}, map[string]int{}); e == nil {
		t.Fatal("invalid policy admitted")
	}
	if b.limiter().Limit() != 750000 {
		t.Fatal("invalid policy replaced valid policy")
	}
}
