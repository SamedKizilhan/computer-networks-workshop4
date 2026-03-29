# CMPE487 Workshop 4 — LAN Chat Client & UDP File Transfer

A peer-to-peer chat and file transfer application. It features a custom JSON protocol with **Python TCP sockets** for chat messaging and discovery, and introduces **UDP sockets with custom sliding-window flow control** for large file transfers, completely bypassing TCP's built-in congestion and flow control.

---

## Workshop 4
Tested with ...

## Prerequisites

- Python 3.10+
- No external dependencies — stdlib only

---

## How to Run

On **each machine**, inside the project directory:

```bash
python3 chat.py
```

1. The app detects your local IP, then asks for your name.
2. It broadcasts a UDP ASK packet to the LAN; any peer already online replies over TCP.
3. If peers are found, pick one by number to start chatting or sending files.
4. **To test file transfer:** Make sure you have a file larger than 2 MB in your directory. Use the `/sendfile <filename>` command.

*Note: For peer discovery to work correctly, both machines must be on the same LAN.*

---

## Protocol

Discovery uses **UDP broadcast on port 12487**. Chat messages use **TCP on port 12487**. File transfers use **UDP on port 12487**.

### 1. Discovery & Chat
| Type | Transport | Fields | Purpose |
|---|---|---|---|
| `ASK` | UDP broadcast | `SENDER_IP` | Discover peers |
| `REPLY` | TCP unicast | `RECEIVER_NAME`, `RECEIVER_IP` | Respond to ASK |
| `MESSAGE` | TCP unicast | `SENDER_IP`, `SENDER_NAME`, `PAYLOAD` | Send a text message |

### 2. File Transfer (Flow Control)
File transfer bypasses TCP and implements a custom sliding window protocol over UDP.
* **Payload Encoding:** File bytes are chunked into 1500-byte pieces and encoded using **Base62**.
* **Flow Control:** The receiver dictates the maximum number of unacknowledged packets in flight using the `RWND` field. 
* **Retransmission:** The sender expects an `ACK` for every sequence. If an `ACK` is not received within 1.0 second, the packet is retransmitted.

| Type | Transport | Fields | Purpose |
|---|---|---|---|
| `File` | UDP unicast | `NAME`, `SEQ`, `BODY` (Base62), `EOF` (boolean) | Sends a 1500-byte file chunk |
| `ACK` | UDP unicast | `SEQ`, `RWND` | Acknowledges receipt and advertises window size |

---

## Shell Commands

Once the app is running, these commands are available at the `You:` prompt:

| Command | Description |
|---|---|
| `/scan` | Broadcast ASK immediately to discover new peers |
| `/list` | Show all currently known peers |
| `/switch` | Pick a new chat target from the known peers list |
| `/sendfile <file>` | **[NEW]** Send a file to the currently selected peer via UDP |
| `/quit` | Exit the application |
| `<any text>` | Send a TCP message to the currently selected peer |

---

## Project Structure

```text
Workshop/
├── chat.py          # Entire application logic (Chat + Flow Control)
├── requirements.txt # Empty — stdlib only
└── README.md        # This file
```