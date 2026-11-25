"""Código compartilhado entre host.py e viewer.py:
protocolo binário, transferência de arquivos, área de transferência e áudio."""
import asyncio, hashlib, io, os, queue, re, struct, subprocess, sys, threading, time

# Tipos de mensagem binária (1º byte)
V_VIDEO, V_AUDIO, V_FILE, V_IMG = 1, 2, 3, 4
CHUNK = 256 * 1024
MAX_FILE = 2 << 30          # 2 GB
MAX_CLIP_IMG = 8 << 20      # 8 MB
WS_MAX = 2**25


# ===================== ARQUIVOS =====================
def recv_dir():
    d = os.path.join(os.path.expanduser("~"), "Downloads", "MiniDesk")
    os.makedirs(d, exist_ok=True)
    return d


def safe_name(name):
    n = re.sub(r"[^\w.\- ()]", "_", os.path.basename(str(name).replace("\\", "/")))
    return n.lstrip(". ") or "arquivo"


def human(n):
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024


class FileReceiver:
    """Recebe arquivos em partes, confere o SHA-256 e só então grava o arquivo final."""

    def __init__(self):
        self.cur = {}

    def start(self, m):
        tid, size = int(m["id"]), int(m.get("size", 0))
        if size > MAX_FILE:
            return "Arquivo grande demais (máx. 2 GB)"
        path = os.path.join(recv_dir(), safe_name(m.get("name", "arquivo")))
        base, ext = os.path.splitext(path); n = 1
        while os.path.exists(path) or os.path.exists(path + ".part"):
            path = f"{base} ({n}){ext}"; n += 1
        self.cur[tid] = dict(f=open(path + ".part", "wb"), path=path, got=0, size=size,
                             h=hashlib.sha256(), t0=time.time())
        return None

    def chunk(self, tid, data):
        """Devolve o estado do arquivo, None se o id for desconhecido ou 'ERR' se algo estiver errado."""
        fl = self.cur.get(tid)
        if fl is None:
            return None
        fl["got"] += len(data)
        if fl["got"] > fl["size"]:
            self._drop(tid)
            return "ERR"
        fl["f"].write(data); fl["h"].update(data)
        return fl

    def end(self, m):
        tid = int(m["id"])
        fl = self.cur.pop(tid, None)
        if fl is None:
            return None
        fl["f"].close()
        ok = fl["got"] == fl["size"] and fl["h"].hexdigest() == m.get("sha256")
        if ok:
            os.replace(fl["path"] + ".part", fl["path"])
        else:
            try: os.remove(fl["path"] + ".part")
            except OSError: pass
        return dict(ok=ok, name=os.path.basename(fl["path"]), path=fl["path"], size=fl["size"])

    def _drop(self, tid):
        fl = self.cur.pop(tid, None)
        if fl:
            try:
                fl["f"].close(); os.remove(fl["path"] + ".part")
            except OSError:
                pass

    def abort_all(self):
        for tid in list(self.cur):
            self._drop(tid)


async def send_file(send_text, send_bin, path, tid, progress=None):
    """send_text(dict) e send_bin(bytes) são corrotinas. Calcula o SHA-256 enquanto envia."""
    size, name = os.path.getsize(path), os.path.basename(path)
    if size > MAX_FILE:
        raise ValueError("Arquivo grande demais (máx. 2 GB)")
    loop = asyncio.get_running_loop()
    h = hashlib.sha256()
    await send_text({"type": "file_start", "id": tid, "name": name, "size": size})
    sent, t0 = 0, time.time()
    head = struct.pack(">BI", V_FILE, tid)
    with open(path, "rb") as f:
        while True:
            b = await loop.run_in_executor(None, f.read, CHUNK)
            if not b:
                break
            h.update(b)
            await send_bin(head + b)
            sent += len(b)
            if progress:
                progress(name, sent, size, time.time() - t0)
    await send_text({"type": "file_end", "id": tid, "sha256": h.hexdigest()})


