#!/usr/bin/env python3
"""
aiohttp WebSocket game server with per-lobby isolation and readiness handshake.
Features:
- Automatic cleanup of incomplete lobbies (1 player for >30s)
- Manual server reset endpoint
- Proper connection handling and game state management
"""
import asyncio
import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict

from aiohttp import web, WSMsgType
import pathlib

BASE_DIR = pathlib.Path(__file__).parent
GODOT_BUILD_DIR = BASE_DIR / "game_build" 

# Configuration
HOST = "0.0.0.0"
PORT = 5000
LOBBY_TIMEOUT = 300.0  # seconds - idle timeout
INCOMPLETE_LOBBY_TIMEOUT = 30.0  # seconds - timeout for lobbies with only 1 player
CLEANUP_INTERVAL = 5.0  # seconds - how often to check for timeouts

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
    creation_time: float = field(default_factory=time.time)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    start_task: asyncio.Task | None = None
    game_completed: bool = False

    def players_count(self) -> int:
        return len(self.players)

    def is_full(self) -> bool:
        return self.players_count() >= 2
    
    def is_incomplete(self) -> bool:
        """Check if lobby has been waiting for a second player too long."""
        return (
            self.players_count() == 1 
            and self.start_time is None 
            and time.time() - self.creation_time > INCOMPLETE_LOBBY_TIMEOUT
        )
    
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

    async def close_all_connections(self, reason: str = "Game completed"):
        """Close all WebSocket connections in this lobby."""
        async with self.lock:
            print(f"[Server] Closing all connections in lobby '{self.name}': {reason}")
            for pid, ws in list(self.players.items()):
                try:
                    await ws.close(code=1000, message=reason.encode())
                except Exception as e:
                    print(f"[Server] Error closing connection for {pid}: {e}")
            self.players.clear()
            self.ready.clear()

    async def attempt_start_game(self):
        """Start game when we have exactly 2 players and both ready."""
        async with self.lock:
            if self.players_count() == 2 and all(self.ready.get(pid, False) for pid in self.players):
                if self.start_task and not self.start_task.done():
                    self.start_task.cancel()
                self.start_time = time.time()
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

# Background cleanup task
async def cleanup_task():
    """Periodically clean up inactive and incomplete lobbies."""
    while True:
        await asyncio.sleep(CLEANUP_INTERVAL)
        now = time.time()
        to_cleanup = []

        async with lobbies_lock:
            for name, lobby in list(lobbies.items()):
                # Check for general inactivity timeout
                if now - lobby.last_activity > LOBBY_TIMEOUT:
                    lobbies.pop(name, None)
                    to_cleanup.append((name, lobby, "inactivity"))
                # Check for incomplete lobby timeout (1 player waiting too long)
                elif lobby.is_incomplete():
                    lobbies.pop(name, None)
                    to_cleanup.append((name, lobby, "incomplete"))

        # Notify and close connections for timed-out lobbies
        for name, lobby, reason in to_cleanup:
            if reason == "inactivity":
                print(f"[Server] Lobby '{name}' timed out due to inactivity")
                message = {"type": "lobby_timeout", "message": "Lobby closed due to inactivity"}
            else:  # incomplete
                print(f"[Server] Lobby '{name}' timed out - waited too long for second player")
                message = {"type": "lobby_timeout", "message": "Lobby closed - no second player joined"}
            
            try:
                await lobby.broadcast(message)
            except Exception as e:
                print(f"[Server] Error broadcasting timeout for lobby '{name}': {e}")

            await lobby.close_all_connections(f"Lobby timeout ({reason})")

