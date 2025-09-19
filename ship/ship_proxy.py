# ship_proxy.py
from http.server import BaseHTTPRequestHandler, HTTPServer
import socket
import threading
import queue
import sys
import time

# framing helpers (same as offshore)
def recv_all(sock, n):
    data = b''
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            raise ConnectionError("Socket closed")
        data += chunk
    return data

def read_message(sock):
    hdr = recv_all(sock, 5)
    length = int.from_bytes(hdr[:4], 'big')
    mtype = hdr[4]
    payload = recv_all(sock, length) if length > 0 else b''
    return mtype, payload

def send_message(sock, mtype, payload):
    hdr = len(payload).to_bytes(4, 'big') + bytes([mtype])
    sock.sendall(hdr + payload)

# global queue between HTTP handler threads and single TCP worker
request_queue = queue.Queue()

class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _read_body(self):
        length = int(self.headers.get('Content-Length', 0))
        if length:
            return self.rfile.read(length)
        return b''

    def do_CONNECT(self):
        # For CONNECT, we need to queue special object and then once worker sets up the tunnel,
        # perform bidirectional raw relay (via worker communicating with offshore using framed TUNNEL messages).
        req_bytes = f"{self.command} {self.path} {self.request_version}\r\n".encode()  # CONNECT host:port HTTP/1.1\r\n
        for k,v in self.headers.items():
            req_bytes += f"{k}: {v}\r\n".encode()
        req_bytes += b"\r\n"
        ev = threading.Event()
        conn_pair = {"handler": self, "request": req_bytes, "event": ev, "type":"CONNECT"}
        request_queue.put(conn_pair)
        # worker will set a field in conn_pair: 'tunnel_ok'=True if remote connected
        ev.wait()
        if not conn_pair.get('tunnel_ok'):
            self.send_error(502, "Tunnel Failed")
            return
        self.connection.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")

        # At this point worker has accepted the CONNECT and will handle framing; we need to do local->worker relay
        self.connection.setblocking(True)
        # We'll spawn a thread to read from client and send framed type 2 messages to offshore (worker shares tcp_sock)
        def client_to_ship():
            try:
                while True:
                    data = self.connection.recv(4096)
                    if not data:
                        break
                    # worker will be reading type 2 frames from the offshore connection because we send them through shared tcp_sock
                    request_queue.put({"type":"TUNNEL_FROM_CLIENT", "data": data})
                # signal close
                request_queue.put({"type":"TUNNEL_FROM_CLIENT", "data": b''})
            except Exception:
                pass
        t = threading.Thread(target=client_to_ship, daemon=True)
        t.start()
        # Meanwhile, worker will push data from offshore as queue items of type TUNNEL_TO_CLIENT via conn_pair 'tunnel_recv_queue'
        # Wait until worker signals tunnel end
        while True:
            item = conn_pair.get('tunnel_recv_queue').get()
            if item is None:
                break
            try:
                self.connection.sendall(item)
            except Exception:
                break
        try:
            self.connection.close()
        except:
            pass

    def do_METHOD(self):
        # handle all normal methods: GET, POST, PUT, DELETE, etc.
        body = self._read_body()
        # reconstruct raw request bytes
        path = self.path
        request_line = f"{self.command} {path} {self.request_version}\r\n"
        raw = request_line.encode()
        for k,v in self.headers.items():
            raw += f"{k}: {v}\r\n".encode()
        raw += b"\r\n" + body
        ev = threading.Event()
        resp_holder = {}
        conn_pair = {"handler": self, "request": raw, "event": ev, "type":"HTTP", "response_holder": resp_holder}
        request_queue.put(conn_pair)
        ev.wait()
        resp = resp_holder.get('response')
        if not resp:
            self.send_error(502, "Upstream error")
            return
        # resp is raw HTTP response bytes; send back to client
        try:
            # parse status line and headers minimally
            head, _, body = resp.partition(b"\r\n\r\n")
            head_lines = head.split(b"\r\n")
            status_line = head_lines[0].decode()
            parts = status_line.split(' ',2)
            if len(parts) < 3:
                self.send_error(502, "Bad response")
                return
            # send status code line
            version, code, reason = parts[0], parts[1], parts[2] if len(parts)>2 else ''
            self.send_response(int(code), reason)
            # headers
            for line in head_lines[1:]:
                if not line: continue
                k,v = line.decode().split(':',1)
                self.send_header(k.strip(), v.strip())
            self.end_headers()
            self.wfile.write(body)
        except Exception:
            self.send_error(500, "Local proxy error")

    # map all methods to do_METHOD
    def do_GET(self): return self.do_METHOD()
    def do_POST(self): return self.do_METHOD()
    def do_PUT(self): return self.do_METHOD()
    def do_DELETE(self): return self.do_METHOD()
    def do_PATCH(self): return self.do_METHOD()
    def do_HEAD(self): return self.do_METHOD()
    def do_OPTIONS(self): return self.do_METHOD()

