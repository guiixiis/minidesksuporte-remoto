"""MiniDesk HOST v3 - roda no computador que será acessado.
Uso: python host.py --server ws://IP:8765
"""
import argparse, asyncio, hashlib, hmac, io, json, os, queue, secrets, struct, sys, threading, time
from collections import deque
from concurrent.futures import ThreadPoolExecutor

if sys.platform == "win32":  # coordenadas corretas com escala de tela
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

import tkinter as tk
from tkinter import filedialog, simpledialog
import mss
from PIL import Image, ImageChops
from pynput.keyboard import Controller as KB, Key
from pynput.mouse import Controller as Mouse, Button
from websockets.asyncio.client import connect

from common import (V_VIDEO, V_AUDIO, V_FILE, V_IMG, WS_MAX, AUDIO_FMT, FileReceiver, ClipSync,
                    AudioCapture, audio_available, to_pcm, send_file, recv_dir, human)

DEFAULT_SERVER = "ws://192.168.2.130:8765"

PRESETS = {
    "fast":     dict(qmax=55, max_w=1280, fps=25),
    "balanced": dict(qmax=75, max_w=1920, fps=30),
    "high":     dict(qmax=92, max_w=3840, fps=30),
}
cfg = {"mon": 1, "q": 75, "qmax": 75, "max_w": 1920, "fps": 30, "reset": True, "preset": "balanced"}
CFG_FILE = os.path.expanduser("~/.minidesk_host.json")
ID_FILE = os.path.expanduser("~/.minidesk_id")
ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"

MSS = getattr(mss, "MSS", None) or mss.mss  # compatível com versões novas e antigas do mss
mouse, kb = Mouse(), KB()
pool = ThreadPoolExecutor(max_workers=1)  # mss precisa ficar sempre na mesma thread
_tl = threading.local()
cap = AudioCapture()

with MSS() as _s:
    MONS = [dict(m) for m in _s.monitors[1:]]

_names = {"return": "enter", "backspace": "backspace", "tab": "tab", "escape": "esc",
          "delete": "delete", "insert": "insert", "left": "left", "right": "right",
          "up": "up", "down": "down", "home": "home", "end": "end",
          "prior": "page_up", "next": "page_down", "shift_l": "shift", "shift_r": "shift_r",
          "control_l": "ctrl", "control_r": "ctrl_r", "alt_l": "alt", "alt_r": "alt_r",
          "caps_lock": "caps_lock", "super_l": "cmd", "super_r": "cmd_r"}
SPECIAL = {k: getattr(Key, v) for k, v in _names.items() if hasattr(Key, v)}
for _i in range(1, 13):
    SPECIAL[f"f{_i}"] = getattr(Key, f"f{_i}")
BTN = {1: Button.left, 2: Button.middle, 3: Button.right}


def new_password():
    return "".join(secrets.choice(ALPHABET) for _ in range(8))


def apply_preset(name):
    p = PRESETS.get(name)
    if p:
        cfg.update(qmax=p["qmax"], q=p["qmax"], max_w=p["max_w"], fps=p["fps"], reset=True, preset=name)


def grab_frame():
    """Captura a tela e devolve só a região que mudou (ou None se nada mudou).
    Formato: tipo(1) + x,y,w,h,larguraTotal,alturaTotal (6x uint16) + JPEG da região."""
    t = _tl
    if getattr(t, "sct", None) is None:
        t.sct = MSS()
        t.prev = None
    if cfg["reset"]:
        t.prev = None
        cfg["reset"] = False
    shot = t.sct.grab(t.sct.monitors[cfg["mon"]])
    img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    mw = cfg["max_w"]
    if img.width > mw:
        img = img.resize((mw, int(img.height * mw / img.width)), Image.Resampling.BILINEAR)
    W, H = img.size
    prev = t.prev
    if prev is None or prev.size != img.size:
        box = (0, 0, W, H)
    else:
        box = ImageChops.difference(prev, img).getbbox()
        if box is None:
            return None
        if (box[2] - box[0]) * (box[3] - box[1]) > 0.6 * W * H:
            box = (0, 0, W, H)
    t.prev = img
    q = cfg["q"]
    buf = io.BytesIO()
    img.crop(box).save(buf, "JPEG", quality=q, subsampling=0 if q >= 85 else 2)
    x0, y0, x1, y1 = box
    return struct.pack(">BHHHHHH", V_VIDEO, x0, y0, x1 - x0, y1 - y0, W, H) + buf.getvalue()


