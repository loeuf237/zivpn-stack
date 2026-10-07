package qos

import (
	"context"
	"time"

	"github.com/apernet/hysteria/core/server"
)

const closeCapacity = 8192
const closeRetention = 48 * time.Hour

type CloseView struct {
	Sequence        uint64  `json:"sequence"`
	ClosedMS        int64   `json:"closed_ms"`
	Email           string  `json:"email"`
	IP              string  `json:"ip"`
	LifetimeSeconds float64 `json:"lifetime_seconds"`
	LastPayloadMS   int64   `json:"last_payload_ms"`
	Reason          string  `json:"reason"`
	Counter
	Transport server.TransportView `json:"transport"`
}
type BucketView struct {
	Generation uint64 `json:"generation"`
	Key        string `json:"key"`
	Limit      int    `json:"limit_bytes_per_second"`
	Sessions   int    `json:"sessions"`
	Counter
	WaitNS       uint64 `json:"wait_ns"`
	WaitCalls    uint64 `json:"wait_calls"`
	DelayedCalls uint64 `json:"delayed_calls"`
}
type HealthView struct {
	Defaults      Rates                 `json:"defaults"`
	Epoch         string                `json:"epoch"`
	StartedMS     int64                 `json:"started_ms"`
	ObservedMS    int64                 `json:"observed_ms"`
	Auth          map[string]uint64     `json:"auth"`
	Closes        []CloseView           `json:"closes"`
	CloseSequence uint64                `json:"close_sequence"`
	Buckets       map[string]BucketView `json:"buckets"`
}

func (s *Session) bucketKey() string {
	if s.vip {
		return "P:" + s.email
	}
	return "S:" + host(s.addr())
}
func (m *Manager) RecordAuth(reason string) {
	m.mu.Lock()
	m.auth[reason]++
	m.mu.Unlock()
}
func (m *Manager) finish(s *Session) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if _, exists := m.sessions[s.sid]; !exists {
		return
	}
	delete(m.sessions, s.sid)
	m.refreshRatesLocked()
	m.closeSequence++
	now := time.Now().UnixMilli()
	v := CloseView{Sequence: m.closeSequence, ClosedMS: now, Email: s.email, IP: host(s.addr()),
		LifetimeSeconds: float64(now-s.started) / 1000, LastPayloadMS: s.lastPayload,
		Reason: server.ClassifyClose(context.Cause(s.ctx)), Counter: s.counter, Transport: s.transport.View()}
	// Fixed-size ring: closing a tunnel never shifts thousands of records.
	m.closes[(m.closeSequence-1)%closeCapacity] = v
}
func (m *Manager) Health() HealthView {
	m.mu.Lock()
	defer m.mu.Unlock()
	now := time.Now().UnixMilli()
	out := HealthView{Defaults: m.defaults, Epoch: m.epoch, StartedMS: m.started, ObservedMS: now,
		Auth: map[string]uint64{}, Closes: []CloseView{}, CloseSequence: m.closeSequence, Buckets: map[string]BucketView{}}
	for k, v := range m.auth {
		out.Auth[k] = v
	}
	first := uint64(1)
	if m.closeSequence >= closeCapacity {
		first = m.closeSequence - closeCapacity + 1
	}
	for seq := first; seq <= m.closeSequence; seq++ {
		v := m.closes[(seq-1)%closeCapacity]
		if now-v.ClosedMS <= closeRetention.Milliseconds() {
			out.Closes = append(out.Closes, v)
		}
	}
	for k, b := range m.buckets {
		if time.Since(b.last) > time.Minute {
			continue
		}
		speed := int(b.limiter.Limit())
		out.Buckets[k] = BucketView{Generation: b.generation, Key: k, Limit: speed, Counter: b.counter, WaitNS: b.waitNS, WaitCalls: b.waitCalls, DelayedCalls: b.delayedCalls}
	}
	for _, s := range m.sessions {
		k := s.bucketKey()
		v, exists := out.Buckets[k]
		if !exists {
			speed := m.groupRates[k]
			v = BucketView{Key: k, Limit: speed}
		}
		v.Sessions++
		out.Buckets[k] = v
	}
	return out
}
