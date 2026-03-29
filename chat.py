import json
import socket
import sys
import threading
import time
import os

PORT           = 12487
MAX_BYTES      = 2048
SOCK_TIMEOUT   = 2
DISCOVERY_WAIT = 3
HEARTBEAT_INTERVAL = 60   # seconds between automatic re-broadcasts

peers: dict[str, str] = {}
peers_lock = threading.Lock()
print_lock  = threading.Lock()

# --- Base62 Encode/Decode ---
BASE62_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"

def encode_base62(data: bytes) -> str:
    """Encodes byte data to a Base62 string."""
    if not data:
        return ""
    
    # Convert byte array to a large integer
    num = int.from_bytes(data, byteorder='big')
    if num == 0:
        return BASE62_ALPHABET[0]

    encoded = []
    while num > 0:
        num, rem = divmod(num, 62)
        encoded.append(BASE62_ALPHABET[rem])
    
    # Pad with the first character of the alphabet ('0') to preserve leading null (\x00) bytes
    num_leading_zeros = len(data) - len(data.lstrip(b'\x00'))
    res = BASE62_ALPHABET[0] * num_leading_zeros + "".join(reversed(encoded))
    return res

def decode_base62(s: str) -> bytes:
    """Decodes a Base62 string back to byte data."""
    if not s:
        return b""
    
    num = 0
    for char in s:
        num = num * 62 + BASE62_ALPHABET.index(char)
        
    if num == 0:
        res = b""
    else:
        # Calculate the minimum length required to convert the int back to bytes
        byte_len = (num.bit_length() + 7) // 8
        res = num.to_bytes(byte_len, byteorder='big')
            
    # Add null bytes for each leading '0' character
    num_leading_zeros = len(s) - len(s.lstrip(BASE62_ALPHABET[0]))
    return b'\x00' * num_leading_zeros + res

# --- Packet builders ---
def make_ask(sender_ip: str) -> dict:
    return {"type": "ASK", "SENDER_IP": sender_ip}

def make_reply(receiver_name: str, receiver_ip: str) -> dict:
    return {"type": "REPLY", "RECEIVER_NAME": receiver_name, "RECEIVER_IP": receiver_ip}

def make_message(sender_ip: str, sender_name: str, payload: str) -> dict:
    return {"type": "MESSAGE", "SENDER_IP": sender_ip, "SENDER_NAME": sender_name, "PAYLOAD": payload}

def make_file_packet(filename: str, seq: int, body_bytes: bytes, is_eof: bool) -> dict:
    """
    Creates a File packet according to the slide requirements.
    Payload (BODY) is automatically Base62 encoded.
    """
    return {
        "type": "File",
        "NAME": filename,
        "SEQ": seq,          
        "BODY": encode_base62(body_bytes), 
        "EOF": is_eof        
    }

def make_ack_packet(seq: int, rwnd: int) -> dict:
    """ACK packet to be returned by the receiver."""
    return {
        "type": "ACK",
        "SEQ": seq,          
        "RWND": rwnd         
    }

# --- Network helpers ---
def get_local_ip() -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]

def send_packet(target_ip: str, packet: dict) -> bool:
    """Sends a JSON packet over TCP (for messages/discovery)."""
    try:
        data = json.dumps(packet).encode("utf-8")
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(SOCK_TIMEOUT)
            s.connect((target_ip, PORT))
            s.sendall(data)
        return True
    except (OSError, socket.timeout):
        return False

def send_udp_packet(target_ip: str, packet: dict) -> bool:
    """Sends a JSON packet over UDP (for file transfer)."""
    try:
        data = json.dumps(packet).encode("utf-8")
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.sendto(data, (target_ip, PORT))
        return True
    except OSError:
        return False

def broadcast_ask(local_ip: str) -> None:
    packet = make_ask(local_ip)
    data = json.dumps(packet).encode("utf-8")
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.sendto(data, ("<broadcast>", PORT))


# --- State for Incoming Files ---
incoming_files: dict[tuple[str, str], dict] = {}
incoming_lock = threading.Lock()
MAX_RWND = 10  # Assume a 10 packet buffer per connection

# --- State for Outgoing Files ---
transfer_active = False
transfer_lock = threading.Lock()
unacked_packets: dict[int, dict] = {} 
current_rwnd = 1  


