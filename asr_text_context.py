"""One preceding raw transcript; tokenizer work stays in the Qwen process."""
CONTEXT_TOKEN_LIMIT = 256


class PreviousTranscript:
    def __init__(self):
        self.clear()

    def clear(self):
        self.text = ''
        self.end = None
        self.epoch = None

    def get(self, start, epoch, enabled):
        if not enabled or epoch != self.epoch or self.end is None or abs(start-self.end) > 1e-5:
            self.clear()
        return self.text

    def update(self, text, end, epoch, enabled):
        if enabled and text.strip():
            self.text, self.end, self.epoch = text, end, epoch
        else:
            self.clear()


def make_context_prompt(tokenizer, text):
    """Limit reference text, excluding the fixed label, to 256 model tokens."""
    if not text or not text.strip():
        return None, 0
    tokens = tokenizer.encode(text.strip(), add_special_tokens=False)[-CONTEXT_TOKEN_LIMIT:]
    while tokens:
        tail = tokenizer.decode(tokens, skip_special_tokens=True, clean_up_tokenization_spaces=False)
        count = len(tokenizer.encode(tail, add_special_tokens=False))
        # Byte-level tokens may cut a CJK character; discard an incomplete prefix.
        if not tail.startswith('\ufffd') and count <= CONTEXT_TOKEN_LIMIT:
            break
        tokens = tokens[1:]
    else:
        return None, 0
    return 'Previous segment transcript (background context only):\n' + tail, count