def tcp_worker(offshore_host, offshore_port):
    # connect and keep connection open
    while True:
        try:
            tcp = socket.create_connection((offshore_host, offshore_port))
            print("Connected to offshore at", offshore_host, offshore_port)
            tcp.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            # process queue sequentially
            while True:
                item = request_queue.get()
                if item is None:
                    continue
                if item.get("type") == "CONNECT":
                    conn_pair = item
                    # send framed CONNECT request (type 0)
                    send_message(tcp, 0, conn_pair['request'])
                    # wait for response (should be type 1 with HTTP 200)
                    mtype, payload = read_message(tcp)
                    if mtype == 1 and payload.startswith(b"HTTP/1.1 200"):
                        # signal handler that tunnel is established
                        conn_pair['tunnel_ok'] = True
                        # prepare a queue for receiving tunnel bytes from worker
                        conn_pair['tunnel_recv_queue'] = queue.Queue()
                        conn_pair['event'].set()
                        # Now worker and handler must coordinate tunnel frames:
                        # Create a background thread that reads from offshore framed type 2 messages and pushes into recv_queue
                        def offshore_to_handler():
                            try:
                                while True:
                                    mt, pl = read_message(tcp)
                                    if mt == 2:
                                        conn_pair['tunnel_recv_queue'].put(pl)
                                        if not pl:
                                            break
                                    elif mt == 3:
                                        # control
                                        if pl == b"TUNNEL-CLOSED":
                                            break
                                    else:
                                        # ignore other messages while in tunnel
                                        pass
                            except Exception:
                                pass
                            finally:
                                conn_pair['tunnel_recv_queue'].put(None)

                        t = threading.Thread(target=offshore_to_handler, daemon=True)
                        t.start()

                        # Now there may be client->worker tunnel messages queued onto request_queue by handler
                        # We'll loop and forward any TUNNEL_FROM_CLIENT items to offshore as type 2 frames
                        while True:
                            it = request_queue.get()
                            if isinstance(it, dict) and it.get('type') == 'TUNNEL_FROM_CLIENT':
                                data = it.get('data')
                                send_message(tcp, 2, data)
                                if not data:
                                    break
                            else:
                                # if some other normal HTTP request arrives while tunnel is active, queue it back and wait until tunnel closes
                                request_queue.put(it)
                                break
                        # tunnel finished (continue to next queued item)
                    else:
                        conn_pair['tunnel_ok'] = False
                        conn_pair['event'].set()
                        # consume other frames until done?
                elif item.get("type") == "HTTP":
                    conn_pair = item
                    # send request across
                    send_message(tcp, 0, conn_pair['request'])
                    # wait for response message type 1
                    while True:
                        mtype, payload = read_message(tcp)
                        if mtype == 1:
                            conn_pair['response_holder']['response'] = payload
                            conn_pair['event'].set()
                            break
                        elif mtype == 3:
                            # control error
                            conn_pair['response_holder']['response'] = None
                            conn_pair['event'].set()
                            break
                        else:
                            # ignore other frames
                            pass
                else:
                    # handle tunnel-from-client or unknown top-level item (shouldn't happen)
                    pass
        except Exception as e:
            print("Failed to connect to offshore:", e)
            time.sleep(2)
            continue

def send_message(sock, mtype, payload):
    hdr = len(payload).to_bytes(4, 'big') + bytes([mtype])
    sock.sendall(hdr + payload)

def read_message(sock):
    hdr = recv_all(sock, 5)
    length = int.from_bytes(hdr[:4], 'big')
    mtype = hdr[4]
    payload = recv_all(sock, length) if length > 0 else b''
    return mtype, payload

if __name__ == '__main__':
    offshore_host = '127.0.0.1'
    offshore_port = 9999
    if len(sys.argv) >= 2:
        offshore_host = sys.argv[1]
    if len(sys.argv) >= 3:
        offshore_port = int(sys.argv[2])

    # start worker
    t = threading.Thread(target=tcp_worker, args=(offshore_host, offshore_port), daemon=True)
    t.start()

    server = HTTPServer(('0.0.0.0', 8080), ProxyHandler)
    print("Ship proxy listening on 0.0.0.0:8080 and connecting to", offshore_host, offshore_port)
    server.serve_forever()
