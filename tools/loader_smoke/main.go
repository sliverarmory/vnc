package main

import (
	"fmt"
	"os"
	"runtime"
	"strings"
	"unsafe"

	"github.com/sliverarmory/reflektor/native"
)

const exportName = "VNCDesktopCommand"

func invoke(library *native.Library, argument string) (string, error) {
	buffer := []byte(argument)
	var outputs []string
	callback := newCallback(func(data unsafe.Pointer, dataLen int32) int32 {
		if data == nil || dataLen < 0 || dataLen > 4096 {
			return 1
		}
		outputs = append(outputs, string(unsafe.Slice((*byte)(data), int(dataLen))))
		return 0
	})

	_, err := library.CallExportWithArgs(exportName,
		uintptr(unsafe.Pointer(&buffer[0])), uintptr(len(buffer)), callback)
	runtime.KeepAlive(buffer)
	if err != nil {
		return "", err
	}
	if len(outputs) != 1 {
		return "", fmt.Errorf("%q produced %d callbacks, want one", argument, len(outputs))
	}
	return outputs[0], nil
}

func run(path string) error {
	payload, err := os.ReadFile(path)
	if err != nil {
		return fmt.Errorf("read native library: %w", err)
	}
	library, err := native.LoadLibrary(payload)
	if err != nil {
		return fmt.Errorf("Reflektor in-memory load: %w", err)
	}
	defer library.Close()

	for _, check := range []struct {
		argument string
		want     string
	}{
		{"status", "VNC stopped; last status 0"},
		{"start --port 0", "Invalid port"},
	} {
		output, err := invoke(library, check.argument)
		if err != nil {
			return fmt.Errorf("call %q: %w", check.argument, err)
		}
		if !strings.Contains(output, check.want) {
			return fmt.Errorf("call %q returned %q; want %q", check.argument, output, check.want)
		}
		fmt.Printf("%s/%s %q -> %q\n", runtime.GOOS, runtime.GOARCH,
			check.argument, strings.TrimSpace(output))
	}
	return nil
}

func main() {
	if len(os.Args) != 2 {
		fmt.Fprintln(os.Stderr, "usage: loader_smoke path-to-vncdesktop-library")
		os.Exit(2)
	}
	if err := run(os.Args[1]); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
