package qos

import (
	"context"
	"testing"
	"time"

	"github.com/apernet/quic-go"
)

func TestCloseTelemetryBoundedAndTyped(t *testing.T) {
	m := NewManager()
	for i := 0; i < closeCapacity+3; i++ {
		ctx, cancel := context.WithCancelCause(context.Background())
		s := session(m, ctx, "S:fixture", "127.0.0.1")
		s.limiter()
		s.Add(1, 2)
		cancel(&quic.IdleTimeoutError{})
		m.finish(s) // idempotent against the context cleanup goroutine
	}
	h := m.Health()
	if len(h.Closes) != closeCapacity || h.CloseSequence != closeCapacity+3 {
		t.Fatalf("unbounded or duplicated history: %d/%d", len(h.Closes), h.CloseSequence)
	}
	if h.Closes[0].Sequence != 4 {
		t.Fatal("ring order corrupted")
	}
	for _, v := range h.Closes {
		if v.Reason != "idle_timeout" || v.LastPayloadMS == 0 || v.Up != 1 || v.Down != 2 {
			t.Fatal("missing activity or typed reason")
		}
	}
}
func TestBucketTelemetryPreservesSharedCaps(t *testing.T) {
	m := NewManager()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	a := session(m, ctx, "S:one", "127.0.0.1")
	b := session(m, ctx, "S:two", "127.0.0.1")
	if err := a.Wait(ctx, Burst); err != nil {
		t.Fatal(err)
	}
	a.Add(Burst, 0)
	if err := b.Wait(ctx, Chunk); err != nil {
		t.Fatal(err)
	}
	b.Add(0, Chunk)
	v := m.Health().Buckets["S:127.0.0.1"]
	if v.Sessions != 2 || v.Limit != StandardRate || v.Up != Burst || v.Down != Chunk || v.DelayedCalls == 0 || v.WaitNS < uint64(time.Millisecond) {
		t.Fatalf("wrong shared metrics: %+v", v)
	}
}
