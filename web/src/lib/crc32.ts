/** CRC-32（zlib.crc32 と同じ多項式 0xEDB88320）。フレームの検証と ZIP の作成に使う。 */

const TABLE = (() => {
  const t = new Uint32Array(256);
  for (let n = 0; n < 256; n++) {
    let c = n;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    t[n] = c >>> 0;
  }
  return t;
})();

export function crc32(data: Uint8Array, start = 0, end = data.length, seed = 0): number {
  let c = (seed ^ 0xffffffff) >>> 0;
  for (let i = start; i < end; i++) c = TABLE[(c ^ data[i]) & 0xff] ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
}
