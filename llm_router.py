"""Proveedores alternativos: una petición normal, sin sondeos de saldo."""
import asyncio
from dataclasses import dataclass
from datetime import timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
import math
import os
import time

from openai import APIError, AsyncOpenAI


@dataclass
class Provider:
    name: str
    model: str
    client: AsyncOpenAI
    fingerprint: str
    api: str = 'chat'


def positive_setting(env, name, default):
    try:
        value = float(env.get(name, str(default)))
    except ValueError:
        raise ValueError(f'{name} debe ser un número positivo.') from None
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f'{name} debe ser un número positivo.')
    return value


def configured_providers(env=None):
    env = os.environ if env is None else env
    specs = {
        'openai': ('OPENAI_API_KEY', 'OPENAI_MODEL', 'gpt-4.1-mini', 'https://api.openai.com/v1', 'responses'),
        'deepseek': ('DEEPSEEK_API_KEY', 'DEEPSEEK_MODEL', 'deepseek-flash', 'https://api.deepseek.com', 'chat'),
        'google': ('GOOGLE_API_KEY', 'GOOGLE_MODEL', 'gemini-3.1-flash-lite', 'https://generativelanguage.googleapis.com/v1beta/openai/', 'chat'),
    }
    order = [name.strip().lower() for name in env.get('LLM_PROVIDERS', 'google,deepseek,openai').split(',') if name.strip()]
    order = list(dict.fromkeys('google' if name == 'gemini' else name for name in order))
    if not order or any(name not in specs for name in order):
        raise ValueError('LLM_PROVIDERS admite openai,deepseek,google, en el orden deseado.')
    configured = []
    # Valida todo antes de construir clientes para evitar recursos a medio crear.
    for name in order:
        key_name, model_name, default_model, url, api = specs[name]
        key = env.get(key_name, '').strip()
        if name == 'google' and not key:
            key = env.get('GEMINI_API_KEY', '').strip()
        if key:
            model = env.get(model_name, '').strip() or default_model
            fingerprint = hashlib.sha256(f'{name}|{model}|{key}'.encode()).hexdigest()
            configured.append((name, model, key, url, api, fingerprint))
    if not configured:
        raise ValueError('Configura al menos una clave: OPENAI_API_KEY, DEEPSEEK_API_KEY o GOOGLE_API_KEY.')
    timeout = positive_setting(env, 'LLM_TIMEOUT_SECONDS', 30)
    return [Provider(name, model, AsyncOpenAI(api_key=key, base_url=url, timeout=timeout, max_retries=0), fingerprint, api)
            for name, model, key, url, api, fingerprint in configured]