# --- Packet handlers ---
def handle_ask(packet: dict, local_ip: str, name: str) -> None:
    sender_ip = packet.get("SENDER_IP")
    if sender_ip and sender_ip != local_ip:
        send_packet(sender_ip, make_reply(name, local_ip))
        with peers_lock:
            if sender_ip not in peers:
                peers[sender_ip] = f"peer@{sender_ip}"

def handle_reply(packet: dict) -> None:
    peer_ip   = packet.get("RECEIVER_IP")
    peer_name = packet.get("RECEIVER_NAME")
    if peer_ip and peer_name:
        with peers_lock:
            peers[peer_ip] = peer_name

def handle_message(packet: dict) -> None:
    sender_ip   = packet.get("SENDER_IP", "")
    sender_name = packet.get("SENDER_NAME", "unknown")
    payload     = packet.get("PAYLOAD", "")
    if sender_ip:
        with peers_lock:
            peers[sender_ip] = sender_name
    with print_lock:
        print(f"\n[{sender_name}]: {payload}")
        print("You: ", end="", flush=True)

def handle_file(packet: dict, sender_ip: str) -> None:
    """Handles incoming File packets over UDP, buffers them, and reconstructs the file."""
    filename = packet.get("NAME")
    seq = packet.get("SEQ")
    body_b62 = packet.get("BODY")
    is_eof = packet.get("EOF", False)

    if not filename or not isinstance(seq, int) or body_b62 is None:
        return

    file_key = (sender_ip, filename)
    
    with incoming_lock:
        if file_key not in incoming_files:
            incoming_files[file_key] = {
                "packets": {},
                "eof_seq": None
            }
        
        state = incoming_files[file_key]
        state["packets"][seq] = body_b62
        
        if is_eof:
            state["eof_seq"] = seq

        ack_packet = make_ack_packet(seq, MAX_RWND)
        send_udp_packet(sender_ip, ack_packet)

        if state["eof_seq"] is not None:
            expected_seqs = set(range(1, state["eof_seq"] + 1))
            received_seqs = set(state["packets"].keys())
            
            if expected_seqs == received_seqs:
                try:
                    save_name = f"downloaded_{filename}"
                    with open(save_name, "wb") as f:
                        for i in range(1, state["eof_seq"] + 1):
                            chunk_bytes = decode_base62(state["packets"][i])
                            f.write(chunk_bytes)
                    
                    with print_lock:
                        print(f"\n[*] File '{filename}' successfully received from {sender_ip}.")
                        print(f"[*] Saved as '{save_name}'.")
                        print("You: ", end="", flush=True)
                except Exception as e:
                    with print_lock:
                        print(f"\n[!] Failed to write file '{filename}': {e}")
                        print("You: ", end="", flush=True)
                
                del incoming_files[file_key]

def handle_ack(packet: dict) -> None:
    """Handles incoming ACK packets from the receiver and updates the sliding window."""
    global current_rwnd
    seq = packet.get("SEQ")
    rwnd = packet.get("RWND")
    
    if not isinstance(seq, int) or not isinstance(rwnd, int):
        return
        
    with transfer_lock:
        if seq in unacked_packets:
            del unacked_packets[seq]
        current_rwnd = rwnd  


