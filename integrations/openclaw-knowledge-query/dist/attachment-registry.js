/** In-memory, bounded registry populated only by OpenClaw's trusted message hook. */
import { constants } from "node:fs";
import { lstat, open } from "node:fs/promises";
import path from "node:path";
import { parseAgentSessionKey } from "openclaw/plugin-sdk/routing";
import { parseExplicitImportConsent } from "./import-consent.js";
export const ATTACHMENT_TTL_MS = 30 * 60 * 1000;
export const CONSENT_TTL_MS = 5 * 60 * 1000;
export const MAX_SESSIONS = 100;
export const MAX_ATTACHMENTS_PER_SESSION = 8;
export const MAX_MESSAGE_IDS_PER_SESSION = 128;
export const MAX_ATTACHMENT_BYTES = 100 * 1024 * 1024;
export const REGISTRATION_WAIT_MS = 2_000;
const SUFFIXES = new Set([".pdf", ".docx", ".md", ".txt"]);
function identity(stat) {
    return { dev: stat.dev, ino: stat.ino, size: stat.size, mtimeNs: stat.mtimeNs };
}
function sameIdentity(a, b) {
    return a.dev === b.dev && a.ino === b.ino && a.size === b.size && a.mtimeNs === b.mtimeNs;
}
export class AttachmentRegistry {
    now;
    statPath;
    sessions = new Map();
    registrations = new Map();
    constructor(now = Date.now, statPath = (file) => lstat(file, { bigint: true })) {
        this.now = now;
        this.statPath = statPath;
    }
    key(agentId, sessionKey) {
        return `${agentId}\0${sessionKey}`;
    }
    prune() {
        for (const [key, session] of this.sessions) {
            if (this.now() - session.receivedAt > ATTACHMENT_TTL_MS)
                this.sessions.delete(key);
        }
        while (this.sessions.size > MAX_SESSIONS) {
            const oldest = this.sessions.keys().next().value;
            if (oldest === undefined)
                break;
            this.sessions.delete(oldest);
        }
    }
    /** The host resolves the agent-scoped session; message_received has no agentId field. */
    registerMessageReceived(event, ctx) {
        const sessionKey = ctx.sessionKey;
        const parsed = parseAgentSessionKey(sessionKey);
        if (!sessionKey || !parsed || (event.sessionKey && event.sessionKey !== sessionKey) ||
            (event.messageId && ctx.messageId && event.messageId !== ctx.messageId))
            return Promise.resolve();
        // Only canonical media is consumed. OpenClaw 2026.9.4 projects QQBot's
        // legacy MediaPaths into event.media before this observation hook fires.
        // Only a trusted inbound user hook can grant consent; model tool arguments cannot.
        return this.register({ content: event.content, media: event.media, mediaStagingPending: event.mediaStagingPending,
            messageId: event.messageId ?? ctx.messageId, sessionKey }, { agentId: parsed.agentId, sessionKey });
    }
    /** Announce registration synchronously so a same-turn tool can await it. */
    register(event, ctx) {
        const agentId = ctx.agentId;
        const sessionKey = ctx.sessionKey;
        if (!agentId || !sessionKey || (event.sessionKey && event.sessionKey !== sessionKey) ||
            (event.messageId && ctx.messageId && event.messageId !== ctx.messageId))
            return Promise.resolve();
        const key = this.key(agentId, sessionKey);
        const previous = this.registrations.get(key) ?? Promise.resolve();
        const registration = previous.catch(() => { }).then(() => this.registerOne(event, key));
        this.registrations.set(key, registration);
        const clear = () => { if (this.registrations.get(key) === registration)
            this.registrations.delete(key); };
        void registration.then(clear, clear);
        return registration;
    }
    /** Wait only for this agent/session, and fail closed if registration stalls. */
    async waitForRegistration(agentId, sessionKey) {
        const key = this.key(agentId, sessionKey);
        const deadline = Date.now() + REGISTRATION_WAIT_MS;
        while (true) {
            const pending = this.registrations.get(key);
            if (!pending)
                return true;
            const remaining = deadline - Date.now();
            if (remaining <= 0)
                return false;
            let timer;
            try {
                const completed = await Promise.race([
                    pending.then(() => true, () => false),
                    new Promise((resolve) => { timer = setTimeout(() => resolve(false), remaining); }),
                ]);
                if (!completed)
                    return false;
            }
            finally {
                if (timer)
                    clearTimeout(timer);
            }
            // A newer delivery for the same session may have arrived while awaiting.
            if (this.registrations.get(key) === pending)
                return true;
        }
    }
    /** Never consume originalMedia, URLs, text-supplied paths, or unstaged media. */
    async registerOne(event, key) {
        this.prune();
        const previous = this.sessions.get(key);
        const media = event.media ?? [];
        if (!previous && media.length === 0 && event.mediaStagingPending !== true)
            return;
        const samePendingMessage = previous?.pending && previous.messageId === event.messageId &&
            event.mediaStagingPending !== true && media.length > 0;
        if (event.messageId && previous?.seenMessageIds.includes(event.messageId) && !samePendingMessage)
            return;
        // Never evict an old authorization message ID and accidentally accept its replay.
        if (previous && previous.seenMessageIds.length >= MAX_MESSAGE_IDS_PER_SESSION &&
            !samePendingMessage) {
            previous.consent = undefined;
            return;
        }
        // A repeated hook delivery must not turn a queued attachment back into READY.
        if (previous && event.messageId && previous.attachments.some((item) => item.messageId === event.messageId && item.state !== "READY"))
            return;
        if (previous?.attachments.some((item) => item.state !== "READY" &&
            media.some((fact) => fact.path === item.trustedPath)))
            return;
        const receivedAt = this.now();
        const seenMessageIds = previous?.seenMessageIds ?? [];
        const nextSeenMessageIds = event.messageId && !seenMessageIds.includes(event.messageId)
            ? [...seenMessageIds, event.messageId] : seenMessageIds;
        if (media.length === 0 && event.mediaStagingPending !== true) {
            if (!previous)
                return;
            // A new user turn supersedes prior consent, even when it has no media.
            const attachment = !previous.pending && !previous.overflow && previous.attachments.length === 1
                ? previous.attachments[0] : undefined;
            const kind = event.messageId ? parseExplicitImportConsent(event.content) : null;
            previous.consent = kind && attachment?.state === "READY"
                ? { kind, attachment, expiresAt: receivedAt + CONSENT_TTL_MS } : undefined;
            // A later user turn must prevent late staging of the older message from reviving its consent.
            previous.messageId = event.messageId;
            previous.seenMessageIds = nextSeenMessageIds;
            return;
        }
        const pending = event.mediaStagingPending === true;
        const overflow = media.length > MAX_ATTACHMENTS_PER_SESSION;
        const attachments = [];
        if (!pending && !overflow) {
            for (const fact of media) {
                if (!fact.path || !path.isAbsolute(fact.path))
                    continue;
                try {
                    const info = await this.statPath(fact.path);
                    if (!info.isFile() || info.isSymbolicLink())
                        continue;
                    const currentIdentity = identity(info);
                    const earlier = previous?.attachments.find((item) => item.state !== "READY" &&
                        sameIdentity(item, currentIdentity));
                    attachments.push({ ...currentIdentity, trustedPath: fact.path,
                        filename: path.basename(fact.path), contentType: fact.contentType ?? "application/octet-stream",
                        messageId: fact.messageId ?? event.messageId, receivedAt,
                        state: earlier?.state ?? "READY" });
                }
                catch { /* A vanished or inaccessible staged file is not a usable attachment. */ }
            }
        }
        this.sessions.delete(key);
        const kind = event.messageId && !pending && !overflow && attachments.length === 1
            ? parseExplicitImportConsent(event.content) : null;
        this.sessions.set(key, { receivedAt, messageId: event.messageId,
            seenMessageIds: nextSeenMessageIds, pending, overflow, attachments,
            consent: kind && attachments[0].state === "READY"
                ? { kind, attachment: attachments[0], expiresAt: receivedAt + CONSENT_TTL_MS } : undefined });
        this.prune();
    }
    select(agentId, sessionKey) {
        this.prune();
        const session = this.sessions.get(this.key(agentId, sessionKey));
        if (!session || session.pending)
            return { status: "NO_ATTACHMENT" };
        if (session.overflow || session.attachments.length > 1)
            return { status: "SELECTION_REQUIRED" };
        const attachment = session.attachments[0];
        if (!attachment)
            return { status: "NO_ATTACHMENT" };
        if (attachment.state === "TOO_LARGE")
            return { status: "TOO_LARGE" };
        if (attachment.state !== "READY")
            return { status: "ALREADY_QUEUED" };
        return { status: "READY", attachment };
    }
    hasConsent(agentId, sessionKey, kind, attachment) {
        this.prune();
        const consent = this.sessions.get(this.key(agentId, sessionKey))?.consent;
        return consent?.kind === kind && consent.attachment === attachment && this.now() < consent.expiresAt;
    }
    /** Check and consume consent in the same synchronous step as READY -> SENDING. */
    startIfConsented(agentId, sessionKey, kind, attachment) {
        if (!this.hasConsent(agentId, sessionKey, kind, attachment))
            return "CONSENT_REQUIRED";
        if (!this.start(attachment))
            return "ALREADY_QUEUED";
        this.sessions.get(this.key(agentId, sessionKey)).consent = undefined;
        return "STARTED";
    }
    /** Open the recorded inode, then verify the actual fd before sending bytes. */
    async openSelected(attachment) {
        if (!SUFFIXES.has(path.extname(attachment.filename).toLowerCase()))
            return "UNSUPPORTED_TYPE";
        if (attachment.size > BigInt(MAX_ATTACHMENT_BYTES))
            return "TOO_LARGE";
        let handle;
        try {
            const before = await lstat(attachment.trustedPath, { bigint: true });
            if (!before.isFile() || before.isSymbolicLink() || !sameIdentity(attachment, identity(before))) {
                return "ATTACHMENT_CHANGED";
            }
            handle = await open(attachment.trustedPath, constants.O_RDONLY | (constants.O_NOFOLLOW ?? 0) |
                (constants.O_NONBLOCK ?? 0));
            const opened = await handle.stat({ bigint: true });
            if (!opened.isFile() || !sameIdentity(attachment, identity(opened)))
                return "ATTACHMENT_CHANGED";
            if (opened.size > BigInt(MAX_ATTACHMENT_BYTES))
                return "TOO_LARGE";
            const result = { handle, attachment };
            handle = undefined;
            return result;
        }
        catch {
            return "ATTACHMENT_CHANGED";
        }
        finally {
            await handle?.close();
        }
    }
    start(attachment) {
        if (attachment.state !== "READY" || this.now() - attachment.receivedAt > ATTACHMENT_TTL_MS)
            return false;
        attachment.state = "SENDING";
        return true;
    }
    queued(attachment) { attachment.state = "QUEUED"; }
    tooLarge(attachment) { attachment.state = "TOO_LARGE"; }
}
