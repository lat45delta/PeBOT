import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from openai import APIConnectionError, APIStatusError, AsyncOpenAI

from llm_router import Provider, ProviderRouter, configured_providers


def api_error(status, code=None, *, kind=None, headers=None, message='error'):
    response = httpx.Response(status, headers=headers, request=httpx.Request('POST', 'https://api.example.test'))
    return APIStatusError('fallo de prueba', response=response,
                          body={'code': code, 'type': kind, 'message': message})


def provider(name):
    return Provider(name, name + '-model', SimpleNamespace(close=AsyncMock()), 'hash-' + name)


class RouterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.providers = [provider(name) for name in ('openai', 'deepseek', 'google')]
        self.saved = {}
        self.now = 1000.0
        self.logs = []
        self.router = self.make_router()

    def make_router(self):
        return ProviderRouter(self.providers, lambda key, default: self.saved.get(key, default),
                              lambda key, value: self.saved.update({key: json.loads(json.dumps(value))}),
                              env={}, clock=lambda: self.now, log=self.logs.append)

    async def test_healthy_provider_makes_one_request_per_message(self):
        with patch.object(self.router, '_request', AsyncMock(return_value='{"respuesta":"hola"}')) as request:
            for _ in range(3):
                self.assertIsNotNone(await self.router.generate('JSON', 'hola'))
        self.assertEqual(request.await_count, 3)
        self.assertTrue(all(call.args[0].name == 'openai' for call in request.call_args_list))
        self.assertEqual(self.saved, {})  # Sin escrituras de estado por cada éxito.

    async def test_credit_failure_retries_same_message_on_backup_and_sticks(self):
        request = AsyncMock(side_effect=[api_error(429, 'credit_balance_exhausted'), 'respuesta', 'siguiente'])
        with patch.object(self.router, '_request', request):
            self.assertEqual(await self.router.generate('JSON', 'mismo mensaje'), 'respuesta')
            self.assertEqual(await self.router.generate('JSON', 'otro mensaje'), 'siguiente')
        self.assertEqual([call.args[0].name for call in request.call_args_list], ['openai', 'deepseek', 'deepseek'])
        self.assertEqual(request.call_args_list[0].args[1:], request.call_args_list[1].args[1:])
        self.assertEqual(self.router.blocked['openai']['until'], 4600)

    async def test_deepseek_402_and_google_429_skip_until_cooldown(self):
        request = AsyncMock(side_effect=[api_error(402), api_error(402), api_error(429)])
        with patch.object(self.router, '_request', request):
            self.assertIsNone(await self.router.generate('JSON', 'hola'))
            self.assertIsNone(await self.router.generate('JSON', 'hola de nuevo'))
        self.assertEqual(request.await_count, 3)
        self.assertEqual(self.router.blocked['google']['until'], 1060)

    async def test_retry_after_is_respected(self):
        error = api_error(429, headers={'Retry-After': '180'})
        request = AsyncMock(side_effect=[error, 'ok'])
        with patch.object(self.router, '_request', request):
            await self.router.generate('JSON', 'hola')
        self.assertEqual(self.router.blocked['openai']['until'], 1180)

    async def test_retry_after_http_date(self):
        from email.utils import formatdate
        error = api_error(429, headers={'Retry-After': formatdate(1300, usegmt=True)})
        self.assertEqual(self.router._failure(error), ('límite temporal', 300))

    async def test_daily_google_quota_uses_long_pause(self):
        error = api_error(429, 'RESOURCE_EXHAUSTED', message='GenerateRequestsPerDay quota exhausted')
        self.assertEqual(self.router._failure(error), ('cuota diaria', 3600))

    async def test_expired_provider_retries_only_on_new_request(self):
        self.router.providers = self.providers[:1]
        request = AsyncMock(side_effect=[api_error(429), 'ok'])
        with patch.object(self.router, '_request', request):
            self.assertIsNone(await self.router.generate('JSON', 'hola'))
            self.now += 59
            self.assertIsNone(await self.router.generate('JSON', 'hola'))
            self.now += 2
            self.assertEqual(await self.router.generate('JSON', 'hola'), 'ok')
        self.assertEqual(request.await_count, 2)
        self.assertNotIn('openai', self.router.blocked)

    async def test_persisted_pause_and_active_provider_survive_restart(self):
        with patch.object(self.router, '_request', AsyncMock(side_effect=[api_error(402), 'ok'])):
            await self.router.generate('JSON', 'hola')
        restored = self.make_router()
        self.assertEqual(restored.active, 'deepseek')
        self.assertEqual(restored.blocked['openai']['until'], 4600)
        self.assertNotIn('api_key', json.dumps(self.saved))

    async def test_key_change_invalidates_old_pause(self):
        with patch.object(self.router, '_request', AsyncMock(side_effect=[api_error(402), 'ok'])):
            await self.router.generate('JSON', 'hola')
        self.providers[0].fingerprint = 'new-key-hash'
        self.assertNotIn('openai', self.make_router().blocked)

    async def test_new_configured_order_resets_initial_priority(self):
        with patch.object(self.router, '_request', AsyncMock(side_effect=[api_error(402), 'ok'])):
            await self.router.generate('JSON', 'hola')
        self.providers = [self.providers[2], self.providers[1], self.providers[0]]
        self.assertEqual(self.make_router().active, 'google')

    async def test_reset_reactivates_original_priority_without_network(self):
        with patch.object(self.router, '_request', AsyncMock(side_effect=[api_error(402), 'ok'])) as request:
            await self.router.generate('JSON', 'hola')
            await self.router.reset()
        self.assertEqual(request.await_count, 2)
        self.assertEqual(self.router.active, 'openai')
        self.assertEqual(self.router.blocked, {})

    async def test_concurrent_messages_only_hit_exhausted_provider_once(self):
        async def request(provider, *_):
            await asyncio.sleep(0)
            if provider.name == 'openai':
                raise api_error(402)
            return 'ok'
        mocked = AsyncMock(side_effect=request)
        with patch.object(self.router, '_request', mocked):
            self.assertEqual(await asyncio.gather(self.router.generate('JSON', 'uno'),
                                                 self.router.generate('JSON', 'dos')), ['ok', 'ok'])
        names = [call.args[0].name for call in mocked.call_args_list]
        self.assertEqual(names.count('openai'), 1)

    async def test_network_and_server_errors_fall_back(self):
        for error in (APIConnectionError(request=httpx.Request('POST', 'https://api.example.test')),
                      api_error(503), api_error(401), api_error(403), api_error(404)):
            await self.router.reset()
            with patch.object(self.router, '_request', AsyncMock(side_effect=[error, 'ok'])):
                self.assertEqual(await self.router.generate('JSON', 'hola'), 'ok')

    async def test_invalid_request_does_not_rotate_providers(self):
        with patch.object(self.router, '_request', AsyncMock(side_effect=api_error(400))) as request:
            self.assertIsNone(await self.router.generate('JSON', 'hola'))
        self.assertEqual(request.await_count, 1)
        self.assertEqual(self.router.blocked, {})

    async def test_refused_or_incomplete_output_does_not_rotate(self):
        with patch.object(self.router, '_request', AsyncMock(return_value=None)) as request:
            self.assertIsNone(await self.router.generate('JSON', 'hola'))
        self.assertEqual(request.await_count, 1)
        self.assertEqual(self.router.blocked, {})

    async def test_status_does_not_call_api(self):
        with patch.object(self.router, '_request', AsyncMock()) as request:
            self.assertIn('[activo]', self.router.status())
            request.assert_not_called()

    async def test_close_cleans_up_all_clients(self):
        await self.router.close()
        for item in self.providers:
            item.client.close.assert_awaited_once()

    async def test_real_sdk_serializes_chat_adapters_without_network(self):
        for name, model, effort in (('deepseek', 'deepseek-flash', None),
                                    ('google', 'gemini-3.8-flash', 'low'),
                                    ('google', 'gemini-3.1-flash-lite', 'minimal'),
                                    ('google', 'gemini-2.5-flash', 'none')):
            requests = []
            def handle(request):
                requests.append(json.loads(request.content))
                return httpx.Response(200, json={'id': 'test', 'object': 'chat.completion', 'created': 1,
                    'model': model, 'choices': [{'index': 0, 'finish_reason': 'stop',
                    'message': {'role': 'assistant', 'content': '{"respuesta":"ya pe"}'}}]})
            async with AsyncOpenAI(api_key='fake-key', base_url='https://example.test/v1', max_retries=0,
                                  http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle))) as client:
                item = Provider(name, model, client, 'fake-hash')
                result = await self.router._request(item, 'Devuelve JSON como PeBOT', 'hola')
            self.assertEqual(json.loads(result)['respuesta'], 'ya pe')
            params = requests[0]
            self.assertEqual(params['response_format'], {'type': 'json_object'})
            self.assertEqual(params['messages'][0]['role'], 'system')
            self.assertEqual(params['model'], model)
            if name == 'deepseek':
                self.assertEqual(params['thinking'], {'type': 'disabled'})
            else:
                self.assertEqual(params['reasoning_effort'], effort)