# --- File Transfer Logic ---
def _file_sender_loop(target_ip: str, filename: str) -> None:
    """Background thread that handles chunking, encoding, and window logic."""
    global transfer_active, current_rwnd, unacked_packets
    
    try:
        with open(filename, "rb") as f:
            file_data = f.read()
    except FileNotFoundError:
        with print_lock:
            print(f"\n[!] File '{filename}' not found.")
            print("You: ", end="", flush=True)
        with transfer_lock:
            transfer_active = False
        return

    CHUNK_SIZE = 1500
    chunks = [file_data[i:i+CHUNK_SIZE] for i in range(0, len(file_data), CHUNK_SIZE)]
    if not chunks:
        chunks = [b""] 
        
    total_seqs = len(chunks)
    packets_to_send = {}
    
    for i, chunk in enumerate(chunks):
        seq = i + 1
        is_eof = (seq == total_seqs)
        clean_filename = os.path.basename(filename)
        packets_to_send[seq] = make_file_packet(clean_filename, seq, chunk, is_eof)

    with print_lock:
        print(f"\n[*] Starting transfer of '{filename}' ({len(file_data)} bytes) to {target_ip}...")
        print("You: ", end="", flush=True)

    seq_to_send = 1
    while True:
        with transfer_lock:
            if seq_to_send > total_seqs and len(unacked_packets) == 0:
                break 
                
            now = time.time()
            
            for unacked_seq, data in unacked_packets.items():
                if now - data["timestamp"] >= 1.0:
                    send_udp_packet(target_ip, data["packet"])
                    data["timestamp"] = now 
                    
            while len(unacked_packets) < current_rwnd and seq_to_send <= total_seqs:
                pkt = packets_to_send[seq_to_send]
                send_udp_packet(target_ip, pkt)
                
                unacked_packets[seq_to_send] = {
                    "packet": pkt,
                    "timestamp": time.time()
                }
                seq_to_send += 1
                
        time.sleep(0.01) 

    with print_lock:
        print(f"\n[*] File '{filename}' successfully sent and verified!")
        print("You: ", end="", flush=True)
        
    with transfer_lock:
        transfer_active = False
        unacked_packets.clear()

def start_file_transfer(target_ip: str, filename: str) -> None:
    """Initiates the file transfer in a separate thread."""
    global transfer_active, current_rwnd
    
    with transfer_lock:
        if transfer_active:
            print("[!] A file transfer is already in progress. Please wait.")
            return
        transfer_active = True
        current_rwnd = 1  
        unacked_packets.clear()
        
    threading.Thread(target=_file_sender_loop, args=(target_ip, filename), daemon=True).start()


# --- TCP/UDP Listeners ---
def _handle_connection(conn: socket.socket, local_ip: str, name: str) -> None:
    try:
        chunks = []
        conn.settimeout(SOCK_TIMEOUT)
        while True:
            chunk = conn.recv(4096)
            if not chunk:
                break
            chunks.append(chunk)
        raw = b"".join(chunks)
    except (OSError, socket.timeout):
        raw = b""
    finally:
        conn.close()

    if not raw.strip():
        return

    try:
        packet = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return

    ptype = packet.get("type")
    if ptype == "ASK":
        handle_ask(packet, local_ip, name)
    elif ptype == "REPLY":
        handle_reply(packet)
    elif ptype == "MESSAGE":
        handle_message(packet)

def _tcp_listener_loop(local_ip: str, name: str) -> None:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        server.bind(("", PORT))
        server.listen()
    except OSError as e:
        print(f"[ERROR] Cannot bind to port {PORT}: {e}")
        sys.exit(1)

    while True:
        try:
            conn, _ = server.accept()
        except OSError:
            time.sleep(0.5)
            continue
        threading.Thread(target=_handle_connection, args=(conn, local_ip, name), daemon=True).start()

def start_listener(local_ip: str, name: str) -> None:
    threading.Thread(target=_tcp_listener_loop, args=(local_ip, name), daemon=True).start()


