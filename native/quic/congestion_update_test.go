package quic

import (
	"github.com/apernet/quic-go/congestion"
	"github.com/apernet/quic-go/internal/ackhandler"
	"sync"
	"testing"
)

type fixtureCongestion struct{ congestion.CongestionControl }
type trackingAckHandler struct {
	ackhandler.SentPacketHandler
	changes []congestion.CongestionControl
}

func (h *trackingAckHandler) SetCongestionControl(cc congestion.CongestionControl) {
	h.changes = append(h.changes, cc)
}

func TestCongestionUpdatesAreAppliedByEventLoop(t *testing.T) {
	handler := &trackingAckHandler{}
	conn := &connection{sentPacketHandler: handler, sendingScheduled: make(chan struct{}, 1)}
	var group sync.WaitGroup
	for i := 0; i < 32; i++ {
		group.Add(1)
		go func() {
			defer group.Done()
			for j := 0; j < 8; j++ {
				conn.SetCongestionControl(&fixtureCongestion{})
			}
		}()
	}
	done := make(chan struct{})
	go func() { group.Wait(); close(done) }()
	for {
		select {
		case <-conn.sendingScheduled:
			conn.applyPendingCongestionControl()
		case <-done:
			goto complete
		}
	}
complete:
	group.Wait()
	final := &fixtureCongestion{}
	before := len(handler.changes)
	conn.SetCongestionControl(final)
	if len(handler.changes) != before {
		t.Fatal("setter modified ACK handler")
	}
	conn.applyPendingCongestionControl()
	if len(handler.changes) == 0 || handler.changes[len(handler.changes)-1] != final {
		t.Fatal("latest congestion update lost")
	}
	before = len(handler.changes)
	conn.applyPendingCongestionControl()
	if len(handler.changes) != before {
		t.Fatal("update applied twice")
	}
}
