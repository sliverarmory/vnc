//go:build darwin || linux

package main

import (
	"unsafe"

	"github.com/ebitengine/purego"
)

func newCallback(output func(unsafe.Pointer, int32) int32) uintptr {
	return purego.NewCallback(output)
}
