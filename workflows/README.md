# Autowebinar apply-SMS workflows (Zapier)

Source of the two Zapier durable workflows behind the post-webinar "apply" SMS.
Zapier is the source of truth for what runs; these copies are for review and history.

| File | Zapier workflow | Trigger |
|---|---|---|
| `log-autowebinar-applicant.ts` | `log-autowebinar-applicant` (01a15880-faac-7b27-be04-a61d3ad85209) | Simplero: tag "Søkt autowebinar" (221554) added |
| `autowebinar-apply-sms.ts` | `autowebinar-apply-sms` (01a15881-43da-716b-a0bd-4656a983f040) | WebinarFuel: registered to "BLI VENN MED PIANOET DITT!" (#18107) |

## How it fits together

1. The application page (toriskrede.no/ansok) and the survey "Application page
   questionnaire - final" both add the Simplero tag **Søkt autowebinar**.
2. `log-autowebinar-applicant` stores `email -> applied-at` in Storage by Zapier
   (key = `s_` + sha256(email), 32 chars max) and removes the tag again, so a later
   application fires the trigger again.
3. `autowebinar-apply-sms` runs once per registrant. It waits until Tue/Wed/Thu 18:00
   Oslo time after the registrant's webinar and, before each text, skips them if they
   applied on or after the webinar day. Duplicate registrations are de-duplicated per
   phone + webinar date + day. Numbers on ClickSend's "Opt-Out List" are not texted.
