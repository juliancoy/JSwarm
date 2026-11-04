"""Authenticated portal proxy to the fixed loopback Whisper service. No audio retention."""
import http.client
import json
import threading
import uuid

MAX_AUDIO=8*1024*1024
TYPES={'audio/webm':'webm','audio/ogg':'ogg','audio/mp4':'mp4','audio/wav':'wav','audio/x-wav':'wav'}
SLOT=threading.BoundedSemaphore(1)

class VoiceError(Exception):
    def __init__(self,message,status=503):super().__init__(message);self.status=status

def transcribe(audio,content_type):
    kind=content_type.split(';',1)[0].lower()
    if kind not in TYPES or not 32<=len(audio)<=MAX_AUDIO:
        raise VoiceError('Record up to 3 minutes of audio (8 MB maximum).',400)
    valid=(kind=='audio/webm' and audio.startswith(b'\x1aE\xdf\xa3') or
           kind=='audio/ogg' and audio.startswith(b'OggS') or
           kind=='audio/mp4' and audio[4:8]==b'ftyp' or
           kind in ('audio/wav','audio/x-wav') and audio.startswith(b'RIFF') and audio[8:12]==b'WAVE')
    if not valid:raise VoiceError('Unsupported or damaged audio recording.',400)
    if not SLOT.acquire(blocking=False):raise VoiceError('Whisper is transcribing another recording. Retry shortly.',429)
    connection=http.client.HTTPConnection('127.0.0.1',8094,timeout=120)
    try:
        boundary='swarm-'+uuid.uuid4().hex
        body=(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="recording.{TYPES[kind]}"\r\nContent-Type: {kind}\r\n\r\n'.encode()+audio+
              f'\r\n--{boundary}\r\nContent-Disposition: form-data; name="response_format"\r\n\r\njson\r\n--{boundary}--\r\n'.encode())
        connection.request('POST','/inference',body,{'Content-Type':'multipart/form-data; boundary='+boundary})
        response=connection.getresponse();raw=response.read(256*1024+1)
        if response.status!=200 or len(raw)>256*1024:raise VoiceError('Whisper could not transcribe this recording. Your draft is unchanged.')
        result=json.loads(raw);text=result.get('text')
        if not isinstance(text,str) or len(text)>16000:raise VoiceError('Transcription exceeded the message limit. Try a shorter recording.')
        return {'text':text.strip()}
    except (OSError,ValueError,http.client.HTTPException):raise VoiceError('Whisper is unavailable. Keep your draft and retry the recording.') from None
    finally:connection.close();SLOT.release()
