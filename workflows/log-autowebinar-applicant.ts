import { defineDurable } from "@zapier/zapier-durable";
import { createZapierSdk } from "@zapier/zapier-sdk";
import { createHash } from "node:crypto";
import { z } from "zod";

const sdk = createZapierSdk();

const SIMPLERO_APP_KEY = "SimpleroConvertedCLIAPI";
const SIMPLERO_CONNECTION = "simplero_tori";
const STORAGE_APP_KEY = "StorageCLIAPI";
const STORAGE_CONNECTION = "storage";
const SOKT_TAG_ID = "221554"; // "Søkt autowebinar"

const InputSchema = z
  .object({
    email: z.string().nullable().optional(),
    name: z.string().nullable().optional(),
    id: z.unknown(),
  })
  .loose();
type Input = z.infer<typeof InputSchema>;

function normalizeInput(rawInput: unknown): unknown {
  if (typeof rawInput === "string") {
    return JSON.parse(rawInput);
  }
  return rawInput;
}

// Storage keys are limited to 32 characters, so emails are hashed.
function applicantKey(email: string): string {
  return "s_" + createHash("sha256").update(email).digest("hex").slice(0, 30);
}

export default defineDurable<Input, unknown>(
  "log-autowebinar-applicant",
  async (ctx, rawInput) => {
    const input = InputSchema.parse(normalizeInput(rawInput));
    const email = (input.email ?? "").trim().toLowerCase();
    if (!email) {
      return { skipped: true, reason: "no-email" };
    }

    const appliedAt = await ctx.step("capture-applied-at", async () =>
      new Date().toISOString(),
    );

    await ctx.step("store-applicant", async () =>
      sdk.runAction({
        appKey: STORAGE_APP_KEY,
        actionType: "write",
        actionKey: "set_value",
        connection: STORAGE_CONNECTION,
        inputs: { key: applicantKey(email), value: appliedAt },
      }),
    );

    // Remove the tag so the next application (a later round) fires the trigger again.
    await ctx.step("remove-sokt-tag", async () =>
      sdk.runAction({
        appKey: SIMPLERO_APP_KEY,
        actionType: "write",
        actionKey: "remove_tag",
        connection: SIMPLERO_CONNECTION,
        inputs: { email, tag_id: SOKT_TAG_ID },
      }),
    );

    return { email, appliedAt };
  },
);
