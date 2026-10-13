export const RECORDING_SAMPLE_RATE = 24000;
export const MAX_RECORDING_BYTES = 2 * 1024 * 1024;
export const MAX_RECORDING_SECONDS = 30;

export interface PreparedRecording {
  name: string;
  byteLength: number;
  sampleRate: 24000;
  channels: 1;
  bitsPerSample: 16;
  sampleCount: number;
  durationSeconds: number;
  samples: Float32Array;
}

function invalid(message: string): never {
  throw new Error(message);
}

export function parseRecording(
  buffer: ArrayBuffer,
  name: string,
): PreparedRecording {
  const bytes = new Uint8Array(buffer);
  if (bytes.byteLength > MAX_RECORDING_BYTES)
    invalid("The WAV file must be no larger than 2 MiB.");
  if (bytes.byteLength < 12) invalid("Choose a complete RIFF WAV recording.");

  const view = new DataView(buffer);
  const fourCC = (offset: number) =>
    String.fromCharCode(
      bytes[offset],
      bytes[offset + 1],
      bytes[offset + 2],
      bytes[offset + 3],
    );
  if (fourCC(0) !== "RIFF" || fourCC(8) !== "WAVE")
    invalid("Choose a complete RIFF WAV recording.");
  if (view.getUint32(4, true) + 8 !== bytes.byteLength)
    invalid("The WAV RIFF size does not match the file length.");

  let formatSeen = false;
  let dataOffset = -1;
  let dataLength = 0;
  for (let offset = 12; offset < bytes.byteLength;) {
    if (bytes.byteLength - offset < 8)
      invalid("The WAV contains an incomplete chunk header.");
    const kind = fourCC(offset);
    const length = view.getUint32(offset + 4, true);
    const payload = offset + 8;
    const end = payload + length;
    const next = end + (length % 2);
    if (end > bytes.byteLength || next > bytes.byteLength)
      invalid("The WAV contains a truncated chunk or missing padding.");
    if (kind === "fmt ") {
      if (formatSeen) invalid("The WAV contains more than one format chunk.");
      formatSeen = true;
      if (
        (length !== 16 && length !== 18) ||
        (length === 18 && view.getUint16(payload + 16, true) !== 0)
      )
        invalid("The WAV must use the standard PCM16 format.");
      if (
        view.getUint16(payload, true) !== 1 ||
        view.getUint16(payload + 2, true) !== 1 ||
        view.getUint32(payload + 4, true) !== RECORDING_SAMPLE_RATE ||
        view.getUint32(payload + 8, true) !== RECORDING_SAMPLE_RATE * 2 ||
        view.getUint16(payload + 12, true) !== 2 ||
        view.getUint16(payload + 14, true) !== 16
      )
        invalid("The WAV must be 24 kHz, mono, PCM16 audio.");
    } else if (kind === "data") {
      if (dataOffset !== -1)
        invalid("The WAV contains more than one audio data chunk.");
      if (!length || length % 2)
        invalid(
          "The WAV audio data must contain complete, nonempty PCM16 samples.",
        );
      dataOffset = payload;
      dataLength = length;
    }
    offset = next;
  }
  if (!formatSeen || dataOffset === -1)
    invalid("The WAV is missing its format or audio data chunk.");
  const sampleCount = dataLength / 2;
  if (sampleCount > RECORDING_SAMPLE_RATE * MAX_RECORDING_SECONDS)
    invalid("The WAV recording must be no longer than 30 seconds.");
  const samples = new Float32Array(sampleCount);
  for (let i = 0; i < sampleCount; i++)
    samples[i] = view.getInt16(dataOffset + i * 2, true) / 32768;
  return {
    name,
    byteLength: bytes.byteLength,
    sampleRate: RECORDING_SAMPLE_RATE,
    channels: 1,
    bitsPerSample: 16,
    sampleCount,
    durationSeconds: sampleCount / RECORDING_SAMPLE_RATE,
    samples,
  };
}

export async function prepareRecording(file: File): Promise<PreparedRecording> {
  if (file.size > MAX_RECORDING_BYTES)
    invalid("The WAV file must be no larger than 2 MiB.");
  return parseRecording(await file.arrayBuffer(), file.name);
}