# WebSocket handler
async def ws_handler(request):
    lobby_name = request.match_info.get("lobby")
    player_id = request.match_info.get("player_id")

    if not lobby_name or not player_id:
        return web.Response(status=400, text="Missing lobby or player_id")

    print(f"[Server] Player '{player_id}' attempting to connect to lobby '{lobby_name}'")
    ws = web.WebSocketResponse(heartbeat=5)
    await ws.prepare(request)

    async with lobbies_lock:
        if lobby_name not in lobbies:
            lobbies[lobby_name] = Lobby(name=lobby_name)
            print(f"[Server] Created new lobby: {lobby_name}")
        lobby = lobbies[lobby_name]

    async with lobby.lock:
        lobby.last_activity = time.time()
        
        # Check if game is already completed
        if lobby.game_completed:
            await ws.send_str(json.dumps({"type": "error", "message": "Game already completed"}))
            await ws.close()
            return ws
        
        if lobby.is_full() and player_id not in lobby.players:
            await ws.send_str(json.dumps({"type": "error", "message": "Lobby is full"}))
            await ws.close()
            return ws

        lobby.players[player_id] = ws
        lobby.ready.setdefault(player_id, False)
        player_number = list(lobby.players.keys()).index(player_id) + 1

        print(f"[Server] Player '{player_id}' connected to lobby '{lobby_name}' as Player {player_number}")
        print(f"[Server] Lobby '{lobby_name}' status: {lobby.players_count()}/2 players")

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
                if mtype == "ready":
                    async with lobby.lock:
                        lobby.ready[player_id] = True
                        print(f"[Server] Player '{player_id}' in lobby '{lobby_name}' is READY")
                    await lobby.attempt_start_game()
                elif mtype == "ping":
                    await ws.send_str(json.dumps({"type": "pong"}))
                else:
                    await lobby.broadcast(data | {"from": player_id})

            elif msg.type == WSMsgType.ERROR:
                print(f"[Server] WebSocket error for {player_id}: {ws.exception()}")
                break

    except Exception as e:
        print(f"[Server] WebSocket receive error for player '{player_id}': {e}")
    finally:
        print(f"[Server] Player '{player_id}' disconnecting from lobby '{lobby_name}'")
        async with lobby.lock:
            if player_id in lobby.players:
                lobby.players.pop(player_id, None)
                lobby.ready.pop(player_id, None)

            print(f"[Server] Lobby '{lobby_name}' status: {lobby.players_count()}/2 players")
            
            # Only notify if game hasn't completed (prevents notification after intentional disconnect)
            if lobby.players and not lobby.game_completed:
                await lobby.broadcast({
                    "type": "player_disconnected",
                    "message": "Other player disconnected",
                    "disconnected_player": player_id
                })

        # Clean up empty lobby
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
    
    await asyncio.sleep(0.01)
    await lobby.broadcast({"type": "next_level", "level": level, "player_id": player_id, "message": f"Advancing to level {level}"})
    await asyncio.sleep(0.01)
    
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

    # Mark game as completed
    lobby.game_completed = True
    
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

    # Broadcast completion to both clients
    print(f"[Server] Broadcasting game_complete to all players in lobby '{lobby_name}'")
    await asyncio.sleep(0.02)
    await lobby.broadcast({
        "type": "game_complete", 
        "total_time": total_time, 
        "message": f"Game completed in {total_time} seconds!"
    })
    await asyncio.sleep(0.02)

    # Remove lobby from registry
    async with lobbies_lock:
        if lobby_name in lobbies:
            print(f"[Server] Removing lobby '{lobby_name}' after game completion")
            lobbies.pop(lobby_name, None)
    
    # Close all connections
    await lobby.close_all_connections("Game completed")

    return web.json_response({"success": True, "total_time": total_time})

async def http_leaderboard(request):
    async with leaderboard_lock:
        top = leaderboard[:50]
    return web.json_response({"leaderboard": top})

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

async def http_clear_all(request):
    """Clear all lobbies and reset the server state."""
    print("[Server] Clearing all lobbies and resetting server state...")
    
    lobby_count = 0
    player_count = 0
    
    # Get all lobbies to clean up
    async with lobbies_lock:
        lobbies_to_clear = list(lobbies.values())
        lobby_count = len(lobbies_to_clear)
        lobbies.clear()
    
    # Close all connections and clean up
    for lobby in lobbies_to_clear:
        player_count += lobby.players_count()
        try:
            await lobby.broadcast({
                "type": "server_reset",
                "message": "Server has been reset. Please reconnect."
            })
        except Exception as e:
            print(f"[Server] Error broadcasting reset to lobby '{lobby.name}': {e}")
        
        await lobby.close_all_connections("Server reset")
    
    # Clear leaderboard
    async with leaderboard_lock:
        leaderboard.clear()
    
    print(f"[Server] Reset complete. Cleared {lobby_count} lobbies and {player_count} connections")
    
    return web.json_response({
        "success": True,
        "message": "All lobbies and connections cleared",
        "lobbies_cleared": lobby_count,
        "connections_closed": player_count
    })

async def http_health(request):
    async with lobbies_lock:
        active_lobbies = len(lobbies)
        total_players = sum(lobby.players_count() for lobby in lobbies.values())
    
    async with leaderboard_lock:
        leaderboard_entries = len(leaderboard)
    
    return web.json_response({
        "status": "ok",
        "active_lobbies": active_lobbies,
        "total_players": total_players,
        "leaderboard_entries": leaderboard_entries
    })

# App setup
app = web.Application()

app.add_routes([
    web.get("/ws/{lobby}/{player_id}", ws_handler),
    web.post("/api/next_level", http_next_level),
    web.post("/api/complete_game", http_complete_game),
    web.get("/api/leaderboard", http_leaderboard),
    web.get("/api/lobby_status/{lobby_name}", http_lobby_status),
    web.get("/api/clear", http_clear_all),
    web.get("/api/health", http_health),
])

async def serve_index(request):
    return web.FileResponse(GODOT_BUILD_DIR / "index.html")

app.router.add_get("/", serve_index)
app.router.add_static("/", path=str(GODOT_BUILD_DIR), show_index=False)

async def on_startup(app):
    app["cleanup_task"] = asyncio.create_task(cleanup_task())
    print("[Server] Cleanup task started")

async def on_cleanup(app):
    task = app.get("cleanup_task")
    if task:
        task.cancel()
        with asyncio.suppress(asyncio.CancelledError):
            await task
    print("[Server] Cleanup task stopped")

app.on_startup.append(on_startup)
app.on_cleanup.append(on_cleanup)

if __name__ == "__main__":
    print(f"[Server] Starting aiohttp server on http://{HOST}:{PORT}")
    print(f"[Server] Lobby timeout: {LOBBY_TIMEOUT}s (inactivity)")
    print(f"[Server] Incomplete lobby timeout: {INCOMPLETE_LOBBY_TIMEOUT}s (waiting for 2nd player)")
    print(f"[Server] Cleanup interval: {CLEANUP_INTERVAL}s")
    web.run_app(app, host=HOST, port=PORT)