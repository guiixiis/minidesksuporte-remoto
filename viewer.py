"""MiniDesk VIEWER v2 - roda no computador de onde você controla o outro.
Uso: python viewer.py            (abre a tela de login)
  ou python viewer.py --server wss://... --id 123456789 --password SENHA
"""
import argparse, asyncio, base64, hashlib, hmac, io, json, os, struct, threading, time
import tkinter as tk
from tkinter import filedialog
from PIL import Image, ImageTk
from websockets.asyncio.client import connect

CFG = os.path.expanduser("~/.minidesk_viewer.json")


def norm_url(u):
    u = u.strip()
    if u.startswith("https://"): return "wss://" + u[8:]
    if u.startswith("http://"): return "ws://" + u[7:]
    if not u.startswith(("ws://", "wss://")): return "wss://" + u
    return u


class App:
    def __init__(self, args):
        self.root = tk.Tk()
        self.root.title("MiniDesk")
        self.gen = 0
        self.fullscreen = False
        if args.server and args.id and args.password:
            self.start(norm_url(args.server), args.id, args.password)
        else:
            self.login(prefill=args)

    # ================= LOGIN =================
    def login(self, msg="", prefill=None):
        self.gen += 1
        for fn in (lambda: self.root.attributes("-fullscreen", False), lambda: self.root.config(menu="")):
            try: fn()
            except tk.TclError: pass
        for w in self.root.winfo_children():
            w.destroy()
        self.root.title("MiniDesk")
        self.root.geometry("400x340")
        try: saved = json.load(open(CFG))
        except Exception: saved = {}
        f = tk.Frame(self.root, padx=26, pady=18); f.pack(fill="both", expand=True)
        tk.Label(f, text="MiniDesk", font=("Segoe UI", 20, "bold")).pack(pady=(0, 10))
        vars_ = {}
        for key, label, show in (("server", "Servidor", ""), ("id", "ID do computador", ""), ("pw", "Senha", "*")):
            tk.Label(f, text=label, anchor="w").pack(fill="x")
            v = tk.StringVar(); vars_[key] = v
            tk.Entry(f, textvariable=v, show=show, font=("Segoe UI", 11)).pack(fill="x", pady=(0, 8))
        vars_["server"].set((prefill and prefill.server) or saved.get("server", ""))
        vars_["id"].set((prefill and prefill.id) or saved.get("id", ""))
        err = tk.Label(f, text=msg, fg="#c0392b", wraplength=340); err.pack()

        def go(*_):
            s, i, p = vars_["server"].get().strip(), vars_["id"].get().replace(" ", ""), vars_["pw"].get()
            if not (s and i and p):
                err.config(text="Preencha todos os campos"); return
            try: json.dump({"server": norm_url(s), "id": i}, open(CFG, "w"))
            except OSError: pass
            self.start(norm_url(s), i, p)

        tk.Button(f, text="Conectar", command=go, font=("Segoe UI", 11, "bold")).pack(fill="x", pady=8)
        self.root.bind("<Return>", go)

    # ================= SESSÃO =================
    def start(self, url, rid, pw):
        self.gen += 1; gen = self.gen
        self.root.unbind("<Return>")
        for w in self.root.winfo_children():
            w.destroy()
        self.root.geometry("1100x700"); self.root.title(f"MiniDesk - {rid}")
        self.url, self.rid, self.pw = url, rid, pw
        self.loop = asyncio.new_event_loop(); self.ws = None
        self.lock = threading.Lock()
        self.screen = None; self.dirty = False
        self.ready = self.ended = self.leaving = False
        self.user_left = False
        self.msg = "Conectando..."; self.shown_msg = None
        self.perms = {}; self.monitors = []; self.need_menu = False
        self.frames = self.bytes = 0; self.t0 = time.time(); self.ping_ms = None
        self.pending_clip = None; self.last_clip = None
        self.toast = ("", 0); self.down_keys = set(); self.last_move = 0
        self.photo = None; self.img_item = None; self.geom = (0, 0, 1, 1)

        self.status_lbl = tk.Label(self.root, anchor="w", bg="#222", fg="#ddd", padx=8, font=("Segoe UI", 9))
        self.status_lbl.pack(side="bottom", fill="x")
        cv = self.cv = tk.Canvas(self.root, bg="black", highlightthickness=0, takefocus=1)
        cv.pack(fill="both", expand=True)
        cv.focus_set()
        cv.bind("<Button>", lambda e: cv.focus_set(), add="+")
        cv.bind("<Configure>", lambda e: setattr(self, "dirty", self.screen is not None))
        cv.bind("<Motion>", self.on_move)
        cv.bind("<ButtonPress>", lambda e: self.on_btn(e, "down"))
        cv.bind("<ButtonRelease>", lambda e: self.on_btn(e, "up"))
        cv.bind("<MouseWheel>", self.on_wheel)
        cv.bind("<KeyPress>", lambda e: self.on_key(e, True))
        cv.bind("<KeyRelease>", lambda e: self.on_key(e, False))
        cv.bind("<FocusOut>", self.release_keys)
        threading.Thread(target=self.net_thread, daemon=True).start()
        self.root.after(15, lambda: self.poll(gen))
        self.root.after(1000, lambda: self.tick(gen))

    # ---------- rede (thread separada) ----------
    def net_thread(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self.net())

    async def net(self):
        try:
            async with connect(self.url, max_size=2**24, compression=None) as ws:
                self.ws = ws
                await ws.send(json.dumps({"type": "viewer", "id": self.rid}))
                self.msg = "Aguardando aprovação do dono do computador..."
                pinger = asyncio.create_task(self.pinger(ws))
                try:
                    async for msg in ws:
                        if isinstance(msg, bytes): await self.on_frame(ws, msg)
                        else: await self.on_text(ws, json.loads(msg))
                finally:
                    pinger.cancel()
        except Exception as e:
            if not self.user_left:
                self.msg = f"Erro de conexão: {e}"
        if self.ready and not self.user_left and not self.msg.startswith("Erro"):
            self.msg = "Conexão encerrada"
        self.ready = False; self.ended = True

    async def pinger(self, ws):
        while True:
            await asyncio.sleep(2)
            if self.ready:
                await ws.send(json.dumps({"type": "ping", "t": time.time()}))

    async def on_frame(self, ws, msg):
        x, y, w, h, fw, fh = struct.unpack(">HHHHHH", msg[:12])
        region = Image.open(io.BytesIO(msg[12:])); region.load()
        with self.lock:
            if self.screen is None or self.screen.size != (fw, fh):
                self.screen = Image.new("RGB", (fw, fh))
            self.screen.paste(region, (x, y)); self.dirty = True
        self.frames += 1; self.bytes += len(msg)
        await ws.send('{"type": "ack"}')

    async def on_text(self, ws, m):
        t = m.get("type")
        if t == "challenge":
            mac = hmac.new(self.pw.encode(), m["nonce"].encode(), hashlib.sha256).hexdigest()
            await ws.send(json.dumps({"type": "auth", "mac": mac}))
        elif t == "accepted":
            self.perms = m.get("perms", {}); self.monitors = m.get("monitors", [])
            self.msg = "Carregando tela..."; self.ready = True; self.need_menu = True
        elif t in ("denied", "error", "host_left"):
            self.msg = m.get("msg", t)
        elif t == "pong":
            self.ping_ms = int((time.time() - m["t"]) * 1000)
        elif t == "clip":
            self.pending_clip = m.get("text")
        elif t == "file_done":
            self.set_toast(f"✔ Arquivo entregue: {m.get('name')}")
        elif t == "file_err":
            self.set_toast(f"✖ {m.get('msg')}")

    def send(self, d):
        if self.ws is not None and self.ready:
            asyncio.run_coroutine_threadsafe(self.ws.send(json.dumps(d)), self.loop)

    def send_ctl(self, d):
        if self.perms.get("control"):
            self.send(d)

    def set_toast(self, text):
        self.toast = (text, time.time() + 6)

    # ---------- interface ----------
    def build_menu(self):
        m = tk.Menu(self.root)
        c = tk.Menu(m, tearoff=0)
        c.add_command(label="Enviar arquivo...", command=self.send_file,
                      state="normal" if self.perms.get("files") else "disabled")
        c.add_command(label="Tela cheia (F11)", command=self.toggle_fs)
        c.add_separator()
        c.add_command(label="Desconectar", command=self.disconnect)
        m.add_cascade(label="Conexão", menu=c)

        q = tk.Menu(m, tearoff=0)
        self.preset = tk.StringVar(value="balanced")
        for key, label in (("fast", "Rápido (menos qualidade, mais fluido)"),
                           ("balanced", "Equilibrado"), ("high", "Alta qualidade (nítido)")):
            q.add_radiobutton(label=label, variable=self.preset, value=key,
                              command=lambda: self.send({"type": "settings", "preset": self.preset.get()}))
        m.add_cascade(label="Qualidade", menu=q)

        if len(self.monitors) > 1:
            mm = tk.Menu(m, tearoff=0)
            self.mon = tk.IntVar(value=1)
            for i, (w, h) in enumerate(self.monitors, 1):
                mm.add_radiobutton(label=f"Monitor {i} ({w}x{h})", variable=self.mon, value=i,
                                   command=lambda: self.send({"type": "monitor", "i": self.mon.get()}))
            m.add_cascade(label="Monitor", menu=mm)

        o = tk.Menu(m, tearoff=0)
        self.clip_on = tk.BooleanVar(value=bool(self.perms.get("clip")))
        o.add_checkbutton(label="Sincronizar área de transferência", variable=self.clip_on,
                          state="normal" if self.perms.get("clip") else "disabled")
        m.add_cascade(label="Opções", menu=o)
        self.root.config(menu=m)
        try: self.last_clip = self.root.clipboard_get()
        except tk.TclError: self.last_clip = ""

    def poll(self, gen):
        if gen != self.gen:
            return
        if self.need_menu:
            self.need_menu = False; self.build_menu()
        if self.dirty and self.screen is not None:
            self.redraw()
        elif self.screen is None and self.msg != self.shown_msg:
            self.shown_msg = self.msg
            self.cv.delete("all"); self.img_item = None
            self.cv.create_text(self.cv.winfo_width() // 2, self.cv.winfo_height() // 2,
                                text=self.msg, fill="white", font=("Segoe UI", 16))
        if self.ended and not self.leaving:
            self.leaving = True
            msg = "" if self.user_left else self.msg
            self.root.after(150 if self.user_left else 2500, lambda: gen == self.gen and self.login(msg))
        self.root.after(15, lambda: self.poll(gen))

    def redraw(self):
        cw, ch = max(self.cv.winfo_width(), 1), max(self.cv.winfo_height(), 1)
        with self.lock:
            scr = self.screen; self.dirty = False
            s = min(cw / scr.width, ch / scr.height)
            dw, dh = max(int(scr.width * s), 1), max(int(scr.height * s), 1)
            img = scr.resize((dw, dh), Image.Resampling.BILINEAR)
        self.photo = ImageTk.PhotoImage(img)
        ox, oy = (cw - dw) // 2, (ch - dh) // 2
        self.geom = (ox, oy, dw, dh)
        if self.img_item is None:
            self.cv.delete("all")
            self.img_item = self.cv.create_image(ox, oy, anchor="nw", image=self.photo)
        else:
            self.cv.coords(self.img_item, ox, oy)
            self.cv.itemconfig(self.img_item, image=self.photo)

    def tick(self, gen):
        if gen != self.gen:
            return
        now = time.time(); dt = max(now - self.t0, 0.001)
        fps, kbs = self.frames / dt, self.bytes / 1024 / dt
        self.frames = self.bytes = 0; self.t0 = now
        if self.ready:
            p = self.perms
            modo = "controle" if p.get("control") else "somente visualização"
            text = f"{fps:.0f} fps  |  {kbs:.0f} KB/s  |  ping {self.ping_ms if self.ping_ms is not None else '-'} ms  |  {modo}"
            if self.toast[1] > now:
                text = self.toast[0]
            self.status_lbl.config(text=text)
            # área de transferência
            if self.perms.get("clip") and self.clip_on.get():
                if self.pending_clip is not None:
                    self.last_clip = self.pending_clip
                    self.root.clipboard_clear(); self.root.clipboard_append(self.pending_clip)
                    self.pending_clip = None
                else:
                    try: cur = self.root.clipboard_get()
                    except tk.TclError: cur = ""
                    if cur and cur != self.last_clip and len(cur) < 1_000_000:
                        self.last_clip = cur
                        self.send({"type": "clip", "text": cur})
        else:
            self.status_lbl.config(text=self.msg)
        self.root.after(1000, lambda: self.tick(gen))

    def toggle_fs(self):
        self.fullscreen = not self.fullscreen
        self.root.attributes("-fullscreen", self.fullscreen)

    def disconnect(self):
        self.user_left = True
        if self.ws is not None:
            asyncio.run_coroutine_threadsafe(self.ws.close(), self.loop)
        else:
            self.ended = True

    def send_file(self):
        path = filedialog.askopenfilename(title="Escolha o arquivo para enviar")
        if path:
            threading.Thread(target=self._send_file, args=(path,), daemon=True).start()

    def _send_file(self, path):
        size, name = os.path.getsize(path), os.path.basename(path)

        def tx(d):
            asyncio.run_coroutine_threadsafe(self.ws.send(json.dumps(d)), self.loop).result(60)

        try:
            tx({"type": "file_start", "name": name, "size": size}); sent = 0
            with open(path, "rb") as f:
                while True:
                    b = f.read(192 * 1024)
                    if not b: break
                    tx({"type": "file_chunk", "data": base64.b64encode(b).decode()})
                    sent += len(b)
                    self.set_toast(f"Enviando {name}: {sent * 100 // max(size, 1)}%")
            tx({"type": "file_end"})
        except Exception as e:
            self.set_toast(f"✖ Falha ao enviar: {e}")

    # ---------- entrada ----------
    def norm(self, e):
        ox, oy, dw, dh = self.geom
        x, y = (e.x - ox) / dw, (e.y - oy) / dh
        return (x, y) if 0 <= x <= 1 and 0 <= y <= 1 else None

    def on_move(self, e):
        now = time.time()
        if now - self.last_move < 1 / 60: return
        self.last_move = now
        p = self.norm(e)
        if p: self.send_ctl({"type": "move", "x": p[0], "y": p[1]})

    def on_btn(self, e, kind):
        if e.num in (4, 5):  # rolagem no Linux
            if kind == "down": self.send_ctl({"type": "scroll", "dy": 1 if e.num == 4 else -1})
            return
        p = self.norm(e)
        if p and e.num in (1, 2, 3):
            self.send_ctl({"type": kind, "x": p[0], "y": p[1], "b": e.num})

    def on_wheel(self, e):
        n = max(1, abs(e.delta) // 120)
        self.send_ctl({"type": "scroll", "dy": n if e.delta > 0 else -n})

    def on_key(self, e, down):
        if e.keysym == "F11":
            if down: self.toggle_fs()
            return "break"
        ch = e.char
        if len(ch) == 1 and ch.isprintable():
            if down: self.send_ctl({"type": "char", "c": ch})
        elif len(e.keysym) == 1:  # ex.: Ctrl+C
            if down: self.send_ctl({"type": "char", "c": e.keysym.lower()})
        else:
            k = e.keysym.lower()
            (self.down_keys.add if down else self.down_keys.discard)(k)
            self.send_ctl({"type": "key", "k": k, "d": down})
        return "break"

    def release_keys(self, _e=None):
        for k in list(self.down_keys):
            self.send_ctl({"type": "key", "k": k, "d": False})
        self.down_keys.clear()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--server"); ap.add_argument("--id"); ap.add_argument("--password")
    App(ap.parse_args()).root.mainloop()
