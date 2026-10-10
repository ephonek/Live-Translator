"""Local subtitle viewer. It only reads the transcript; closing requests a stop."""
from asr_models import MODELS
from app_preferences import load_preferences, save_preferences, choice, bounded, flag
import argparse
import json
import os
import sys
import time
from collections import deque
from pathlib import Path

# Conda 子程序未啟用 shell 時，明確指向同一環境的 Tcl/Tk。
_tk_dll_handle = None
_library = Path(sys.prefix) / 'Library'
if os.name == 'nt' and (_library / 'lib/tcl8.6/init.tcl').is_file():
    os.environ['TCL_LIBRARY'] = str(_library / 'lib/tcl8.6')
    os.environ['TK_LIBRARY'] = str(_library / 'lib/tk8.6')
    _tk_dll_handle = os.add_dll_directory(str(_library / 'bin'))

import tkinter as tk
from tkinter import ttk
from usage_meter import UsageStats, UsagePanel, duration
from process_audio import list_applications, supported as process_capture_supported


class TranscriptTail:
    def __init__(self, path):
        self.path = Path(path)
        self.position = 0
        self.pending = b''

    def read(self):
        try:
            with self.path.open('rb') as stream:
                stream.seek(self.position)
                data = stream.read()
                self.position = stream.tell()
        except FileNotFoundError:
            return []
        lines = (self.pending + data).split(b'\n')
        self.pending = lines.pop()
        rows = []
        for line in lines:
            if line.strip():
                rows.append(json.loads(line.decode('utf-8')))
        return rows


class SubtitleState:
    """Raw ASR advances independently; draft/final windows merge by covered IDs."""
    def __init__(self):
        self.entries = deque(maxlen=100)
        self.original = None
        self.translation = None
        self.counter = 0

    def accept(self, row):
        if not row.get('source') and row.get('event') != 'asr_failed':
            return False
        self.counter += 1
        row = dict(row)
        row['_order'] = row.get('asr_call', self.counter)
        event = row.get('event', 'translation_failed' if row.get('error') else 'translation_ready')
        row['_phase'] = event
        key = row.get('window_id', row.get('asr_call', self.counter))
        if event == 'asr_ready':
            if self.original is None or row['_order'] >= self.original['_order']:
                self.original = dict(row)
            # A replayed ASR event cannot recreate an already merged subtitle.
            if any(key in r.get('covered_asr_calls', [r.get('asr_call')]) for r in self.entries):
                return True
        else:
            if self.original is None:  # Legacy final-only logs.
                self.original = dict(row)
            elif 'window_id' not in row and row['_order'] > self.original['_order']:
                self.original = dict(row)
        existing = next((r for r in self.entries if r.get('window_id', r.get('asr_call', r['_order'])) == key), None)
        if existing is not None:
            if existing.get('is_final') and not row.get('is_final', False):
                return False
            if row.get('version', 0) < existing.get('version', 0):
                return False
            existing.update(row)
        else:
            existing = row
            self.entries.append(existing)
        covered = row.get('covered_asr_calls', [])
        for entry in list(self.entries):
            if entry is not existing and entry.get('asr_call') in covered:
                self.entries.remove(entry)
        if event not in ('asr_ready', 'asr_failed'):
            if self.translation is None or existing['_order'] >= self.translation['_order']:
                self.translation = existing
        return True

    @property
    def pending(self):
        return sum(r.get('_phase') == 'asr_ready' or not r.get('is_final', True) for r in self.entries)


def segment_label(row):
    if row is None:
        return ''
    number = row.get('asr_call', row.get('_order', ''))
    return f"#{number} · {row.get('start', 0):.1f}–{row.get('end', 0):.1f}s"


class PillButton(tk.Canvas):
    """Small rounded control with keyboard focus and optional toggle state."""
    def __init__(self, parent, text, command, variable=None, value=None, danger=False, width=None):
        import tkinter.font as tkfont
        self._font = tkfont.Font(family='Microsoft JhengHei UI', size=10)
        self._width = width or max(38, self._font.measure(text) + 24)
        self._disabled = False
        self._hover = False
        self._focused = False
        self._variable = variable
        self._value = value
        self._command = command
        self._danger = danger
        self._text = text
        super().__init__(parent, width=self._width, height=32, bg=parent.cget('bg'),
                         highlightthickness=0, borderwidth=0, takefocus=True, cursor='hand2')
        self.bind('<Enter>', lambda e: self._set_hover(True))
        self.bind('<Leave>', lambda e: self._set_hover(False))
        self.bind('<Button-1>', self.invoke)
        self.bind('<space>', self.invoke)
        self.bind('<Return>', self.invoke)
        self.bind('<FocusIn>', lambda e: self._set_focus(True))
        self.bind('<FocusOut>', lambda e: self._set_focus(False))
        self._trace = variable.trace_add('write', lambda *a: self.redraw()) if variable is not None else None
        self.bind('<Destroy>', self._destroy_trace)
        self.redraw()

    def _destroy_trace(self, event):
        if event.widget is self and self._trace:
            self._variable.trace_remove('write', self._trace)
            self._trace = None

    def _set_hover(self, value):
        self._hover = value
        self.redraw()

    def _set_focus(self, value):
        self._focused = value
        self.redraw()

    def configure(self, cnf=None, **kw):
        if 'text' in kw:
            self._text = kw.pop('text')
            self.redraw()
        if 'state' in kw:
            self._disabled = kw.pop('state') == 'disabled'
            super().configure(cursor='arrow' if self._disabled else 'hand2', takefocus=not self._disabled)
            self.redraw()
        return super().configure(cnf, **kw)

    config = configure

    def invoke(self, event=None):
        if not self._disabled:
            self.focus_set()
            if self._variable is not None:
                self._variable.set(not self._variable.get() if self._value is None else self._value)
            self._command()
        return 'break'

    def redraw(self):
        selected = self._variable is not None and (bool(self._variable.get()) if self._value is None else self._variable.get() == self._value)
        fill = '#23474b' if selected else '#202d3d'
        fg = '#b7f1e4' if selected else '#d7e2ef'
        if self._danger:
            fill, fg = '#382b35', '#f0bec8'
        if self._hover and not self._disabled:
            fill = '#345660' if selected else ('#583342' if self._danger else '#30445a')
        if self._disabled:
            fill, fg = '#1b2531', '#627186'
        self.delete('all')
        x0, y0, x1, y1, r = 1, 2, self._width-1, 30, 10
        self.create_polygon(x0+r,y0,x1-r,y0,x1,y0,x1,y0+r,x1,y1-r,x1,y1,x1-r,y1,
                            x0+r,y1,x0,y1,x0,y1-r,x0,y0+r,x0,y0, smooth=True,
                            fill=fill, outline='#8cbbb9' if self._focused else fill)
        self.create_text(self._width/2, 16, text=self._text, fill=fg, font=self._font)


