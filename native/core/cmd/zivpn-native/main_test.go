package main

import (
	"net"
	"os"
	"path/filepath"
	"testing"

	"github.com/apernet/hysteria/core/qos"
)

func TestStructuredAuthenticationResults(t *testing.T) {
	for _, tc := range []struct {
		name, body, reason string
		allowed            bool
	}{
		{"accept", `printf '%s' '{"ok":true,"reason":"accepted","id":"P:fixture"}'`, "accepted", true},
		{"refuse", `printf '%s' '{"ok":false,"reason":"expired"}'`, "expired", false},
		{"sqlite", `printf '%s' '{"ok":false,"reason":"database_error"}'`, "database_error", false},
		{"broken", `exit 1`, "helper_exit", false},
		{"invalid", `printf '%s' '{"ok":true,"reason":"accepted","id":"bad"}'`, "invalid_helper_response", false},
		{"unexpected", `printf '%s' '{"ok":false,"reason":"PRIVATE-DETAILS"}'`, "invalid_helper_response", false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			path := filepath.Join(t.TempDir(), "helper")
			if e := os.WriteFile(path, []byte("#!/bin/sh\n"+tc.body+"\n"), 0700); e != nil {
				t.Fatal(e)
			}
			m := qos.NewManager()
			a := authenticator{path, "", 5667, m}
			ok, id := a.Authenticate(&net.UDPAddr{IP: net.IPv4(127, 0, 0, 1)}, "fixture-credential", 0)
			if ok != tc.allowed || (!ok && id != "") {
				t.Fatal("incorrect admission")
			}
			if m.Health().Auth[tc.reason] != 1 {
				t.Fatalf("incorrect classification: %+v", m.Health().Auth)
			}
		})
	}
}
