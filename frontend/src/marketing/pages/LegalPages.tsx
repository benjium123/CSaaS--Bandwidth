/**
 * The three legal marketing pages: /legal/privacy, /legal/terms and /legal/refunds.
 * Ringlite is a product of Sabine Property Group LLC.
 */
import { Link } from "react-router-dom";
import { SitePage } from "@/marketing/SiteChrome";

export const LEGAL_ENTITY = "Sabine Property Group LLC";
export const LEGAL_ADDRESS = "2106 House Ave, Suite 820, Cheyenne, WY 82001, USA";
export const SUPPORT_EMAIL = "support@ringlite.io";
export const SUPPORT_PHONE = "+1 (469) 461-7576";

export function PrivacyPage() {
  return (
    <SitePage title="Privacy policy" description="What Ringlite collects, why we collect it, and the choices you have.">
      <article className="ms-prose rl-wrap">
        <h1>Privacy policy</h1>
        <p>Last updated September 2026</p>

        <h2>What Ringlite is</h2>
        <p>Ringlite is a business phone system that runs in the browser. It gives your team numbers, calls, texts (SMS and MMS), voicemail, call recording and transcripts, AI call summaries and a shared inbox. We serve teams in the US and the UK.</p>

        <h2>Information we collect</h2>
        <ul>
          <li><strong>Account data.</strong> Your name, email, phone number and company.</li>
          <li><strong>Identity verification data.</strong> Your ID document, a selfie and proof of address, collected through our verification provider.</li>
          <li><strong>Billing data.</strong> Handled by Stripe. We never store full card numbers.</li>
          <li><strong>Communications data.</strong> Call records, message content, recordings, transcripts and voicemails, processed to provide the service.</li>
          <li><strong>Usage and device data.</strong> IP address, browser and logs, used for security and fraud prevention.</li>
        </ul>

        <h2>How we use information</h2>
        <p>We use it to provide and bill the service, to verify identity (KYC) and carrier registration (10DLC), to prevent fraud, spam and abuse — including automated review of calls and texts — to support you, and to meet legal requirements.</p>
        <p>We do not sell personal data, and we do not use it for advertising.</p>

        <h2>Who we share it with</h2>
        <p>We share personal data with processors, only as far as they need it:</p>
        <ul>
          <li>Our payment processor (Stripe).</li>
          <li>Telecom carriers (Telnyx, SignalWire).</li>
          <li>Our identity verification provider.</li>
          <li>Cloud hosting and email delivery providers.</li>
          <li>Speech transcription and AI summary providers.</li>
          <li>Authorities when the law requires it, or for a 911 call.</li>
        </ul>

        <h2>How we protect information</h2>
        <p>Data is encrypted in transit (TLS). We use access controls, per-workspace isolation, audit logs and two-step sign-in.</p>

        <h2>How long we keep information</h2>
        <p>We keep data while the account is active and as long as we need it for legal, tax, fraud and dispute purposes. Recordings and messages can be deleted by the workspace, and data is deleted on request, subject to our legal obligations.</p>

        <h2>Your rights and choices</h2>
        <p>You can ask for access to, correction of, deletion of or an export of your data. Email <a className="rl-text-link" href={`mailto:${SUPPORT_EMAIL}`}>{SUPPORT_EMAIL}</a>.</p>
        <p>For messaging, a recipient who replies STOP is opted out of texts.</p>

        <h2>Children</h2>
        <p>Ringlite is not for people under 18.</p>

        <h2>Contact</h2>
        <p>Ringlite is operated by {LEGAL_ENTITY}, {LEGAL_ADDRESS}. Email <a className="rl-text-link" href={`mailto:${SUPPORT_EMAIL}`}>{SUPPORT_EMAIL}</a> or call {SUPPORT_PHONE}.</p>

        <p><Link className="rl-text-link" to="/">Back to home</Link></p>
      </article>
    </SitePage>
  );
}

