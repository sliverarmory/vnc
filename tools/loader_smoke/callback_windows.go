//go:build windows

package main

import (
	"syscall"
	"unsafe"
)

func newCallback(output func(unsafe.Pointer, int32) int32) uintptr {
	return syscall.NewCallback(func(data uintptr, dataLen uintptr) uintptr {
		return uintptr(output(unsafe.Pointer(data), int32(dataLen)))
	})
}
