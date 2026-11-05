#!/bin/sh
# Build the 32-bit J2534 proxy DLL (needs i686-w64-mingw32-gcc).
set -e
i686-w64-mingw32-gcc -O2 -Wall -shared -o j2534_proxy.dll j2534_proxy.c -Wl,--kill-at -lkernel32
echo "built j2534_proxy.dll"
