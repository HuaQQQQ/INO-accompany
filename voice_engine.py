import os
import sys
import subprocess
import time
import requests
import io
import atexit
import threading
import queue
import re
from concurrent.futures import ThreadPoolExecutor

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

import numpy as np
import sounddevice as sd

# VOICEVOX 可合成字符集：假名/汉字/ASCII/常用标点/长音。其余符号（•、♡、颜文字等）
# 会导致 audio_query 422 而静默 fallback，合成前必须过滤。
_VV_SAFE_RE = re.compile(
    r'[^\u3040-\u30FF\u4E00-\u9FFF\uFF61-\uFF9F\u0020-\u007E'
    r'\u3000-\u303F\uFF01-\uFF5E\u30FB\u3005\u2014\u2018\u2019\u201c\u201d]'
)

# 名字读音修正：書寫「INO」但 Voicevox 需讀作「いの」。
# 不用 \b：日文语境下 INO 直接接假名（如 INOは），\w 含假名导致边界不成立。
# 改用前后非英文字母断言，避免误伤英文单词（如 DOMINO），同时兼容日语无空格场景。
_INO_KANA_RE = re.compile(r'(?<![A-Za-z])INO(?![A-Za-z])')

class StreamingAudioPlayer:
    """常驻音频流播放器：新语音数据平稳追加到播放缓冲，播完自动回到静音待机。
    防爆音三重设计：
    1. 流常驻不关闭：播完后流继续输出静音，避免反复开关 PortAudio 流的启动杂音；
    2. underrun 软淡出：缓冲意外播空时从上一采样 5ms 快速衰减到零，而非硬切到静音；
    3. latency=0.15：加大 PortAudio 缓冲，容忍合成线程短暂卡顿不欠载。"""

    _FADE_SECONDS = 0.005  # underrun 软淡出时长

    def __init__(self, rate: int = 24000, channels: int = 2):
        self.rate = rate
        self.channels = channels
        self._stream = None
        self._buffer = np.zeros(0, dtype=np.float32)
        self._lock = threading.Lock()
        self._finished = False
        self._volume = 1.0
        self._last_samples = np.zeros(0, dtype=np.float32)  # 最近输出的采样（供欠载淡出起算）

    def _callback(self, outdata, frames, time_info, status):
        out = np.zeros((frames, self.channels), dtype=np.float32)
        with self._lock:
            pos = 0  # 当前写入行号
            need = frames * self.channels
            n = min(len(self._buffer), need)
            if n > 0:
                chunk = self._buffer[:n] * self._volume
                rows = n // self.channels
                out[pos:pos + rows] = chunk.reshape(rows, self.channels)
                self._buffer = self._buffer[n:]
                self._last_samples = chunk[-self.channels * 4:]
                pos += rows
                need -= n
            if need > 0 and len(self._buffer) == 0 and len(self._last_samples) > 0:
                # 欠载：不硬切静音，从上一采样快速淡出（防咔嗒）
                fade_n = min(int(self.rate * self._FADE_SECONDS) * self.channels, need)
                last = self._last_samples[-self.channels:]
                ramp = np.linspace(1.0, 0.0, fade_n // self.channels, dtype=np.float32)
                fade = (last.reshape(1, -1) * ramp.reshape(-1, 1)).flatten()
                rows = fade_n // self.channels
                out[pos:pos + rows] = fade.reshape(rows, self.channels)
                self._last_samples = np.zeros(0, dtype=np.float32)
        outdata[:] = out

    def _ensure_stream(self):
        if self._stream is None or not self._stream.active:
            try:
                self._stream = sd.OutputStream(
                    samplerate=self.rate,
                    channels=self.channels,
                    dtype='float32',
                    callback=self._callback,
                    blocksize=1024,
                    latency=0.15  # 加大缓冲：容忍合成卡顿，减少欠载概率
                )
                self._stream.start()
            except Exception as e:
                # 输出设备不可用（被独占/拔除）：清空缓冲，避免残留数据一直等待
                print(f"[AudioPlayer] 打开输出流失败: {e}")
                with self._lock:
                    self._buffer = np.zeros(0, dtype=np.float32)
                    self._last_samples = np.zeros(0, dtype=np.float32)
                self._stream = None

    @staticmethod
    def _wav_to_float32_stereo(wav_bytes: bytes):
        """wav(16bit mono/stereo) → (interleaved float32 stereo 数组, 实际采样率)。
        返回实际采样率：播放时按 wav 真实采样率开流，避免采样率错配导致变慢/变调。"""
        import wave as wave_mod
        try:
            src = io.BytesIO(wav_bytes)
            with wave_mod.open(src, 'rb') as w:
                ch = w.getnchannels()
                sw = w.getsampwidth()
                rate = w.getframerate()
                frames = w.readframes(w.getnframes())
            if sw != 2 or rate <= 0:
                return np.zeros(0, dtype=np.float32), 0
            data = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
            if ch == 1:
                data = np.repeat(data, 2)
            return data.astype(np.float32), rate
        except Exception:
            return np.zeros(0, dtype=np.float32), 0

    def _close_stream_locked(self):
        """关闭当前输出流并清空播放缓冲（采样率切换/流失效时调用，需持锁）。"""
        try:
            if self._stream is not None:
                self._stream.stop()
                self._stream.close()
        except Exception:
            pass
        self._stream = None
        self._buffer = np.zeros(0, dtype=np.float32)
        self._last_samples = np.zeros(0, dtype=np.float32)

    def feed(self, wav_bytes: bytes, volume: float = 1.0):
        """追加一段语音到播放缓冲（数据平稳接续到常驻流）。
        采样率随数据自适应：与当前流不一致时自动按新采样率重开流。"""
        data, rate = self._wav_to_float32_stereo(wav_bytes)
        if len(data) == 0:
            return
        with self._lock:
            if rate != self.rate or self._stream is None or not self._stream.active:
                # 采样率变化/流不可用 → 关闭旧流，按新采样率重建（防按错误采样率播放导致变慢变调）
                self._close_stream_locked()
                self.rate = rate
            self._volume = max(0.0, min(1.0, volume))
            self._buffer = np.concatenate((self._buffer, data))
            self._finished = False
        self._ensure_stream()

    def finish(self):
        """标记所有语音已喂完（缓冲播空后回到静音待机，流不关闭）。"""
        with self._lock:
            self._finished = True

    def wait_until_done(self, is_muted=None, aborted=None):
        """阻塞直到全部播完（或静音/打断/代次失效）。播完不停流，避免下次播放重开流杂音。"""
        while True:
            with self._lock:
                idle = self._finished and len(self._buffer) == 0
            if idle:
                break
            if is_muted is not None and is_muted():
                self.stop()
                return
            if aborted is not None and aborted():
                return  # 被新语音/打断取代：立即退出，不卡死 worker 线程
            time.sleep(0.05)
        with self._lock:
            self._last_samples = np.zeros(0, dtype=np.float32)

    def stop(self):
        """清空缓冲（打断/静音时调用）。流保持常驻，继续输出静音。"""
        with self._lock:
            self._buffer = np.zeros(0, dtype=np.float32)
            self._last_samples = np.zeros(0, dtype=np.float32)
            self._finished = False


class VoiceEngine:
    def __init__(self, engine_dir: str = "voicevox_engine", exe_name: str = "run.exe", speaker_id: int = 16, volume: float = 0.8, voice_lang_mode: str = "ja", is_muted: bool = False, speed_scale: float = 1.0, llm_translator=None, **kwargs):
        self.engine_dir = engine_dir
        self.exe_name = exe_name
        self.speaker_id = int(speaker_id)
        self.volume = max(0.0, min(1.0, volume))
        self.voice_lang_mode = voice_lang_mode  # "ja": 日语原生翻译朗读 (推荐), "zh": 中文拟音朗读
        self.is_muted = bool(is_muted)
        self.speed_scale = max(0.5, min(2.0, float(speed_scale)))
        # Google 翻译不可达时交给大模型翻译的回调（由 core_agent 注入，保持模块解耦）
        self._llm_translate_cb = llm_translator
        self.process = None
        self.base_url = "http://127.0.0.1:50021"
        self.use_voicevox = True
        self.start_engine()
        # 常驻音频流播放器（块间零切换，消除爆音）
        self.player = StreamingAudioPlayer()

        # Dedicated speech queue: ensures sequential playback, new items interrupt old
        self._speech_queue = queue.Queue()
        self._speech_thread = threading.Thread(target=self._speech_worker, daemon=True)
        self._speech_thread.start()

        # Global playback lock: ensures only ONE audio plays at a time across all threads
        self._playback_lock = threading.Lock()
        # 播放代次：每次打断/新播报自增，被卡住的旧流水线靠代次失配自行退出
        # （修复：打断后 wait_until_done 因 _finished 被重置永久阻塞 → 语音永久消失）
        self._play_gen = 0

    def set_speed_scale(self, speed: float):
        self.speed_scale = max(0.5, min(2.0, float(speed)))
        print(f"语速设定为: {round(self.speed_scale, 2)}x")

    def set_voice_lang_mode(self, mode: str):
        self.voice_lang_mode = "ja" if mode == "ja" else "zh"
        mode_desc = "日语原生朗读 (地道日文翻译)" if self.voice_lang_mode == "ja" else "中文拟音朗读 (拼音假名)"
        print(f"语音语言模式切换为: {mode_desc}")

    @staticmethod
    def _has_japanese(text: str) -> bool:
        """Check if text contains Japanese Hiragana or Katakana."""
        return bool(re.search(r'[\u3040-\u309F\u30A0-\u30FF]', text))

    @staticmethod
    def _is_mostly_japanese(text: str) -> bool:
        """Check if text is primarily Japanese kana/kanji."""
        kana_count = len(re.findall(r'[\u3040-\u309F\u30A0-\u30FF]', text))
        hanzi_count = len(re.findall(r'[\u4e00-\u9fff]', text))
        if kana_count > 0 and hanzi_count == 0:
            return True
        return kana_count > 0 and kana_count >= hanzi_count

    @staticmethod
    def _strip_unspeakable(text: str) -> str:
        """过滤 VOICEVOX 无法合成的符号（保留假名/汉字/ASCII/常用标点），防 422 fallback。"""
        cleaned = _VV_SAFE_RE.sub('', text or '')
        cleaned = re.sub(r'\s+', ' ', cleaned).strip()
        return cleaned

    def _translate_to_japanese(self, text: str) -> str:
        """LLM 翻译为可爱日语口语（含情绪语气词）；纯日文直读；失败回退原文保证有声音。
        不再依赖 Google 翻译（网络超时曾卡死语音 worker）。"""
        clean = self._strip_unspeakable(text)
        clean = re.sub(r'\*.*?\*', '', clean).strip()
        if not clean:
            return text

        # 纯日文（含假名且无中文）→ 直读，无需翻译
        if self._has_japanese(clean) and not re.search(r'[\u4e00-\u9fff]', clean):
            return clean

        # LLM 翻译（由 core_agent 注入的回调）；失败/无回调 → 回退原文，保证语音不中断
        if self._llm_translate_cb is not None:
            try:
                ja_text = self._llm_translate_cb(clean)
                if ja_text and ja_text.strip():
                    print(f"[LLM 日语翻译]: {ja_text[:80]}")
                    return self._strip_unspeakable(ja_text).strip()
            except Exception as e:
                print(f"[LLM Translation Warning]: {e}")
        return clean

    def _speech_worker(self):
        """Dedicated thread that processes speech requests sequentially."""
        while True:
            try:
                item = self._speech_queue.get()
                if item is None:
                    break

                if isinstance(item, tuple):
                    text, expr = item
                else:
                    text, expr = item, "idle"

                if not self._playback_lock.locked():
                    latest = (text, expr)
                    while not self._speech_queue.empty():
                        try:
                            next_item = self._speech_queue.get_nowait()
                            if next_item is None:
                                return
                            latest = next_item if isinstance(next_item, tuple) else (next_item, "idle")
                        except queue.Empty:
                            break
                    self._do_speak(latest[0], latest[1])
                else:
                    self._do_speak(text, expr)
            except Exception as e:
                print(f"Speech worker error: {e}")

    def stop_speech(self):
        """Immediately stop current speech audio and clear queued speech (含流水线预取)。"""
        try:
            self._play_gen += 1  # 代次推进：卡住的旧 wait_until_done 立即解锁退出
            while not self._speech_queue.empty():
                try:
                    self._speech_queue.get_nowait()
                except queue.Empty:
                    break
            # 中断分块流水线（停止后续预取合成）
            if getattr(self, '_pipeline_stop', None) is not None:
                self._pipeline_stop.set()
                self._pipeline_stop = None
            # 停止常驻音频流（清空缓冲）
            try:
                self.player.stop()
            except Exception:
                pass
        except Exception as e:
            print(f"[Stop Speech Error]: {e}")

    def set_volume(self, volume: float):
        self.volume = max(0.0, min(1.0, float(volume)))
        try:
            if not self.is_muted:
                self.player._volume = self.volume
        except Exception:
            pass
        print(f"音量调整为: {int(self.volume * 100)}%")

    def set_muted(self, muted: bool):
        self.is_muted = bool(muted)
        try:
            if self.is_muted:
                self.player.stop()  # 清空当前缓冲
                self.player._volume = 0.0  # 双保险：即使有残留缓冲也不出声
            else:
                self.player._volume = self.volume  # 恢复音量
        except Exception:
            pass
        print(f"语音静音开关: {'已静音' if self.is_muted else '已开启语音播报'}")

    def _find_exe(self):
        # 优先寻找轻量无窗口后端 run.exe (vv-engine/run.exe)
        preferred = os.path.join(self.engine_dir, "VOICEVOX", "vv-engine", "run.exe")
        if os.path.exists(preferred):
            return preferred
        if not os.path.exists(self.engine_dir):
            return None
        for root, dirs, files in os.walk(self.engine_dir):
            for f in files:
                if f.lower() == "run.exe":
                    return os.path.join(root, f)
        for root, dirs, files in os.walk(self.engine_dir):
            for f in files:
                if f.lower() == "voicevox.exe":
                    return os.path.join(root, f)
        return None

    def start_engine(self):
        exe_path = self._find_exe()
        if exe_path or self._is_engine_running_externally():
            if not self._is_engine_running_externally():
                print(f"Starting Voicevox engine (Multi-threaded CPU mode) from {exe_path}...")
                cmd = [exe_path, "--host", "127.0.0.1", "--port", "50021", "--cpu_num_threads", "4"]
                self.process = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                atexit.register(self.stop_engine)
                for _ in range(15):
                    if self._is_engine_running_externally():
                        self.use_voicevox = True
                        print("Voicevox Multi-threaded Engine started successfully.")
                        return
                    time.sleep(1)
            else:
                self.use_voicevox = True
                print("Voicevox engine connected.")
                return
        print(f"⚠️ [Voicevox Warning] 未连接到 Voicevox 引擎服务 (http://127.0.0.1:50021)。请启动 Voicevox 或将其解压至 {self.engine_dir} 目录。")

    def stop_engine(self):
        if self.process:
            print("Stopping Voicevox engine...")
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()

    def speak(self, text: str, expression: str = "idle"):
        """Queue text for speech with emotion expression tag. Non-blocking — returns immediately."""
        if self.is_muted:
            print("[Voice Muted]: 静音状态，跳过语音朗读。")
            return
        self._speech_queue.put((text, expression))

    def _do_speak(self, text: str, expression: str = "idle"):
        """Internal: 分块流水线合成 + 无缝播放（runs in speech worker thread）。"""
        if self.is_muted:
            return

        # Acquire playback lock — only ONE voice plays at a time across all threads
        if not self._playback_lock.acquire(blocking=False):
            print("[VoiceEngine] 另一段语音正在播放中，跳过本次（防重叠）")
            return

        try:
            clean = self._strip_unspeakable(text)
            clean = re.sub(r'\*.*?\*', '', clean).strip()
            if not clean:
                return

            # 停止任何正在播放的音频（新语音抢占）：代次推进让旧流水线自行退出
            self._play_gen += 1
            gen = self._play_gen
            self.player.stop()
            if getattr(self, '_pipeline_stop', None) is not None:
                self._pipeline_stop.set()
                self._pipeline_stop = None

            has_ja = self._has_japanese(clean)

            success = self._speak_pipeline(clean, expression, has_ja, gen)
            if not success:
                print("[Voicevox Status] 检测到 Voicevox 引擎未连接或掉线，尝试连接/启动 VOICEVOX...")
                self.start_engine()
                success = self._speak_pipeline(clean, expression, has_ja, gen)

            if not success:
                print(f"❌ [Voicevox FAILED] 合成失败。原文: {clean[:100]}")

        finally:
            self._playback_lock.release()

    # ── 分块流水线（播放连贯性优先：预取充分，宁可等待不跳句）────

    @staticmethod
    def _split_blocks(text: str, max_chars: int = 60) -> list:
        """按句子边界切块并合并（每块 ~max_chars 字符，保留句子完整；超长句强制拆分）。"""
        text = re.sub(r'\s+', ' ', text or '').strip()
        if not text:
            return []
        parts = re.split(r'(?<=[。！？!?～~\n])', text)
        blocks, cur = [], ''
        for p in parts:
            p = p.strip()
            if not p:
                continue
            if len(cur) + len(p) <= max_chars:
                cur += p
            else:
                if cur:
                    blocks.append(cur)
                while len(p) > max_chars:
                    blocks.append(p[:max_chars])
                    p = p[max_chars:]
                cur = p
        if cur:
            blocks.append(cur)
        return [b for b in blocks if b.strip()]

    def _speak_pipeline(self, clean: str, expression: str, has_ja: bool, gen: int) -> bool:
        """整段预处理（翻译/转换一次保证语义连贯）→ 切块 → 流水线合成播放。gen=本次播放代次。"""
        if self.voice_lang_mode == "ja":
            # ja 模式：纯日文直读；含中文 → 整句翻译为日文
            if has_ja and not re.search(r'[\u4e00-\u9fff]', clean):
                tts_text = clean
            else:
                tts_text = self._translate_to_japanese(clean)
            print(f"[Voicevox 日语原生朗读 (情绪: {expression}{', 自动识别日文' if has_ja else ''})]: {tts_text[:120]}")
            ja_style = True
        else:
            # zh 模式：始终中文拟音朗读（转换器自动处理日文假名/英文混排，不再被 has_ja 劫持到日语翻译）
            from pinyin_katakana import chinese_to_voicevox_katakana
            tts_text = chinese_to_voicevox_katakana(clean)
            print(f"[Voicevox Katakana 转换]: {tts_text[:120]}")
            ja_style = False

        # 名字读音修正：書寫「INO」但 Voicevox 需讀作「いの」（见模块级 _INO_KANA_RE）。
        tts_text = _INO_KANA_RE.sub('いの', tts_text)

        tts_text = self._strip_unspeakable(tts_text)
        print(f"[TTS 最終文本]: {tts_text[:120]}")
        if not tts_text or not tts_text.strip():
            return False

        blocks = self._split_blocks(tts_text)
        if not blocks:
            return False

        if len(blocks) == 1:
            wav = self._synthesize_block(blocks[0], expression, ja_style)
            if not wav:
                return False
            if gen != self._play_gen:
                return True  # 合成期间已被打断，丢弃音频但不算引擎失败
            self._play_wav(wav, gen)
            return True

        return self._pipeline_play(blocks, expression, ja_style, gen)

    def _play_wav(self, wav_bytes: bytes, gen: int = None):
        """播放单段 wav（单块场景，走常驻音频流）。"""
        if self.is_muted:
            return
        if gen is not None and gen != self._play_gen:
            return
        self.player.feed(self._apply_edge_smoothing(self._normalize_wav_for_mixer(wav_bytes)), self.volume)
        self.player.finish()
        self.player.wait_until_done(is_muted=lambda: self.is_muted,
                                    aborted=lambda: gen is not None and gen != self._play_gen)

    def _pipeline_play(self, blocks: list, expression: str, ja_style: bool, gen: int = None) -> bool:
        """2 并发预取合成 + Channel.queue 无缝接续播放（连贯性优先）。"""
        stop = threading.Event()
        self._pipeline_stop = stop
        results = {}
        results_lock = threading.Lock()
        any_success = [False]

        def synth(idx, blk):
            if stop.is_set():
                return
            wav = self._synthesize_block(blk, expression, ja_style)
            with results_lock:
                results[idx] = wav
                if wav:
                    any_success[0] = True

        executor = ThreadPoolExecutor(max_workers=2)
        for i, blk in enumerate(blocks):
            executor.submit(synth, i, blk)

        channel = None
        sounds = []  # 保持引用防 GC
        for i in range(len(blocks)):
            if stop.is_set() or self.is_muted or (gen is not None and gen != self._play_gen):
                break  # 静音时立即退出循环，不再 feed 后续块
            # 等待本块就绪：顺序保证 + 连贯优先（宁可等待，不跳句、不乱序）
            while i not in results and not stop.is_set():
                if gen is not None and gen != self._play_gen:
                    break
                time.sleep(0.05)
            if gen is not None and gen != self._play_gen:
                break
            wav = results.get(i)
            if not wav:
                continue  # 单块合成失败，跳过但继续后续
            # 流式追加：数据平稳接续到常驻音频流（无播放器切换，无爆音）
            if self.is_muted:
                break  # 双保险：feed 前再检查一次
            self.player.feed(self._apply_edge_smoothing(self._normalize_wav_for_mixer(wav)), self.volume)

        interrupted = gen is not None and gen != self._play_gen
        # 全部喂完，等待播完（可被静音/打断）；被打断时立即退出不阻塞 worker
        if not interrupted:
            self.player.finish()
            self.player.wait_until_done(is_muted=lambda: self.is_muted,
                                        aborted=lambda: gen is not None and gen != self._play_gen)
        # 被打断时不等慢合成收尾（wait=False），worker 立即可接下一段语音
        executor.shutdown(wait=False)
        self._pipeline_stop = None
        return any_success[0]

    @staticmethod
    def _normalize_wav_for_mixer(wav_bytes: bytes) -> bytes:
        """把 VOICEVOX wav（24000Hz 16bit mono）显式转换为双声道格式（stereo），
        避免隐式转换导致播放时长减半/音调变高（花栗鼠）。"""
        import wave as wave_mod
        try:
            src = io.BytesIO(wav_bytes)
            with wave_mod.open(src, 'rb') as w:
                ch = w.getnchannels()
                sw = w.getsampwidth()
                rate = w.getframerate()
                frames = w.readframes(w.getnframes())
            if sw != 2:
                return wav_bytes  # 非 16bit 不做处理（VOICEVOX 固定 16bit）
            if ch == 1:
                import numpy as np
                data = np.frombuffer(frames, dtype=np.int16)
                frames = np.repeat(data, 2).astype(np.int16).tobytes()
                ch = 2
            out = io.BytesIO()
            with wave_mod.open(out, 'wb') as w:
                w.setnchannels(ch)
                w.setsampwidth(2)
                w.setframerate(rate)
                w.writeframes(frames)
            return out.getvalue()
        except Exception as e:
            print(f"[Wav Normalize Warning]: {e}")
            return wav_bytes

    @staticmethod
    def _apply_edge_smoothing(wav_bytes: bytes, fade_out_ms: int = 40, fade_in_ms: int = 10) -> bytes:
        """句尾线性淡出 + 句首淡入，消除分块切换时的电平阶跃爆音。
        Voicevox 的 postPhonemeLength 不会在波形末尾加静音，句尾非零电平直接切到下一块会咔嗒。"""
        import wave as wave_mod
        try:
            src = io.BytesIO(wav_bytes)
            with wave_mod.open(src, 'rb') as w:
                ch = w.getnchannels()
                sw = w.getsampwidth()
                rate = w.getframerate()
                frames = w.readframes(w.getnframes())
            if sw != 2 or ch == 0:
                return wav_bytes
            import numpy as np
            data = np.frombuffer(frames, dtype=np.int16).astype(np.float32).reshape(-1, ch)
            n_frames = len(data)
            fade_out = max(1, min(int(rate * fade_out_ms / 1000), n_frames // 4))
            fade_in = max(1, min(int(rate * fade_in_ms / 1000), n_frames // 8))
            # 尾部线性淡出到 0
            ramp_out = np.linspace(1.0, 0.0, fade_out, dtype=np.float32)
            data[-fade_out:] *= ramp_out[:, None]
            # 头部线性淡入从 0 起
            ramp_in = np.linspace(0.0, 1.0, fade_in, dtype=np.float32)
            data[:fade_in] *= ramp_in[:, None]
            out = data.astype(np.int16).tobytes()
            out_io = io.BytesIO()
            with wave_mod.open(out_io, 'wb') as w:
                w.setnchannels(ch)
                w.setsampwidth(2)
                w.setframerate(rate)
                w.writeframes(out)
            return out_io.getvalue()
        except Exception as e:
            print(f"[Edge Smoothing Warning]: {e}")
            return wav_bytes

    def _synthesize_block(self, tts_text: str, expression: str, ja_style: bool):
        """合成单个分块 → wav bytes；422（生僻字不在词典）时跳过符号/转假名拼接重试一次。"""
        try:
            return self._synth_once(tts_text, ja_style)
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 422:
                unknown = self._extract_unknown_words(e)
                if unknown:
                    repaired = self._repair_unknown_words(tts_text, unknown)
                    if repaired != tts_text:
                        print(f"🔧 [Voicevox 词典修复] 未知词 {unknown} → 已跳过符号/转假名拼接，重试合成")
                        try:
                            return self._synth_once(repaired, ja_style)
                        except Exception as e2:
                            print(f"[Voicevox Synthesis Warning]: {e2}")
                            return None
            print(f"[Voicevox Synthesis Warning]: {e}")
            return None
        except Exception as e:
            print(f"[Voicevox Synthesis Warning]: {e}")
            return None

    def _apply_ja_style(self, query_data: dict):
        """ja 风格朗读参数：句间停顿收紧 + INO 自称「いの」发音调整（第一声高平 + 第三声低降）。"""
        for phrase in query_data.get("accent_phrases", []):
            pause = phrase.get("pause_mora")
            if pause:
                pause["vowel_length"] = 0.28
            # INO 自称「いの」：第一声（阴平，高平）+ 第三声（上声，低降）
            moras = phrase.get("moras", [])
            for i in range(len(moras) - 1):
                t1, t2 = moras[i].get("text"), moras[i + 1].get("text")
                if (t1 in ("イ", "い") and t2 in ("ノ", "の")):
                    # い（第一声）：高平音，元音缩短
                    m_i = moras[i]
                    vl_i = m_i.get("vowel_length") or 0.1
                    m_i["vowel_length"] = round(max(vl_i * 0.6, 0.04), 4)
                    m_i["pitch"] = round((m_i.get("pitch") or 5.0) + 0.6, 2)
                    # の（第三声）：低音，元音更短
                    m_no = moras[i + 1]
                    vl_no = m_no.get("vowel_length") or 0.1
                    m_no["vowel_length"] = round(max(vl_no * 0.5, 0.04), 4)
                    m_no["pitch"] = round((m_no.get("pitch") or 5.0) - 0.6, 2)

    def _synth_once(self, tts_text: str, ja_style: bool) -> bytes:
        """单块 audio_query + synthesis → wav bytes。"""
        query_payload = {"text": tts_text, "speaker": self.speaker_id}
        query_res = requests.post(f"{self.base_url}/audio_query", params=query_payload, timeout=15)
        query_res.raise_for_status()
        query_data = query_res.json()

        if ja_style:
            self._apply_ja_style(query_data)
        else:
            for phrase in query_data.get("accent_phrases", []):
                pause = phrase.get("pause_mora")
                if pause:
                    pause["vowel_length"] = 0.30
                for m in phrase.get("moras", []):
                    vl = m.get("vowel_length") or 0
                    cl = m.get("consonant_length") or 0
                    if vl > 0.11:
                        m["vowel_length"] = 0.105
                    if cl > 0.08:
                        m["consonant_length"] = 0.075
        query_data["prePhonemeLength"] = 0.04
        query_data["postPhonemeLength"] = 0.04
        query_data["speedScale"] = round(self.speed_scale, 2)

        synth_res = requests.post(
            f"{self.base_url}/synthesis",
            params={"speaker": self.speaker_id},
            json=query_data,
            timeout=60
        )
        synth_res.raise_for_status()
        return synth_res.content

    @staticmethod
    def _extract_unknown_words(err) -> list:
        """从 VOICEVOX 422 响应中提取不在词典的字词列表（兼容引号/无引号格式）。"""
        words = []
        try:
            resp = err.response
            if resp is None:
                return words
            body = resp.json()
            details = body.get("detail", body) if isinstance(body, dict) else body
            msgs = []
            if isinstance(details, str):
                msgs.append(details)
            elif isinstance(details, list):
                for d in details:
                    if isinstance(d, dict):
                        msgs.append(str(d.get("msg", "")))
                    elif isinstance(d, str):
                        msgs.append(d)
            for msg in msgs:
                for w in re.findall(r"'([^']+)'|\"([^\"]+)\"", msg):
                    words.append(w[0] if w[0] else w[1])
                for w in re.findall(r'(?:dictionary|text)\s*:\s*([\u4e00-\u9fff\u3040-\u30ff]{1,4})', msg):
                    words.append(w)
        except Exception:
            pass
        return list(dict.fromkeys(words))

    @staticmethod
    def _repair_unknown_words(text: str, words: list) -> str:
        """不可发音符号 → 跳过（删除）；能读但不在词典的字 → 转片假名近似读音拼接。"""
        repaired = text
        for w in words:
            if not w or w not in repaired:
                continue
            # 纯符号/不可发音内容 → 直接删除跳过
            if not re.search(r'[\u4e00-\u9fff\u3040-\u30ff\u0041-\u007a]', w):
                repaired = repaired.replace(w, '')
            else:
                # 能读但不在词典（汉字/假名）→ 转片假名近似读音拼接
                try:
                    from pinyin_katakana import chinese_to_voicevox_katakana
                    kana = chinese_to_voicevox_katakana(w)
                    if kana and kana != w:
                        repaired = repaired.replace(w, kana)
                    else:
                        repaired = repaired.replace(w, '')
                except Exception:
                    repaired = repaired.replace(w, '')
        return repaired

    def _is_engine_running_externally(self):
        try:
             res = requests.get(f"{self.base_url}/version", timeout=1)
             return res.status_code == 200
        except Exception:
             return False
