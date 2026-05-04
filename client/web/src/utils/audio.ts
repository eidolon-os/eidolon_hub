export interface WsEnvelope {
  type: string;
  version: string;
  transport: string;
  payload: Record<string, unknown>;
}

export function buildEnvelope(
  type: string,
  payload: Record<string, unknown> = {}
): string {
  return JSON.stringify({
    type,
    version: "v1",
    transport: "websocket",
    payload,
  });
}

export function sleep(ms: number) {
  return new Promise<void>((resolve) => setTimeout(resolve, ms));
}

export async function encodeAudioToOpus(
  audioData: Uint8Array,
  mimeType: string
): Promise<Int8Array[]> {
  const audioCtx = new AudioContext({ sampleRate: 16000 });

  let audioBuffer: AudioBuffer;

  if (
    mimeType === "audio/wav" ||
    mimeType === "audio/wave" ||
    mimeType === "audio/x-wav"
  ) {
    audioBuffer = await audioCtx.decodeAudioData(audioData.buffer.slice(0) as ArrayBuffer);
  } else {
    audioBuffer = await audioCtx.decodeAudioData(audioData.buffer.slice(0) as ArrayBuffer);
  }

  await audioCtx.close();

  const ch = audioBuffer.numberOfChannels;
  const length = audioBuffer.length;

  const pcmData = new Int16Array(length * ch);
  for (let c = 0; c < ch; c++) {
    const channelData = audioBuffer.getChannelData(c);
    for (let i = 0; i < length; i++) {
      pcmData[i * ch + c] = Math.max(
        -32768,
        Math.min(32767, Math.round(channelData[i] * 32767))
      );
    }
  }

  const mono = ch === 1 ? pcmData : downmixToMono(pcmData, ch);

  const frames: Int8Array[] = [];
  const frameSize = 960;
  const pageSize = 4000;

  for (let offset = 0; offset < mono.length; offset += frameSize) {
    const frame = mono.subarray(offset, offset + frameSize);
    const pcmBytes = int16ToBytes(frame);
    const page = buildOpusPage(pcmBytes, pageSize);
    frames.push(page);
  }

  return frames;
}

function downmixToMono(pcm: Int16Array, channels: number): Int16Array {
  const result = new Int16Array(pcm.length / channels);
  for (let i = 0; i < result.length; i++) {
    let sum = 0;
    for (let c = 0; c < channels; c++) {
      sum += pcm[i * channels + c];
    }
    result[i] = Math.round(sum / channels);
  }
  return result;
}

function int16ToBytes(pcm: Int16Array): Uint8Array {
  const buf = new ArrayBuffer(pcm.length * 2);
  const view = new DataView(buf);
  for (let i = 0; i < pcm.length; i++) {
    view.setInt16(i * 2, pcm[i], true);
  }
  return new Uint8Array(buf);
}

export function buildOpusPage(pcm: Uint8Array, pageSize: number): Int8Array {
  const headerSize = 19;
  const output = new Uint8Array(headerSize + pageSize + pcm.length);

  output[0] = 0x4f; // 'O'
  output[1] = 0x70; // 'p'
  output[2] = 0x75; // 'u'
  output[3] = 0x73; // 's'
  output[4] = 0x54; // 'T'
  output[5] = 0x61; // 'a'
  output[6] = 0x67; // 'g'
  output[7] = 0x65; // 'e'
  output[8] = 0x01; // version
  output[9] = 0x00; // channel count placeholder
  output[10] = 0x00; // granule position LSB
  output[11] = 0x00;
  output[12] = 0x00;
  output[13] = 0x00;
  output[14] = 0x00; // granule position MSB
  output[15] = 0x01; // serialno LSB
  output[16] = 0x00;
  output[17] = 0x00;
  output[18] = 0x00; // serialno MSB

  output.set(pcm, headerSize);

  return new Int8Array(output.buffer, output.byteOffset, output.length);
}

export async function encodeBlobToOpusFrames(
  blob: Blob
): Promise<Int8Array[]> {
  const arrayBuffer = await blob.arrayBuffer();
  const audioData = new Uint8Array(arrayBuffer);
  return encodeAudioToOpus(audioData, blob.type);
}