class ConfigurationTests(unittest.TestCase):
    def test_skips_empty_keys_and_accepts_google_alias(self):
        with patch('llm_router.AsyncOpenAI') as client:
            providers = configured_providers({'LLM_PROVIDERS': 'openai,gemini,google,deepseek',
                                              'GEMINI_API_KEY': 'fake-google'})
        self.assertEqual([p.name for p in providers], ['google'])
        self.assertEqual(client.call_args.kwargs['max_retries'], 0)
        self.assertIn('generativelanguage.googleapis.com', client.call_args.kwargs['base_url'])

    def test_unknown_provider_and_missing_keys_are_actionable(self):
        with self.assertRaisesRegex(ValueError, 'LLM_PROVIDERS'):
            configured_providers({'LLM_PROVIDERS': 'unknown'})
        with self.assertRaisesRegex(ValueError, 'al menos una clave'):
            configured_providers({})

    def test_configuration_order_and_models(self):
        with patch('llm_router.AsyncOpenAI'):
            providers = configured_providers({'LLM_PROVIDERS': 'deepseek,openai', 'OPENAI_API_KEY': 'fake-a',
                                              'DEEPSEEK_API_KEY': 'fake-b', 'DEEPSEEK_MODEL': 'custom-model'})
        self.assertEqual([p.name for p in providers], ['deepseek', 'openai'])
        self.assertEqual(providers[0].model, 'custom-model')

    def test_invalid_cooldown_rejected(self):
        with self.assertRaisesRegex(ValueError, 'LLM_QUOTA_COOLDOWN_SECONDS'):
            ProviderRouter([provider('openai')], lambda *_: {}, lambda *_: None,
                           env={'LLM_QUOTA_COOLDOWN_SECONDS': 'nan'})


if __name__ == '__main__':
    unittest.main()
