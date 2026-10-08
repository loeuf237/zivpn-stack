package obfs

import (
	"bytes"
	"sync"
	"sync/atomic"
	"testing"
)

func TestSalamanderConcurrentPacketIntegrity(t *testing.T) {
	for _, spare := range []bool{false, true} {
		name := "exact_capacity"
		psk := []byte("fixture09")
		if spare {
			name = "spare_capacity"
			psk = make([]byte, 9, 32)
			copy(psk, "fixture09")
		}
		t.Run(name, func(t *testing.T) {
			shared, _ := NewSalamanderObfuscator(psk)
			t.Logf("PSK length=%d capacity=%d", len(shared.PSK), cap(shared.PSK))
			var failures atomic.Uint64
			var wg sync.WaitGroup
			for worker := 0; worker < 4; worker++ {
				wg.Add(1)
				go func(worker int) {
					defer wg.Done()
					peer, _ := NewSalamanderObfuscator([]byte("fixture09"))
					payload := bytes.Repeat([]byte{byte(worker + 1)}, 1200)
					encoded := make([]byte, 2048)
					decoded := make([]byte, 2048)
					for i := 0; i < 2000; i++ {
						if worker%2 == 0 {
							n := shared.Obfuscate(payload, encoded)
							m := peer.Deobfuscate(encoded[:n], decoded)
							if !bytes.Equal(payload, decoded[:m]) {
								failures.Add(1)
							}
						} else {
							n := peer.Obfuscate(payload, encoded)
							m := shared.Deobfuscate(encoded[:n], decoded)
							if !bytes.Equal(payload, decoded[:m]) {
								failures.Add(1)
							}
						}
					}
				}(worker)
			}
			wg.Wait()
			if failures.Load() != 0 {
				t.Fatalf("corrupted packets: %d", failures.Load())
			}
		})
	}
}
