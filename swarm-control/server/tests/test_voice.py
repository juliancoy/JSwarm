import unittest
from unittest.mock import MagicMock,patch
import voice

AUDIO=b'RIFF'+b'\0'*4+b'WAVE'+b'\0'*40
class VoiceTests(unittest.TestCase):
    def test_fixed_endpoint_and_filename_only_transcript_returned(self):
        connection=MagicMock();response=connection.getresponse.return_value;response.status=200;response.read.return_value=b'{"text":" test words ","segments":["private"]}'
        with patch('voice.http.client.HTTPConnection',return_value=connection) as factory:
            self.assertEqual(voice.transcribe(AUDIO,'audio/wav'),{'text':'test words'})
        factory.assert_called_once_with('127.0.0.1',8094,timeout=120)
        self.assertEqual(connection.request.call_args.args[:2],('POST','/inference'))
        self.assertIn(b'filename="recording.wav"',connection.request.call_args.args[2])
    def test_invalid_and_oversized_audio_never_reaches_service(self):
        with patch('voice.http.client.HTTPConnection') as connection:
            for audio,kind in [(b'x'*64,'audio/wav'),(AUDIO,'text/plain'),(b'x'*(voice.MAX_AUDIO+1),'audio/webm')]:
                with self.assertRaises(voice.VoiceError):voice.transcribe(audio,kind)
            connection.assert_not_called()
    def test_busy_refuses_and_failure_releases_slot(self):
        voice.SLOT.acquire()
        try:
            with self.assertRaises(voice.VoiceError) as error:voice.transcribe(AUDIO,'audio/wav')
            self.assertEqual(error.exception.status,429)
        finally:voice.SLOT.release()
        connection=MagicMock();connection.request.side_effect=OSError('private details')
        with patch('voice.http.client.HTTPConnection',return_value=connection),self.assertRaises(voice.VoiceError) as error:voice.transcribe(AUDIO,'audio/wav')
        self.assertNotIn('private details',str(error.exception));self.assertTrue(voice.SLOT.acquire(False));voice.SLOT.release()
