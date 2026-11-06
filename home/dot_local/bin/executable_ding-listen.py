#!/usr/bin/env python3
# Слушает 127.0.0.1:51090 и на каждое подключение играет звук.
# Сюда стучится Stop-хук с dev через ssh RemoteForward (Host dev в ~/.ssh/config).
import socket, subprocess, os

snd = f"/System/Library/Sounds/{os.environ.get('KLALI_DING', 'Glass')}.aiff"
s = socket.socket()
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("127.0.0.1", 51090))
s.listen()
while True:
    c, _ = s.accept()
    c.close()
    subprocess.Popen(["afplay", snd])
