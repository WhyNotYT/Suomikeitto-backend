#!/usr/bin/env python3
"""
aiohttp WebSocket game server with per-lobby isolation and readiness handshake.

Install:
    pip install aiohttp

Run:
    python server_aiohttp.py
"""
import asyncio
import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict

from aiohttp import web, WSMsgType

# Configuration
HOST = "0.0.0.0"
PORT = 5000
LOBBY_TIMEOUT = 300.0  # seconds
CLEANUP_INTERVAL = 30.0  # seconds

# In-memory leaderboard
leaderboard = []
leaderboard_lock = asyncio.Lock()

@dataclass
class Lobby:
    name: str
    players: Dict[str, "web.WebSocketResponse"] = field(default_factory=dict)
    ready: Dict[str, bool] = field(default_factory=dict)
    start_time: float | None = None
    last_activity: float = field(default_factory=time.time)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    start_task: asyncio.Task | None = None

    def players_count(self) -> int:
        return len(self.players)

    def is_full(self) -> bool:
        return self.players_count() >= 2
    
    
    async def broadcast(self, message: dict):
        """Broadcast JSON to all players; remove dead sockets."""
        dead = []
        text = json.dumps(message)
        print(f"[Server] Broadcasting to {len(self.players)} players in lobby '{self.name}': {message['type']}")
        async with self.lock:
            for pid, ws in list(self.players.items()):
                try:
                    print(f"[Server] Sending {message['type']} to player '{pid}'")
                    await ws.send_str(text)
                    print(f"[Server] Successfully sent to player '{pid}'")
                except Exception as e:
                    print(f"[Server] Broadcast error for {pid}: {e}; removing")
                    dead.append(pid)
            for pid in dead:
                await self._remove_player_no_lock(pid)

    async def _remove_player_no_lock(self, player_id: str):
        """Remove player without acquiring lock (caller must hold lock)."""
        if player_id in self.players:
            ws = self.players.pop(player_id)
            self.ready.pop(player_id, None)
            try:
                await ws.close()
            except Exception:
                pass

    async def remove_player(self, player_id: str):
        async with self.lock:
            if player_id in self.players:
                ws = self.players.pop(player_id)
                self.ready.pop(player_id, None)
                try:
                    await ws.close()
                except Exception:
                    pass

    async def attempt_start_game(self):
        """Start game when we have exactly 2 players and both ready."""
        async with self.lock:
            if self.players_count() == 2 and all(self.ready.get(pid, False) for pid in self.players):
                # Cancel previous start_task if running
                if self.start_task and not self.start_task.done():
                    self.start_task.cancel()
                self.start_time = time.time()
                # Short delay still useful to ensure clients fully processed readiness
                async def delayed():
                    await asyncio.sleep(0.15)
                    await self.broadcast({
                        "type": "game_start",
                        "message": "Game starting! Both players connected and ready.",
                        "timestamp": self.start_time
                    })
                self.start_task = asyncio.create_task(delayed())

# Global lobbies registry
lobbies: Dict[str, Lobby] = {}
lobbies_lock = asyncio.Lock()

# Background cleanup
async def cleanup_task():
    while True:
        await asyncio.sleep(CLEANUP_INTERVAL)
        now = time.time()
        to_delete = []
        async with lobbies_lock:
            for name, lobby in list(lobbies.items()):
                if now - lobby.last_activity > LOBBY_TIMEOUT:
                    to_delete.append(name)
            for name in to_delete:
                lobby = lobbies.pop(name)
                print(f"[Server] Lobby '{name}' timed out; notifying and deleting")
                await lobby.broadcast({"type": "lobby_timeout", "message": "Lobby closed due to inactivity"})
                # close websockets
                async with lobby.lock:
                    for pid, ws in list(lobby.players.items()):
                        try:
                            await ws.close(code=1001, message=b"Lobby timeout")
                        except:
                            pass
                    lobby.players.clear()
                    lobby.ready.clear()


