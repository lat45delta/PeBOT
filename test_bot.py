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
            'CHANNEL_IDS': '', 'SLEEP_FROM': '1', 'SLEEP_TO': '8',
        })
        cls.env.start()
        spec = importlib.util.spec_from_file_location('pebot_tested', Path(__file__).with_name('bot.py'))
        cls.bot = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.bot)

    @classmethod
    def tearDownClass(cls):
        cls.bot.db.close()
        asyncio.run(cls.bot.cliente.close())
        cls.env.stop()
        cls.temp.cleanup()

    def setUp(self):
        for table in ('mensajes', 'recuerdos', 'estado'):
            self.bot.db.execute(f'DELETE FROM {table}')
        self.bot.db.commit()

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
        channel.send.assert_awaited_once_with('Ya pe, sale una partida')
        self.assertEqual(self.bot.recuerdos(1), ['Le gustan los juegos'])
        self.assertIn('Ya pe, sale una partida', self.bot.historial(1))

    def test_missing_keys_stops_before_connection(self):
        env = dict(os.environ, DISCORD_TOKEN='', OPENAI_API_KEY='')
        result = subprocess.run([sys.executable, str(Path(__file__).with_name('bot.py'))],
                                capture_output=True, text=True, env=env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Falta DISCORD_TOKEN', result.stderr)

    def test_missing_openai_key_stops_before_connection(self):
        env = dict(os.environ, DISCORD_TOKEN='test-token', OPENAI_API_KEY='')
        result = subprocess.run([sys.executable, str(Path(__file__).with_name('bot.py'))],
                                capture_output=True, text=True, env=env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Falta OPENAI_API_KEY', result.stderr)

    def test_openai_request_uses_personality_and_channel_context(self):
        self.bot.guardar_mensaje(1, 'usuario', 'habla causa')
        self.bot.guardar_mensaje(2, 'usuario', 'otro canal')
        response = SimpleNamespace(status='completed', output_text='  {"respuesta":"Habla, causa"}  ')
        request = AsyncMock(return_value=response)
        with patch.object(self.bot.cliente.responses, 'create', request):
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
        with patch.object(self.bot.cliente.responses, 'create', request), patch('builtins.print'):
            self.assertIsNone(asyncio.run(self.bot.pensar('Responde en JSON', 1)))

    def test_openai_incomplete_response_is_not_published(self):
        response = SimpleNamespace(status='incomplete', output_text='{"respuesta":')
        with patch.object(self.bot.cliente.responses, 'create', AsyncMock(return_value=response)), \
             patch('builtins.print'):
            self.assertIsNone(asyncio.run(self.bot.pensar('Responde en JSON', 1)))


if __name__ == '__main__':
    unittest.main()