def pos(m):
    g = MONS[cfg["mon"] - 1]
    x = min(max(m["x"], 0), 1)
    y = min(max(m["y"], 0), 1)
    return (g["left"] + int(x * (g["width"] - 1)), g["top"] + int(y * (g["height"] - 1)))


class HostApp:
    def __init__(self, url, auto, password=None):
        self.url, self.auto = url, auto
        self.password = password or new_password()
        self.ui_q = queue.Queue()
        self.loop = asyncio.new_event_loop()
        self.ws = None
        self.ask_win = None
        self.cur = None  # sessão ativa (para enviar arquivos ao viewer)

        r = self.root = tk.Tk()
        r.title("MiniDesk - Host")
        r.resizable(False, False)
        f = tk.Frame(r, padx=20, pady=16)
        f.pack()
        tk.Label(f, text="Seu ID", font=("Segoe UI", 10)).grid(row=0, column=0, sticky="w")
        self.id_var = tk.StringVar(value="conectando...")
        tk.Entry(f, textvariable=self.id_var, font=("Consolas", 22), width=12,
                 state="readonly", justify="center").grid(row=1, column=0)
        tk.Button(f, text="Copiar", command=lambda: self.copy(self.id_var.get())).grid(row=1, column=1, padx=6)
        tk.Label(f, text="Senha", font=("Segoe UI", 10)).grid(row=2, column=0, sticky="w", pady=(10, 0))
        self.pw_var = tk.StringVar(value=self.password)
        tk.Entry(f, textvariable=self.pw_var, font=("Consolas", 22), width=12,
                 state="readonly", justify="center").grid(row=3, column=0)
        tk.Button(f, text="Copiar", command=lambda: self.copy(self.pw_var.get())).grid(row=3, column=1, padx=6)
        tk.Button(f, text="Nova senha", command=self.regen).grid(row=4, column=0, sticky="w", pady=4)
        self.status = tk.Label(f, text="Conectando ao servidor...", fg="#555", font=("Segoe UI", 10, "bold"))
        self.status.grid(row=5, column=0, columnspan=2, pady=(8, 2))
        bf = tk.Frame(f); bf.grid(row=6, column=0, columnspan=2, pady=4)
        tk.Button(bf, text="Enviar arquivo ao viewer...", command=self.pick_files).pack(side="left", padx=3)
        tk.Button(bf, text="Pasta de recebidos", command=self.open_folder).pack(side="left", padx=3)
        tk.Button(bf, text="Desconectar", command=self.kick).pack(side="left", padx=3)
        self.logbox = tk.Text(f, height=8, width=56, state="disabled", font=("Consolas", 9))
        self.logbox.grid(row=7, column=0, columnspan=2, pady=(8, 0))

        threading.Thread(target=self.run_loop, daemon=True).start()
        r.after(100, self.pump)

    # ---------- GUI ----------
    def copy(self, txt):
        self.root.clipboard_clear(); self.root.clipboard_append(txt)

    def regen(self):
        self.password = new_password()
        self.pw_var.set(self.password)
        self.log("Nova senha gerada.")

    def kick(self):
        if self.ws is not None:
            asyncio.run_coroutine_threadsafe(self.ws.send(json.dumps({"type": "kick"})), self.loop)

    def open_folder(self):
        d = recv_dir()
        try:
            if sys.platform == "win32": os.startfile(d)
            elif sys.platform == "darwin": os.system(f'open "{d}"')
            else: os.system(f'xdg-open "{d}" >/dev/null 2>&1 &')
        except Exception as e:
            self.log(f"Não consegui abrir a pasta: {e}")

    def pick_files(self):
        paths = filedialog.askopenfilenames(title="Escolha os arquivos para enviar ao viewer")
        if paths:
            self.send_paths(list(paths))

    def send_paths(self, paths):
        asyncio.run_coroutine_threadsafe(self.send_to_viewer(paths), self.loop)

    def ui(self, *msg):
        self.ui_q.put(msg)

    def log(self, txt):
        self.logbox.config(state="normal")
        self.logbox.insert("end", time.strftime("%H:%M:%S ") + txt + "\n")
        self.logbox.see("end")
        self.logbox.config(state="disabled")

    def pump(self):
        try:
            while True:
                kind, *a = self.ui_q.get_nowait()
                if kind == "id": self.id_var.set(a[0])
                elif kind == "status": self.status.config(text=a[0])
                elif kind == "log": self.log(a[0])
                elif kind == "ask": self.ask_consent(a[0])
                elif kind == "close_ask" and self.ask_win is not None:
                    try: self.ask_win.destroy()
                    except tk.TclError: pass
                    self.ask_win = None
        except queue.Empty:
            pass
        self.root.after(100, self.pump)

    def ask_consent(self, fut):
        win = self.ask_win = tk.Toplevel(self.root)
        win.title("Pedido de conexão")
        win.attributes("-topmost", True)
        done = {"v": False}

        def choose(v):
            if done["v"]:
                return
            done["v"] = True
            self.loop.call_soon_threadsafe(lambda: fut.done() or fut.set_result(v))
            try: win.destroy()
            except tk.TclError: pass

        tk.Label(win, text="Alguém digitou a senha correta e quer\nacessar este computador.\n\nO que você permite?",
                 padx=20, pady=12).pack()
        opts = {}
        for key, txt in (("control", "Controlar mouse e teclado"),
                         ("audio", "Ouvir o som deste computador"),
                         ("files", "Transferir arquivos"),
                         ("clip", "Sincronizar área de transferência")):
            v = tk.BooleanVar(value=True); opts[key] = v
            tk.Checkbutton(win, text=txt, variable=v, anchor="w").pack(fill="x", padx=24)
        bf = tk.Frame(win); bf.pack(pady=12)
        tk.Button(bf, text="Permitir", width=12,
                  command=lambda: choose({k: v.get() for k, v in opts.items()})).pack(side="left", padx=4)
        tk.Button(bf, text="Recusar", width=12, command=lambda: choose(None)).pack(side="left", padx=4)
        win.protocol("WM_DELETE_WINDOW", lambda: choose(None))
        win.after(60000, lambda: choose(None))
        win.bell(); win.lift(); win.focus_force()

    # ---------- rede ----------
    def run_loop(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self.main())

    async def main(self):
        while True:
            try:
                async with connect(self.url, max_size=WS_MAX, compression=None) as ws:
                    self.ws = ws
                    await self.session(ws)
            except Exception as e:
                self.ui("status", "Sem conexão com o servidor")
                self.ui("log", f"Erro: {e}")
            self.ws = None
            await asyncio.sleep(3)

    async def send_to_viewer(self, paths):
        s = self.cur
        if not s or not s["perms"].get("files"):
            self.ui("log", "Ninguém conectado com permissão de arquivos.")
            return
        ws = s["ws"]
        async with s["lock"]:
            for p in paths:
                s["tid"] += 1
                last = {"pct": -1}

                def progress(name, sent, size, dt):
                    pct = sent * 100 // max(size, 1)
                    if pct != last["pct"] and pct % 10 == 0:
                        last["pct"] = pct
                        self.ui("status", f"Enviando {name}: {pct}% ({human(sent / max(dt, 0.001))}/s)")

                try:
                    self.ui("log", f"Enviando {os.path.basename(p)} ({human(os.path.getsize(p))})...")
                    await send_file(lambda d: ws.send(json.dumps(d)), ws.send, p, s["tid"], progress)
                except Exception as e:
                    self.ui("log", f"Falha ao enviar {os.path.basename(p)}: {e}")
            self.ui("status", "Conectado")

    async def session(self, ws):
        loop = asyncio.get_running_loop()
        st = dict(sid=0, authed=False, nonce="", tasks=[], down=set(), perms=None, inflight=0,
                  sent=deque(), fails=0, pending=None, audio=None, clip=None)
        ack_evt = asyncio.Event()
        recv = FileReceiver()
        j = json.dumps
        send_bin = ws.send
        send_text = lambda d: ws.send(j(d))

        def stop():
            for t in st["tasks"]:
                t.cancel()
            st["tasks"] = []
            if st["audio"]:
                st["audio"].cancel(); st["audio"] = None
            cap.stop(); cap.sink = None
            for k in st["down"]:
                try: kb.release(k)
                except Exception: pass
            st["down"].clear()
            recv.abort_all()
            st["authed"] = False; st["perms"] = None; st["clip"] = None
            st["inflight"] = 0; st["sent"].clear()
            self.cur = None

        async def stream():
            idle = 0
            while True:
                if st["inflight"] >= 2:  # no máx. 2 quadros "no ar"
                    try:
                        await asyncio.wait_for(ack_evt.wait(), 1.5)
                    except asyncio.TimeoutError:
                        st["inflight"] = 0; st["sent"].clear()
                    ack_evt.clear()
                    continue
                data = await loop.run_in_executor(pool, grab_frame)
                if data is None:
                    idle += 1
                    await asyncio.sleep(max(1 / cfg["fps"], min(0.12, idle * 0.01)))
                    continue
                idle = 0
                st["inflight"] += 1; st["sent"].append(time.monotonic())
                await ws.send(data)
                await asyncio.sleep(1 / cfg["fps"])

        async def audio_loop():
            aq = asyncio.Queue(maxsize=8)

            def put(data):
                if aq.full():
                    aq.get_nowait()
                aq.put_nowait(data)

            cap.sink = lambda d: loop.call_soon_threadsafe(put, d)
            cap.start()
            warned = False
            try:
                while True:
                    try:
                        data = await asyncio.wait_for(aq.get(), 2)
                    except asyncio.TimeoutError:
                        if cap.error and not warned:
                            warned = True
                            self.ui("log", f"Som indisponível: {cap.error}")
                            await ws.send(j({"type": "audio_err", "msg": f"Som indisponível no host: {cap.error}"}))
                        continue
                    rate, ch = AUDIO_FMT.get(cfg["preset"], AUDIO_FMT["balanced"])
                    pcm = to_pcm(data, rate, ch)
                    tr = getattr(ws, "transport", None)
                    if tr is not None and tr.get_write_buffer_size() > 300_000:
                        continue  # rede lenta: descarta o som em vez de atrasar tudo
                    await ws.send(struct.pack(">BIB", V_AUDIO, rate, ch) + pcm)
            finally:
                cap.stop(); cap.sink = None

        def set_audio(on):
            if st["audio"]:
                st["audio"].cancel(); st["audio"] = None
            if on and st["perms"] and st["perms"]["audio"]:
                st["audio"] = asyncio.create_task(audio_loop())

        def adapt(rtt):  # qualidade automática conforme a rede
            if rtt > 0.35: cfg["q"] = max(35, cfg["q"] - 6)
            elif rtt < 0.12: cfg["q"] = min(cfg["qmax"], cfg["q"] + 2)

        async def deny(msg):
            await ws.send(j({"type": "denied", "msg": msg}))
            await ws.send(j({"type": "kick"}))

        async def handle_auth(m, sid):
            expected = hmac.new(self.password.encode(), st["nonce"].encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(expected, str(m.get("mac", ""))):
                st["fails"] += 1
                await asyncio.sleep(min(2 * st["fails"], 30))  # freia força bruta
                self.ui("log", "Tentativa com senha incorreta.")
                return await deny("Senha incorreta")
            if self.auto:
                choice = dict(control=True, audio=True, files=True, clip=True)
            else:
                fut = st["pending"] = loop.create_future()
                self.ui("status", "Pedido de conexão - responda na janela")
                self.ui("ask", fut)
                choice = await fut
            if st["sid"] != sid:
                return
            if choice is None:
                self.ui("status", "Aguardando conexão...")
                return await deny("Conexão recusada pelo dono do computador")
            perms = dict(choice)
            if perms["audio"] and not audio_available():
                perms["audio"] = False
                self.ui("log", "Som desativado: instale 'soundcard' e 'numpy'.")
            st["perms"] = perms; st["authed"] = True
            apply_preset("balanced"); cfg["mon"] = 1
            self.cur = dict(ws=ws, perms=perms, lock=asyncio.Lock(), tid=0)
            await ws.send(j({"type": "accepted", "perms": perms,
                             "monitors": [[g["width"], g["height"]] for g in MONS]}))
            st["tasks"].append(asyncio.create_task(stream()))
            if perms["clip"]:
                st["clip"] = ClipSync(send_text, send_bin, lambda s: self.ui("log", s))
                st["tasks"].append(asyncio.create_task(st["clip"].run()))
            set_audio(True)
            on = [k for k, v in perms.items() if v]
            self.ui("status", "Conectado")
            self.ui("log", "Conexão aceita. Permissões: " + (", ".join(on) or "só visualizar"))

        def apply(m):
            t = m.get("type")
            if t == "move":
                mouse.position = pos(m)
            elif t == "down":
                mouse.position = pos(m); mouse.press(BTN.get(m["b"], Button.left))
            elif t == "up":
                mouse.position = pos(m); mouse.release(BTN.get(m["b"], Button.left))
            elif t == "scroll":
                mouse.scroll(0, int(m["dy"]))
            elif t == "char":
                kb.type(m["c"])
            elif t == "key":
                k = SPECIAL.get(m["k"])
                if k is None:
                    return
                if m["d"]:
                    kb.press(k); st["down"].add(k)
                else:
                    kb.release(k); st["down"].discard(k)

        async def on_binary(msg):
            perms = st["perms"]
            k = msg[0]
            if k == V_FILE and perms["files"]:
                tid = struct.unpack(">I", msg[1:5])[0]
                if recv.chunk(tid, msg[5:]) == "ERR":
                    await send_text({"type": "file_err", "id": tid, "msg": "Tamanho inválido"})
            elif k == V_IMG and perms["clip"] and st["clip"]:
                await st["clip"].recv_image(msg[1:])

        async def on_text(t, m):
            perms = st["perms"]
            if t == "ping":
                await send_text({"type": "pong", "t": m["t"]})
            elif t == "settings":
                apply_preset(m.get("preset"))
            elif t == "monitor":
                i = int(m["i"])
                if 1 <= i <= len(MONS):
                    cfg["mon"] = i; cfg["reset"] = True
            elif t == "audio":
                set_audio(bool(m.get("on")))
            elif t == "clipcfg":
                if st["clip"]: st["clip"].enabled = bool(m.get("on"))
            elif t == "clip":
                if perms["clip"] and st["clip"]: await st["clip"].recv_text(m.get("text"))
            elif t == "file_start":
                if perms["files"]:
                    err = recv.start(m)
                    if err:
                        await send_text({"type": "file_err", "id": m["id"], "msg": err})
                    else:
                        self.ui("log", f"Recebendo {m.get('name')} ({human(int(m.get('size', 0)))})...")
            elif t == "file_end":
                if perms["files"]:
                    r = recv.end(m)
                    if r:
                        self.ui("log", ("Arquivo recebido: " + r["path"]) if r["ok"] else f"Arquivo corrompido: {r['name']}")
                        await send_text({"type": "file_done", "id": m["id"], "name": r["name"], "ok": r["ok"],
                                         "msg": "" if r["ok"] else "Falha na verificação (SHA-256)"})
            elif t == "file_done":
                self.ui("log", f"Viewer recebeu {m.get('name')}: " + ("OK" if m.get("ok") else m.get("msg", "falhou")))
            elif t == "file_err":
                self.ui("log", f"Viewer recusou arquivo: {m.get('msg')}")
            elif perms["control"]:
                apply(m)

        try:
            try:
                saved = open(ID_FILE).read().strip()
            except OSError:
                saved = None
            await ws.send(j({"type": "host", "id": saved}))
            async for msg in ws:
                try:
                    if isinstance(msg, bytes):
                        if st["authed"]:
                            await on_binary(msg)
                        continue
                    m = json.loads(msg)
                    t = m.get("type")
                    if t == "registered":
                        open(ID_FILE, "w").write(m["id"])
                        self.ui("id", m["id"]); self.ui("status", "Aguardando conexão...")
                        self.ui("log", "Conectado ao servidor.")
                    elif t == "viewer_joined":
                        stop(); st["sid"] += 1
                        st["nonce"] = secrets.token_hex(16)
                        self.ui("status", "Alguém está tentando conectar...")
                        await ws.send(j({"type": "challenge", "nonce": st["nonce"]}))
                    elif t == "viewer_left":
                        stop(); st["sid"] += 1
                        if st["pending"] and not st["pending"].done():
                            st["pending"].set_result(None)
                        self.ui("close_ask"); self.ui("status", "Aguardando conexão...")
                        self.ui("log", "Quem acessava desconectou.")
                    elif t == "auth":
                        asyncio.create_task(handle_auth(m, st["sid"]))
                    elif t == "ack":
                        st["inflight"] = max(0, st["inflight"] - 1)
                        if st["sent"]:
                            adapt(time.monotonic() - st["sent"].popleft())
                        ack_evt.set()
                    elif st["authed"]:
                        await on_text(t, m)
                except Exception as e:
                    self.ui("log", f"Erro ao processar mensagem: {type(e).__name__}: {e}")
        finally:
            stop()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", help="ex: ws://1.2.3.4:8765 ou wss://xxx.ngrok-free.app")
    ap.add_argument("--password", help="senha fixa (padrão: aleatória a cada execução)")
    ap.add_argument("--auto-accept", action="store_true",
                    help="aceita sem perguntar, com todas as permissões (use só com senha forte)")
    a = ap.parse_args()
    url = a.server
    if not url:
        try:
            url = json.load(open(CFG_FILE)).get("server")
        except Exception:
            url = None
    if not url:
        url = DEFAULT_SERVER
    url = url.strip()
    if url.startswith("https://"): url = "wss://" + url[8:]
    elif url.startswith("http://"): url = "ws://" + url[7:]
    elif not url.startswith(("ws://", "wss://")): url = "wss://" + url
    try:
        json.dump({"server": url}, open(CFG_FILE, "w"))
    except OSError:
        pass
    HostApp(url, a.auto_accept, a.password).root.mainloop()
