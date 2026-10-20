import { defineDurable } from "@zapier/zapier-durable";
import { createZapierSdk } from "@zapier/zapier-sdk";
import { createHash } from "node:crypto";
import { z } from "zod";

const sdk = createZapierSdk();

const CLICKSEND_APP_KEY = "ClickSendCLIAPI";
const CLICKSEND_CONNECTION = "clicksend";
const STORAGE_APP_KEY = "StorageCLIAPI";
const STORAGE_CONNECTION = "storage";
const CLICKSEND_OPT_OUT_LIST = 2407312; // "Opt-Out List" — numbers that replied STOP

const SEND_HOUR_OSLO = 18;
// Texts are filled in once Tori supplies them. {navn} = first name.
const FOLLOW_UPS = [
  { day: "tuesday", offsetDays: 1, text: "TEKST TIRSDAG {navn}" },
  { day: "wednesday", offsetDays: 2, text: "TEKST ONSDAG {navn}" },
  { day: "thursday", offsetDays: 3, text: "TEKST TORSDAG {navn}" },
] as const;
// A follow-up whose send time passed more than this long ago is skipped, not sent late.
const LATE_TOLERANCE_SECONDS = 2 * 60 * 60;

const InputSchema = z
  .object({
    email: z.string().nullable().optional(),
    first_name: z.string().nullable().optional(),
    phone: z.string().nullable().optional(),
    webinar: z
      .object({ scheduled_at: z.string().nullable().optional() })
      .loose()
      .nullable()
      .optional(),
    // Draft-test only: skip the waits and send straight away. Never set by the trigger.
    test_mode: z.boolean().nullable().optional(),
  })
  .loose();
type Input = z.infer<typeof InputSchema>;

function normalizeInput(rawInput: unknown): unknown {
  if (typeof rawInput === "string") {
    return JSON.parse(rawInput);
  }
  return rawInput;
}

// Same rules as the registration Zap: Norwegian and Swedish mobiles to E.164.
function cleanPhone(phone: string): string | null {
  let d = phone.replace(/\D/g, "").replace(/^0+/, "");
  if (d.startsWith("460")) d = "46" + d.slice(3);
  if (d.startsWith("47") && d.length === 10) return "+" + d;
  if (d.startsWith("46") && d.length === 11 && d[2] === "7") return "+" + d;
  if (d.length === 8) return "+47" + d;
  if (d.length === 9 && d[0] === "7") return "+46" + d;
  return null;
}

function osloDateParts(date: Date): { year: number; month: number; day: number } {
  const parts = Object.fromEntries(
    new Intl.DateTimeFormat("en-US", { timeZone: "Europe/Oslo", year: "numeric", month: "2-digit", day: "2-digit" })
      .formatToParts(date)
      .map((p) => [p.type, p.value]),
  );
  return { year: +parts.year, month: +parts.month, day: +parts.day };
}

// Unix seconds for a wall-clock time in Oslo, correct across summer/winter time.
function osloTimeToUnix(year: number, month: number, day: number, hour: number): number {
  const guess = new Date(Date.UTC(year, month - 1, day, hour, 0));
  const offset = new Intl.DateTimeFormat("en-US", { timeZone: "Europe/Oslo", timeZoneName: "shortOffset" })
    .formatToParts(guess)
    .find((p) => p.type === "timeZoneName")?.value;
  const offsetHours = parseInt((offset?.match(/GMT([+-]\d+)/) ?? [0, "0"])[1], 10);
  return Math.floor(guess.getTime() / 1000) - offsetHours * 3600;
}

// Storage keys are limited to 32 characters, so identifiers are hashed.
function storageKey(prefix: string, value: string): string {
  return prefix + createHash("sha256").update(value).digest("hex").slice(0, 32 - prefix.length);
}

