"""只模拟本地进程及 OpenSSH 日志，不访问网络。"""
import sys
import time

mode = sys.argv[1]

def emit(line):
    print(line, file=sys.stderr, flush=True)

if mode == "authfail":
    emit("Permission denied (publickey).")
    sys.exit(255)
if mode == "hang":
    time.sleep(30)
    sys.exit(0)
emit("debug3: receive packet: type 52")
emit('Authenticated to fake ([127.0.0.1]:22) using "publickey".')
emit("debug1: Entering interactive session.")
time.sleep(0.1)
if mode == "forwardfail":
    emit("Error: remote port forwarding failed for listen port 12345")
    sys.exit(255)
emit("debug1: remote forward success for: listen 127.0.0.1:12345, connect 127.0.0.1:12345")
if mode == "drop":
    time.sleep(0.15)
    emit("Timeout, server fake not responding.")
    sys.exit(255)
while True:
    time.sleep(0.2)
    emit("debug3: receive packet: type 82")