# WebSocket handler
async def ws_handler(request):
    lobby_name = request.match_info.get("lobby")
    player_id = request.match_info.get("player_id")

    if not lobby_name or not player_id:
        return web.Response(status=400, text="Missing lobby or player_id")

    print(f"[Server] Player '{player_id}' attempting to connect to lobby '{lobby_name}'")
    ws = web.WebSocketResponse()
    await ws.prepare(request)

    # Ensure lobby exists and add player
    async with lobbies_lock:
        if lobby_name not in lobbies:
            lobbies[lobby_name] = Lobby(name=lobby_name)
            print(f"[Server] Created new lobby: {lobby_name}")
        lobby = lobbies[lobby_name]

    async with lobby.lock:
        lobby.last_activity = time.time()
        if lobby.is_full() and player_id not in lobby.players:
            await ws.send_str(json.dumps({"type": "error", "message": "Lobby is full"}))
            await ws.close()
            return ws

        # Add or replace player's socket
        lobby.players[player_id] = ws
        lobby.ready.setdefault(player_id, False)
        player_number = list(lobby.players.keys()).index(player_id) + 1

        print(f"[Server] Player '{player_id}' connected to lobby '{lobby_name}' as Player {player_number}")
        print(f"[Server] Lobby '{lobby_name}' status: {lobby.players_count()}/2 players")

        # Send connected and waiting messages
        await ws.send_str(json.dumps({
            "type": "connected",
            "player_number": player_number,
            "message": f"Connected as Player {player_number}",
            "players_count": lobby.players_count()
        }))
        if lobby.players_count() < 2:
            await ws.send_str(json.dumps({
                "type": "waiting",
                "message": "Waiting for another player...",
                "players_count": lobby.players_count()
            }))

    # After connecting, main receive loop
    try:
        async for msg in ws:
            lobby.last_activity = time.time()
            if msg.type == WSMsgType.TEXT:
                data_text = msg.data.strip()
                try:
                    data = json.loads(data_text)
                except Exception:
                    print(f"[Server] Invalid JSON from {player_id}: {data_text}")
                    continue

                mtype = data.get("type", "")
                # Client telling server it's ready to receive game_start
                if mtype == "ready":
                    async with lobby.lock:
                        lobby.ready[player_id] = True
                        print(f"[Server] Player '{player_id}' in lobby '{lobby_name}' is READY")
                    await lobby.attempt_start_game()

                # next_level request can also come from WS messages if you prefer; currently handled via HTTP
                elif mtype == "ping":
                    # keepalive / debugging
                    await ws.send_str(json.dumps({"type": "pong"}))

                else:
                    print(f"[Server] Received unknown message type from {player_id}: {mtype}")

            elif msg.type == WSMsgType.ERROR:
                print(f"[Server] WebSocket error for {player_id}: {ws.exception()}")
                break

    except Exception as e:
        print(f"[Server] WebSocket receive error for player '{player_id}': {e}")
    finally:
        print(f"[Server] Player '{player_id}' disconnecting from lobby '{lobby_name}'")
        # Remove player, notify other, delete empty lobby
        async with lobby.lock:
            removed = False
            if player_id in lobby.players:
                lobby.players.pop(player_id, None)
                lobby.ready.pop(player_id, None)
                removed = True

            print(f"[Server] Lobby '{lobby_name}' status: {lobby.players_count()}/2 players")
            if lobby.players:
                # notify remaining players who disconnected (player numbers are dynamic)
                await lobby.broadcast({
                    "type": "player_disconnected",
                    "message": "Other player disconnected",
                    "disconnected_player": player_id
                })

        # If lobby now empty, remove it from global registry
        async with lobbies_lock:
            if lobby_name in lobbies and lobbies[lobby_name].players_count() == 0:
                print(f"[Server] Lobby '{lobby_name}' is empty, deleting")
                lobbies.pop(lobby_name, None)

    return ws
