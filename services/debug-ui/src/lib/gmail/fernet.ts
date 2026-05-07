import { createHmac, createDecipheriv, timingSafeEqual } from "crypto";

/**
 * Decrypt a Fernet-encrypted token using Node's built-in crypto.
 *
 * Fernet spec: https://github.com/fernet/spec/blob/master/Spec.md
 * Layout: Version (1B) | Timestamp (8B) | IV (16B) | Ciphertext (var) | HMAC (32B)
 * Key: signing_key (16B) | encryption_key (16B) — decoded from base64url.
 */
export function fernetDecrypt(
  ciphertext: Buffer,
  fernetKeyBase64: string
): string {
  const keyBuf = Buffer.from(fernetKeyBase64, "base64url");
  if (keyBuf.length !== 32) {
    throw new Error(
      `Fernet key must be 32 bytes, got ${keyBuf.length}`
    );
  }

  const signingKey = keyBuf.subarray(0, 16);
  const encryptionKey = keyBuf.subarray(16, 32);

  // ciphertext may be base64-encoded bytes from Postgres BYTEA
  let token: Buffer;
  if (ciphertext[0] === 0x80) {
    token = ciphertext;
  } else {
    token = Buffer.from(ciphertext.toString("utf-8"), "base64");
  }

  if (token.length < 57) {
    throw new Error("Fernet token too short");
  }

  const version = token[0];
  if (version !== 0x80) {
    throw new Error(`Unsupported Fernet version: 0x${version.toString(16)}`);
  }

  const iv = token.subarray(9, 25);
  const ct = token.subarray(25, token.length - 32);
  const hmac = token.subarray(token.length - 32);

  // Verify HMAC-SHA256 over everything except the HMAC itself
  const signed = token.subarray(0, token.length - 32);
  const computedHmac = createHmac("sha256", signingKey)
    .update(signed)
    .digest();

  if (!timingSafeEqual(computedHmac, hmac)) {
    throw new Error("Fernet HMAC verification failed — wrong encryption key?");
  }

  // Decrypt AES-128-CBC
  const decipher = createDecipheriv("aes-128-cbc", encryptionKey, iv);
  const decrypted = Buffer.concat([decipher.update(ct), decipher.final()]);

  return decrypted.toString("utf-8");
}
