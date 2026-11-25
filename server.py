"""Servidor de retransmissão (relay). Roda em uma máquina com IP público/túnel.
Ele só repassa mensagens entre o Host e o Viewer; não vê senhas.
Uso: python server.py            (porta 8765, ou defina a variável PORT)
"""
import asyncio, json, os, random
from websockets.asyncio.server import serve

hosts = {}  # id -> {"ws": websocket do host, "viewer": websocket do viewer ou None}


def new_id():
    while True:
        i = str(random.randint(100_000_000, 999_999_999))
        if i not in hosts:
            return i


async def send(ws, obj):
    try:
        await ws.send(json.dumps(obj))
    except Exception:
        pass


async def handler(ws):
    role = rid = None
    try:
        hello = json.loads(await asyncio.wait_for(ws.recv(), 10))
        if hello.get("type") == "host":
            rid = hello.get("id")
            if not (isinstance(rid, str) and rid.isdigit() and len(rid) == 9) or rid in hosts:
                rid = new_id()
            hosts[rid] = {"ws": ws, "viewer": None}
            role = "host"
            await send(ws, {"type": "registered", "id": rid})
        elif hello.get("type") == "viewer":
            rid = hello.get("id")
            h = hosts.get(rid)
            if not h:
                await send(ws, {"type": "error", "msg": "ID não encontrado"})
                return
            if h["viewer"] is not None:
                await send(ws, {"type": "error", "msg": "Esse computador já está em uso"})
                return
            h["viewer"] = ws
            role = "viewer"
            await send(h["ws"], {"type": "viewer_joined"})
            await send(ws, {"type": "joined"})
        else:
            return

        async for msg in ws:
            h = hosts.get(rid)
            if not h:
                break
            if role == "host":
                v = h["viewer"]
                if v is not None:
                    await v.send(msg)
                    if isinstance(msg, str) and json.loads(msg).get("type") == "kick":
                        await v.close()
            else:
                await h["ws"].send(msg)
    except Exception:
        pass
    finally:
        h = hosts.get(rid) if rid else None
        if h and role == "host" and h["ws"] is ws:
            del hosts[rid]
            if h["viewer"] is not None:
                await send(h["viewer"], {"type": "host_left", "msg": "O host desconectou"})
                await h["viewer"].close()
        elif h and role == "viewer" and h["viewer"] is ws:
            h["viewer"] = None
            await send(h["ws"], {"type": "viewer_left"})


async def main():
    port = int(os.environ.get("PORT", 8765))
    async with serve(handler, "0.0.0.0", port, max_size=2**25, compression=None):
        print(f"Relay rodando na porta {port}")
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