export function TermsPage() {
  return (
    <SitePage title="Terms of service" description="The terms that apply when you use Ringlite.">
      <article className="ms-prose rl-wrap">
        <h1>Terms of service</h1>
        <p>Last updated September 2026</p>

        <h2>Who we are</h2>
        <p>Ringlite is operated by {LEGAL_ENTITY}, {LEGAL_ADDRESS}. By creating an account you agree to these terms.</p>

        <h2>Eligibility</h2>
        <p>You must be 18 or older and use Ringlite for business purposes. The information you give us has to be accurate, and identity verification is required.</p>

        <h2>Prepaid balance and billing</h2>
        <p>Usage — calls, texts, numbers and features — is paid from a prepaid balance or plan. When the balance reaches $0, the service pauses. Auto-recharge charges the saved card when the balance runs low; you can turn it off in Billing.</p>

        <h2>Acceptable use</h2>
        <p>You may not use Ringlite to send spam or unsolicited bulk texts, or to break the law. You must follow the TCPA, CAN-SPAM, CTIA and carrier rules, including 10DLC registration. No fraud, scams, harassment, robocalls or spoofing.</p>
        <p>We monitor traffic automatically and may pause or close accounts that break these rules.</p>

        <h2>Phone numbers</h2>
        <p>Numbers are rented monthly. A released number may not be recoverable.</p>

        <h2>911</h2>
        <p>Every number carries E911, and internet calling has limits. Read the <Link className="rl-text-link" to="/legal/911">911 disclosure</Link>.</p>

        <h2>Availability and liability</h2>
        <p>The service is provided “as is”. Our liability is limited to the fees you paid us in the prior 3 months, and we are not liable for indirect damages.</p>

        <h2>Governing law</h2>
        <p>These terms are governed by the laws of Wyoming, USA.</p>

        <h2>Changes</h2>
        <p>We may update these terms. Changes are posted on this page.</p>

        <h2>Contact</h2>
        <p>Ringlite is operated by {LEGAL_ENTITY}, {LEGAL_ADDRESS}. Email <a className="rl-text-link" href={`mailto:${SUPPORT_EMAIL}`}>{SUPPORT_EMAIL}</a> or call {SUPPORT_PHONE}.</p>

        <p><Link className="rl-text-link" to="/">Back to home</Link></p>
      </article>
    </SitePage>
  );
}

export function RefundPage() {
  return (
    <SitePage title="Refunds and cancellation" description="What Ringlite refunds, what it does not, and how cancellation works.">
      <article className="ms-prose rl-wrap">
        <h1>Refunds and cancellation</h1>
        <p>Last updated September 2026</p>

        <h2>What we refund</h2>
        <p>Only <strong>unused prepaid account credit</strong> is refundable: credit top-ups, auto-recharges, and balance credit bought on a custom invoice.</p>
        <p>Card processing fees are not returned. We deduct the processor&apos;s fee on the original payment, in proportion to the part refunded, and any fee the processor charges on the refund itself.</p>
        <p><strong>Example:</strong> you top up $50 and the card fee is $1.75. You use $20, so $30 is unused. We refund $30 less $1.75 × 30/50 = <strong>$28.95</strong>.</p>

        <h2>What we do not refund</h2>
        <ul>
          <li>Bundles of any kind (SMS, MMS, call minutes), whether the units are used or unused.</li>
          <li>Credit already spent on calls, texts, fax, recording, transcription, AI summaries or the AI agent.</li>
          <li>Plans, seats and add-ons for the current period.</li>
          <li>Number rental, setup and porting fees.</li>
          <li>10DLC brand and campaign registration fees.</li>
          <li>Card processing fees.</li>
          <li>Any payment under an open chargeback or dispute.</li>
        </ul>

        <h2>Cancelling a plan</h2>
        <p>You can cancel any time in Billing. Plans are billed per period: access runs to the end of the period you paid for, and we do not refund partial periods.</p>

        <h2>Phone numbers</h2>
        <p>Numbers are rented monthly. A released number is not refunded for the current month and may not be recoverable.</p>

        <h2>Billing errors</h2>
        <p>If we charge you in error or charge you twice, we refund that charge in full. Contact support within 30 days.</p>

        <h2>How to ask for a refund</h2>
        <p>Email <a className="rl-text-link" href={`mailto:${SUPPORT_EMAIL}`}>{SUPPORT_EMAIL}</a> from the account owner&apos;s address. Refunds go back to the original payment method and usually arrive in 5-10 business days.</p>

        <h2>Contact</h2>
        <p>Ringlite is operated by {LEGAL_ENTITY}, {LEGAL_ADDRESS}. Email <a className="rl-text-link" href={`mailto:${SUPPORT_EMAIL}`}>{SUPPORT_EMAIL}</a> or call {SUPPORT_PHONE}.</p>

        <p><Link className="rl-text-link" to="/">Back to home</Link></p>
      </article>
    </SitePage>
  );
}