# ===================== ÁREA DE TRANSFERÊNCIA =====================
def _sig_text(s):
    return "t" + hashlib.sha1(s.encode("utf-8", "ignore")).hexdigest()


def _sig_img(img):
    return "i%dx%d" % img.size + hashlib.md5(img.tobytes()).hexdigest()


def clip_read():
    """Devolve (tipo, conteúdo, assinatura). Tipo: text | image | files | None."""
    try:
        import pyperclip
        t = pyperclip.paste()
    except Exception:
        t = ""
    if isinstance(t, str) and t:
        return ("text", t, _sig_text(t))
    try:
        from PIL import ImageGrab
        g = ImageGrab.grabclipboard()
    except Exception:
        g = None
    if isinstance(g, list):
        return ("files", [str(p) for p in g], "f" + "|".join(map(str, g)))
    if g is not None:
        img = g.convert("RGB")
        buf = io.BytesIO(); img.save(buf, "PNG")
        return ("image", buf.getvalue(), _sig_img(img))
    return (None, None, "empty")


def clip_write_text(t):
    import pyperclip
    pyperclip.copy(t)


def _win_set_image(img):
    import ctypes
    from ctypes import wintypes
    out = io.BytesIO(); img.save(out, "BMP")
    data = out.getvalue()[14:]  # CF_DIB = BMP sem o cabeçalho de arquivo
    u32, k32 = ctypes.windll.user32, ctypes.windll.kernel32
    k32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]; k32.GlobalAlloc.restype = ctypes.c_void_p
    k32.GlobalLock.argtypes = [ctypes.c_void_p]; k32.GlobalLock.restype = ctypes.c_void_p
    k32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    u32.OpenClipboard.argtypes = [wintypes.HWND]
    u32.SetClipboardData.argtypes = [wintypes.UINT, ctypes.c_void_p]
    h = k32.GlobalAlloc(0x0002, len(data))
    p = k32.GlobalLock(h); ctypes.memmove(p, data, len(data)); k32.GlobalUnlock(h)
    for _ in range(10):  # outro programa pode estar usando a área de transferência
        if u32.OpenClipboard(None):
            break
        time.sleep(0.05)
    else:
        raise RuntimeError("área de transferência ocupada")
    try:
        u32.EmptyClipboard(); u32.SetClipboardData(8, h)
    finally:
        u32.CloseClipboard()


