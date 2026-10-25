"""Pruebas locales: no conectan con Discord ni consumen la API de OpenAI."""
import asyncio
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


class BotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.env = patch.dict(os.environ, {
            'DISCORD_TOKEN': 'test-token', 'OPENAI_API_KEY': 'test-key',
            'DB_PATH': str(Path(cls.temp.name) / 'test.db'),
            'BOT_TZ': 'America/Lima', 'BOT_NAME': 'PeBOT',
            'OPENAI_MODEL': 'gpt-4.1-mini',
            'LLM_PROVIDERS': 'openai',
            'CHANNEL_IDS': '', 'SLEEP_FROM': '1', 'SLEEP_TO': '8',
        })
        cls.env.start()
        spec = importlib.util.spec_from_file_location('pebot_tested', Path(__file__).with_name('bot.py'))
        cls.bot = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.bot)

    @classmethod
    def tearDownClass(cls):
        cls.bot.db.close()
        asyncio.run(cls.bot.enrutador.close())
        cls.env.stop()
        cls.temp.cleanup()

    def setUp(self):
        for table in ('mensajes', 'recuerdos', 'estado'):
            self.bot.db.execute(f'DELETE FROM {table}')
        self.bot.db.commit()
        self.bot.enrutador.blocked.clear()
        self.bot.enrutador.active = 'openai'
        self.bot.charlas.clear()

    def test_json_markdown_and_invalid_input(self):
        self.assertEqual(self.bot.parse_json('```json\n{"respuesta":"ya pe"}\n```'), {'respuesta': 'ya pe'})
        self.assertEqual(self.bot.parse_json('{} texto {"otra": 1}'), {})
        self.assertIsNone(self.bot.parse_json(None))
        self.assertIsNone(self.bot.parse_json('no es json'))

    def test_memory_does_not_cross_channels(self):
        self.bot.recordar(1, 'A juega fútbol')
        self.bot.recordar(2, 'B juega ajedrez')
        self.assertEqual(self.bot.recuerdos(1), ['A juega fútbol'])
        self.assertEqual(self.bot.recuerdos(2), ['B juega ajedrez'])

    def test_memory_limit_preserves_other_channels(self):
        with patch.object(self.bot, 'MAX_RECUERDOS', 2):
            self.bot.recordar(2, 'otro canal')
            for value in ('viejo', 'nuevo', 'más nuevo'):
                self.bot.recordar(1, value)
        self.assertEqual(self.bot.recuerdos(1), ['nuevo', 'más nuevo'])
        self.assertEqual(self.bot.recuerdos(2), ['otro canal'])

    def test_history_order_and_channel(self):
        for canal, texto in ((1, 'uno'), (2, 'secreto'), (1, 'dos'), (1, 'tres')):
            self.bot.guardar_mensaje(canal, 'usuario', texto)
        self.assertEqual(self.bot.historial(1, 2), 'usuario: dos\nusuario: tres')

    def test_traits_are_separate(self):
        self.bot.guardar_estado('rasgos:1', {'humor': 0.1})
        self.assertEqual(self.bot.leer_rasgos(1)['humor'], 0.1)
        self.assertEqual(self.bot.leer_rasgos(2)['humor'], 0.7)

    def test_sleep_boundaries_and_midnight(self):
        for start, end, hour, expected in ((1, 8, 1, True), (1, 8, 8, False),
                                           (23, 7, 0, True), (23, 7, 12, False),
                                           (0, 0, 0, False)):
            with self.subTest(start=start, end=end, hour=hour), \
                 patch.object(self.bot, 'SUENO_DESDE', start), \
                 patch.object(self.bot, 'SUENO_HASTA', end), \
                 patch.object(self.bot, 'ahora', return_value=SimpleNamespace(hour=hour)):
                self.assertEqual(self.bot.durmiendo(), expected)

    def test_name_matches_whole_word(self):
        msg = SimpleNamespace(mentions=[], channel=object(), content='¡pebot!')
        self.assertTrue(self.bot.me_hablan(msg))
        msg.content = 'pebotito'
        self.assertFalse(self.bot.me_hablan(msg))

    def message(self, **overrides):
        values = dict(mentions=[], content='jajaja sí', clean_content='jajaja sí',
                      channel=SimpleNamespace(id=1, fetch_message=AsyncMock()),
                      author=SimpleNamespace(id=20, display_name='Usuario'),
                      reference=None, guild=None)
        values.update(overrides)
        return SimpleNamespace(**values)

    def reference(self, **overrides):
        import discord
        values = dict(type=discord.MessageReferenceType.default, channel_id=1,
                      message_id=99, resolved=None, cached_message=None)
        values.update(overrides)
        return SimpleNamespace(**values)

    def test_mention_does_not_require_name_in_text(self):
        with patch.object(self.bot.bot._connection, 'user', SimpleNamespace(id=10)):
            message = self.message(mentions=[SimpleNamespace(id=10)])
            self.assertTrue(self.bot.me_hablan(message))

    def test_reply_to_bot_without_ping_is_direct(self):
        original = SimpleNamespace(author=SimpleNamespace(id=10))
        message = self.message(reference=self.reference(resolved=original))
        with patch.object(self.bot.bot._connection, 'user', SimpleNamespace(id=10)):
            resolved = asyncio.run(self.bot.mensaje_referenciado(message))
            self.assertTrue(self.bot.me_hablan(message, resolved))
        message.channel.fetch_message.assert_not_called()

    def test_reply_to_someone_else_is_not_direct(self):
        original = SimpleNamespace(author=SimpleNamespace(id=30))
        with patch.object(self.bot.bot._connection, 'user', SimpleNamespace(id=10)):
            self.assertFalse(self.bot.me_hablan(self.message(), original))

    def test_uncached_reply_fetches_one_discord_message(self):
        original = SimpleNamespace(author=SimpleNamespace(id=10))
        message = self.message(reference=self.reference())
        message.channel.fetch_message.return_value = original
        self.assertIs(asyncio.run(self.bot.mensaje_referenciado(message)), original)
        message.channel.fetch_message.assert_awaited_once_with(99)

    def test_deleted_reply_is_ignored(self):
        import discord
        message = self.message(reference=self.reference())
        message.channel.fetch_message.side_effect = discord.NotFound(
            SimpleNamespace(status=404, reason='Not Found'), 'deleted')
        self.assertIsNone(asyncio.run(self.bot.mensaje_referenciado(message)))

    def test_cross_channel_reply_and_forward_are_not_fetched(self):
        import discord
        for reference in (self.reference(channel_id=2),
                          self.reference(type=discord.MessageReferenceType.forward)):
            message = self.message(reference=reference)
            self.assertIsNone(asyncio.run(self.bot.mensaje_referenciado(message)))
            message.channel.fetch_message.assert_not_called()

    def test_followups_only_same_person_before_expiry(self):
        self.bot.charlas[1] = {'usuario': 20, 'hasta': 190, 'restantes': 2}
        with patch.object(self.bot.time, 'monotonic', return_value=100):
            self.assertTrue(self.bot.sigue_charla(self.message()))
            self.assertFalse(self.bot.sigue_charla(self.message(author=SimpleNamespace(id=30))))
            self.assertFalse(self.bot.sigue_charla(self.message(mentions=[SimpleNamespace(id=30)])))
            self.assertFalse(self.bot.sigue_charla(self.message(reference=self.reference())))
        with patch.object(self.bot.time, 'monotonic', return_value=190):
            self.assertFalse(self.bot.sigue_charla(self.message()))
        self.assertNotIn(1, self.bot.charlas)

    def test_followups_stop_after_two_messages(self):
        message = self.message()
        self.bot.charlas[1] = {'usuario': 20, 'hasta': float('inf'), 'restantes': 2}
        response = AsyncMock(return_value=True)
        with patch.object(self.bot, 'responder', response):
            for _ in range(3):
                asyncio.run(self.bot.procesar_mensaje(message))
        self.assertEqual(response.await_count, 2)

    def test_direct_reply_starts_conversation_even_during_sleep(self):
        original = SimpleNamespace(author=SimpleNamespace(id=10))
        message = self.message(reference=self.reference(resolved=original))
        response = AsyncMock(return_value=True)
        with patch.object(self.bot.bot._connection, 'user', SimpleNamespace(id=10)), \
             patch.object(self.bot, 'durmiendo', return_value=True), \
             patch.object(self.bot, 'responder', response):
            asyncio.run(self.bot.procesar_mensaje(message))
        response.assert_awaited_once_with(message, original)
        self.assertEqual(self.bot.charlas[1]['usuario'], 20)

    def test_failed_response_does_not_start_conversation(self):
        message = self.message(content='PeBOT hola')
        with patch.object(self.bot, 'responder', AsyncMock(return_value=False)):
            asyncio.run(self.bot.procesar_mensaje(message))
        self.assertEqual(self.bot.charlas, {})

    def test_unrelated_message_does_not_invoke_model(self):
        with patch.object(self.bot, 'responder', AsyncMock()) as response:
            asyncio.run(self.bot.procesar_mensaje(self.message()))
        response.assert_not_called()

    def test_reply_context_includes_original_message(self):
        message = self.message()
        message.channel.typing = lambda: AsyncMock()
        message.channel.send = AsyncMock()
        original = SimpleNamespace(author=SimpleNamespace(display_name='PeBOT'), clean_content='sale partida?')
        mocked = AsyncMock(return_value='{"respuesta":"ya pe"}')
        with patch.object(self.bot, 'pensar', mocked):
            asyncio.run(self.bot.responder(message, original))
        self.assertIn('sale partida?', mocked.call_args.args[2])

    def test_peruvian_personality(self):
        prompt = self.bot.personalidad(1)
        self.assertEqual(self.bot.NOMBRE, 'PeBOT')
        self.assertIn('Interpretas a PeBOT', prompt)
        self.assertIn('peruano de Lima', prompt)
        self.assertIn('causa', prompt)
        self.assertTrue(self.bot.bot.allowed_mentions.everyone is False)

    def test_response_and_memory_with_mocked_model(self):
        channel = SimpleNamespace(id=1, send=AsyncMock(), typing=lambda: AsyncMock())
        message = SimpleNamespace(channel=channel)
        response = '{"respuesta":"Ya pe, sale una partida", "recuerdo":"Le gustan los juegos"}'
        with patch.object(self.bot, 'pensar', AsyncMock(return_value=response)):
            asyncio.run(self.bot.responder(message))
        channel.send.assert_awaited_once_with('ya pe, sale una partida')
        self.assertEqual(self.bot.recuerdos(1), ['Le gustan los juegos'])
        self.assertIn('ya pe, sale una partida', self.bot.historial(1))

    def test_message_burst_is_one_model_request(self):
        message = self.message()
        message.channel.send = AsyncMock()
        message.channel.typing = lambda: AsyncMock()
        mocked = AsyncMock(return_value='{"mensajes":["Ya pe.","jalo al toque","q jugamos"],"recuerdo":null}')
        with patch.object(self.bot, 'pensar', mocked), patch.object(self.bot.asyncio, 'sleep', AsyncMock()) as sleep:
            self.assertTrue(asyncio.run(self.bot.responder(message)))
        self.assertEqual(mocked.await_count, 1)
        self.assertEqual([call.args[0] for call in message.channel.send.call_args_list],
                         ['ya pe', 'jalo al toque', 'q jugamos'])
        self.assertEqual(sleep.await_count, 2)
        self.assertIn('PeBOT: jalo al toque', self.bot.historial(1))

    def test_bursts_reject_invalid_entries_duplicates_and_limit_count(self):
        values = {'mensajes': [None, 42, {}, '', 'Ya pe.', 'ya pe', 'dos', 'tres', 'cuatro']}
        self.assertEqual(self.bot.extraer_mensajes(values), ['ya pe', 'dos', 'tres'])
        self.assertEqual(self.bot.extraer_mensajes({'mensaje': 'Oe mira.'}), ['oe mira'])
        self.assertEqual(self.bot.extraer_mensajes({'mensajes': []}), [])

    def test_empty_json_does_not_send_raw_json(self):
        message = self.message()
        message.channel.send = AsyncMock()
        message.channel.typing = lambda: AsyncMock()
        with patch.object(self.bot, 'pensar', AsyncMock(return_value='{}')):
            self.assertFalse(asyncio.run(self.bot.responder(message)))
        message.channel.send.assert_not_called()

    def test_partial_send_failure_keeps_only_sent_messages(self):
        import discord
        channel = SimpleNamespace(id=1, send=AsyncMock(side_effect=[None,
            discord.Forbidden(SimpleNamespace(status=403, reason='Forbidden'), 'denied')]),
            typing=lambda: AsyncMock())
        with patch.object(self.bot.asyncio, 'sleep', AsyncMock()), patch('builtins.print'):
            sent = asyncio.run(self.bot.enviar_tanda(channel, ['uno', 'dos', 'tres']))
        self.assertEqual(sent, 1)
        self.assertEqual(self.bot.historial(1), 'PeBOT: uno')

    def test_chat_style_preserves_names_urls_code_and_acronyms(self):
        for text in ('Alejandro juega', 'Google funciona', 'GPU nueva', '```Python\nprint(1)\n```',
                     'https://example.com/A.B', 'Get-ChildItem'):
            self.assertEqual(self.bot.estilo_chat(text), text)
        self.assertEqual(self.bot.estilo_chat('Asu que paliza.'), 'asu que paliza')
        self.assertEqual(self.bot.estilo_chat('no se...'), 'no se...')

    def prepare_initiative(self):
        self.bot.guardar_mensaje(1, 'usuario', 'un torneo de juegos malos seria divertido')
        self.bot.db.execute('UPDATE mensajes SET ts = ? WHERE canal = 1', (self.bot.time.time() - 120,))
        self.bot.db.commit()

    def test_initiative_requires_fresh_human_context_and_not_recent_bot(self):
        self.assertFalse(self.bot.puede_iniciar(1))
        self.prepare_initiative()
        self.assertTrue(self.bot.puede_iniciar(1))
        self.bot.guardar_mensaje(1, self.bot.NOMBRE, 'ya pe')
        self.assertFalse(self.bot.puede_iniciar(1))

    def test_initiative_does_not_repeat_consumed_or_old_context(self):
        self.prepare_initiative()
        last = self.bot.db.execute('SELECT MAX(id) FROM mensajes').fetchone()[0]
        self.bot.guardar_estado('contexto_espontaneo:1', last)
        self.assertFalse(self.bot.puede_iniciar(1))
        self.bot.guardar_estado('contexto_espontaneo:1', 0)
        self.bot.db.execute('UPDATE mensajes SET ts = ?', (self.bot.time.time() - 7 * 3600,))
        self.bot.db.commit()
        self.assertFalse(self.bot.puede_iniciar(1))

    def test_spontaneous_burst_counts_as_one_initiative(self):
        self.prepare_initiative()
        channel = SimpleNamespace(id=1, send=AsyncMock(), typing=lambda: AsyncMock())
        count = {'dia': '2026-10-24', 'n': 0}
        mocked = AsyncMock(return_value='{"hablar":true,"mensajes":["oe se me ocurrio algo","hagamos torneo"]}')
        with patch.object(self.bot, 'pensar', mocked), patch.object(self.bot.asyncio, 'sleep', AsyncMock()):
            asyncio.run(self.bot.enviar_espontaneo(channel, 1, count))
            asyncio.run(self.bot.enviar_espontaneo(channel, 1, count))
        self.assertEqual(mocked.await_count, 1)
        self.assertEqual(channel.send.await_count, 2)
        self.assertEqual(count['n'], 1)

    def test_spontaneous_can_decide_to_stay_quiet(self):
        self.prepare_initiative()
        channel = SimpleNamespace(id=1, send=AsyncMock())
        count = {'dia': '2026-10-24', 'n': 0}
        with patch.object(self.bot, 'pensar', AsyncMock(return_value='{"hablar":false,"mensajes":[]}')):
            asyncio.run(self.bot.enviar_espontaneo(channel, 1, count))
        channel.send.assert_not_called()
        self.assertEqual(count['n'], 0)

    def test_daily_initiative_limit_avoids_model_calls(self):
        self.bot.guardar_estado('espontaneos', {'dia': self.bot.ahora().date().isoformat(), 'n': 6})
        with patch.object(self.bot, 'durmiendo', return_value=False), \
             patch.object(self.bot, 'pensar', AsyncMock()) as mocked:
            asyncio.run(self.bot.espontaneo.coro())
        mocked.assert_not_called()

    def test_missing_keys_stops_before_connection(self):
        env = dict(os.environ, DISCORD_TOKEN='', OPENAI_API_KEY='')
        result = subprocess.run([sys.executable, str(Path(__file__).with_name('bot.py'))],
                                capture_output=True, text=True, env=env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Falta DISCORD_TOKEN', result.stderr)

    def test_missing_provider_key_stops_before_connection(self):
        env = dict(os.environ, DISCORD_TOKEN='test-token', OPENAI_API_KEY='')
        result = subprocess.run([sys.executable, str(Path(__file__).with_name('bot.py'))],
                                capture_output=True, text=True, env=env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Configura al menos una clave', result.stderr)

    def test_openai_request_uses_personality_and_channel_context(self):
        self.bot.guardar_mensaje(1, 'usuario', 'habla causa')
        self.bot.guardar_mensaje(2, 'usuario', 'otro canal')
        response = SimpleNamespace(status='completed', output_text='  {"respuesta":"Habla, causa"}  ')
        request = AsyncMock(return_value=response)
        with patch.object(self.bot.enrutador.providers[0].client.responses, 'create', request):
            text = asyncio.run(self.bot.pensar('Responde en JSON', 1))
        self.assertEqual(text, '{"respuesta":"Habla, causa"}')
        params = request.call_args.kwargs
        self.assertEqual(params['model'], 'gpt-4.1-mini')
        self.assertIn('PeBOT', params['instructions'])
        self.assertIn('habla causa', params['input'][0]['content'])
        self.assertNotIn('otro canal', params['input'][0]['content'])
        self.assertEqual(params['text'], {'format': {'type': 'json_object'}})
        self.assertIs(params['store'], False)

    def test_openai_error_is_handled(self):
        from openai import APIConnectionError
        import httpx
        error = APIConnectionError(request=httpx.Request('POST', 'https://api.openai.com/v1/responses'))
        request = AsyncMock(side_effect=error)
        with patch.object(self.bot.enrutador.providers[0].client.responses, 'create', request), patch('builtins.print'):
            self.assertIsNone(asyncio.run(self.bot.pensar('Responde en JSON', 1)))

    def test_openai_incomplete_response_is_not_published(self):
        response = SimpleNamespace(status='incomplete', output_text='{"respuesta":')
        with patch.object(self.bot.enrutador.providers[0].client.responses, 'create', AsyncMock(return_value=response)), \
             patch('builtins.print'):
            self.assertIsNone(asyncio.run(self.bot.pensar('Responde en JSON', 1)))


if __name__ == '__main__':
    unittest.main()
