/**
 * P41 passkeys: the browser half of WebAuthn.
 *
 * The backend (py_webauthn) sends options as JSON with base64url-encoded binary fields and
 * expects the credential back in the same shape. The browser API wants ArrayBuffers. These
 * helpers convert both ways - no third-party library.
 */

export function base64urlToBuffer(value: string): ArrayBuffer {
  const padded = value.replace(/-/g, "+").replace(/_/g, "/").padEnd(Math.ceil(value.length / 4) * 4, "=");
  const binary = atob(padded);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
  return bytes.buffer;
}

export function bufferToBase64url(buffer: ArrayBuffer): string {
  const bytes = new Uint8Array(buffer);
  let binary = "";
  for (let i = 0; i < bytes.length; i += 1) binary += String.fromCharCode(bytes[i]);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

type JsonDescriptor = { id: string; type: string; transports?: string[] };

export function passkeysSupported(): boolean {
  return typeof window !== "undefined" && typeof window.PublicKeyCredential !== "undefined";
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
export function toCreationOptions(json: any): PublicKeyCredentialCreationOptions {
  return {
    ...json,
    challenge: base64urlToBuffer(json.challenge),
    user: { ...json.user, id: base64urlToBuffer(json.user.id) },
    excludeCredentials: (json.excludeCredentials ?? []).map((c: JsonDescriptor) => ({
      ...c,
      id: base64urlToBuffer(c.id),
    })),
  };
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
export function toRequestOptions(json: any): PublicKeyCredentialRequestOptions {
  return {
    ...json,
    challenge: base64urlToBuffer(json.challenge),
    allowCredentials: (json.allowCredentials ?? []).map((c: JsonDescriptor) => ({
      ...c,
      id: base64urlToBuffer(c.id),
    })),
  };
}

export function serializeCredential(credential: PublicKeyCredential): Record<string, unknown> {
  const response = credential.response as AuthenticatorAttestationResponse &
    AuthenticatorAssertionResponse;
  const out: Record<string, unknown> = {
    id: credential.id,
    rawId: bufferToBase64url(credential.rawId),
    type: credential.type,
    clientExtensionResults: credential.getClientExtensionResults?.() ?? {},
    response: {
      clientDataJSON: bufferToBase64url(response.clientDataJSON),
    } as Record<string, unknown>,
  };
  const body = out.response as Record<string, unknown>;
  if ("attestationObject" in response && response.attestationObject) {
    body.attestationObject = bufferToBase64url(response.attestationObject);
    if (typeof response.getTransports === "function") body.transports = response.getTransports();
  }
  if ("authenticatorData" in response && response.authenticatorData) {
    body.authenticatorData = bufferToBase64url(response.authenticatorData);
    body.signature = bufferToBase64url(response.signature);
    if (response.userHandle) body.userHandle = bufferToBase64url(response.userHandle);
  }
  return out;
}

export async function createPasskey(optionsJson: unknown): Promise<Record<string, unknown>> {
  const credential = (await navigator.credentials.create({
    publicKey: toCreationOptions(optionsJson),
  })) as PublicKeyCredential | null;
  if (!credential) throw new Error("No passkey was created");
  return serializeCredential(credential);
}

export async function getPasskeyAssertion(optionsJson: unknown): Promise<Record<string, unknown>> {
  const credential = (await navigator.credentials.get({
    publicKey: toRequestOptions(optionsJson),
  })) as PublicKeyCredential | null;
  if (!credential) throw new Error("No passkey was selected");
  return serializeCredential(credential);
}