def clip_write_image(png):
    from PIL import Image
    img = Image.open(io.BytesIO(png)); img.load(); img = img.convert("RGB")
    if sys.platform == "win32":
        _win_set_image(img)
    elif sys.platform.startswith("linux"):
        buf = io.BytesIO(); img.save(buf, "PNG")
        subprocess.run(["xclip", "-selection", "clipboard", "-t", "image/png", "-i"], input=buf.getvalue(),
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
    else:
        raise NotImplementedError("imagens na área de transferência não suportadas neste sistema")
    return _sig_img(img)


class ClipSync:
    """Sincroniza texto e imagens. Evita loops: ignora o que acabou de receber."""

    def __init__(self, send_text, send_bin, log=lambda s: None):
        self.send_text, self.send_bin, self.log = send_text, send_bin, log
        self.enabled = True; self.last = None; self.mute_until = 0

    async def run(self):
        loop = asyncio.get_running_loop()
        self.last = (await loop.run_in_executor(None, clip_read))[2]  # o que já estava lá não é enviado
        while True:
            await asyncio.sleep(0.8)
            if not self.enabled:
                continue
            try:
                kind, payload, sig = await loop.run_in_executor(None, clip_read)
            except Exception:
                continue
            if sig == self.last:
                continue
            self.last = sig
            if time.time() < self.mute_until:
                continue
            if kind == "text" and len(payload) < 1_000_000:
                await self.send_text({"type": "clip", "text": payload})
            elif kind == "image":
                if len(payload) <= MAX_CLIP_IMG:
                    await self.send_bin(bytes([V_IMG]) + payload)
                else:
                    self.log("Imagem grande demais para sincronizar (máx. 8 MB)")

    async def recv_text(self, text):
        if not self.enabled or not isinstance(text, str) or len(text) >= 1_000_000:
            return
        self.mute_until = time.time() + 1.5
        self.last = _sig_text(text)
        await asyncio.get_running_loop().run_in_executor(None, clip_write_text, text)

    async def recv_image(self, png):
        if not self.enabled or len(png) > MAX_CLIP_IMG:
            return
        self.mute_until = time.time() + 1.5
        try:
            self.last = await asyncio.get_running_loop().run_in_executor(None, clip_write_image, png)
        except Exception as e:
            self.log(f"Não foi possível copiar a imagem: {e}")


# ===================== ÁUDIO =====================
AUDIO_FMT = {"fast": (16000, 1), "balanced": (24000, 2), "high": (48000, 2)}  # (taxa, canais)


def audio_available():
    try:
        import importlib.util
        return importlib.util.find_spec("soundcard") is not None and importlib.util.find_spec("numpy") is not None
    except Exception:
        return False


class AudioCapture:
    """Captura o som que sai do alto-falante do computador (loopback)."""
    RATE = 48000

    def __init__(self):
        self.sink = None; self.want = threading.Event(); self.thread = None; self.error = None

    def start(self):
        self.error = None; self.want.set()
        if self.thread is None or not self.thread.is_alive():
            self.thread = threading.Thread(target=self._run, daemon=True)
            self.thread.start()

    def stop(self):
        self.want.clear()

    def _run(self):
        try:
            import soundcard as sc
            spk = sc.default_speaker()
            mic = sc.get_microphone(id=str(spk.name), include_loopback=True)
            with mic.recorder(samplerate=self.RATE, channels=2) as rec:
                while self.want.is_set():
                    data = rec.record(numframes=1920)  # 40 ms
                    s = self.sink
                    if s:
                        s(data)
        except Exception as e:
            self.error = f"{type(e).__name__}: {e}"


def to_pcm(data, rate, ch):
    """float32 (frames, 2) a 48 kHz -> int16 little-endian na taxa/canais pedidos."""
    import numpy as np
    f = AudioCapture.RATE // rate
    if f > 1:
        n = (len(data) // f) * f
        data = data[:n].reshape(-1, f, data.shape[1]).mean(axis=1)
    if ch == 1 and data.shape[1] > 1:
        data = data.mean(axis=1, keepdims=True)
    return (np.clip(data, -1, 1) * 32767).astype("<i2").tobytes()


class AudioPlayer:
    """Toca no alto-falante local o som recebido (com fila curta para manter baixa latência)."""

    def __init__(self, on_error=None):
        self.q = queue.Queue(); self.thread = None
        self.muted = False; self.failed = False; self.stop_flag = False; self.on_error = on_error

    def push(self, rate, ch, pcm):
        if self.muted or self.failed:
            return
        while self.q.qsize() > 10:  # >~400 ms acumulados: descarta o mais antigo
            try: self.q.get_nowait()
            except queue.Empty: break
        self.q.put((rate, ch, pcm))
        if self.thread is None:
            self.thread = threading.Thread(target=self._run, daemon=True)
            self.thread.start()

    def close(self):
        self.stop_flag = True
        self.q.put(None)

    def _run(self):
        player = None
        try:
            import numpy as np, soundcard as sc
            cur = None
            while not self.stop_flag:
                try:
                    item = self.q.get(timeout=0.5)
                except queue.Empty:
                    continue
                if item is None:
                    break
                rate, ch, pcm = item
                if cur != (rate, ch):
                    if player:
                        player.__exit__(None, None, None)
                    player = sc.default_speaker().player(samplerate=rate, channels=ch)
                    player.__enter__(); cur = (rate, ch)
                arr = np.frombuffer(pcm, dtype="<i2").reshape(-1, ch).astype("float32") / 32768.0
                player.play(arr)
        except Exception as e:
            self.failed = True
            if self.on_error:
                self.on_error(f"Som indisponível: {type(e).__name__}: {e}")
        finally:
            if player:
                try: player.__exit__(None, None, None)
                except Exception: pass