class SubtitleWindow:
    def __init__(self, root, log, stop_file, language_file=None, language="ja", pause_file=None, mode_file=None, source_file=None, model_file=None):
        self.root = root
        self.saved_window = load_preferences('window')
        self.saved_runtime = load_preferences('runtime')
        self._save_after = None
        self._last_saved_window = None
        self.tail = TranscriptTail(log)
        self.stop_file = Path(stop_file)
        self.language_file = Path(language_file) if language_file else None
        self.selected_language = tk.StringVar(value=language)
        self.active_language = language
        self.pause_file = Path(pause_file) if pause_file else None
        self.source_file = Path(source_file) if source_file else None
        self.model_file = Path(model_file) if model_file else None
        self.model_choice = tk.StringVar(value=MODELS[choice(self.saved_runtime, 'model', MODELS, 'qwen')]['label'])
        self.model_status = tk.StringVar(value='正在啟動 ASR…')
        self.model_busy = True
        self.active_model_label = '載入中'
        self.translation_model_label = '等待連線'
        self.models_label = tk.StringVar(value='ASR：載入中 ｜ 翻譯：等待連線')
        self.source_mode = tk.StringVar(value=choice(self.saved_runtime, 'source_mode', ('device', 'process'), 'device'))
        self.source_choice = tk.StringVar()
        self.source_status = tk.StringVar(value='音訊來源：整個輸出裝置')
        self.source_request_id = None
        self.applications = []
        self.mode_file = Path(mode_file) if mode_file else None
        self.active_mode = choice(self.saved_runtime, 'translation_mode', ('paired', 'sliding'), 'sliding')
        self.selected_mode = tk.StringVar(value=self.active_mode)
        self.mode_status = tk.StringVar(value='目前：' + ('逐段滑動' if self.active_mode == 'sliding' else '兩段合併'))
        self.paused = False
        self.pause_requested = tk.BooleanVar(value=False)
        self.language_status = tk.StringVar(value="辨識語言")
        self.state = SubtitleState()
        self.entries = self.state.entries
        self.stopped = False
        self.drawer_mode = None
        self.usage = UsageStats()
        self.elapsed_base = 0
        self.elapsed_anchor = time.monotonic()
        self.transparent = tk.BooleanVar(value=False)
        self.controls_visible = True
        self.size = 26
        self.show_llm_original = tk.BooleanVar(value=True)
        self.show_original = tk.BooleanVar(value=True)
        self.topmost = tk.BooleanVar(value=True)
        self.compact = tk.BooleanVar(value=False)
        self.settings_open = tk.BooleanVar(value=False)
        self.opacity = tk.IntVar(value=95)
        self.translation_text = ""
        self.llm_text = ""
        self._rendered_translation = None
        self._regular_height = 540
        self.stage = tk.StringVar(value='等待語音')
        bg, panel, muted = '#10151d', '#19222e', '#99aabe'
        style = ttk.Style(root)
        style.theme_use('clam')
        style.configure('TButton', background='#263447', foreground='#e8edf5', borderwidth=0,
                        padding=(10, 6), font=('Microsoft JhengHei UI', 10))
        style.map('TButton', background=[('active', '#354b63')], foreground=[('disabled', '#647489')])
        style.configure('Toolbutton', background=bg, foreground=muted, padding=(9, 6), borderwidth=0)
        style.map('Toolbutton', background=[('selected', '#294459'), ('active', '#263447')],
                  foreground=[('selected', '#aee9ee'), ('active', '#ffffff')])
        style.configure('Danger.TButton', background='#372a31', foreground='#edb5bf')
        style.map('Danger.TButton', background=[('active', '#57313e')])
        style.configure('Vertical.TScrollbar', background='#34465a', troughcolor=bg, borderwidth=0)
        root.title('直播字幕')
        root.overrideredirect(True)
        self._restore_borderless = False
        root.bind('<Map>', self.restore_borderless)
        root.geometry(f'900x540+{max(0, (root.winfo_screenwidth()-900)//2)}+{max(0, root.winfo_screenheight()-640)}')
        root.minsize(660, 330)
        root.configure(bg=bg)
        root.attributes('-topmost', True)
        root.attributes('-alpha', .95)
        self.opacity.trace_add('write', lambda *a: self.apply_opacity())
        root.protocol('WM_DELETE_WINDOW', self.close)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(1, weight=4)
        root.rowconfigure(2, weight=1)

        self.header = tk.Frame(root, bg=bg)
        self.header.grid(row=0, column=0, sticky='ew', padx=16, pady=(10, 8))
        title = tk.Label(self.header, text='⋮⋮  直播字幕', fg='#f1f5fb', bg=bg, cursor='fleur',
                         font=('Microsoft JhengHei UI', 12, 'bold'))
        title.pack(side='left', padx=(0, 14))
        for handle in (self.header, title):
            handle.bind('<ButtonPress-1>', self.start_drag)
            handle.bind('<B1-Motion>', self.drag_window)
        if self.language_file is not None:
            for label, code in [('EN', 'en'), ('JP', 'ja')]:
                PillButton(self.header, label, self.request_language, self.selected_language, code).pack(side='left', padx=2)
        PillButton(self.header, '×', self.close, danger=True, width=34).pack(side='right', padx=(6, 0))
        PillButton(self.header, '−', self.minimize_window, width=34).pack(side='right', padx=2)
        PillButton(self.header, '字幕', self.toggle_compact, self.compact).pack(side='right', padx=3)
        PillButton(self.header, '設定', self.toggle_settings, self.settings_open).pack(side='right', padx=3)
        PillButton(self.header, '歷史', self.show_history).pack(side='right', padx=3)
        PillButton(self.header, '用量', lambda: self.toggle_drawer('usage')).pack(side='right', padx=3)

        self.chinese_panel = tk.Frame(root, bg=bg)
        self.chinese_panel.grid(row=1, column=0, sticky='nsew', padx=18)
        self.chinese_panel.columnconfigure(0, weight=1)
        self.chinese_panel.rowconfigure(1, weight=1)
        caption = self.caption = tk.Frame(self.chinese_panel, bg=bg)
        caption.grid(row=0, column=0, sticky='ew', pady=(0, 5))
        self.chinese_label = tk.StringVar(value='繁體中文')
        tk.Label(caption, textvariable=self.chinese_label, anchor='w', bg=bg, fg=muted,
                 font=('Microsoft JhengHei UI', 10)).pack(side='left')
        self.stage_badge = tk.Label(caption, textvariable=self.stage, bg='#243449', fg='#acc2da',
                                   font=('Microsoft JhengHei UI', 9), padx=8, pady=2)
        self.stage_badge.pack(side='right')
        self.chinese = self.text_box(self.chinese_panel, self.size, '#f5f7fc', 5)
        self.chinese.grid(row=1, column=0, sticky='nsew')
        self.chinese.bind('<Configure>', lambda e: root.after_idle(lambda: self.chinese.see('end')))
        self.chinese.tag_configure('llm_source', font=('Microsoft JhengHei UI', 16), foreground='#a8bdd0')

        self.original_panel = tk.Frame(root, bg=panel, padx=10, pady=8)
        self.original_panel.grid(row=2, column=0, sticky='nsew', padx=18, pady=(10, 5))
        self.original_label = tk.StringVar(value='即時原文 · 等待辨識')
        tk.Label(self.original_panel, textvariable=self.original_label, anchor='w', bg=panel, fg=muted,
                 font=('Microsoft JhengHei UI', 9)).pack(fill='x', pady=(0, 4))
        self.original = self.text_box(self.original_panel, 15, '#c6d4e4', 2)
        self.original.configure(bg=panel)
        self.original.pack(fill='both', expand=True)
        self.show_translation('播放影片，字幕會顯示在這裡。', '')

        self.settings_panel = tk.Frame(root, bg=panel, padx=10, pady=8)
        self.settings_panel.grid(row=3, column=0, sticky='ew', padx=18, pady=5)
        choices = tk.Frame(self.settings_panel, bg=panel)
        choices.pack(fill='x')
        for label, var, command in [
            ('置頂', self.topmost, lambda: root.attributes('-topmost', self.topmost.get())),
            ('即時原文', self.show_original, self.toggle_original),
            ('LLM 原文', self.show_llm_original, self.render_translation),
            ('透明背景', self.transparent, self.apply_opacity),
        ]:
            PillButton(choices, label, command, var).pack(side='left', padx=(0, 8))
        tk.Label(choices, textvariable=self.language_status, bg=panel, fg=muted).pack(side='right')
        appearance = tk.Frame(self.settings_panel, bg=panel)
        appearance.pack(fill='x', pady=(6, 0))
        PillButton(appearance, 'A−', lambda: self.resize(-2)).pack(side='left')
        self.size_label = tk.StringVar(value=f'{self.size} pt')
        tk.Label(appearance, textvariable=self.size_label, bg=panel, fg=muted, width=6).pack(side='left')
        PillButton(appearance, 'A＋', lambda: self.resize(2)).pack(side='left')
        tk.Label(appearance, text='視窗不透明度', bg=panel, fg=muted).pack(side='left', padx=(18, 5))
        tk.Scale(appearance, from_=55, to=100, variable=self.opacity, orient='horizontal', length=140,
                 bg=panel, fg=muted, highlightthickness=0, troughcolor='#34465a',
                 command=lambda v: self.apply_opacity()).pack(side='left')
        if self.mode_file is not None:
            modes = tk.Frame(self.settings_panel, bg=panel)
            modes.pack(fill='x', pady=(6, 0))
            tk.Label(modes, text='翻譯模式', bg=panel, fg=muted).pack(side='left', padx=(0,8))
            for label, code in [('兩段合併', 'paired'), ('逐段滑動', 'sliding')]:
                PillButton(modes, label, self.request_mode, self.selected_mode, code).pack(side='left', padx=3)
            tk.Label(modes, textvariable=self.mode_status, bg=panel, fg=muted).pack(side='left', padx=10)
        if self.model_file is not None:
            models = tk.Frame(self.settings_panel, bg=panel)
            models.pack(fill='x', pady=(6, 2))
            tk.Label(models, text='辨識模型', bg=panel, fg=muted).pack(side='left', padx=(0, 8))
            self.model_combo = ttk.Combobox(models, textvariable=self.model_choice, state='readonly',
                                           values=[v['label'] for v in MODELS.values()], width=28)
            self.model_combo.pack(side='left', fill='x', expand=True, padx=4)
            self.model_button = PillButton(models, '切換模型', self.request_model)
            self.model_button.pack(side='left', padx=4)
            self.model_button.configure(state='disabled')
            tk.Label(self.settings_panel, textvariable=self.model_status, bg=panel, fg=muted,
                     wraplength=620, anchor='w', justify='left').pack(fill='x')
        if self.source_file is not None:
            sources = tk.Frame(self.settings_panel, bg=panel)
            sources.pack(fill='x', pady=(6, 0))
            tk.Label(sources, text='音訊來源', bg=panel, fg=muted).pack(side='left', padx=(0, 8))
            for label, mode in [('全部輸出', 'device'), ('指定程式', 'process')]:
                button = PillButton(sources, label, self.update_source_controls, self.source_mode, mode)
                button.pack(side='left', padx=3)
                if mode == 'process' and not process_capture_supported():
                    button.configure(state='disabled')
            self.application_combo = ttk.Combobox(sources, textvariable=self.source_choice,
                                                   state='readonly', width=26)
            self.application_combo.pack(side='left', fill='x', expand=True, padx=6)
            self.refresh_button = PillButton(sources, '重新整理', self.refresh_applications)
            self.refresh_button.pack(side='left', padx=3)
            self.source_button = PillButton(sources, '套用', self.request_source)
            self.source_button.pack(side='left', padx=3)
            tk.Label(self.settings_panel, textvariable=self.source_status, bg=panel, fg=muted,
                     anchor='w', wraplength=620).pack(fill='x', pady=(4, 0))
            self.update_source_controls()
        self.settings_panel.grid_remove()

        footer = self.footer = tk.Frame(root, bg=bg)
        footer.grid(row=4, column=0, sticky='ew', padx=18, pady=(6, 10))
        footer.columnconfigure(0, weight=1)
        self.status = tk.StringVar(value='等待語音…')
        tk.Label(footer, textvariable=self.status, bg=bg, fg=muted, anchor='w',
                 font=('Microsoft JhengHei UI', 9)).grid(row=0, column=0, sticky='ew')
        self.stop_button = PillButton(footer, '停止', self.request_stop, danger=True)
        self.stop_button.grid(row=0, column=2, padx=(8, 0))
        self.pause_button = PillButton(footer, '暫停', self.request_pause, self.pause_requested)
        if self.pause_file is not None:
            self.pause_button.grid(row=0, column=1, padx=(8, 0))
        grip = tk.Label(footer, text='◢', bg=bg, fg='#99aabe', cursor='sizing', padx=8, font=('Segoe UI', 15))
        grip.grid(row=0, column=3, sticky='se', padx=(4, 0))
        grip.bind('<ButtonPress-1>', self.start_resize)
        grip.bind('<B1-Motion>', self.resize_window)
        self.install_resize_edges()
        root.bind('<Escape>', self.exit_compact)
        root.bind('<Alt-F4>', lambda e: self.close())
        self.drawer = tk.Frame(root, bg=panel, padx=8, pady=6)
        self.drawer.grid(row=5, column=0, sticky='nsew', padx=18, pady=(0, 10))
        drawer_bar = tk.Frame(self.drawer, bg=panel)
        drawer_bar.pack(fill='x')
        self.drawer_title = tk.StringVar(value='歷史字幕 · 最近 100 段')
        tk.Label(drawer_bar, textvariable=self.drawer_title, bg=panel, fg=muted).pack(side='left')
        PillButton(drawer_bar, '收起', lambda: self.toggle_drawer(self.drawer_mode)).pack(side='right')
        PillButton(drawer_bar, '複製最新', self.copy_translation).pack(side='right', padx=6)
        self.history_frame = tk.Frame(self.drawer, bg=panel)
        self.history = self.text_box(self.history_frame, 14, '#dbe5f0', 7)
        scroll = ttk.Scrollbar(self.history_frame, command=self.history.yview)
        self.history.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y')
        self.history.pack(fill='both', expand=True)
        self.usage_panel = UsagePanel(self.drawer, self.usage)
        self.drawer.grid_remove()
        self.usage_label = tk.StringVar(value='0 tokens')
        metadata = tk.Frame(footer, bg=bg)
        metadata.grid(row=1, column=0, columnspan=4, sticky='ew', pady=(2, 0))
        metadata.columnconfigure(2, weight=1)
        tk.Label(metadata, textvariable=self.usage_label, bg=bg, fg='#90e2ce',
                 font=('Microsoft JhengHei UI', 9)).grid(row=0, column=0, sticky='w')
        tk.Label(metadata, text='·', bg=bg, fg=muted,
                 font=('Microsoft JhengHei UI', 9)).grid(row=0, column=1, padx=8)
        tk.Label(metadata, textvariable=self.models_label, bg=bg, fg=muted,
                 font=('Microsoft JhengHei UI', 9), anchor='w').grid(row=0, column=2, sticky='ew')
        self.restore_preferences()
        for var in (self.opacity, self.transparent, self.topmost, self.show_original,
                    self.show_llm_original, self.compact, self.settings_open, self.size_label):
            var.trace_add('write', lambda *args: self.schedule_preferences())
        root.bind('<Configure>', lambda e: self.schedule_preferences() if e.widget is root else None, add='+')
        root.after(100, self.poll)
        root.after(250, self.overlay_tick)

    def schedule_preferences(self):
        if self._save_after is not None:
            self.root.after_cancel(self._save_after)
        self._save_after = self.root.after(700, self.store_preferences)

    def store_preferences(self):
        self._save_after = None
        if self.root.state() == 'iconic':
            return
        data = {key: getattr(self, key).get() for key in
                ('opacity', 'transparent', 'topmost', 'show_original', 'show_llm_original', 'compact', 'settings_open')}
        data.update(size=self.size, x=self.root.winfo_x(), y=self.root.winfo_y(),
                    width=self.root.winfo_width(), height=self.root.winfo_height())
        if self.drawer_mode is not None:
            data['height'] = self._before_drawer_height
            data['x'], data['y'] = self._before_drawer_position
        if data == self._last_saved_window:
            return
        try:
            save_preferences('window', data)
            self._last_saved_window = data
        except OSError as exc:
            self.status.set(f'設定無法保存：{exc}')

    def restore_preferences(self):
        data = self.saved_window
        for key, default in [('transparent', False), ('topmost', True), ('show_original', True),
                             ('show_llm_original', True), ('compact', False), ('settings_open', False)]:
            getattr(self, key).set(flag(data, key, default))
        self.opacity.set(bounded(data, 'opacity', 95, 55, 100))
        self.resize(bounded(data, 'size', 26, 14, 64) - self.size)
        self.root.attributes('-topmost', self.topmost.get())
        self.apply_opacity()
        if self.compact.get():
            self.settings_open.set(False)
            self.toggle_compact()
        self.toggle_settings()
        self.toggle_original()
        self.root.update_idletasks()
        width = bounded(data, 'width', 900, 660, 7680)
        height = bounded(data, 'height', 300 if self.compact.get() else 540, 260, 4320)
        x = bounded(data, 'x', self.root.winfo_x(), -32000, 32000)
        y = bounded(data, 'y', self.root.winfo_y(), -32000, 32000)
        self.set_window_bounds(x, y, width, height)
        self.root.update_idletasks()
        # Clamp to the nearest monitor, including monitors with negative coordinates.
        left, top, right, bottom = self.work_area()
        width, height = min(width, right-left), min(height, bottom-top)
        self.set_window_bounds(max(left,min(x,right-width)), max(top,min(y,bottom-height)), width,height)

    def set_window_bounds(self, left, top, width=None, height=None):
        if os.name == 'nt':
            import ctypes
            from ctypes import wintypes
            api = ctypes.windll.user32
            api.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
            api.GetAncestor.restype = wintypes.HWND
            api.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                        ctypes.c_int, ctypes.c_int, wintypes.UINT]
            api.SetWindowPos.restype = wintypes.BOOL
            hwnd = api.GetAncestor(self.root.winfo_id(), 2)
            flags = 0x0004 | 0x0010  # preserve Z order and activation
            if width is None:
                flags |= 0x0001
            if not api.SetWindowPos(hwnd, None, int(left), int(top), int(width or 0), int(height or 0), flags):
                raise OSError('無法調整視窗位置')
        else:
            size = '' if width is None else f'{width}x{height}'
            self.root.geometry(f'{size}{left:+d}{top:+d}')

    def work_area(self):
        if os.name == 'nt':
            import ctypes
            from ctypes import wintypes
            class MonitorInfo(ctypes.Structure):
                _fields_ = [('size', wintypes.DWORD), ('monitor', wintypes.RECT),
                            ('work', wintypes.RECT), ('flags', wintypes.DWORD)]
            api = ctypes.windll.user32
            api.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
            api.MonitorFromWindow.restype = wintypes.HANDLE
            api.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MonitorInfo)]
            info = MonitorInfo(); info.size = ctypes.sizeof(info)
            monitor = api.MonitorFromWindow(self.root.winfo_id(), 2)
            if api.GetMonitorInfoW(monitor, ctypes.byref(info)):
                r = info.work
                return r.left, r.top, r.right, r.bottom
        return 0, 0, self.root.winfo_screenwidth(), self.root.winfo_screenheight()

    def start_drag(self, event):
        self._drag_origin = (event.x_root, event.y_root, self.root.winfo_x(), self.root.winfo_y())

    def drag_window(self, event):
        x, y, left, top = self._drag_origin
        # Windows virtual-desktop coordinates may be negative on other monitors.
        self.set_window_bounds(left+event.x_root-x, top+event.y_root-y)

    def install_resize_edges(self):
        self.resize_handles = []
        specs = [
            ('n', 'sb_v_double_arrow', dict(relx=0, rely=0, relwidth=1, height=6)),
            ('s', 'sb_v_double_arrow', dict(relx=0, rely=1, relwidth=1, height=6, anchor='sw')),
            ('w', 'sb_h_double_arrow', dict(relx=0, rely=0, relheight=1, width=6)),
            ('e', 'sb_h_double_arrow', dict(relx=1, rely=0, relheight=1, width=6, anchor='ne')),
            ('nw', 'size_nw_se', dict(relx=0, rely=0, width=12, height=12)),
            ('ne', 'size_ne_sw', dict(relx=1, rely=0, width=12, height=12, anchor='ne')),
            ('sw', 'size_ne_sw', dict(relx=0, rely=1, width=12, height=12, anchor='sw')),
            ('se', 'size_nw_se', dict(relx=1, rely=1, width=12, height=12, anchor='se')),
        ]
        for direction, cursor, placement in specs:
            handle = tk.Frame(self.root, bg='#10151d', cursor=cursor)
            handle.place(**placement)
            handle.bind('<ButtonPress-1>', lambda e, edge=direction: self.start_resize(e, edge))
            handle.bind('<B1-Motion>', self.resize_window)
            self.resize_handles.append(handle)

    def start_resize(self, event, direction='se'):
        self._resize_origin = (event.x_root, event.y_root, self.root.winfo_width(),
                               self.root.winfo_height(), self.root.winfo_x(), self.root.winfo_y(), direction)

    def resize_window(self, event):
        x, y, width, height, left, top, direction = self._resize_origin
        minimum_w, minimum_h = self.root.minsize()
        dx, dy = event.x_root-x, event.y_root-y
        new_w = max(minimum_w, width + (dx if 'e' in direction else -dx if 'w' in direction else 0))
        new_h = max(minimum_h, height + (dy if 's' in direction else -dy if 'n' in direction else 0))
        if 'w' in direction:
            left += width-new_w
        if 'n' in direction:
            top += height-new_h
        self.set_window_bounds(left, top, new_w, new_h)

    def minimize_window(self):
        self._restore_borderless = True
        self.root.overrideredirect(False)
        self.root.iconify()

    def restore_borderless(self, event):
        if event.widget is self.root and self._restore_borderless and self.root.state() == 'normal':
            self._restore_borderless = False
            self.root.after_idle(lambda: self.root.overrideredirect(True))

    def copy_translation(self):
        text = self.translation_text
        if self.show_llm_original.get() and self.llm_text:
            text += '\n' + self.llm_text
        self.root.clipboard_clear()
        self.root.clipboard_append(text)

    def toggle_settings(self):
        if self.settings_open.get():
            if self.compact.get():
                self.compact.set(False)
                self.toggle_compact()
            self.settings_panel.grid()
            self.root.minsize(760, 650 if self.model_file else 580)
        else:
            self.settings_panel.grid_remove()
            self.root.minsize(660, 260 if self.compact.get() else 330)

    def toggle_compact(self):
        if self.compact.get():
            if self.drawer_mode is not None:
                self.toggle_drawer(self.drawer_mode)
            self._regular_height = self.root.winfo_height()
            self.settings_open.set(False)
            self.settings_panel.grid_remove()
            self.root.minsize(660, 260)
            self.root.geometry(f'{max(660, self.root.winfo_width())}x300')
        else:
            self.root.minsize(660, 330)
            self.root.geometry(f'{max(660, self.root.winfo_width())}x{max(330, self._regular_height)}')
        self.toggle_original()

    def exit_compact(self, event=None):
        if self.compact.get():
            self.compact.set(False)
            self.toggle_compact()


    @staticmethod
    def text_box(parent, size, color, height):
        return tk.Text(parent, font=('Microsoft JhengHei UI', size), fg=color, bg='#10151d', insertbackground=color, wrap='word', height=height, relief='flat', borderwidth=0, highlightthickness=0, padx=2, pady=3, spacing1=2, spacing3=2, state='disabled')

    @staticmethod
    def set_text(widget, value):
        if widget.get('1.0', 'end-1c') == value:
            return
        widget.configure(state='normal')
        widget.delete('1.0', 'end')
        widget.insert('1.0', value)
        widget.configure(state='disabled')

    def resize(self, delta):
        self.size = max(16, min(42, self.size + delta))
        self.chinese.configure(font=('Microsoft JhengHei UI', self.size))
        self.chinese.tag_configure('earlier', font=('Microsoft JhengHei UI', max(16, self.size-5)))
        self.size_label.set(f'{self.size} pt')
        self.chinese.tag_configure('llm_source', font=('Microsoft JhengHei UI', max(13, round(self.size * .62))))

    def show_translation(self, chinese, llm_source):
        self.translation_text = chinese
        self.llm_text = llm_source
        self.render_translation()

    def render_translation(self):
        rows = [r for r in self.entries if r.get('zh_tw') and r.get('is_final', True)][-3:]
        content = tuple((r.get('window_id', r.get('asr_call')), r['zh_tw'], r.get('source_punctuated', '')) for r in rows)
        signature = (content, self.translation_text if not rows else '', self.show_llm_original.get())
        if self._rendered_translation == signature:
            return
        self._rendered_translation = signature
        at_bottom = not self.chinese.winfo_ismapped() or self.chinese.yview()[1] >= .99
        position = self.chinese.yview()[0]
        self.chinese.configure(state='normal')
        self.chinese.delete('1.0', 'end')
        self.chinese.tag_configure('separator', font=('Microsoft JhengHei UI', 8), spacing1=0, spacing3=0)
        self.chinese.tag_configure('earlier', foreground='#a9b8c8', font=('Microsoft JhengHei UI', max(16, self.size-5)))
        if not rows:
            self.chinese.insert('end', self.translation_text)
        for index, row in enumerate(rows):
            if index: self.chinese.insert('end', '\n\n', 'separator')
            self.chinese.insert('end', row['zh_tw'], 'earlier' if index < len(rows)-1 else ())
            if index == len(rows)-1 and self.show_llm_original.get() and row.get('source_punctuated'):
                self.chinese.insert('end', '\n'+row['source_punctuated'], 'llm_source')
        self.chinese.configure(state='disabled')
        if at_bottom: self.root.after_idle(lambda: self.chinese.see('end'))
        else: self.chinese.yview_moveto(position)

    def toggle_original(self):
        if self.show_original.get() and not self.compact.get():
            self.original_panel.grid()
            self.root.rowconfigure(2, weight=1)
        else:
            self.original_panel.grid_remove()
            self.root.rowconfigure(2, weight=0)

    def update_source_controls(self):
        enabled = self.source_mode.get() == 'process' and not self.stopped
        self.application_combo.configure(state='readonly' if enabled else 'disabled')
        self.refresh_button.configure(state='normal' if enabled else 'disabled')
        if enabled and not self.applications:
            self.refresh_applications()

    def refresh_applications(self):
        old = self.application_combo.current()
        previous = self.applications[old] if 0 <= old < len(self.applications) else None
        try:
            self.applications = list_applications()
            self.application_combo.configure(values=[f"{r['name']} · {r['pid']} · {r['title']}" for r in self.applications])
            index = next((i for i,r in enumerate(self.applications) if previous and
                          (r['pid'],r['created']) == (previous['pid'],previous['created'])), 0)
            if previous is None:
                index = next((i for i,r in enumerate(self.applications) if r['name'] == self.saved_runtime.get('source_name')), index)
            if self.applications: self.application_combo.current(index)
            else: self.source_choice.set('沒有可選程式，請先開啟瀏覽器')
        except OSError as exc:
            self.source_status.set(f'無法取得程式清單：{exc}')

    def request_source(self):
        if self.stopped or self.source_file is None:
            return
        request = {'mode': self.source_mode.get(), 'request_id': str(time.time_ns())}
        if request['mode'] == 'process':
            index = self.application_combo.current()
            if index < 0 or index >= len(self.applications):
                self.source_status.set('請先選擇程式。')
                return
            request.update(self.applications[index])
        try:
            temporary = self.source_file.with_suffix('.tmp')
            temporary.write_text(json.dumps(request, ensure_ascii=False), encoding='utf-8')
            temporary.replace(self.source_file)
            self.source_request_id = request['request_id']
            self.source_status.set('正在切換音訊來源…')
        except OSError as exc:
            self.source_status.set(f'音訊來源切換失敗：{exc}')

    def request_model(self):
        if self.stopped or self.model_busy or self.model_file is None:
            return
        key = next(k for k, v in MODELS.items() if v['label'] == self.model_choice.get())
        if self.active_language not in MODELS[key]['languages']:
            self.model_status.set('Kotoba 只支援日文，請先切換 JP。')
            return
        temporary = self.model_file.with_suffix('.tmp')
        try:
            temporary.write_text(json.dumps({'model': key, 'request_id': str(time.time_ns())}), encoding='utf-8')
            temporary.replace(self.model_file)
            self.model_status.set('已送出切換要求…')
        except OSError as exc:
            self.model_status.set(f'無法切換：{exc}')

    def request_mode(self):
        if self.stopped or self.mode_file is None:
            self.selected_mode.set(self.active_mode)
            return
        value = self.selected_mode.get()
        if value not in ('paired', 'sliding'):
            return
        temporary = self.mode_file.with_suffix('.tmp')
        try:
            temporary.write_text(json.dumps({'mode': value}), encoding='utf-8')
            temporary.replace(self.mode_file)
            label = '兩段合併' if self.active_mode == 'paired' else '逐段滑動'
            self.mode_status.set('目前：'+label if value == self.active_mode else '等待下一輪切換')
        except OSError as exc:
            self.selected_mode.set(self.active_mode)
            self.status.set(f'模式切換失敗：{exc}')

    def request_language(self):
        if self.stopped or self.language_file is None:
            self.selected_language.set(self.active_language)
            return
        value = self.selected_language.get()
        if value not in ('en', 'ja'):
            return
        temporary = self.language_file.with_suffix('.tmp')
        try:
            temporary.write_text(json.dumps({'language': value}), encoding='utf-8')
            temporary.replace(self.language_file)
            self.language_status.set('等待切換' if value != self.active_language else '辨識語言')
        except OSError as exc:
            self.selected_language.set(self.active_language)
            self.language_status.set('切換失敗')
            self.status.set(f'無法切換語言：{exc}')

    def request_pause(self):
        if self.stopped or self.pause_file is None:
            self.pause_requested.set(self.paused)
            return
        temporary = self.pause_file.with_suffix('.tmp')
        try:
            temporary.write_text(json.dumps({'paused': self.pause_requested.get()}), encoding='utf-8')
            temporary.replace(self.pause_file)
            self.pause_button.configure(text='繼續' if self.pause_requested.get() else '暫停')
            self.status.set('正在暫停…' if self.pause_requested.get() else '正在繼續…')
        except OSError as exc:
            self.pause_requested.set(self.paused)
            self.status.set(f'暫停切換失敗：{exc}')

    def request_stop(self):
        if not self.stopped:
            try:
                self.stop_file.touch()
            except OSError as exc:
                self.status.set(f'無法停止：{exc}')
                return
            self.status.set('停止收音，等待剩餘字幕完成…')
            self.stop_button.configure(state='disabled')

    def close(self):
        self.store_preferences()
        self.request_stop()
        self.root.destroy()

    def show_history(self):
        self.toggle_drawer('history')

    def toggle_drawer(self, mode):
        if self.drawer_mode == mode:
            self.drawer_mode = None
            self.drawer.grid_remove()
            self.root.rowconfigure(5, weight=0)
            self.set_window_bounds(*self._before_drawer_position, self.root.winfo_width(), self._before_drawer_height)
            return
        if self.compact.get():
            self.exit_compact()
        if self.drawer_mode is None:
            self._before_drawer_height = self.root.winfo_height()
            self._before_drawer_position = (self.root.winfo_x(), self.root.winfo_y())
            left, top, right, bottom = self.work_area()
            height = min(bottom-top-20, self.root.winfo_height()+250)
            self.set_window_bounds(self.root.winfo_x(), max(top, min(self.root.winfo_y(), bottom-height)),
                                   self.root.winfo_width(), height)
        self.drawer_mode = mode
        self.history_frame.pack_forget()
        self.usage_panel.pack_forget()
        self.drawer.grid()
        self.root.rowconfigure(5, weight=2)
        self.drawer_title.set('歷史字幕 · 最近 100 段' if mode == 'history' else '本次 API 累積用量')
        if mode == 'history':
            self.history_frame.pack(fill='both', expand=True)
            self.refresh_history()
        else:
            self.usage_panel.pack(fill='both', expand=True)
            self.usage_panel.refresh(self.elapsed_seconds())

    def refresh_history(self):
        if self.drawer_mode != 'history':
            return
        at_bottom = self.history.yview()[1] >= .99
        position = self.history.yview()[0]
        text = '\n\n'.join(f"{segment_label(r)}\n{r.get('zh_tw') or ('（等待後文／翻譯中）' if r.get('_phase') == 'asr_ready' else ('（辨識失敗，已略過）' if r.get('_phase') == 'asr_failed' else '（翻譯失敗）'))}\n{r.get('source_punctuated') or r.get('source', '')}" for r in self.entries)
        self.set_text(self.history, text)
        if at_bottom: self.history.see('end')
        else: self.history.yview_moveto(position)

    def elapsed_seconds(self):
        return self.elapsed_base + (0 if self.stopped else time.monotonic()-self.elapsed_anchor)

    def apply_opacity(self):
        if os.name == 'nt':
            self.root.attributes('-transparentcolor', '#10151d' if self.transparent.get() else '')
        self.root.attributes('-alpha', 1 if self.transparent.get() else self.opacity.get()/100)

    def overlay_tick(self):
        x, y = self.root.winfo_pointerxy()
        inside = (self.root.winfo_rootx() <= x < self.root.winfo_rootx()+self.root.winfo_width()
                  and self.root.winfo_rooty() <= y < self.root.winfo_rooty()+self.root.winfo_height())
        visible = not self.compact.get() or inside
        if visible != self.controls_visible:
            self.controls_visible = visible
            for widget in (self.header, self.footer, self.caption):
                if visible:
                    widget.grid()
                    widget.master.rowconfigure(int(widget.grid_info()['row']), minsize=0)
                else:
                    # Reserve control space so subtitle position does not jump on hover.
                    parent, row = widget.master, int(widget.grid_info()['row'])
                    parent.rowconfigure(row, minsize=widget.winfo_height()+12)
                    widget.grid_remove()
            for handle in self.resize_handles:
                handle.configure(bg='#283b50' if visible else '#10151d')
        elapsed = self.elapsed_seconds()
        self.usage_label.set(f'{self.usage.total:,} tokens · {duration(elapsed)}'
                             + (f' · {self.usage.unknown} 次用量未知' if self.usage.unknown else ''))
        if self.drawer_mode == 'usage':
            self.usage_panel.refresh(elapsed)
        self.root.after(250, self.overlay_tick)

    def poll(self):
        try:
            for row in self.tail.read():
                if row.get('event') in ('session_started', 'api_usage', 'session_stopped'):
                    self.elapsed_base = row.get('elapsed_seconds', self.elapsed_seconds())
                    self.elapsed_anchor = time.monotonic()
                    if row.get('translation_model'):
                        self.translation_model_label = row['translation_model']
                        self.models_label.set(f'ASR：{self.active_model_label} ｜ 翻譯：{self.translation_model_label}')
                if row.get('event') == 'api_usage':
                    self.usage.accept(row)
                elif row.get('event') in ('source_changed', 'source_error'):
                    if self.source_request_id is None or row.get('request_id') == self.source_request_id:
                        if row['event'] == 'source_changed':
                            self.source_status.set('擷取中：' + row['name'])
                        else:
                            self.source_status.set('未收音：' + row['error'])
                elif row.get('event') == 'pipeline_notice':
                    self.status.set(row['message'])
                elif row.get('event') == 'model_released':
                    self.model_busy = False
                    if self.model_file is not None:
                        self.model_button.configure(state='normal')
                    key = row.get('model')
                    label = MODELS[key]['label'] if key in MODELS else '未選擇模型'
                    if key in MODELS:
                        self.model_choice.set(label)
                    self.active_model_label = label + '（已卸載）'
                    self.models_label.set(f'ASR：{self.active_model_label} ｜ 翻譯：{self.translation_model_label}')
                    self.model_status.set('模型資源已釋放；按繼續後重新載入。')
                    self.status.set('已暫停 · ASR RAM／顯存已釋放')
                elif row.get('event') in ('model_loading', 'model_changed'):
                    loading = row['event'] == 'model_loading'
                    self.model_busy = loading
                    if self.model_file is not None:
                        self.model_button.configure(state='disabled' if loading else 'normal')
                    key = row.get('model')
                    label = MODELS[key]['label'] if key in MODELS else '未載入模型'
                    self.active_model_label = ('切換中 → ' if loading else '') + label
                    self.models_label.set(f'ASR：{self.active_model_label} ｜ 翻譯：{self.translation_model_label}')
                    if not loading and key in MODELS:
                        self.model_choice.set(label)
                    message = row.get('message') if loading else row.get('backend', '')
                    self.model_status.set(label + ' · ' + (message or '') +
                                          (' · ' + row['error'] if row.get('error') else ''))
                    self.status.set('載入模型中 · 暫停辨識' if loading else
                                    ('模型未就緒 · 請在設定重試' if key is None else label + ' · 已就緒'))
                elif row.get('event') == 'mode_changed':
                    self.active_mode = row['mode']
                    label = '兩段合併' if self.active_mode == 'paired' else '逐段滑動'
                    self.mode_status.set('目前：'+label if self.selected_mode.get() == self.active_mode else '等待下一輪切換')
                elif row.get('event') == 'asr_failed':
                    self.state.accept(row)
                    self.original_label.set('即時原文 · ' + segment_label(row))
                    self.set_text(self.original, '此段辨識失敗，已略過並繼續。')
                    self.status.set('單段辨識失敗 · 持續收音中')
                    self.refresh_history()
                elif row.get('event') == 'pause_changed':
                    self.paused = row['paused']
                    self.status.set('已暫停 · 完成佇列後釋放模型' if self.paused else '正在恢復 · 等待模型就緒')
                elif row.get('event') == 'language_changed':
                    self.active_language = row['language']
                    if row.get('rejected'):
                        self.selected_language.set(self.active_language)
                        self.model_status.set('Kotoba 只支援日文；切換其他模型後才能選 EN。')
                    self.language_status.set('辨識語言' if self.selected_language.get() == self.active_language else '等待切換')
                elif row.get('status') == 'session_stopped':
                    self.stopped = True
                    if self.model_file is not None:
                        self.model_button.configure(state='disabled')
                    self.stage.set('已停止')
                    self.stop_button.configure(state='disabled')
                    self.pause_button.configure(state='disabled')
                    if self.source_file is not None:
                        self.source_button.configure(state='disabled')
                    stopped_text = '已停止 · 可回看歷史字幕' if row.get('delete_event_log') else '已停止 · 字幕已存檔'
                    self.status.set(stopped_text if not row.get('error') else '已停止 · ' + row['error'])
                    if row.get('delete_event_log'):
                        self.tail.path.unlink(missing_ok=True)
                elif self.state.accept(row):
                    original = self.state.original
                    translated = self.state.translation
                    self.original_label.set('即時原文 · ' + segment_label(original))
                    self.set_text(self.original, original['source'])
                    if translated is not None:
                        self.chinese_label.set('繁體中文 · ' + segment_label(translated))
                        final = translated.get('is_final', True)
                        self.stage.set('翻譯失敗' if translated.get('error') else ('已定稿' if final else '草稿 · 等待後文'))
                        self.stage_badge.configure(bg='#382a32' if translated.get('error') else ('#183c39' if final else '#343024'), fg='#f3b6c3' if translated.get('error') else ('#a7e4d4' if final else '#e7cd93'))
                        self.show_translation(translated.get('zh_tw') or '翻譯失敗，原文已保留於歷史字幕。', translated.get('source_punctuated') or '')
                    else:
                        self.chinese_label.set('中文翻譯 · 等待翻譯')
                        self.show_translation('等待後文，再顯示中文…', '')
                    self.status.set('已暫停 · 已送出的翻譯仍會完成' if self.paused else (f'原文即時更新 · {self.state.pending} 段等待翻譯' if self.state.pending else '原文與翻譯已更新'))
                    self.refresh_history()
        except (OSError, ValueError) as exc:
            self.status.set(f'字幕讀取失敗：{exc}')
        self.root.after(150, self.poll)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--log', required=True)
    parser.add_argument('--stop-file', required=True)
    parser.add_argument('--title', default='直播字幕 · SenseVoice')
    parser.add_argument('--pause-file')
    parser.add_argument('--mode-file')
    parser.add_argument('--source-file')
    parser.add_argument('--model-file')
    parser.add_argument('--language-file')
    parser.add_argument('--language', choices=['en', 'ja'], default='ja')
    args = parser.parse_args()
    root = tk.Tk()
    app = SubtitleWindow(root, args.log, args.stop_file, args.language_file, args.language, args.pause_file, args.mode_file, args.source_file, args.model_file)
    root.title(args.title)
    root.mainloop()
