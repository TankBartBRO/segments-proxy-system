# offshore_server.py
import socket
import threading
import http.client
import ssl
import sys
import traceback

# framing helpers
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

# parse start-line to determine host
def parse_request_target(req_bytes):
    # req_bytes includes request-line and headers; we look for Host: header
    try:
        text = req_bytes.decode('iso-8859-1')
    except:
        text = req_bytes.decode('utf-8', 'ignore')
    lines = text.split('\r\n')
    request_line = lines[0]
    method, path, version = request_line.split(' ', 2)
    host = None
    for h in lines[1:]:
        if h.lower().startswith('host:'):
            host = h.split(':', 1)[1].strip()
            break
    return method, path, version, host

def handle_http_request(conn_sock, request_bytes):
    method, path, version, host_hdr = parse_request_target(request_bytes)
    if method.upper() == "CONNECT":
        # host_hdr should be like host:port
        target = host_hdr
        if ':' in target:
            target_host, target_port_str = target.split(':',1)
            target_port = int(target_port_str)
        else:
            target_host = target
            target_port = 443
        # try to connect
        try:
            remote = socket.create_connection((target_host, target_port), timeout=10)
        except Exception as e:
            send_message(conn_sock, 3, f"CONNECT-ERR: {e}".encode())
            return

        # send 200 OK response framed back (type 1)
        ok_response = b"HTTP/1.1 200 Connection established\r\n\r\n"
        send_message(conn_sock, 1, ok_response)

        # Now enter tunnel relay mode: read TUNNEL/DATA frames from ship and forward to remote
        # Meanwhile spawn thread to forward remote->ship frames
        def remote_to_ship():
            try:
                while True:
                    data = remote.recv(4096)
                    if not data:
                        break
                    send_message(conn_sock, 2, data)
            except Exception:
                pass
            finally:
                try:
                    send_message(conn_sock, 3, b"TUNNEL-CLOSED")
                except:
                    pass

        t = threading.Thread(target=remote_to_ship, daemon=True)
        t.start()

        try:
            while True:
                mtype, payload = read_message(conn_sock)
                if mtype == 2:
                    if not payload:
                        break
                    remote.sendall(payload)
                elif mtype == 3:
                    # control: maybe tunnel close
                    break
                else:
                    # ignore other types inside tunnel
                    pass
        except Exception:
            pass
        finally:
            try:
                remote.close()
            except:
                pass
        return

    # Non-CONNECT: forward as HTTP to the intended server
    # We need host header
    if not host_hdr:
        send_message(conn_sock, 3, b"NO-HOST")
        return
    # determine scheme and port
    target_host = host_hdr
    target_port = 80
    if ':' in host_hdr:
        target_host, port_s = host_hdr.split(':',1)
        target_port = int(port_s)
    try:
        # build http.client connection
        conn = http.client.HTTPConnection(target_host, target_port, timeout=20)
        # send raw request bytes via conn.sock by parsing the request minimally
        # Simpler: extract method and path and headers/body and use http.client.request
        raw_text = request_bytes.decode('iso-8859-1')
        head, _, body = raw_text.partition('\r\n\r\n')
        lines = head.split('\r\n')
        request_line = lines[0]
        method, path, ver = request_line.split(' ',2)
        headers = {}
        for h in lines[1:]:
            if not h: continue
            k,v = h.split(':',1)
            headers[k.strip()] = v.strip()
        # use http.client to perform request
        conn.request(method, path, body=body.encode('iso-8859-1'), headers=headers)
        resp = conn.getresponse()
        resp_body = resp.read()
        # reconstruct raw response
        status_line = f"HTTP/{resp.version//10}.{resp.version%10} {resp.status} {resp.reason}\r\n"
        headers_raw = ''.join(f"{k}: {v}\r\n" for k,v in resp.getheaders())
        raw_response = (status_line + headers_raw + "\r\n").encode('iso-8859-1') + resp_body
        send_message(conn_sock, 1, raw_response)
    except Exception as e:
        # send error
        msg = f"HTTP-FWD-ERR: {e}"
        send_message(conn_sock, 3, msg.encode())

def handle_ship_connection(conn_sock, addr):
    print("Ship connected:", addr)
    try:
        while True:
            mtype, payload = read_message(conn_sock)
            if mtype == 0:
                # HTTP request
                handle_http_request(conn_sock, payload)
            elif mtype == 2:
                # stray tunnel data (no op)
                pass
            elif mtype == 3:
                # control message
                print("Control:", payload)
            else:
                print("Unknown message type", mtype)
    except ConnectionError:
        print("Ship disconnected")
    except Exception:
        traceback.print_exc()
    finally:
        try:
            conn_sock.close()
        except:
            pass

def start_server(listen_host='0.0.0.0', listen_port=9999):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((listen_host, listen_port))
    s.listen(1)
    print(f"Offshore proxy listening on {listen_host}:{listen_port}")
    while True:
        conn, addr = s.accept()
        t = threading.Thread(target=handle_ship_connection, args=(conn, addr), daemon=True)
        t.start()

if __name__ == '__main__':
    host='0.0.0.0'
    port=9999
    if len(sys.argv) >= 2:
        port = int(sys.argv[1])
    start_server(host, port)
