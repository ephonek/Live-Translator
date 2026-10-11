"""Shared validation for the capture process and settings UI (no GPU imports)."""
import math

FIELDS = {
    'overlap_seconds': ('前文重疊（秒）', 2.0, 0.0, 5.0, 0.5),
    'min_seconds': ('最短切段（秒）', 2.0, 0.5, 10.0, 0.5),
    'max_seconds': ('最長切段（秒）', 10.0, 1.0, 20.0, 0.5),
    'pause_seconds': ('停頓切段（秒）', 0.6, 0.2, 2.0, 0.1),
    'vad_threshold': ('VAD 人聲門檻', 0.4, 0.1, 0.9, 0.05),
}
DEFAULTS = {key: spec[1] for key, spec in FIELDS.items()}
DEFAULTS['qwen_context'] = False


def validate_settings(values):
    if not isinstance(values, dict):
        raise ValueError('設定格式不正確。')
    context = values.get('qwen_context', False)
    if type(context) is not bool:
        raise ValueError('Qwen 前文參考必須為開或關。')
    result = {'qwen_context': context}
    for key, (label, default, low, high, _) in FIELDS.items():
        raw = values.get(key, default)
        try:
            if isinstance(raw, bool):
                raise ValueError
            value = float(raw)
        except (TypeError, ValueError):
            raise ValueError(f'{label} 請輸入數字。') from None
        if not math.isfinite(value) or not low <= value <= high:
            raise ValueError(f'{label} 範圍為 {low:g}～{high:g}。')
        result[key] = value
    if result['min_seconds'] > result['max_seconds']:
        raise ValueError('最短切段時間不能大於最長切段時間。')
    return result


def load_settings(values):
    try:
        return validate_settings(values)
    except ValueError:
        return dict(DEFAULTS)