export default defineDurable<Input, unknown>(
  "autowebinar-apply-sms",
  async (ctx, rawInput) => {
    const input = InputSchema.parse(normalizeInput(rawInput));
    const email = (input.email ?? "").trim().toLowerCase();
    const firstName = (input.first_name ?? "").trim();
    const phone = cleanPhone(input.phone ?? "");
    const scheduledAt = input.webinar?.scheduled_at ?? null;
    const webinarStart = scheduledAt ? new Date(scheduledAt) : null;

    if (!phone) {
      return { skipped: true, reason: "invalid-phone", phone: input.phone ?? null };
    }
    if (!webinarStart || isNaN(webinarStart.getTime())) {
      return { skipped: true, reason: "no-webinar-date" };
    }

    const webinarDay = osloDateParts(webinarStart);
    // Applications from the webinar day onwards count; earlier rounds do not.
    const applyCutoff = osloTimeToUnix(webinarDay.year, webinarDay.month, webinarDay.day, 0);
    const webinarDateLabel = `${webinarDay.year}-${webinarDay.month}-${webinarDay.day}`;

    const results: Record<string, string> = {};

    for (const followUp of FOLLOW_UPS) {
      const sendAt = osloTimeToUnix(webinarDay.year, webinarDay.month, webinarDay.day + followUp.offsetDays, SEND_HOUR_OSLO);

      if (!input.test_mode) {
        const now = await ctx.step(`capture-time-before-${followUp.day}`, async () => Math.floor(Date.now() / 1000));
        if (now > sendAt + LATE_TOLERANCE_SECONDS) {
          results[followUp.day] = "skipped-too-late";
          continue;
        }
        if (sendAt > now) {
          await ctx.wait(`wait-until-${followUp.day}-18`, sendAt - now);
        }
      }

      if (email) {
        const applied = await ctx.step(`check-applied-before-${followUp.day}`, async () =>
          sdk.runAction({
            appKey: STORAGE_APP_KEY,
            actionType: "search",
            actionKey: "get_value",
            connection: STORAGE_CONNECTION,
            inputs: { key: storageKey("s_", email) },
          }),
        );
        const appliedAtRaw = (applied.data[0] as { value?: unknown } | undefined)?.value;
        const appliedAt = typeof appliedAtRaw === "string" ? Date.parse(appliedAtRaw) / 1000 : NaN;
        if (!isNaN(appliedAt) && appliedAt >= applyCutoff) {
          results[followUp.day] = "skipped-applied";
          continue;
        }
      }

      // Registering twice starts two runs; only the first one sends each text.
      const sentKey = storageKey("m_", `${phone}|${webinarDateLabel}|${followUp.day}`);
      const alreadySent = await ctx.step(`check-duplicate-${followUp.day}`, async () =>
        sdk.runAction({
          appKey: STORAGE_APP_KEY,
          actionType: "search",
          actionKey: "get_value",
          connection: STORAGE_CONNECTION,
          inputs: { key: sentKey },
        }),
      );
      if ((alreadySent.data[0] as { value?: unknown } | undefined)?.value) {
        results[followUp.day] = "skipped-duplicate";
        continue;
      }
      await ctx.step(`mark-sent-${followUp.day}`, async () =>
        sdk.runAction({
          appKey: STORAGE_APP_KEY,
          actionType: "write",
          actionKey: "set_value",
          connection: STORAGE_CONNECTION,
          inputs: { key: sentKey, value: "1" },
        }),
      );

      const body = followUp.text.replaceAll("{navn}", firstName);
      await ctx.step(`send-${followUp.day}-sms`, async () =>
        sdk.runAction({
          appKey: CLICKSEND_APP_KEY,
          actionType: "write",
          actionKey: "clicksend_sendsms",
          connection: CLICKSEND_CONNECTION,
          inputs: {
            messages: [
              {
                to: phone,
                body,
                opt_out: CLICKSEND_OPT_OUT_LIST,
                custom_string: `apply-${followUp.day}`,
              },
            ],
          },
        }),
      );
      results[followUp.day] = "sent";
    }

    return { phone, email, webinar: webinarDateLabel, results };
  },
);
