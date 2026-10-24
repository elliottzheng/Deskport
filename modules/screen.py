# -*- coding: utf-8 -*-
"""屏幕模块 — MJPEG 单捕获广播推流 + 远程键鼠控制"""
import io
import threading
import time
from functools import cache

from flask import Blueprint, Response, jsonify, request

blueprint = Blueprint('screen', __name__)


@cache
def _controllers():
    """首次按键/鼠标时才导入 pynput 并实例化控制器，import 不碰设备。"""
    from pynput.keyboard import Controller as KeyboardController
    from pynput.mouse import Controller as MouseController
    return MouseController(), KeyboardController()


@cache
def _keymaps():
    from pynput.keyboard import Key
    special = {
        'enter': Key.enter, 'space': Key.space, 'tab': Key.tab,
        'backspace': Key.backspace, 'delete': Key.delete, 'esc': Key.esc,
        'up': Key.up, 'down': Key.down, 'left': Key.left, 'right': Key.right,
        'home': Key.home, 'end': Key.end,
        'page_up': Key.page_up, 'page_down': Key.page_down,
        'insert': Key.insert, 'menu': Key.menu, 'pause': Key.pause,
        'print_screen': Key.print_screen,
        'caps_lock': Key.caps_lock, 'num_lock': Key.num_lock,
        'scroll_lock': Key.scroll_lock,
    }
    for i in range(1, 25):
        special['f%d' % i] = getattr(Key, 'f%d' % i)
    modifiers = {'ctrl': Key.ctrl, 'alt': Key.alt, 'shift': Key.shift, 'cmd': Key.cmd}
    return special, modifiers


@cache
def _mouseButtons():
    from pynput.mouse import Button
    return Button


@cache
def _grabber():
    from PIL import ImageGrab
    return ImageGrab

CAPTURE_INTERVAL = 0.1   # 10 FPS 上限
JPEG_QUALITY = 80


class FrameBroadcaster:
    """单捕获广播: 一条捕获线程服务所有客户端。

    旧架构 (每客户端独立抓屏+编码) 在多客户端下帧率 ÷N, 且抓屏由
    Windows 驱动串行化互相拖慢 — 本类修复该问题。
    """

    def __init__(self):
        self.cond = threading.Condition()
        self.frame = None        # 最新 JPEG bytes
        self.seq = 0             # 帧序号
        self.clients = 0
        self.thread = None
        self.stopEvent = threading.Event()
        self.lastError = ''

    def acquire(self):
        with self.cond:
            self.clients += 1
            if self.thread is None or not self.thread.is_alive():
                self.stopEvent.clear()
                self.seq = 0
                self.frame = None
                self.thread = threading.Thread(target=self._loop, daemon=True)
                self.thread.start()

    def release(self):
        with self.cond:
            self.clients = max(0, self.clients - 1)
            if self.clients == 0:
                self.stopEvent.set()

    def getFrame(self, lastSeq, timeout=1.0):
        """阻塞等待比 lastSeq 新的帧; 返回 (jpegBytes|None, seq)"""
        with self.cond:
            self.cond.wait_for(
                lambda: self.seq > lastSeq or self.stopEvent.is_set(),
                timeout)
            if self.seq <= lastSeq:
                return None, lastSeq
            return self.frame, self.seq

    def _loop(self):
        while not self.stopEvent.is_set():
            cycleStart = time.time()
            try:
                img = _grabber().grab()
                buf = io.BytesIO()
                img.save(buf, 'JPEG', quality=JPEG_QUALITY)
                with self.cond:
                    self.frame = buf.getvalue()
                    self.seq += 1
                    self.cond.notify_all()
            except (OSError, ImportError) as e:
                # 锁屏/RDP 断开/桌面切换/缺 Pillow: 保留上一帧, 跳帧重试
                self.lastError = str(e)
            # 自适应间隔: 目标 10 FPS, 捕获本身耗时大时不额外等待
            self.stopEvent.wait(max(0.0, CAPTURE_INTERVAL - (time.time() - cycleStart)))


broadcaster = FrameBroadcaster()


@blueprint.route('/api/screen/stream')
def stream():
    broadcaster.acquire()

    def generate():
        lastSeq = -1
        try:
            while True:
                frame, seq = broadcaster.getFrame(lastSeq, timeout=1.0)
                if frame is None:
                    continue
                lastSeq = seq
                yield (b'--frame\r\n'
                       b'Content-Type: image/jpeg\r\n'
                       b'Content-Length: %d\r\n\r\n' % len(frame))
                yield frame
                yield b'\r\n'
        finally:
            broadcaster.release()

    return Response(generate(), mimetype='multipart/x-mixed-replace; boundary=frame')


@blueprint.route('/api/screen/control')
def control():
    controlType = request.args.get('controlType')
    if controlType == 'mouse':
        action = request.args.get('action', 'click')
        x = request.args.get('x')
        y = request.args.get('y')
        if x is None or y is None:
            return jsonify({'error': '缺少坐标'}), 400
        try:
            pos = (int(x), int(y))
        except ValueError:
            return jsonify({'error': '坐标无效'}), 400
        Buttons = _mouseButtons()
        button = Buttons.right if request.args.get('button') == 'right' else Buttons.left
        mouseController, _keyboard = _controllers()
        try:
            if action == 'move':
                mouseController.position = pos
            else:
                mouseController.position = pos
                mouseController.click(button, 1)
            return jsonify({'ok': True})
        except Exception as e:  # noqa: BLE001 — pynput 异常兜底
            return jsonify({'error': str(e)}), 500

    if controlType == 'keyboard':
        keyString = request.args.get('key') or ''
        if not keyString:
            return jsonify({'error': '缺少按键'}), 400
        parts = [p.strip() for p in keyString.lower().split('+') if p.strip()]
        modifiers = []
        specialKeys, modifierKeys = _keymaps()
        _mouse, keyboardController = _controllers()
        try:
            for part in parts:
                if part in modifierKeys:
                    keyboardController.press(modifierKeys[part])
                    modifiers.append(modifierKeys[part])
                elif part in specialKeys:
                    keyboardController.press(specialKeys[part])
                    keyboardController.release(specialKeys[part])
                elif len(part) == 1:
                    keyboardController.press(part)
                    keyboardController.release(part)
                else:
                    return jsonify({'error': '未知按键: ' + part}), 400
            for modifier in reversed(modifiers):
                keyboardController.release(modifier)
            return jsonify({'ok': True})
        except Exception as e:  # noqa: BLE001
            for modifier in modifiers:
                try:
                    keyboardController.release(modifier)
                except Exception:  # noqa: BLE001
                    pass
            return jsonify({'error': str(e)}), 500

    return jsonify({'error': '无效操作'}), 400
