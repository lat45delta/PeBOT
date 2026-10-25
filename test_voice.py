import asyncio
from array import array
import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import discord
from google.genai import types

from voice_audio import DISCORD_FRAME_BYTES, InputMixer, StreamingPCM
from voice_chat import DiscordInput, VoiceCog, VoiceError, VoiceSession, explain_error, voice_config


class AudioTests(unittest.TestCase):
    def test_input_mixes_and_resamples_to_16khz(self):
        mixer = InputMixer()
        pcm = array('h', [1000, 1000] * 960).tobytes()
        for _ in range(5):
            mixer.feed(1, pcm)
            mixer.feed(2, pcm)
        result = mixer.take()
        self.assertEqual(len(result), 3200)  # 100 ms de PCM mono a 16 kHz.
        self.assertTrue(all(value == 1000 for value in array('h', result)))
        self.assertIsNone(mixer.take())

    def test_input_queues_are_bounded(self):
        mixer = InputMixer()
        for _ in range(100):
            mixer.feed(1, bytes(DISCORD_FRAME_BYTES))
        self.assertEqual(len(mixer.users[1]), 10)
        self.assertFalse(mixer.feed(2, b'wrong'))
        mixer.clear()
        self.assertEqual(mixer.users, {})

    def test_output_resamples_and_pads_complete_discord_frames(self):
        source = StreamingPCM(volume=1)
        source.feed(array('h', [1000] * 2400).tobytes())  # 100 ms, 24 kHz mono.
        source.finish()
        frames = []
        while frame := source.read():
            self.assertEqual(len(frame), DISCORD_FRAME_BYTES)
            frames.append(frame)
        self.assertEqual(len(frames), 5)
        self.assertEqual(array('h', frames[0])[0], 1000)
        self.assertFalse(source.is_opus())

    def test_abort_discards_pending_audio(self):
        source = StreamingPCM()
        source.feed(bytes(4800))
        source.abort()
        self.assertEqual(source.read(), b'')
        self.assertTrue(source.frames.empty())

    def test_incomplete_sample_is_rejected(self):
        with self.assertRaises(ValueError):
            StreamingPCM().feed(b'x')

    def test_sink_ignores_bots_and_dispatches_thread_safely(self):
        loop, callback = MagicMock(), MagicMock()
        sink = DiscordInput(loop, callback)
        sink.write(None, SimpleNamespace(pcm=b'pcm'))
        sink.write(SimpleNamespace(id=1, bot=True), SimpleNamespace(pcm=b'pcm'))
        loop.call_soon_threadsafe.assert_not_called()
        sink.write(SimpleNamespace(id=2, bot=False), SimpleNamespace(pcm=b'pcm'))
        loop.call_soon_threadsafe.assert_called_once_with(callback, 2, b'pcm')
        sink.cleanup()
        sink.write(SimpleNamespace(id=2, bot=False), SimpleNamespace(pcm=b'pcm'))
        self.assertEqual(loop.call_soon_threadsafe.call_count, 1)

    def test_live_config_is_audio_and_vad_enabled(self):
        config = voice_config('eres PeBOT', 'Puck')
        self.assertEqual(config.response_modalities, [types.Modality.AUDIO])
        self.assertEqual(config.system_instruction, 'eres PeBOT')
        self.assertFalse(config.realtime_input_config.automatic_activity_detection.disabled)
        self.assertEqual(config.speech_config.voice_config.prebuilt_voice_config.voice_name, 'Puck')


