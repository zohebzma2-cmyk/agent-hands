# Open a sub-account for a member

`cu_core.open_sub_account@1.0.0` · status **validated** · risk **irreversible** · `sha256:2ccdf82ba1b52b90f08d33ad394dce8394fd3136b7ddeb46a1278dbf16681d5a`

Signs on, finds the member by number, reads the base share balance, opens a new sub-account funded from cash, and returns the confirmation number.

App profile `cu_core` (>=4.2,<5), entry `/core/login.do`.

## Contract

| Param | Type | Sensitivity | Constraint |
|---|---|---|---|
| `member_id` | string | pii | ^9\d{5}$ |
| `account_type` | enum | internal | Savings, Holiday Club, Certificate |
| `nickname` | string | internal | ^[A-Za-z0-9 ]{1,20}$ |
| `initial_deposit` | decimal | pii | 5.00–5000.00 |

| Output | Type | Sensitivity | In logs | From |
|---|---|---|---|---|
| `share_balance` | decimal | pii | mask | s07 |
| `confirmation_number` | string | internal | none | s14 |

Business outcomes: `member_not_found` (at s05, s06, s08), `permission_denied` (at s08, s13), `validation_rejected` (at s08, s13)

Recoverable conditions: `cu_core.server_error@1`, `cu_core.session_expired@1`, `cu_core.announcement@1`

## Steps

### s01 · Enter teller user ID  (login, reversible)

- **do:** fill `secret:teller_username`
- **target:** textbox next to "User ID:" in `top`
  1. textbox nearest "User ID:" in its row
  2. css `input[name="uid"]`
- **checkpoint:** textbox next to "User ID:" holds {secret} within 5000 ms

### s02 · Enter teller password  (login, reversible)

- **do:** fill `secret:teller_password`
- **target:** textbox next to "Password:" in `top`
  1. textbox nearest "Password:" in its row
  2. css `input[name="pwd"]`
- **checkpoint:** textbox next to "Password:" holds {secret} within 5000 ms

### s03 · Sign on  (login, safe)

- **do:** click
- **target:** button "Sign On" in `top`
  1. role button "Sign On"
  2. css `body > font > center > table > tbody > tr:nth-of-type(2) > td > form > table > tbody > tr:nth-of-type(3) > td:nth-of-type(2) > input`
- **checkpoint:** top route matches /core/frameset.do* within 8000 ms

### s04 · Enter member number  (task, reversible)

- **do:** fill `{member_id}`
- **target:** textbox next to "Member #:" in `main`
  1. textbox nearest "Member #:" in its row
  2. css `input[name="f1"]`
- **checkpoint:** textbox next to "Member #:" holds {member_id} within 5000 ms
- **after re-login resume at:** s04

### s05 · Search for the member by number  (task, safe)

- **do:** click
- **target:** button "Find" in `main`
  1. role button "Find"
  2. button nearest "Last Name:" in its row
  3. button nearest "Member #:" in its row
  4. css `body > font > form > table > tbody > tr > td:nth-of-type(5) > input`
- **checkpoint:** text contains 'Member # Name Branch' visible in main within 5000 ms
- **after re-login resume at:** s04

### s06 · Open the member record  (task, safe)

- **do:** click
- **target:** cell "{member_id}" in `main`
  1. text exact `{member_id}`
  2. css `body > font > table:nth-of-type(2) > tbody > tr:nth-of-type(2) > td:nth-of-type(1)`
- **checkpoint:** main route matches /core/member.do*; text contains 'Member Detail' visible in main; text contains '{member_id}' visible in main within 8000 ms
- **after re-login resume at:** s04

### s07 · Read the Base Share balance  (task, safe)

- **do:** read → `share_balance`
- **target:** cell next to "Base Share", column Balance in `main`
  1. cell nearest "Base Share" in its row, column "Balance"
  2. css `body > font > table:nth-of-type(3) > tbody > tr:nth-of-type(2) > td:nth-of-type(3)`
- **checkpoint:** element present: cell next to "Base Share", column Balance within 5000 ms
- **after re-login resume at:** s04

