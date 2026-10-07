package qos

import (
	"fmt"
	"strings"
	"time"

	"golang.org/x/time/rate"
)

const MaxRate = 1000000000000

type Rates struct {
	Standard int `json:"standard"`
	Premium  int `json:"premium"`
}

func validRate(n int) bool { return n > 0 && n <= MaxRate }
func (m *Manager) accountRateLocked(id string, vip bool) int {
	if n := m.policies[id]; n > 0 {
		return n
	}
	if vip {
		return m.defaults.Premium
	}
	return m.defaults.Standard
}
func (m *Manager) refreshRatesLocked() {
	groups := map[string]int{}
	for _, s := range m.sessions {
		key := s.bucketKey()
		s.rateKey = key
		n := m.accountRateLocked(s.id, s.vip)
		if old := groups[key]; old == 0 || n < old {
			groups[key] = n
		}
	}
	m.groupRates = groups
	for key, b := range m.buckets {
		n := groups[key]
		if n == 0 {
			n = m.accountRateLocked(key, strings.HasPrefix(key, "P:"))
		}
		if int(b.limiter.Limit()) != n {
			b.limiter.SetLimitAt(time.Now(), rate.Limit(n))
			m.bucketSequence++
			b.generation = m.bucketSequence
		}
	}
}
func (m *Manager) ApplyPolicies(defaults Rates, policies map[string]int) error {
	if !validRate(defaults.Standard) || !validRate(defaults.Premium) {
		return fmt.Errorf("invalid defaults")
	}
	for id, n := range policies {
		if _, _, e := ParseID(id); e != nil || !validRate(n) {
			return fmt.Errorf("invalid account rate")
		}
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	m.defaults = defaults
	m.policies = map[string]int{}
	for id, n := range policies {
		m.policies[id] = n
	}
	m.refreshRatesLocked()
	return nil
}
func (m *Manager) SetAccountRate(id string, n int) error {
	if !validRate(n) {
		return fmt.Errorf("invalid account rate")
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	m.policies[id] = n
	m.refreshRatesLocked()
	return nil
}
