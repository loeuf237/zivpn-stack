package main

import (
	"bytes"
	"context"
	"crypto/tls"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"os"
	"os/exec"
	"os/signal"
	"sync"
	"syscall"
	"time"

	"github.com/apernet/hysteria/core/qos"
	"github.com/apernet/hysteria/core/server"
	"github.com/apernet/hysteria/extras/obfs"
)

type authenticator struct {
	helper, db string
	port       int
	manager    *qos.Manager
}

var helperSlots = make(chan struct{}, 16)

func helperCall(helper, db string, input interface{}, output interface{}) error {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	select {
	case helperSlots <- struct{}{}:
		defer func() { <-helperSlots }()
	case <-ctx.Done():
		return ctx.Err()
	}
	cmd := exec.CommandContext(ctx, helper)
	if db != "" {
		cmd.Env = append(os.Environ(), "ZIVPN_NATIVE_DB="+db)
	}
	b, e := json.Marshal(input)
	if e != nil {
		return e
	}
	cmd.Stdin = bytes.NewReader(b)
	// Never log helper input, credentials, or stdout on rejection.
	b, e = cmd.Output()
	if e != nil {
		if ctx.Err() != nil {
			return ctx.Err()
		}
		return e
	}
	return json.Unmarshal(b, output)
}
func (a authenticator) Authenticate(addr net.Addr, auth string, _ uint64) (bool, string) {
	var out struct {
		ID     string `json:"id"`
		OK     bool   `json:"ok"`
		Rate   int    `json:"rate"`
		Reason string `json:"reason"`
	}
	e := helperCall(a.helper, a.db, map[string]interface{}{"addr": addr.String(), "auth": auth, "server_port": a.port}, &out)
	reason := out.Reason
	if e != nil {
		reason = "helper_error"
		if errors.Is(e, context.DeadlineExceeded) {
			reason = "helper_timeout"
		}
		if _, ok := e.(*exec.ExitError); ok {
			reason = "helper_exit"
		}
	} else {
		switch reason {
		case "accepted", "invalid_credentials", "missing_credentials", "ambiguous_credentials", "wrong_profile", "disabled", "expired", "quota", "unassigned", "database_error", "internal_error":
		default:
			reason = "invalid_helper_response"
		}
		if out.OK && reason == "accepted" {
			if _, _, err := qos.ParseID(out.ID); err != nil {
				reason = "invalid_helper_response"
			} else {
				if out.Rate <= 0 {
					a.manager.RecordAuth("invalid_helper_response")
					return false, ""
				}
				if out.Rate > 0 {
					if e := a.manager.SetAccountRate(out.ID, out.Rate); e != nil {
						a.manager.RecordAuth("invalid_helper_response")
						return false, ""
					}
				}
				a.manager.RecordAuth(reason)
				return true, out.ID
			}
		} else if reason == "accepted" {
			reason = "invalid_helper_response"
		}
	}
	a.manager.RecordAuth(reason)
	// Failures are aggregated in /health, not repeated for every Android worker.
	return false, ""
}

var policyReloadMutex sync.Mutex

