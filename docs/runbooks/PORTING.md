# Porting runbook (P44f)

## Port-in: a customer brings numbers to Ringlite
1. The customer submits the request from Numbers → Port a number. It needs the numbers,
   the losing carrier's account number and PIN, the service address, the authorised person,
   an LOA and a recent invoice.
2. The app refuses the request outright when:
   - the workspace is not business-verified;
   - the LOA name matches neither the verified business nor a verified owner;
   - a number is already active on Ringlite or in another open request;
   - a number is outside the workspace's country.
3. Everything else lands in **Ops → Ports** as `awaiting_review`, with a `port_in_review`
   security alert. Before approving:
   - open the LOA and the invoice (download links on the request);
   - check that the invoice shows the same numbers and the same account holder;
   - if anything looks off, **Reject** with a reason. This is the port-in hijack defence:
     someone porting a victim's number in to steal their calls and 2FA codes.
4. **Approve**:
   - **Telnyx**: the app uploads the documents, creates the porting order(s), fills them in
     (tags `csaas`, our voice connection and messaging profile) and confirms them. The
     sweeper polls every 10 minutes. When the order reaches `ported`, the numbers are
     created in the workspace. They must get a 911 address (P44e) before they can call.
   - **SignalWire** (no porting API): the request shows `manual`. File it yourself in the
     SignalWire dashboard (Phone Numbers → Port Requests) with the same details, then move
     the status by hand in Ops → Ports (`in_process`, `foc_confirmed`, `ported`). Setting
     `ported` imports the numbers.

## Port-out: a number is leaving
- A valid port-out cannot legally be blocked. The **port-out PIN** is the control. Set a
  default port-out PIN on the Telnyx account (Mission Control → Account Settings → Port-out
  PIN). Give it only to a verified owner who asks for it through support.
- The sweeper checks Telnyx port-outs every 10 minutes. For any of our numbers it:
  - opens a `port_out_request` security alert;
  - emails and notifies the workspace owners at once ("If you did not request this,
    contact support immediately");
  - marks the number released once the port completes.
- If an owner says they did not request it, **reject the port-out in the Telnyx portal**
  (reason: PIN/authorisation mismatch) and suspend the requesting session. Then treat it
  as an account-takeover incident.
- **Port lock** (Numbers → lock icon) stops the number being released or moved inside
  Ringlite. It protects against a hijacked Ringlite login. It does not stop a carrier
  port-out; the PIN does that.
