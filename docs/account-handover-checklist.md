# AWS account handover checklist

Use this checklist only after the production deployment, restore test, security
review, and client acceptance test have passed. The account remains controlled
by the current owner until the client confirms access from client-owned contact
details and devices.

## 1. Prepare evidence and acceptance

- [ ] Record the AWS Free Plan expiry/credit status and confirm the named client
      owner will upgrade the account before expiry and before the long-lived
      production rollout. The project team has already verified the complete
      synthetic development deployment while the account remained on Free Plan.
- [ ] Record the AWS account ID, canonical sign-in URL, workload Region
      (`ap-southeast-2`), deployed stack names, Git commit, release identifier,
      API URL, and deployment date.
- [ ] Client signs off functional, accessibility, security, privacy/ethics, and
      load-test acceptance criteria.
- [ ] Run and record an Aurora point-in-time restore and application smoke test.
- [ ] Confirm WAF is attached to the production API and tuned out of count mode.
- [ ] Confirm budget, forecast, DLQ, queue-age, Lambda, API, Aurora, WAF, and
      CloudTrail alarms reach a client-controlled address.
- [ ] Export a non-secret resource inventory, architecture diagram, monthly cost
      estimate, open risks, RPO/RTO decision, and vendor/processors list.
- [ ] Confirm no participant data is present in development or local machines.

## 2. Transfer root ownership

- [ ] Hold a supervised handover call with two authorised client representatives.
- [ ] Change the root email to a durable client-controlled mailbox, not a named
      student, contractor, or temporary project address.
- [ ] Change the root phone number, postal/business contact, security contact,
      operations contact, and billing contact to client-controlled details.
- [ ] Replace the payment method and tax/business information with the client’s.
- [ ] Client resets the root password directly; the project team never records it.
- [ ] Register two independent root MFA devices held by separate authorised
      client custodians. Store recovery details according to the client’s policy.
- [ ] Client signs in as root from a clean browser, reviews account settings, and
      signs out. Root is not used for daily administration afterward.

## 3. Establish client administration

- [ ] Create or confirm two named client administrator identities with MFA.
- [ ] Verify each administrator can sign in and assume the intended operational
      role without using root.
- [ ] Give daily administrators only billing, support, deployment approval,
      restore, incident-response, and identity-management permissions they need.
- [ ] Retain a documented break-glass role; test access and alert on its use.
- [ ] Deactivate `WM-Admin` and all project-team human access after client access
      is proven. Delete unused access keys and revoke active sessions.
- [ ] Review IAM Access Analyzer findings and credential reports. Confirm there
      are no unknown users, roles, keys, certificates, or external principals.

## 4. Transfer repositories and deployment

- [ ] Transfer `meditation-server` and the mobile repository to the client’s
      GitHub organisation or another durable owner.
- [ ] Require two client repository administrators and protect `main`.
- [ ] Recreate/verify `dev` and `production` GitHub environments, reviewers,
      variables, branch rules, and notification addresses.
- [ ] Update the AWS GitHub OIDC role subjects if owner/repository names changed;
      remove trust for the previous repository immediately afterward.
- [ ] Run a client-approved no-op CDK diff and one controlled deployment through
      OIDC. Confirm no AWS access keys exist in GitHub.
- [ ] Transfer signing/release custody for the Android application without
      sharing credentials in tickets, chat, or repository files.

## 5. Transfer services and secrets

- [ ] Client assumes ownership of Google Maps/Routes/Places/Elevation/Weather,
      OpenAI, SES domain/DNS, support mailboxes, and Android distribution accounts.
- [ ] Rotate Google, OpenAI, JWT, rate-limit HMAC, and any legacy credentials after
      ownership transfer. Keep current/previous JWT keys during the controlled
      session transition.
- [ ] Verify Secrets Manager resource policies and rotation runbooks.
- [ ] Confirm SES production access, verified identities, DKIM, SPF, DMARC,
      bounce/complaint handling, and support contact.
- [ ] Confirm Polly remains restricted to Australian English `Russell` and
      `Nicole` standard synthesis and can be disabled through configuration.

## 6. Data, backup, and operations

- [ ] Client approves the personal-data inventory, processor list, privacy notice,
      retention schedule, withdrawal/deletion procedure, and incident contacts.
- [ ] After the paid-plan transition, confirm the client-approved production
      Aurora design has 30-day PITR, deletion protection, encrypted storage,
      Data API, log export, and an explicit Express-versus-full-configuration
      and reader/HA decision.
- [ ] Create and label the final pre-handover snapshot; record its deletion date.
- [ ] Confirm CloudTrail management events are delivered to the retained,
      versioned audit bucket with log-file validation.
- [ ] Review CloudWatch and WAF log retention and access permissions.
- [ ] Walk through incident response, account recovery, DLQ replay, restore,
      credential rotation, user disablement, and researcher approval/revocation.
- [ ] Give the client the one-page administrative user guide plus the technical
      runbooks in the repository.

## 7. Commercial and support closure

- [ ] Client confirms the paid-plan transition is complete (or scheduled before
      the recorded deadline) and acknowledges forecasted AWS and third-party
      costs and all Free Plan eligibility limitations.
- [ ] Update AWS Support plan and support contacts as agreed.
- [ ] Record named client owners for billing, security, privacy, operations,
      backups, GitHub, SES/DNS, Google, OpenAI, and mobile release signing.
- [ ] Agree the post-handover support window and emergency contact route.
- [ ] Both parties sign a handover record listing accepted residual risks and
      outstanding work.
- [ ] After the support window, remove all remaining team access and securely
      delete local production exports, credentials, and recovery material.

