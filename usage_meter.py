"""Session usage from API-reported counters; cached tokens are a subset of input."""
import tkinter as tk


class UsageStats:
    def __init__(self):
        self.seen = set()
        self.calls = self.unknown = self.failed = 0
        self.input = self.output = self.cached = 0
        self.points = [(0., 0, 0)]

    @property
    def total(self):
        return self.input + self.output

    def accept(self, row):
        key = row.get('request_id')
        if key is None or key in self.seen:
            return False
        self.seen.add(key)
        self.calls += 1
        self.failed += not row.get('success', True)
        if row.get('usage_known'):
            self.input += row.get('input_tokens', 0)
            self.output += row.get('output_tokens', 0)
            self.cached += row.get('cached_input_tokens', 0)
        else:
            self.unknown += 1
        self.points.append((max(self.points[-1][0], row.get('elapsed_seconds', 0)), self.input, self.output))
        if len(self.points) > 3600:
            self.points = self.points[::2] + [self.points[-1]]
        return True


def duration(seconds):
    seconds = max(0, int(seconds))
    return f'{seconds//3600:02}:{seconds//60%60:02}:{seconds%60:02}'


class UsagePanel(tk.Frame):
    def __init__(self, parent, stats):
        super().__init__(parent, bg='#19222e')
        self.stats = stats
        self.elapsed = 0
        self.summary = tk.StringVar()
        tk.Label(self, textvariable=self.summary, bg='#19222e', fg='#cfdae8',
                 justify='left', anchor='w', font=('Microsoft JhengHei UI', 10)).pack(fill='x', padx=8, pady=6)
        self.chart = tk.Canvas(self, height=155, bg='#19222e', highlightthickness=0)
        self.chart.pack(fill='both', expand=True)
        self.chart.bind('<Configure>', lambda e: self.refresh(self.elapsed))

    def refresh(self, elapsed):
        self.elapsed = max(elapsed, self.stats.points[-1][0])
        s = self.stats
        self.summary.set(f'本次累積 {s.total:,} tokens  ·  輸入 {s.input:,} / 輸出 {s.output:,}  ·  {duration(self.elapsed)}\n'
                         f'{s.calls} 次請求 / {s.failed} 次失敗  ·  快取輸入 {s.cached:,}（已包含於輸入）'
                         + (f'  ·  {s.unknown} 次用量未知，未計入總量' if s.unknown else ''))
        c = self.chart
        c.delete('all')
        w, h = max(160, c.winfo_width()), max(90, c.winfo_height())
        left, right, top, bottom = 60, w-15, 24, h-26
        maximum, span = max(1, s.total), max(1, self.elapsed)
        for f in (0, .5, 1):
            y = bottom-(bottom-top)*f
            c.create_line(left, y, right, y, fill='#304054')
            c.create_text(left-8, y, text=f'{maximum*f:,.0f}', anchor='e', fill='#92a5ba')
            c.create_text(left+(right-left)*f, bottom+14, text=duration(span*f), fill='#92a5ba', anchor='e' if f == 1 else ('w' if f == 0 else 'center'))
        points = s.points + [(self.elapsed, s.input, s.output)]
        for index, (name, color, field) in enumerate([('合計', '#90e2ce', 0), ('輸入', '#80b6ee', 1), ('輸出', '#e9bd80', 2)]):
            c.create_text(left+index*90, 10, text=name, fill=color, anchor='w')
            coords = []
            for t, inp, out in points:
                value = inp+out if field == 0 else (inp if field == 1 else out)
                coords.extend((left+(right-left)*t/span, bottom-(bottom-top)*value/maximum))
            c.create_line(*coords, fill=color, width=2)