async def http_next_level(request):
    data = await request.json()
    lobby_name = data.get("lobby_name")
    level = data.get("level")
    player_id = data.get("player_id")
    print(f"[Server] Next level request from player '{player_id}' in lobby '{lobby_name}', level: {level}")

    if not lobby_name or level is None:
        return web.json_response({"error": "Missing lobby_name or level"}, status=400)

    async with lobbies_lock:
        lobby = lobbies.get(lobby_name)
    if not lobby:
        return web.json_response({"error": "Lobby not found"}, status=404)

    lobby.last_activity = time.time()
    
    # ADD THIS: Ensure broadcast completes before returning HTTP response
    await asyncio.sleep(0.01)  # Small delay to let WebSocket queue process
    await lobby.broadcast({"type": "next_level", "level": level, "player_id": player_id, "message": f"Advancing to level {level}"})
    await asyncio.sleep(0.01)  # Give time for message to be sent
    
    return web.json_response({"success": True, "level": level})

async def http_complete_game(request):
    data = await request.json()
    lobby_name = data.get("lobby_name")
    player_id = data.get("player_id")
    print(f"[Server] Game complete request from player '{player_id}' in lobby '{lobby_name}'")

    if not lobby_name:
        return web.json_response({"error": "Missing lobby_name"}, status=400)

    async with lobbies_lock:
        lobby = lobbies.get(lobby_name)
    if not lobby or lobby.start_time is None:
        return web.json_response({"error": "Lobby not found or game not started"}, status=400)

    end_time = time.time()
    total_time = round(end_time - lobby.start_time, 2)
    async with leaderboard_lock:
        leaderboard.append({
            "lobby_name": lobby_name,
            "total_time": total_time,
            "timestamp": datetime.utcnow().isoformat(),
            "completed_by": player_id
        })
        leaderboard.sort(key=lambda x: x["total_time"])

    # ADD THIS: Ensure broadcast completes
    await asyncio.sleep(0.01)
    await lobby.broadcast({"type": "game_complete", "total_time": total_time, "message": f"Game completed in {total_time} seconds!"})
    await asyncio.sleep(0.01)

    # Clean up lobby after completion
    async with lobbies_lock:
        if lobby_name in lobbies:
            print(f"[Server] Lobby '{lobby_name}' cleaned up after game completion")
            lobbies.pop(lobby_name)

    return web.json_response({"success": True, "total_time": total_time})
# HTTP: leaderboard
async def http_leaderboard(request):
    async with leaderboard_lock:
        top = leaderboard[:50]
    return web.json_response({"leaderboard": top})

# HTTP: lobby status
async def http_lobby_status(request):
    lobby_name = request.match_info.get("lobby_name")
    async with lobbies_lock:
        lobby = lobbies.get(lobby_name)
    if not lobby:
        return web.json_response({"exists": False, "players_count": 0})
    return web.json_response({
        "exists": True,
        "players_count": lobby.players_count(),
        "is_full": lobby.is_full(),
        "game_started": lobby.start_time is not None
    })

# Health
async def http_health(request):
    return web.json_response({"status": "ok"})

# App setup
app = web.Application()
app.add_routes([
    web.get("/ws/{lobby}/{player_id}", ws_handler),
    web.post("/api/next_level", http_next_level),
    web.post("/api/complete_game", http_complete_game),
    web.get("/api/leaderboard", http_leaderboard),
    web.get("/api/lobby_status/{lobby_name}", http_lobby_status),
    web.get("/api/health", http_health),
])

# Start background cleanup on startup
async def on_startup(app):
    app["cleanup_task"] = asyncio.create_task(cleanup_task())
async def on_cleanup(app):
    task = app.get("cleanup_task")
    if task:
        task.cancel()
        with asyncio.suppress(asyncio.CancelledError):
            await task

app.on_startup.append(on_startup)
app.on_cleanup.append(on_cleanup)

if __name__ == "__main__":
    print(f"[Server] Starting aiohttp server on http://{HOST}:{PORT} (lobby timeout={LOBBY_TIMEOUT}s)")
    web.run_app(app, host=HOST, port=PORT)
