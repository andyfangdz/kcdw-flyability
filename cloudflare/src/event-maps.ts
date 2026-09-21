// Immutable, content-addressed PNGs. Reads use the Worker's existing access gate.
const MAX_BYTES = 4_000_000;
const signature = [137, 80, 78, 71, 13, 10, 26, 10];
const key = (slug: string, hash: string) => `events/${slug}/maps/${hash}.png`;

export async function uploadMap(request: Request, bucket: R2Bucket, slug: string, hash: string): Promise<Response> {
  try {
    if (request.headers.get('Content-Type') !== 'image/png' || !request.body) throw new Error('PNG required');
    const reader = request.body.getReader();
    const chunks: Uint8Array[] = [];
    let length = 0;
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      length += value.byteLength;
      if (length > MAX_BYTES) { await reader.cancel(); throw new Error('Map exceeds size limit'); }
      chunks.push(value);
    }
    const bytes = new Uint8Array(length);
    let offset = 0;
    for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.length; }
    if (length < 45 || !signature.every((b, i) => bytes[i] === b) || new TextDecoder().decode(bytes.slice(12, 16)) !== 'IHDR') throw new Error('Invalid PNG');
    const header = new DataView(bytes.buffer);
    const width = header.getUint32(16), height = header.getUint32(20);
    if (!width || !height || width > 6000 || height > 7000 || width * height > 24_000_000) throw new Error('Invalid dimensions');
    const digest = [...new Uint8Array(await crypto.subtle.digest('SHA-256', bytes))].map(b => b.toString(16).padStart(2, '0')).join('');
    if (digest !== hash) throw new Error('Checksum mismatch');
    await bucket.put(key(slug, hash), bytes, { onlyIf: { etagDoesNotMatch: '*' }, sha256: digest,
      httpMetadata: { contentType: 'image/png' } });
    return Response.json({ stored: true, sha256: hash, width, height });
  } catch (error) {
    if (error instanceof Error && ['PNG required', 'Map exceeds size limit', 'Invalid PNG', 'Invalid dimensions', 'Checksum mismatch'].includes(error.message)) {
      return Response.json({ error: error.message }, { status: 400 });
    }
    throw error;
  }
}

export async function readMap(request: Request, bucket: R2Bucket, slug: string, hash: string, publicRead: boolean): Promise<Response> {
  const object = request.method === 'HEAD' ? await bucket.head(key(slug, hash)) : await bucket.get(key(slug, hash));
  if (!object) return new Response('Map not found', { status: 404, headers: { 'Cache-Control': 'no-store' } });
  const headers = { 'Content-Type': 'image/png', 'Content-Length': String(object.size), 'ETag': `"${hash}"`,
    'Cache-Control': publicRead ? 'public, max-age=31536000, immutable' : 'private, no-store',
    'X-Content-Type-Options': 'nosniff' };
  if (request.headers.get('If-None-Match') === headers.ETag) return new Response(null, { status: 304, headers });
  return new Response(request.method === 'HEAD' ? null : (object as R2ObjectBody).body, { headers });
}