### s08 · Start opening a new sub-account  (task, safe)

- **do:** click
- **target:** button "Open Sub-Account" in `main`
  1. role button "Open Sub-Account"
  2. css `body > font > input:nth-of-type(1)`
- **checkpoint:** main route matches /core/subacct.do*; text contains 'Open Sub-Account' visible in main; text contains '{member_id}' visible in main within 8000 ms
- **after re-login resume at:** s04

### s09 · Choose the account type  (task, reversible)

- **do:** select `{account_type}`
- **target:** combobox next to "Account Type:" in `main`
  1. combobox nearest "Account Type:" in its row
  2. css `select[name="f7"]`
- **checkpoint:** combobox next to "Account Type:" holds {account_type} within 5000 ms
- **after re-login resume at:** s04

### s10 · Enter the nickname  (task, reversible)

- **do:** fill `{nickname}`
- **target:** textbox next to "Nickname:" in `main`
  1. textbox nearest "Nickname:" in its row
  2. css `input[name="f8"]`
- **checkpoint:** textbox next to "Nickname:" holds {nickname} within 5000 ms
- **after re-login resume at:** s04

### s11 · Enter the opening deposit  (task, reversible)

- **do:** fill `{initial_deposit}`
- **target:** textbox next to "Opening Deposit:" in `main`
  1. textbox nearest "Opening Deposit:" in its row
  2. css `input[name="f9"]`
- **checkpoint:** textbox next to "Opening Deposit:" holds {initial_deposit} within 5000 ms
- **after re-login resume at:** s04

### s12 · Set funding source to Cash  (task, reversible)

- **do:** select "Cash"
- **target:** combobox next to "Funding Source:" in `main`
  1. combobox nearest "Funding Source:" in its row
  2. css `select[name="f10"]`
- **checkpoint:** combobox next to "Funding Source:" holds Cash within 5000 ms
- **after re-login resume at:** s04

### s13 · Submit to open the {account_type} sub-account  (task, irreversible)

- **do:** click
- **target:** button "Open Account" in `main`, 2 must agree
  1. role button "Open Account"
  2. css `body > font > form > table > tbody > tr:nth-of-type(6) > td:nth-of-type(2) > input:nth-of-type(1)`
- **checkpoint:** main route matches /core/confirm.do*; text contains 'Sub-Account Opened' visible in main; text contains '{member_id}' visible in main within 8000 ms

### s14 · Read the confirmation number  (task, safe)

- **do:** read → `confirmation_number`
- **target:** Read the confirmation number in `main`
  1. css `body > font > table:nth-of-type(2) > tbody > tr:nth-of-type(1) > td:nth-of-type(2)`
- **checkpoint:** element present: Read the confirmation number within 5000 ms

## Success

main route matches /core/confirm.do*; text contains 'Sub-Account Opened' visible in main; text contains '{member_id}' visible in main

## Provenance

```json
{
  "compiler_version": "agent-hands 0.1.0",
  "created_at": "2026-10-17T20:09:58+00:00",
  "discovered_by_run": "disc-20261017T200007",
  "provider": "openrouter",
  "model": "anthropic/claude-opus-5.5",
  "models_served": [
    "anthropic/claude-opus-5.5"
  ],
  "upstream_providers": [
    "Anthropic"
  ],
  "generation_ids": [
    "gen-1792267210-621MUyYWx0wKPp0wF7B2",
    "gen-1792267216-E4TPG2JRQKivAw1ALTlB",
    "gen-1792267220-SG1KinWhMC1ezuJlGBQA",
    "gen-1792267223-aIqMzVmOBLJqwAhUgYeq",
    "gen-1792267227-c0XBmFzQhtuG0XGhJH9S",
    "gen-1792267236-bIJ6GXL7BW8AeqyAGMAC",
    "gen-1792267778-5OpNi4agXSscIqoFshNL",
    "gen-1792267781-dA4x9pvwq9ve1i5Kq88B"
  ],
  "validated_runs": [
    "disc-20261017T200007-validate-20261017T204352"
  ],
  "approved_by": null,
  "approved_at": null,
  "approved_hash": null
}
```
