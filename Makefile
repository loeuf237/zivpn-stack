.PHONY: build test test-native check
build:
	python3 tools/build.py

test:
	PYTHONPATH=src python3 -m unittest discover -s src -p 'test_*.py'
	PYTHONPATH=src:tools python3 -m unittest discover -s tests -p 'test_*.py'

test-native: build
	cd native/core && ../../.cache/go1.21.13-$$(uname -m | sed 's/x86_64/amd64/;s/aarch64/arm64/')/go/bin/go test -race -skip Stress ./qos ./internal/integration_tests

check:
	python3 tools/secret_scan.py --staged
	python3 -m compileall -q src scripts tools
	bash -n scripts/zivpn-ports
