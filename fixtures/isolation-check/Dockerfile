FROM python:3.13-alpine
RUN python - <<'PY'
import socket
for address, port in [('10.201.0.2', 21999), ('192.0.2.10', 22), ('169.254.169.254', 80)]:
    try:
        connection = socket.create_connection((address, port), timeout=2)
    except OSError:
        print('PRIVATE_EGRESS_BLOCKED', address, port, flush=True)
    else:
        connection.close()
        raise RuntimeError('Isolated build reached a protected endpoint')
PY
RUN sleep 20
CMD ["sleep", "3600"]
