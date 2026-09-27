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
    <SitePage title="Refunds and cancellation" description="How cancellations, plans, prepaid balance and refunds work at Ringlite.">
      <article className="ms-prose rl-wrap">
        <h1>Refunds and cancellation</h1>
        <p>Last updated September 2026</p>

        <h2>Plans</h2>
        <p>You can cancel any time in Billing. The plan stays active until the end of the period you have paid for. We do not refund part of a month.</p>

        <h2>Prepaid balance and bundles</h2>
        <p>Prepaid balance (credits) and bundles (SMS, MMS and minute packages) are non-refundable once purchased, except where the law requires it or where the error is ours.</p>
        <ul>
          <li>Bundle units expire at the monthly renewal.</li>
          <li>Balance credit does not expire while the account is open.</li>
        </ul>

        <h2>Phone numbers</h2>
        <p>Numbers are rented monthly. We do not refund the current month when a number is released.</p>

        <h2>Carrier registration fees</h2>
        <p>10DLC brand and campaign fees are passed through to you and are non-refundable.</p>

        <h2>Billing errors and duplicate charges</h2>
        <p>If we charge you in error or charge you twice, we refund it in full. Contact support within 30 days.</p>

        <h2>Accounts closed for abuse</h2>
        <p>If we close an account for abuse, any unused balance may be forfeited, as the law allows.</p>

        <h2>How refunds are paid</h2>
        <p>Refunds go back to the original payment method and usually take 5-10 business days.</p>

        <h2>Contact</h2>
        <p>Ringlite is operated by {LEGAL_ENTITY}, {LEGAL_ADDRESS}. Email <a className="rl-text-link" href={`mailto:${SUPPORT_EMAIL}`}>{SUPPORT_EMAIL}</a> or call {SUPPORT_PHONE}.</p>

        <p><Link className="rl-text-link" to="/">Back to home</Link></p>
      </article>
    </SitePage>
  );
}