def _udp_listener_loop(local_ip: str, name: str) -> None:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("", PORT))
    except OSError as e:
        print(f"[ERROR] Cannot bind UDP to port {PORT}: {e}")
        sys.exit(1)

    while True:
        try:
            data, addr = s.recvfrom(4096)
            sender_ip = addr[0] 
        except OSError:
            continue
            
        try:
            packet = json.loads(data.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
            
        ptype = packet.get("type")
        if ptype == "ASK":
            handle_ask(packet, local_ip, name)
        elif ptype == "File":
            handle_file(packet, sender_ip)
        elif ptype == "ACK":
            handle_ack(packet)

def start_udp_listener(local_ip: str, name: str) -> None:
    threading.Thread(target=_udp_listener_loop, args=(local_ip, name), daemon=True).start()


def _heartbeat_loop(local_ip: str, interval: int) -> None:
    while True:
        time.sleep(interval)
        broadcast_ask(local_ip)

def start_heartbeat(local_ip: str, interval: int = HEARTBEAT_INTERVAL) -> None:
    threading.Thread(target=_heartbeat_loop, args=(local_ip, interval), daemon=True).start()


# --- Peer discovery ---
def discover_peers(local_ip: str, name: str) -> dict[str, str]:
    print(f"[*] Broadcasting ASK on port {PORT} ...")
    broadcast_ask(local_ip)
    print(f"[*] Waiting {DISCOVERY_WAIT}s for replies ...")
    time.sleep(DISCOVERY_WAIT)
    with peers_lock:
        return {ip: n for ip, n in peers.items() if ip != local_ip}


# --- Peer selection ---
def select_peer(discovered: dict[str, str]) -> tuple[str, str]:
    peer_list = list(discovered.items())
    print("\n[*] Discovered peers:")
    for i, (ip, n) in enumerate(peer_list):
        print(f"  {i + 1}. {n} ({ip})")

    while True:
        try:
            idx = int(input("\nSelect peer number: ").strip()) - 1
            if 0 <= idx < len(peer_list):
                return peer_list[idx]
            print(f"[!] Enter a number between 1 and {len(peer_list)}.")
        except ValueError:
            print("[!] Please enter a valid number.")
        except (EOFError, KeyboardInterrupt):
            print("\n[*] Exiting.")
            sys.exit(0)


# --- Interactive shell ---
def command_shell(local_ip: str, name: str) -> None:
    target_ip:   str | None = None
    target_name: str | None = None

    discovered = discover_peers(local_ip, name)
    if discovered:
        target_ip, target_name = select_peer(discovered)
        print(f"\n[*] Now chatting with {target_name} ({target_ip})")
    else:
        print("\n[*] No peers found yet — staying online, waiting for incoming connections.")

    print("[*] Commands: /scan  /list  /switch  /sendfile  /quit\n")

    while True:
        try:
            user_input = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n[*] Exiting.")
            break

        if user_input == "/quit":
            print("[*] Exiting.")
            break

        elif user_input == "/scan":
            discover_peers(local_ip, name)
            with peers_lock:
                all_peers = {ip: n for ip, n in peers.items() if ip != local_ip}
            if all_peers:
                print("[*] Known peers after scan:")
                for ip, n in all_peers.items():
                    marker = "  <-- current" if ip == target_ip else ""
                    print(f"  {n} ({ip}){marker}")
            else:
                print("[!] No peers found.")

        elif user_input == "/list":
            with peers_lock:
                all_peers = {ip: n for ip, n in peers.items() if ip != local_ip}
            if all_peers:
                print("[*] Known peers:")
                for ip, n in all_peers.items():
                    marker = "  <-- current" if ip == target_ip else ""
                    print(f"  {n} ({ip}){marker}")
            else:
                print("[!] No known peers yet. Try /scan.")

        elif user_input == "/switch":
            with peers_lock:
                all_peers = {ip: n for ip, n in peers.items() if ip != local_ip}
            if not all_peers:
                print("[!] No peers known yet. Try /scan first.")
            else:
                target_ip, target_name = select_peer(all_peers)
                print(f"[*] Switched to {target_name} ({target_ip})")

        elif user_input.startswith("/sendfile"):
            if target_ip is None:
                print("[!] No target selected. Use /switch to pick a peer first.")
                continue
                
            parts = user_input.split(" ", 1)
            if len(parts) < 2:
                print("[!] Usage: /sendfile <path_to_file>")
                continue
                
            filename = parts[1].strip()
            start_file_transfer(target_ip, filename)

        elif not user_input:
            continue

        else:
            if target_ip is None:
                print("[!] No target selected. Use /switch to pick a peer.")
                continue
            encoded = user_input.encode("utf-8")
            if len(encoded) > MAX_BYTES:
                print(f"[!] Message too long ({len(encoded)} bytes, max {MAX_BYTES}).")
                continue
            if not send_packet(target_ip, make_message(local_ip, name, user_input)):
                print(f"[!] Failed to reach {target_name} ({target_ip}).")


def main() -> None:
    local_ip = get_local_ip()
    print(f"[*] Your IP: {local_ip}")

    while True:
        name = input("Enter your name: ").strip()
        if name:
            break
        print("[!] Name cannot be empty.")

    start_listener(local_ip, name)
    start_udp_listener(local_ip, name)
    start_heartbeat(local_ip)
    print(f"[*] Listening on port {PORT} as '{name}'")
    print(f"[*] Heartbeat every {HEARTBEAT_INTERVAL}s — use /scan to discover immediately.")

    command_shell(local_ip, name)

if __name__ == "__main__":
    main()