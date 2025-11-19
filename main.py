#!/usr/bin/env python3
"""
Flask WebSocket game server with per-lobby isolation and readiness handshake.

Install:
    pip install flask flask-sock

Run:
    python server_flask.py
"""
import json
import time
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict
from flask import Flask, request, jsonify
from flask_sock import Sock
from simple_websocket import ConnectionClosed

# Configuration
HOST = "0.0.0.0"
PORT = 5000
LOBBY_TIMEOUT = 300.0  # seconds
CLEANUP_INTERVAL = 30.0  # seconds

# In-memory leaderboard
leaderboard = []
leaderboard_lock = threading.Lock()

@dataclass
class Lobby:
    name: str
    players: Dict[str, "WebSocket"] = field(default_factory=dict)
    ready: Dict[str, bool] = field(default_factory=dict)
    start_time: float | None = None
    last_activity: float = field(default_factory=time.time)
    lock: threading.Lock = field(default_factory=threading.Lock)
    start_timer: threading.Timer | None = None

    def players_count(self) -> int:
        return len(self.players)

    def is_full(self) -> bool:
        return self.players_count() >= 2
    
    def broadcast(self, message: dict):
        """Broadcast JSON to all players; remove dead sockets."""
        dead = []
        text = json.dumps(message)
        print(f"[Server] Broadcasting to {len(self.players)} players in lobby '{self.name}': {message['type']}")
        with self.lock:
            for pid, ws in list(self.players.items()):
                try:
                    print(f"[Server] Sending {message['type']} to player '{pid}'")
                    ws.send(text)
                    print(f"[Server] Successfully sent to player '{pid}'")
                except Exception as e:
                    print(f"[Server] Broadcast error for {pid}: {e}; removing")
                    dead.append(pid)
            for pid in dead:
                self._remove_player_no_lock(pid)

    def _remove_player_no_lock(self, player_id: str):
        """Remove player without acquiring lock (caller must hold lock)."""
        if player_id in self.players:
            ws = self.players.pop(player_id)
            self.ready.pop(player_id, None)
            try:
                ws.close()
            except Exception:
                pass

    def remove_player(self, player_id: str):
        with self.lock:
            if player_id in self.players:
                ws = self.players.pop(player_id)
                self.ready.pop(player_id, None)
                try:
                    ws.close()
                except Exception:
                    pass

    def attempt_start_game(self):
        """Start game when we have exactly 2 players and both ready."""
        with self.lock:
            if self.players_count() == 2 and all(self.ready.get(pid, False) for pid in self.players):
                # Cancel previous start_timer if running
                if self.start_timer and self.start_timer.is_alive():
                    self.start_timer.cancel()
                self.start_time = time.time()
                # Short delay still useful to ensure clients fully processed readiness
                def delayed():
                    time.sleep(0.15)
                    self.broadcast({
                        "type": "game_start",
                        "message": "Game starting! Both players connected and ready.",
                        "timestamp": self.start_time
                    })
                self.start_timer = threading.Timer(0, delayed)
                self.start_timer.start()

# Global lobbies registry
lobbies: Dict[str, Lobby] = {}
lobbies_lock = threading.Lock()

# Background cleanup
def cleanup_task():
    while True:
        time.sleep(CLEANUP_INTERVAL)
        now = time.time()
        to_delete = []
        with lobbies_lock:
            for name, lobby in list(lobbies.items()):
                if now - lobby.last_activity > LOBBY_TIMEOUT:
                    to_delete.append(name)
            for name in to_delete:
                lobby = lobbies.pop(name)
                print(f"[Server] Lobby '{name}' timed out; notifying and deleting")
                lobby.broadcast({"type": "lobby_timeout", "message": "Lobby closed due to inactivity"})
                # close websockets
                with lobby.lock:
                    for pid, ws in list(lobby.players.items()):
                        try:
                            ws.close()
                        except:
                            pass
                    lobby.players.clear()
                    lobby.ready.clear()

# Flask app setup
app = Flask(__name__)
sock = Sock(app)

