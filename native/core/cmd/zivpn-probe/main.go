package main

import (
	"encoding/json"
	"fmt"
	"github.com/apernet/hysteria/core/client"
	"github.com/apernet/hysteria/extras/obfs"
	"io"
	"net"
	"os"
	"time"
)

type factory struct{ psk string }

func (f factory) New(_ net.Addr) (net.PacketConn, error) {
	c, e := net.ListenUDP("udp4", &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1)})
	if e != nil {
		return nil, e
	}
	o, e := obfs.NewSalamanderObfuscator([]byte(f.psk))
	if e != nil {
		c.Close()
		return nil, e
	}
	return obfs.WrapPacketConn(c, o), nil
}
func main() {
	var input struct{ Address, Auth, PSK string }
	if json.NewDecoder(os.Stdin).Decode(&input) != nil {
		os.Exit(2)
	}
	addr, e := net.ResolveUDPAddr("udp", input.Address)
	if e != nil {
		os.Exit(2)
	}
	c, _, e := client.NewClient(&client.Config{ServerAddr: addr, Auth: input.Auth,
		ConnFactory: factory{input.PSK}, TLSConfig: client.TLSConfig{InsecureSkipVerify: true},
		QUICConfig: client.QUICConfig{MaxIdleTimeout: 5 * time.Second, KeepAlivePeriod: 2 * time.Second}})
	if e != nil {
		fmt.Println("Handshake failed:", e)
		os.Exit(1)
	}
	defer c.Close()
	l, e := net.Listen("tcp4", "127.0.0.1:0")
	if e != nil {
		panic(e)
	}
	defer l.Close()
	go func() {
		s, e := l.Accept()
		if e != nil {
			return
		}
		defer s.Close()
		io.Copy(s, s)
	}()
	s, e := c.TCP(l.Addr().String())
	if e != nil {
		panic(e)
	}
	defer s.Close()
	s.SetDeadline(time.Now().Add(5 * time.Second))
	s.Write([]byte("compatibility-test"))
	buf := make([]byte, 18)
	n, e := io.ReadFull(s, buf)
	if e != nil || string(buf[:n]) != "compatibility-test" {
		panic("TCP mismatch")
	}
	u, e := net.ListenUDP("udp4", &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1)})
	if e != nil {
		panic(e)
	}
	defer u.Close()
	go func() {
		b := make([]byte, 1024)
		n, a, e := u.ReadFromUDP(b)
		if e == nil {
			u.WriteToUDP(b[:n], a)
		}
	}()
	cu, e := c.UDP()
	if e != nil {
		panic(e)
	}
	defer cu.Close()
	if e = cu.Send([]byte("udp-test"), u.LocalAddr().String()); e != nil {
		panic(e)
	}
	result := make(chan bool, 1)
	go func() { b, _, e := cu.Receive(); result <- e == nil && string(b) == "udp-test" }()
	select {
	case ok := <-result:
		if !ok {
			panic("UDP mismatch")
		}
	case <-time.After(5 * time.Second):
		panic("UDP timeout")
	}
	fmt.Println("Authentication, TCP and UDP compatibility: PASS")
}
