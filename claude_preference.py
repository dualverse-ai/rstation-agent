"""Reselect the configured model after a successful native fallback turn."""
import time

COOLDOWN_SECONDS = 180
ACK_TIMEOUT_SECONDS = 5
PREFERRED_MODELS = {'claude-opus-5-5', 'claude-fable-5-2'}
FALLBACK_MODELS = {'claude-opus-5', 'claude-opus-4-8'}


class Preference:
    def __init__(self):
        self.model = None
        self.fallback_seen = False
        self.completed = False
        self.eligible = False
        self.not_before = 0
        self.pending = None
        self.serial = 0

    def observe(self, event, now=None):
        now = time.monotonic() if now is None else now
        if event.get('parent_tool_use_id') or event.get('isSidechain'):
            return
        kind = event.get('type')
        if kind == 'control_response' and self.pending:
            response = event.get('response', {})
            if response.get('request_id') == self.pending[0]:
                self.pending = None
            return
        if kind == 'system' and event.get('subtype') in ('model_refusal_fallback', 'model_refusal_no_fallback'):
            self.not_before = now + COOLDOWN_SECONDS
            self.completed = self.eligible = False
        elif kind == 'user':
            self.model = None
            self.completed = self.eligible = False
        elif kind == 'assistant':
            message = event.get('message')
            if not isinstance(message, dict):return
            self.model = message.get('model')
            if self.model in FALLBACK_MODELS and not self.fallback_seen:
                self.not_before = max(self.not_before, now + COOLDOWN_SECONDS)
                self.fallback_seen = True
            elif self.model in PREFERRED_MODELS:
                self.fallback_seen = False
            self.completed = not event.get('isApiErrorMessage') and message.get('stop_reason') == 'end_turn'
            self.eligible = False
            if message.get('stop_reason') == 'refusal':
                self.not_before = now + COOLDOWN_SECONDS
        elif kind == 'result':
            failed = event.get('is_error') or event.get('subtype') != 'success' or event.get('stop_reason') == 'refusal'
            self.eligible = not failed and self.completed and self.model in FALLBACK_MODELS
            if failed:
                self.completed = False
                if event.get('stop_reason') == 'refusal':
                    self.not_before = now + COOLDOWN_SECONDS

    def waiting(self, now=None):
        now = time.monotonic() if now is None else now
        if self.pending and now >= self.pending[1]:self.pending = None
        return self.pending is not None

    def select(self, preferred, *, idle, now=None):
        now = time.monotonic() if now is None else now
        if not idle or self.waiting(now) or not self.eligible or now < self.not_before:
            return None
        if preferred not in PREFERRED_MODELS or self.model == preferred:return None
        self.eligible = False
        self.serial += 1
        request_id = 'model-preference-' + str(self.serial)
        self.pending = (request_id, now + ACK_TIMEOUT_SECONDS)
        self.not_before = now + COOLDOWN_SECONDS
        return {'type':'control_request', 'request_id':request_id,
                'request':{'subtype':'set_model', 'model':preferred}}