class ProviderRouter:
    def __init__(self, providers, read_state, write_state, *, env=None, clock=time.time, log=print):
        self.providers = providers
        self.read_state = read_state
        self.write_state = write_state
        self.clock = clock
        self.log = log
        env = os.environ if env is None else env
        self.quota_pause = positive_setting(env, 'LLM_QUOTA_COOLDOWN_SECONDS', 3600)
        self.rate_pause = positive_setting(env, 'LLM_RATE_COOLDOWN_SECONDS', 60)
        self.error_pause = positive_setting(env, 'LLM_ERROR_COOLDOWN_SECONDS', 30)
        self.lock = asyncio.Lock()
        saved = read_state('llm_router', {})
        saved = saved if isinstance(saved, dict) else {}
        names = {provider.name for provider in providers}
        active = saved.get('active')
        same_order = saved.get('order') == [provider.name for provider in providers]
        self.active = active if same_order and active in names else providers[0].name
        blocked = saved.get('blocked', {})
        blocked = blocked if isinstance(blocked, dict) else {}
        self.blocked = {}
        for provider in providers:
            entry = blocked.get(provider.name)
            if (isinstance(entry, dict) and entry.get('fingerprint') == provider.fingerprint
                    and isinstance(entry.get('until'), (int, float)) and math.isfinite(entry['until'])):
                self.blocked[provider.name] = entry

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self.close()

    async def close(self):
        await asyncio.gather(*(provider.client.close() for provider in self.providers))

    def _save(self):
        self.write_state('llm_router', {'active': self.active, 'blocked': self.blocked,
                                       'order': [provider.name for provider in self.providers]})

    async def reset(self):
        async with self.lock:
            self.active = self.providers[0].name
            self.blocked.clear()
            self._save()

    def status(self):
        result = []
        for provider in self.providers:
            entry = self.blocked.get(provider.name, {})
            remaining = max(0, math.ceil(entry.get('until', 0) - self.clock()))
            state = f'en pausa {remaining}s ({entry.get("reason", "error")})' if remaining else 'disponible'
            active = ' [activo]' if provider.name == self.active else ''
            result.append(f'{provider.name}/{provider.model}: {state}{active}')
        return '\n'.join(result)

    def _failure(self, error):
        status = getattr(error, 'status_code', None)
        body = getattr(error, 'body', None)
        body = body if isinstance(body, dict) else {}
        detail = body.get('error', body)
        detail = detail if isinstance(detail, dict) else {}
        code = str(detail.get('code', '')).lower()
        kind = str(detail.get('type', '')).lower()
        quota_codes = {'insufficient_quota', 'credit_balance_exhausted', 'quota_exceeded',
                       'organization_spend_limit_exceeded', 'project_spend_limit_exceeded',
                       'organization_usage_limit_exceeded', 'billing_hard_limit_reached'}
        if status == 402 or code in quota_codes or kind == 'insufficient_quota':
            reason, pause = 'saldo o cuota', self.quota_pause
        elif status == 429:
            # Gemini puede expresar su cuota diaria en los detalles de RESOURCE_EXHAUSTED.
            details = json.dumps(body).lower()
            daily = any(marker in details for marker in ('perday', 'per day', 'daily'))
            reason, pause = ('cuota diaria', self.quota_pause) if daily else ('límite temporal', self.rate_pause)
        elif status in (401, 403, 404):
            reason, pause = 'clave, permiso o modelo', self.quota_pause
        elif status is None or status in (408, 409) or status >= 500:
            reason, pause = 'conexión o servicio', self.error_pause
        else:
            # 400/422 suelen indicar una petición inválida; no se ocultan rotando claves.
            return None
        response = getattr(error, 'response', None)
        if response is not None:
            retry = response.headers.get('retry-after')
            if retry:
                try:
                    delay = float(retry)
                except ValueError:
                    try:
                        date = parsedate_to_datetime(retry)
                        if date.tzinfo is None:
                            date = date.replace(tzinfo=timezone.utc)
                        delay = date.timestamp() - self.clock()
                    except (ValueError, TypeError, OverflowError):
                        delay = 0
                if math.isfinite(delay):
                    pause = max(pause, delay)
        return reason, pause

    async def _request(self, provider, instructions, prompt):
        if provider.api == 'responses':
            response = await provider.client.responses.create(
                model=provider.model, instructions=instructions, input=[{'role': 'user', 'content': prompt}],
                max_output_tokens=1024, text={'format': {'type': 'json_object'}}, store=False,
            )
            return response.output_text.strip() if response.status == 'completed' else None
        params = dict(model=provider.model, messages=[{'role': 'system', 'content': instructions},
                                                    {'role': 'user', 'content': prompt}],
                      max_tokens=1024, response_format={'type': 'json_object'})
        # Evita gastar el presupuesto de salida en razonamiento para un personaje de chat.
        if provider.name == 'deepseek':
            params['extra_body'] = {'thinking': {'type': 'disabled'}}
        if provider.name == 'google':
            can_disable = provider.model.startswith('gemini-2.5-') and 'pro' not in provider.model
            lite = provider.model.startswith(('gemini-3.1-flash-lite', 'gemini-3.5-flash-lite'))
            params['reasoning_effort'] = 'none' if can_disable else ('minimal' if lite else 'low')
            if not can_disable:
                params['max_tokens'] = 4096
        response = await provider.client.chat.completions.create(**params)
        if not response.choices or response.choices[0].finish_reason != 'stop':
            return None
        return (response.choices[0].message.content or '').strip() or None

    async def generate(self, instructions, prompt):
        # Serializa entre canales: tras un fallo, el siguiente mensaje ya ve la pausa.
        async with self.lock:
            ordered = sorted(self.providers, key=lambda provider: provider.name != self.active)
            for provider in ordered:
                if self.blocked.get(provider.name, {}).get('until', 0) > self.clock():
                    continue
                try:
                    text = await self._request(provider, instructions, prompt)
                except APIError as error:
                    failure = self._failure(error)
                    if failure is None:
                        self.log(f'[PeBOT] {provider.name}: petición rechazada (HTTP {getattr(error, "status_code", "?")}).')
                        return None
                    reason, pause = failure
                    self.blocked[provider.name] = {'until': self.clock() + pause, 'reason': reason,
                                                   'fingerprint': provider.fingerprint}
                    self._save()
                    self.log(f'[PeBOT] {provider.name}: {reason}; pausa de {math.ceil(pause)}s. Probando respaldo.')
                    continue
                # Una salida cortada o un rechazo de contenido no activa rotación.
                if not text:
                    self.log(f'[PeBOT] {provider.name}: respuesta vacía o incompleta.')
                    return None
                if self.active != provider.name or provider.name in self.blocked:
                    self.active = provider.name
                    self.blocked.pop(provider.name, None)
                    self._save()
                    self.log(f'[PeBOT] proveedor activo: {provider.name}/{provider.model}.')
                return text
            return None