func reloadPolicy(manager *qos.Manager, helper, db string) error {
	policyReloadMutex.Lock()
	defer policyReloadMutex.Unlock()
	var out struct {
		OK       bool            `json:"ok"`
		Allowed  map[string]bool `json:"allowed"`
		Policies map[string]int  `json:"policies"`
		Defaults qos.Rates       `json:"defaults"`
	}
	if err := helperCall(helper, db, map[string]interface{}{"check": true, "ids": manager.IDs()}, &out); err != nil {
		return err
	}
	if !out.OK || out.Allowed == nil || out.Policies == nil {
		return fmt.Errorf("invalid policy response")
	}
	if err := manager.ApplyPolicies(out.Defaults, out.Policies); err != nil {
		return err
	}
	manager.RevokeExcept(out.Allowed)
	return nil
}
func main() {
	configPath := flag.String("config", "/etc/zivpn/config.json", "Existing certificate/listen configuration")
	helper := flag.String("auth-helper", "/usr/local/sbin/zivpn-native-auth", "Root identity helper")
	db := flag.String("db", "", "Optional test database")
	socket := flag.String("stats-socket", "/run/zivpn-native.sock", "Root-only statistics socket")
	secondary := flag.String("secondary", ":5668", "Optional legacy VIP listener")
	flag.Parse()
	var config struct {
		Listen, Cert, Key string
		QUIC              struct {
			DisablePathMTUDiscovery bool `json:"disablePathMTUDiscovery"`
		}
	}
	b, e := os.ReadFile(*configPath)
	if e != nil {
		log.Fatal("Cannot read server configuration")
	}
	if e = json.Unmarshal(b, &config); e != nil {
		log.Fatal("Invalid server configuration")
	}
	cert, e := tls.LoadX509KeyPair(config.Cert, config.Key)
	if e != nil {
		log.Fatal("Cannot load existing TLS certificate")
	}
	manager := qos.NewManager()
	if err := reloadPolicy(manager, *helper, *db); err != nil {
		log.Fatal("Initial account policy unavailable")
	}
	var servers []server.Server
	addresses := []string{config.Listen}
	if *secondary != "" {
		addresses = append(addresses, *secondary)
	}
	for _, address := range addresses {
		a, e := net.ResolveUDPAddr("udp", address)
		if e != nil {
			log.Fatal(e)
		}
		c, e := net.ListenUDP("udp", a)
		if e != nil {
			log.Fatal(e)
		}
		port := c.LocalAddr().(*net.UDPAddr).Port
		o, e := obfs.NewSalamanderObfuscator([]byte("hu``hqb`c"))
		if e != nil {
			log.Fatal(e)
		}
		config := &server.Config{Conn: obfs.WrapPacketConn(c, o),
			TLSConfig: server.TLSConfig{Certificates: []tls.Certificate{cert}},
			QUICConfig: server.QUICConfig{DisablePathMTUDiscovery: config.QUIC.DisablePathMTUDiscovery,
				MaxIdleTimeout: 60 * time.Second, KeepAlivePeriod: 5 * time.Second},
			IgnoreClientBandwidth: true, Authenticator: authenticator{*helper, *db, port, manager},
			TransportRegistry: manager.TransportRegistry,
			MasqHandler:       http.NotFoundHandler()}
		config.AuthenticatedOutbound = func(ctx context.Context, id string, addr func() net.Addr, close func(), base server.Outbound) server.Outbound {
			return manager.Attach(ctx, id, addr, close, port).Wrap(base)
		}
		srv, e := server.NewServer(config)
		if e != nil {
			log.Fatal(e)
		}
		servers = append(servers, srv)
	}
	// Unix permissions isolate account metadata; no public API listener.
	if e = os.Remove(*socket); e != nil && !os.IsNotExist(e) {
		log.Fatal(e)
	}
	listener, e := net.Listen("unix", *socket)
	if e != nil {
		log.Fatal(e)
	}
	if e = os.Chmod(*socket, 0600); e != nil {
		log.Fatal(e)
	}
	defer os.Remove(*socket)
	mux := http.NewServeMux()
	mux.HandleFunc("/snapshot", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != "GET" {
			w.WriteHeader(405)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(manager.Snapshot())
	})
	mux.HandleFunc("/health", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != "GET" {
			w.WriteHeader(405)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(manager.Health())
	})
	mux.HandleFunc("/security", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != "GET" {
			w.WriteHeader(405)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(manager.SecuritySnapshot())
	})
	mux.HandleFunc("/reload-policy", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != "POST" {
			w.WriteHeader(405)
			return
		}
		if err := reloadPolicy(manager, *helper, *db); err != nil {
			manager.RecordAuth("recheck_error")
			w.WriteHeader(503)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(map[string]bool{"ok": true})
	})
	mux.HandleFunc("/kick", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != "POST" {
			w.WriteHeader(405)
			return
		}
		var input struct {
			Target string `json:"target"`
		}
		if json.NewDecoder(io.LimitReader(r.Body, 16384)).Decode(&input) != nil || input.Target == "" {
			w.WriteHeader(400)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(map[string]int{"closed": manager.Kick(input.Target)})
	})
	api := &http.Server{Handler: mux, ReadHeaderTimeout: 5 * time.Second, WriteTimeout: 5 * time.Second}
	go api.Serve(listener)
	for _, srv := range servers {
		srv := srv
		go func() {
			if e := srv.Serve(); e != nil {
				log.Print("QUIC listener stopped")
			}
		}()
	}
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer stop()
	go func() {
		ticker := time.NewTicker(15 * time.Second)
		defer ticker.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
				if err := reloadPolicy(manager, *helper, *db); err != nil {
					manager.RecordAuth("recheck_error")
					log.Print("Account recheck unavailable; retrying")
				}
			}
		}
	}()
	rates := manager.Health().Defaults
	fmt.Printf("Native ZIVPN QoS ready: %d listeners; Standard default %d B/s per IP; Premium default %d B/s per account\n", len(servers), rates.Standard, rates.Premium)
	<-ctx.Done()
	for _, srv := range servers {
		srv.Close()
	}
	api.Close()
}
