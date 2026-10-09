"""Wait for the next ASR segment, translate only the current segment, then slide by one."""
from collections import deque
from queue import Empty


def translate_windows(jobs, translate, emit, language, wait_seconds=10.0, get_mode=None):
    history = deque(maxlen=4)
    carry = None
    previous_epoch = None
    previous_id = None
    while True:
        item = carry if carry is not None else jobs.get()
        carry = None
        if item is None:
            return
        mode = get_mode() if get_mode is not None else "sliding"
        if mode not in ("sliding", "paired"):
            raise ValueError("Invalid translation mode")
        first, captured_at = item
        current_language = first.get("language", language)
        epoch = (current_language, first.get("language_epoch", 0))
        if previous_epoch is not None and (epoch != previous_epoch or first['asr_call'] != previous_id + 1):
            history.clear()
        previous_epoch = epoch
        previous_id = first['asr_call']
        base = dict(first, window_id=first['asr_call'],
                    covered_asr_calls=[first['asr_call']], version=1, translation_mode=mode)
        stopped = False
        try:
            following = jobs.get(timeout=wait_seconds)
            stopped = following is None
        except Empty:
            following = None
        changed = following is not None and (following[0].get('language', language), following[0].get('language_epoch', 0)) != epoch
        gap = following is not None and following[0]['asr_call'] != first['asr_call'] + 1
        # B remains pending: it is context for A now, and the translation target next.
        carry = following
        reason = 'boundary' if changed or gap else ('stop' if stopped else 'timeout')
        if following is not None and not changed and not gap:
            second, _ = following
            if mode == 'paired':
                separator = ' ' if current_language == 'en' else ''
                base.update(source=first['source']+separator+second['source'], end=second['end'],
                            covered_asr_calls=[first['asr_call'], second['asr_call']],
                            asr_seconds=first.get('asr_seconds',0)+second.get('asr_seconds',0))
                previous_id = second['asr_call']
                carry = None
            else:
                base['lookahead_source'] = second['source']
                base['lookahead_asr_call'] = second['asr_call']
            reason = 'lookahead'
        final = translate(base, list(history), captured_at)
        final.update(is_final=True, translation_stage='final', finalize_reason=reason)
        emit(final)
        if final.get('zh_tw') and not final.get('error'):
            history.append({'source': final['source'], 'zh_tw': final['zh_tw']})
        if stopped:
            return