# WebSocket handler
@sock.route('/ws/<lobby_name>/<player_id>')
def ws_handler(ws, lobby_name, player_id):
    if not lobby_name or not player_id:
        ws.close()
        return

    print(f"[Server] Player '{player_id}' attempting to connect to lobby '{lobby_name}'")

    # Ensure lobby exists and add player
    with lobbies_lock:
        if lobby_name not in lobbies:
            lobbies[lobby_name] = Lobby(name=lobby_name)
            print(f"[Server] Created new lobby: {lobby_name}")
        lobby = lobbies[lobby_name]

    with lobby.lock:
        lobby.last_activity = time.time()
        if lobby.is_full() and player_id not in lobby.players:
            ws.send(json.dumps({"type": "error", "message": "Lobby is full"}))
            ws.close()
            return

        # Add or replace player's socket
        lobby.players[player_id] = ws
        lobby.ready.setdefault(player_id, False)
        player_number = list(lobby.players.keys()).index(player_id) + 1

        print(f"[Server] Player '{player_id}' connected to lobby '{lobby_name}' as Player {player_number}")
        print(f"[Server] Lobby '{lobby_name}' status: {lobby.players_count()}/2 players")

        # Send connected and waiting messages
        ws.send(json.dumps({
            "type": "connected",
            "player_number": player_number,
            "message": f"Connected as Player {player_number}",
            "players_count": lobby.players_count()
        }))
        if lobby.players_count() < 2:
            ws.send(json.dumps({
                "type": "waiting",
                "message": "Waiting for another player...",
                "players_count": lobby.players_count()
            }))

    # After connecting, main receive loop
    try:
        while True:
            data_text = ws.receive()
            if data_text is None:
                break
                
            lobby.last_activity = time.time()
            try:
                data = json.loads(data_text.strip())
            except Exception:
                print(f"[Server] Invalid JSON from {player_id}: {data_text}")
                continue

            mtype = data.get("type", "")
            # Client telling server it's ready to receive game_start
            if mtype == "ready":
                with lobby.lock:
                    lobby.ready[player_id] = True
                    print(f"[Server] Player '{player_id}' in lobby '{lobby_name}' is READY")
                lobby.attempt_start_game()

            # next_level request can also come from WS messages if you prefer; currently handled via HTTP
            elif mtype == "ping":
                # keepalive / debugging
                ws.send(json.dumps({"type": "pong"}))

            else:
                print(f"[Server] Received unknown message type from {player_id}: {mtype}")

    except ConnectionClosed:
        print(f"[Server] WebSocket connection closed for player '{player_id}'")
    except Exception as e:
        print(f"[Server] WebSocket receive error for player '{player_id}': {e}")
    finally:
        print(f"[Server] Player '{player_id}' disconnecting from lobby '{lobby_name}'")
        # Remove player, notify other, delete empty lobby
        with lobby.lock:
            if player_id in lobby.players:
                lobby.players.pop(player_id, None)
                lobby.ready.pop(player_id, None)

            print(f"[Server] Lobby '{lobby_name}' status: {lobby.players_count()}/2 players")
            if lobby.players:
                # notify remaining players who disconnected (player numbers are dynamic)
                lobby.broadcast({
                    "type": "player_disconnected",
                    "message": "Other player disconnected",
                    "disconnected_player": player_id
                })

        # If lobby now empty, remove it from global registry
        with lobbies_lock:
            if lobby_name in lobbies and lobbies[lobby_name].players_count() == 0:
                print(f"[Server] Lobby '{lobby_name}' is empty, deleting")
                lobbies.pop(lobby_name, None)

@app.route('/api/next_level', methods=['POST'])
def http_next_level():
    data = request.json
    lobby_name = data.get("lobby_name")
    level = data.get("level")
    player_id = data.get("player_id")
    print(f"[Server] Next level request from player '{player_id}' in lobby '{lobby_name}', level: {level}")

    if not lobby_name or level is None:
        return jsonify({"error": "Missing lobby_name or level"}), 400

    with lobbies_lock:
        lobby = lobbies.get(lobby_name)
    if not lobby:
        return jsonify({"error": "Lobby not found"}), 404

    lobby.last_activity = time.time()
    
    # Small delay to let WebSocket queue process
    time.sleep(0.01)
    lobby.broadcast({"type": "next_level", "level": level, "player_id": player_id, "message": f"Advancing to level {level}"})
    time.sleep(0.01)
    
    return jsonify({"success": True, "level": level})

@app.route('/api/complete_game', methods=['POST'])
def http_complete_game():
    data = request.json
    lobby_name = data.get("lobby_name")
    player_id = data.get("player_id")
    print(f"[Server] Game complete request from player '{player_id}' in lobby '{lobby_name}'")

    if not lobby_name:
        return jsonify({"error": "Missing lobby_name"}), 400

    with lobbies_lock:
        lobby = lobbies.get(lobby_name)
    if not lobby or lobby.start_time is None:
        return jsonify({"error": "Lobby not found or game not started"}), 400

    end_time = time.time()
    total_time = round(end_time - lobby.start_time, 2)
    with leaderboard_lock:
        leaderboard.append({
            "lobby_name": lobby_name,
            "total_time": total_time,
            "timestamp": datetime.utcnow().isoformat(),
            "completed_by": player_id
        })
        leaderboard.sort(key=lambda x: x["total_time"])

    # Ensure broadcast completes
    time.sleep(0.01)
    lobby.broadcast({"type": "game_complete", "total_time": total_time, "message": f"Game completed in {total_time} seconds!"})
    time.sleep(0.01)

    # Clean up lobby after completion
    with lobbies_lock:
        if lobby_name in lobbies:
            print(f"[Server] Lobby '{lobby_name}' cleaned up after game completion")
            lobbies.pop(lobby_name)

    return jsonify({"success": True, "total_time": total_time})

@app.route('/api/leaderboard', methods=['GET'])
def http_leaderboard():
    with leaderboard_lock:
        top = leaderboard[:50]
    return jsonify({"leaderboard": top})

@app.route('/api/lobby_status/<lobby_name>', methods=['GET'])
def http_lobby_status(lobby_name):
    with lobbies_lock:
        lobby = lobbies.get(lobby_name)
    if not lobby:
        return jsonify({"exists": False, "players_count": 0})
    return jsonify({
        "exists": True,
        "players_count": lobby.players_count(),
        "is_full": lobby.is_full(),
        "game_started": lobby.start_time is not None
    })

@app.route('/api/health', methods=['GET'])
def http_health():
    return jsonify({"status": "ok"})

if __name__ == "__main__":
    print(f"[Server] Starting Flask server on http://{HOST}:{PORT} (lobby timeout={LOBBY_TIMEOUT}s)")
    
    # Start background cleanup thread
    cleanup_thread = threading.Thread(target=cleanup_task, daemon=True)
    cleanup_thread.start()
    
    app.run(host=HOST, port=PORT, debug=False, threaded=True)