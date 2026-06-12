import argparse
import base64
import json
import select
import socket
import socketserver
from pathlib import Path


def relay(client, upstream):
    sockets = [client, upstream]
    while True:
        readable, _, exceptional = select.select(sockets, [], sockets, 30)
        if exceptional or not readable:
            return
        for source in readable:
            data = source.recv(65536)
            if not data:
                return
            (upstream if source is client else client).sendall(data)


class ProxyHandler(socketserver.BaseRequestHandler):
    def handle(self):
        config = json.loads(self.server.config_path.read_text(encoding="utf-8"))
        client = self.request
        client.settimeout(20)
        request = b""
        while b"\r\n\r\n" not in request and len(request) < 131072:
            chunk = client.recv(8192)
            if not chunk:
                return
            request += chunk

        first_line = request.split(b"\r\n", 1)[0].decode("latin1", "replace")
        upstream = socket.create_connection(
            (config["host"], int(config["port"])), timeout=15
        )
        upstream.settimeout(None)
        auth = base64.b64encode(
            f"{config['username']}:{config['password']}".encode()
        ).decode()

        head, separator, body = request.partition(b"\r\n\r\n")
        lines = head.split(b"\r\n")
        lines = [
            line for line in lines
            if not line.lower().startswith(b"proxy-authorization:")
        ]
        lines.append(f"Proxy-Authorization: Basic {auth}".encode())
        upstream.sendall(b"\r\n".join(lines) + separator + body)

        if first_line.upper().startswith("CONNECT "):
            response = b""
            while b"\r\n\r\n" not in response and len(response) < 65536:
                chunk = upstream.recv(8192)
                if not chunk:
                    break
                response += chunk
            client.sendall(response)
            if not response.startswith(b"HTTP/1.1 200") and not response.startswith(
                b"HTTP/1.0 200"
            ):
                upstream.close()
                return
        relay(client, upstream)


class ThreadingProxy(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--listen", type=int, required=True)
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    with ThreadingProxy(("127.0.0.1", args.listen), ProxyHandler) as server:
        server.config_path = Path(args.config)
        server.serve_forever()


if __name__ == "__main__":
    main()