class VoiceTests(unittest.IsolatedAsyncioTestCase):
    def session(self, env=None):
        channel = SimpleNamespace(id=1, guild=SimpleNamespace(id=2))
        return VoiceSession(channel, SimpleNamespace(send=AsyncMock()), 'fake-key', 'fake-live',
                            voice_config('PeBOT', 'Puck'), self.saved.append,
                            env={} if env is None else env)

    async def asyncSetUp(self):
        self.saved = []

    async def test_no_audio_or_transcripts_are_saved_by_default(self):
        session = self.session()
        session.in_text, session.out_text = 'hola', 'habla causa'
        session.finish_turn()
        self.assertEqual(self.saved, [])
        self.assertEqual(session.in_text, '')

    async def test_optional_transcripts_use_voice_channel_context(self):
        session = self.session({'VOICE_SAVE_TRANSCRIPTS': 'true'})
        saved = MagicMock()
        session.save_message = saved
        session.in_text, session.out_text = 'hola', 'habla causa'
        session.finish_turn()
        self.assertEqual(saved.call_args_list[0].args, (1, 'voz (grupo)', 'hola'))
        self.assertEqual(saved.call_args_list[1].args, (1, 'PeBOT', 'habla causa'))

    async def test_interruption_stops_playback_not_listening(self):
        session = self.session()
        session.vc = MagicMock()
        source = session.source = StreamingPCM()
        session.interrupt()
        session.vc.stop_playing.assert_called_once()
        session.vc.stop_listening.assert_not_called()
        self.assertTrue(source.aborted.is_set())

    async def test_callar_suppresses_rest_of_current_turn_only(self):
        session = self.session()
        session.vc = MagicMock()
        session.receiving_turn = True
        session.interrupt(manual=True)
        session.output_audio(bytes(960))
        session.vc.play.assert_not_called()
        session.finish_turn()
        self.assertFalse(session.discard_output)

    async def test_receive_multiple_turns_and_interrupt(self):
        session = self.session()
        session.vc = MagicMock()
        session.vc.is_playing.return_value = False
        class Live:
            count = 0
            async def receive(self):
                self.count += 1
                if self.count == 1:
                    yield types.LiveServerMessage(server_content=types.LiveServerContent(
                        model_turn=types.Content(parts=[types.Part(inline_data=types.Blob(
                            data=bytes(4800), mime_type='audio/pcm;rate=24000'))]), turn_complete=True))
                else:
                    yield types.LiveServerMessage(server_content=types.LiveServerContent(interrupted=True))
                    session.closed = True
        live = Live()
        await session.receive_audio(live)
        self.assertEqual(live.count, 2)
        session.vc.play.assert_called_once()
        session.vc.stop_playing.assert_called()

    async def test_idle_sender_never_sends_audio_without_speakers(self):
        session = self.session()
        session.vc = MagicMock()
        session.vc.is_connected.side_effect = [True, False]
        live = SimpleNamespace(send_realtime_input=AsyncMock())
        with patch('voice_chat.asyncio.sleep', AsyncMock()):
            await session.send_audio(live)
        live.send_realtime_input.assert_not_called()

    async def test_sender_streams_pcm_then_closes_audio_stream(self):
        session = self.session()
        session.input_audio(4, bytes(DISCORD_FRAME_BYTES * 5))
        session.last_input = 0
        session.vc = MagicMock()
        session.vc.is_connected.side_effect = [True, True, False]
        live = SimpleNamespace(send_realtime_input=AsyncMock())
        with patch('voice_chat.asyncio.sleep', AsyncMock()):
            await session.send_audio(live)
        blob = live.send_realtime_input.call_args_list[0].kwargs['audio']
        self.assertEqual(blob.mime_type, 'audio/pcm;rate=16000')
        live.send_realtime_input.assert_awaited_with(audio_stream_end=True)

    async def test_empty_channel_auto_disconnects(self):
        session = self.session({'VOICE_EMPTY_TIMEOUT_SECONDS': '5'})
        session.vc = MagicMock()
        session.vc.is_connected.return_value = True
        session.vc.channel.members = [SimpleNamespace(bot=True)]
        with patch('voice_chat.time.monotonic', side_effect=[0, 0, 0, 6, 6]), \
             patch('voice_chat.asyncio.sleep', AsyncMock()):
            await session.watch()
        self.assertIn('vacío', session.reason)

    async def test_live_failure_never_connects_discord_and_closes_sdk(self):
        session = self.session()
        session.channel.connect = AsyncMock()
        client = MagicMock()
        client.aio.aclose = AsyncMock()
        connection = MagicMock()
        connection.__aenter__ = AsyncMock(side_effect=RuntimeError('test'))
        client.aio.live.connect.return_value = connection
        with patch('voice_chat.genai.Client', return_value=client), \
             patch('voice_chat.discord.opus.is_loaded', return_value=True), patch('builtins.print'):
            with self.assertRaises(VoiceError):
                await session.start()
        session.channel.connect.assert_not_called()
        client.aio.aclose.assert_awaited_once()
        self.assertTrue(session.closed)

    async def test_stop_closes_active_task(self):
        session = self.session()
        session.task = asyncio.create_task(asyncio.sleep(30))
        await session.stop()
        self.assertTrue(session.task.cancelled())

    async def test_only_participants_or_managers_control_session(self):
        cog = VoiceCog(MagicMock(), MagicMock(), MagicMock(), MagicMock())
        session = self.session()
        cog.sessions[2] = session
        ctx = SimpleNamespace(guild=SimpleNamespace(id=2), author=SimpleNamespace(
            voice=SimpleNamespace(channel=SimpleNamespace(id=3)),
            guild_permissions=SimpleNamespace(manage_guild=False)))
        with self.assertRaises(VoiceError):
            cog.controlled_session(ctx)
        ctx.author.voice.channel.id = 1
        self.assertIs(cog.controlled_session(ctx), session)

    async def test_invalid_settings_fail_before_connecting(self):
        with self.assertRaises(VoiceError):
            self.session({'VOICE_MAX_SESSION_MINUTES': 'nan'})

    async def test_commands_register_without_connecting(self):
        from discord.ext import commands
        bot = commands.Bot(command_prefix='!', intents=discord.Intents.default())
        try:
            await bot.add_cog(VoiceCog(bot, MagicMock(), MagicMock(), MagicMock()))
            for name in ('entrar', 'salir', 'callar', 'join', 'leave'):
                self.assertIsNotNone(bot.get_command(name))
        finally:
            await bot.close()

    def context(self):
        channel = MagicMock(spec=discord.VoiceChannel)
        channel.id = 1
        channel.permissions_for.return_value = SimpleNamespace(view_channel=True, connect=True, speak=True)
        guild = SimpleNamespace(id=2, me=object(), voice_client=None)
        return SimpleNamespace(guild=guild, author=SimpleNamespace(voice=SimpleNamespace(channel=channel)),
                               channel=SimpleNamespace(id=3), send=AsyncMock())

    async def test_join_announces_audio_use_and_creates_one_session(self):
        cog = VoiceCog(MagicMock(), MagicMock(return_value='PeBOT peruano'),
                       MagicMock(return_value='historial'), MagicMock())
        ctx = self.context()
        session = MagicMock()
        session.start, session.stop = AsyncMock(), AsyncMock()
        with patch.dict(os.environ, {'GOOGLE_VOICE_API_KEY': 'fake-key', 'VOICE_CHANNEL_IDS': ''}), \
             patch('voice_chat.VoiceSession', return_value=session):
            await cog.enter_voice.callback(cog, ctx)
            await cog.enter_voice.callback(cog, ctx)
        session.start.assert_awaited_once()
        self.assertIn('Google', ctx.send.call_args_list[0].args[0])
        self.assertIs(cog.sessions[2], session)

    async def test_join_requires_google_key_and_voice_permissions(self):
        cog = VoiceCog(MagicMock(), MagicMock(), MagicMock(), MagicMock())
        ctx = self.context()
        with patch.dict(os.environ, {'GOOGLE_VOICE_API_KEY': '', 'GOOGLE_API_KEY': '', 'GEMINI_API_KEY': '',
                                    'VOICE_CHANNEL_IDS': ''}):
            await cog.enter_voice.callback(cog, ctx)
        self.assertIn('Falta una clave', ctx.send.call_args.args[0])
        ctx.author.voice.channel.permissions_for.return_value.connect = False
        with patch.dict(os.environ, {'GOOGLE_VOICE_API_KEY': 'fake', 'VOICE_CHANNEL_IDS': ''}):
            await cog.enter_voice.callback(cog, ctx)
        self.assertIn('Conectar', ctx.send.call_args.args[0])

    async def test_failed_join_removes_session(self):
        cog = VoiceCog(MagicMock(), MagicMock(return_value='PeBOT'), MagicMock(return_value=''), MagicMock())
        ctx = self.context()
        session = MagicMock()
        session.start = AsyncMock(side_effect=VoiceError('sin cuota'))
        session.stop = AsyncMock()
        with patch.dict(os.environ, {'GOOGLE_VOICE_API_KEY': 'fake', 'VOICE_CHANNEL_IDS': ''}), \
             patch('voice_chat.VoiceSession', return_value=session):
            await cog.enter_voice.callback(cog, ctx)
        self.assertEqual(cog.sessions, {})
        session.stop.assert_awaited_once()
        self.assertEqual(ctx.send.call_args.args[0], 'sin cuota')


if __name__ == '__main__':
    unittest.main()
