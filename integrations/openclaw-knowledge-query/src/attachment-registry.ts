/** In-memory, bounded registry populated only by OpenClaw's public inbound hook. */

import { constants } from "node:fs";
import { lstat, open, type FileHandle } from "node:fs/promises";
import path from "node:path";
import type { PluginHookInboundClaimContext, PluginHookInboundClaimEvent } from "openclaw/plugin-sdk/plugin-entry";

export const ATTACHMENT_TTL_MS = 30 * 60 * 1000;
export const MAX_SESSIONS = 100;
export const MAX_ATTACHMENTS_PER_SESSION = 8;
export const MAX_ATTACHMENT_BYTES = 100 * 1024 * 1024;
const SUFFIXES = new Set([".pdf", ".docx", ".md", ".txt"]);

type Identity = { dev: bigint; ino: bigint; size: bigint; mtimeNs: bigint };
type Attachment = Identity & {
  messageId?: string;
  filename: string;
  contentType: string;
  trustedPath: string;
  receivedAt: number;
  state: "READY" | "SENDING" | "QUEUED" | "TOO_LARGE";
};
type Session = { receivedAt: number; pending: boolean; overflow: boolean; attachments: Attachment[] };

export type Selection =
  | { status: "NO_ATTACHMENT" | "SELECTION_REQUIRED" | "ALREADY_QUEUED" | "TOO_LARGE" }
  | { status: "READY"; attachment: Attachment };

export type OpenedAttachment = { handle: FileHandle; attachment: Attachment };

function identity(stat: { dev: bigint; ino: bigint; size: bigint; mtimeNs: bigint }): Identity {
  return { dev: stat.dev, ino: stat.ino, size: stat.size, mtimeNs: stat.mtimeNs };
}

function sameIdentity(a: Identity, b: Identity): boolean {
  return a.dev === b.dev && a.ino === b.ino && a.size === b.size && a.mtimeNs === b.mtimeNs;
}

export class AttachmentRegistry {
  private readonly sessions = new Map<string, Session>();
  constructor(private readonly now: () => number = Date.now) {}

  private key(agentId: string, sessionKey: string): string {
    return `${agentId}\0${sessionKey}`;
  }

  private prune(): void {
    for (const [key, session] of this.sessions) {
      if (this.now() - session.receivedAt > ATTACHMENT_TTL_MS) this.sessions.delete(key);
    }
    while (this.sessions.size > MAX_SESSIONS) {
      const oldest = this.sessions.keys().next().value as string | undefined;
      if (oldest === undefined) break;
      this.sessions.delete(oldest);
    }
  }

  /** Never consume originalMedia, URLs, text-supplied paths, or unstaged media. */
  async register(event: PluginHookInboundClaimEvent, ctx: PluginHookInboundClaimContext): Promise<void> {
    const agentId = ctx.agentId;
    const sessionKey = ctx.sessionKey;
    if (!agentId || !sessionKey || (event.sessionKey && event.sessionKey !== sessionKey)) return;
    this.prune();
    const key = this.key(agentId, sessionKey);
    const previous = this.sessions.get(key);
    const media = event.media ?? [];
    if (media.length === 0 && event.mediaStagingPending !== true) return;
    // A repeated hook delivery must not turn a queued attachment back into READY.
    if (previous && event.messageId && previous.attachments.some((item) =>
      item.messageId === event.messageId && item.state !== "READY")) return;
    if (previous?.attachments.some((item) => item.state !== "READY" &&
      media.some((fact) => fact.path === item.trustedPath))) return;
    const receivedAt = this.now();
    const pending = event.mediaStagingPending === true;
    const overflow = media.length > MAX_ATTACHMENTS_PER_SESSION;
    const attachments: Attachment[] = [];
    if (!pending && !overflow) {
      for (const fact of media) {
        if (!fact.path || !path.isAbsolute(fact.path)) continue;
        try {
          const info = await lstat(fact.path, { bigint: true });
          if (!info.isFile() || info.isSymbolicLink()) continue;
          const currentIdentity = identity(info);
          const earlier = previous?.attachments.find((item) => item.state !== "READY" &&
            sameIdentity(item, currentIdentity));
          attachments.push({ ...currentIdentity, trustedPath: fact.path,
            filename: path.basename(fact.path), contentType: fact.contentType ?? "application/octet-stream",
            messageId: fact.messageId ?? event.messageId, receivedAt,
            state: earlier?.state ?? "READY" });
        } catch { /* A vanished or inaccessible staged file is not a usable attachment. */ }
      }
    }
    this.sessions.delete(key);
    this.sessions.set(key, { receivedAt, pending, overflow, attachments });
    this.prune();
  }

  select(agentId: string, sessionKey: string): Selection {
    this.prune();
    const session = this.sessions.get(this.key(agentId, sessionKey));
    if (!session || session.pending) return { status: "NO_ATTACHMENT" };
    if (session.overflow || session.attachments.length > 1) return { status: "SELECTION_REQUIRED" };
    const attachment = session.attachments[0];
    if (!attachment) return { status: "NO_ATTACHMENT" };
    if (attachment.state === "TOO_LARGE") return { status: "TOO_LARGE" };
    if (attachment.state !== "READY") return { status: "ALREADY_QUEUED" };
    return { status: "READY", attachment };
  }

  /** Open the recorded inode, then verify the actual fd before sending bytes. */
  async openSelected(attachment: Attachment): Promise<OpenedAttachment | "ATTACHMENT_CHANGED" | "TOO_LARGE" | "UNSUPPORTED_TYPE"> {
    if (!SUFFIXES.has(path.extname(attachment.filename).toLowerCase())) return "UNSUPPORTED_TYPE";
    if (attachment.size > BigInt(MAX_ATTACHMENT_BYTES)) return "TOO_LARGE";
    let handle: FileHandle | undefined;
    try {
      const before = await lstat(attachment.trustedPath, { bigint: true });
      if (!before.isFile() || before.isSymbolicLink() || !sameIdentity(attachment, identity(before))) {
        return "ATTACHMENT_CHANGED";
      }
      handle = await open(attachment.trustedPath, constants.O_RDONLY | (constants.O_NOFOLLOW ?? 0) |
        (constants.O_NONBLOCK ?? 0));
      const opened = await handle.stat({ bigint: true });
      if (!opened.isFile() || !sameIdentity(attachment, identity(opened))) return "ATTACHMENT_CHANGED";
      if (opened.size > BigInt(MAX_ATTACHMENT_BYTES)) return "TOO_LARGE";
      const result = { handle, attachment };
      handle = undefined;
      return result;
    } catch {
      return "ATTACHMENT_CHANGED";
    } finally {
      await handle?.close();
    }
  }

  start(attachment: Attachment): boolean {
    if (attachment.state !== "READY" || this.now() - attachment.receivedAt > ATTACHMENT_TTL_MS) return false;
    attachment.state = "SENDING";
    return true;
  }

  queued(attachment: Attachment): void { attachment.state = "QUEUED"; }
  tooLarge(attachment: Attachment): void { attachment.state = "TOO_LARGE"; }
  // A failed/aborted transfer may have reached the broker. Do not automatically retry.
}
