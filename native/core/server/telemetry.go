package server

import (
	"context"
	"errors"
	"sync"
	"sync/atomic"
	"time"

	"github.com/apernet/quic-go"
	"github.com/apernet/quic-go/logging"
)

// Only numeric transport metadata is collected, never frames or payloads.
type TransportStats struct {
	rtt                 atomic.Int64
	sent                atomic.Uint64
	acked               atomic.Uint64
	lost                atomic.Uint64
	pto                 atomic.Uint32
	lostTime            atomic.Uint64
	lostReordering      atomic.Uint64
	lateAcked           atomic.Uint64
	lossTrackingEvicted atomic.Uint64
	cwnd                atomic.Int64
	inFlight            atomic.Int64
	// Only the connection event loop accesses the bounded packet-number ring.
	lostRing    [4096]uint64
	largestLost uint64
}
type TransportView struct {
	RTTMS               float64 `json:"rtt_ms"`
	Sent                uint64  `json:"sent_packets"`
	Acked               uint64  `json:"acked_packets"`
	Lost                uint64  `json:"lost_packets"`
	PTO                 uint32  `json:"pto_count"`
	LostTime            uint64  `json:"lost_time_threshold"`
	LostReordering      uint64  `json:"lost_reordering_threshold"`
	LateAcked           uint64  `json:"late_acked_packets"`
	LossTrackingEvicted uint64  `json:"loss_tracking_evicted"`
	Cwnd                int64   `json:"congestion_window_bytes"`
	InFlight            int64   `json:"in_flight_bytes"`
}

func (s *TransportStats) View() TransportView {
	if s == nil {
		return TransportView{}
	}
	return TransportView{RTTMS: float64(s.rtt.Load()) / float64(time.Millisecond), Sent: s.sent.Load(), Acked: s.acked.Load(), Lost: s.lost.Load(), PTO: s.pto.Load(), LostTime: s.lostTime.Load(), LostReordering: s.lostReordering.Load(), LateAcked: s.lateAcked.Load(), LossTrackingEvicted: s.lossTrackingEvicted.Load(), Cwnd: s.cwnd.Load(), InFlight: s.inFlight.Load()}
}

// rememberLoss tracks only 1-RTT packet numbers, never payloads or keys.
func (s *TransportStats) rememberLoss(level logging.EncryptionLevel, pn logging.PacketNumber, reason logging.PacketLossReason) {
	s.lost.Add(1)
	if reason == logging.PacketLossTimeThreshold {
		s.lostTime.Add(1)
	} else {
		s.lostReordering.Add(1)
	}
	if level != logging.Encryption1RTT || pn < 0 {
		return
	}
	token := uint64(pn) + 1
	slot := token % uint64(len(s.lostRing))
	if s.lostRing[slot] != 0 {
		s.lossTrackingEvicted.Add(1)
	}
	s.lostRing[slot] = token
	if token > s.largestLost {
		s.largestLost = token
	}
}

// QUIC discards its packet record when declaring loss. ACK frames can still
// prove delivery afterwards; count these separately without changing recovery.
func (s *TransportStats) observeACKs(frames []logging.Frame) {
	for _, frame := range frames {
		ack, ok := frame.(*logging.AckFrame)
		if !ok {
			continue
		}
		for _, r := range ack.AckRanges {
			if r.Smallest < 0 || r.Largest < r.Smallest {
				continue
			}
			first, last := uint64(r.Smallest)+1, uint64(r.Largest)+1
			floor := uint64(1)
			if s.largestLost >= uint64(len(s.lostRing)) {
				floor = s.largestLost - uint64(len(s.lostRing)) + 1
			}
			if first < floor {
				first = floor
			}
			if last > s.largestLost {
				last = s.largestLost
			}
			for token := first; token <= last; token++ {
				slot := token % uint64(len(s.lostRing))
				if s.lostRing[slot] == token {
					s.lostRing[slot] = 0
					s.lateAcked.Add(1)
				}
			}
		}
	}
}

type TransportRegistry struct{ entries sync.Map }

func (r *TransportRegistry) Lookup(ctx context.Context) *TransportStats {
	key := ctx.Value(quic.ConnectionTracingKey)
	if key == nil {
		return nil
	}
	if v, ok := r.entries.Load(key); ok {
		return v.(*TransportStats)
	}
	return nil
}
func (r *TransportRegistry) Tracer(ctx context.Context, _ logging.Perspective, _ quic.ConnectionID) *logging.ConnectionTracer {
	key := ctx.Value(quic.ConnectionTracingKey)
	if key == nil {
		return nil
	}
	s := &TransportStats{}
	r.entries.Store(key, s)
	return &logging.ConnectionTracer{
		UpdatedMetrics: func(stats *logging.RTTStats, cwnd logging.ByteCount, inFlight logging.ByteCount, _ int) {
			s.cwnd.Store(int64(cwnd))
			s.inFlight.Store(int64(inFlight))
			s.rtt.Store(int64(stats.SmoothedRTT()))
		},
		SentLongHeaderPacket: func(_ *logging.ExtendedHeader, _ logging.ByteCount, _ logging.ECN, _ *logging.AckFrame, _ []logging.Frame) {
			s.sent.Add(1)
		},
		SentShortHeaderPacket: func(_ *logging.ShortHeader, _ logging.ByteCount, _ logging.ECN, _ *logging.AckFrame, _ []logging.Frame) {
			s.sent.Add(1)
		},
		AcknowledgedPacket: func(_ logging.EncryptionLevel, _ logging.PacketNumber) { s.acked.Add(1) },
		LostPacket:         s.rememberLoss,
		ReceivedShortHeaderPacket: func(_ *logging.ShortHeader, _ logging.ByteCount, _ logging.ECN, frames []logging.Frame) {
			s.observeACKs(frames)
		},
		UpdatedPTOCount: func(n uint32) { s.pto.Store(n) },
		Close:           func() { r.entries.Delete(key) },
	}
}

// ClassifyClose uses typed QUIC errors, without storing error strings or credentials.
func ClassifyClose(err error) string {
	if err == nil {
		return "normal"
	}
	var idle *quic.IdleTimeoutError
	var handshake *quic.HandshakeTimeoutError
	var app *quic.ApplicationError
	var transport *quic.TransportError
	if errors.As(err, &idle) {
		return "idle_timeout"
	}
	if errors.As(err, &handshake) {
		return "handshake_timeout"
	}
	if errors.As(err, &app) {
		if app.Remote {
			return "peer_closed"
		}
		if app.ErrorCode == closeErrCodeTrafficLimitReached {
			return "access_revoked"
		}
		return "local_closed"
	}
	if errors.As(err, &transport) {
		return "transport_error"
	}
	return "other"
}
