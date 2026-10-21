# Connecting cs-brief to HubSpot

cs-brief reads from HubSpot and never writes to it. It reads three things:
- **companies:** your customers and their properties
- **owners:** to show each account's CSM
- **open high-priority tickets:** counted per company

## 1. Create an access key
Use a **Service Key**, HubSpot's replacement for private apps.
1. In HubSpot, go to **Settings, then Integrations, then Service Keys** (some accounts show this
   under **Development, then Keys**).
2. Create a key with these read scopes:
   - `crm.objects.companies.read`
   - `crm.objects.owners.read`
   - `tickets`

A token from an existing legacy private app with the same scopes also works.

## 2. Give cs-brief the key
Choose **one** of these:
- **The page:** on **Settings, then Connections**, paste the key into "Access token" and choose
  **Save token**. cs-brief stores it in your user profile (`%LOCALAPPDATA%\cs-brief` on
  Windows), never in the project folder, so it isn't synced by OneDrive or committed to git.
  The page only ever shows the last 4 characters.
- **The environment:** set `CS_BRIEF_HUBSPOT_TOKEN` before starting cs-brief. If it's set, it
  takes precedence over a saved token.

Then choose **HubSpot** as the data source and **Test connection**.

## 3. What cs-brief reads
These are the defaults. To use your own property names, create `config/hubspot.yaml` with only
the fields you want to change (see "Using your own property names" below).

| cs-brief field | HubSpot company property | Notes |
|---|---|---|
| Which companies | `lifecyclestage` = `customer` | `customer_property` / `customer_value` |
| Name | `name` | standard |
| Owner (CSM) | `hubspot_owner_id` | standard; the name comes from the owners API |
| ARR | `cs_arr` | number, custom |
| Tier | `cs_tier` | text, custom; "Unknown" when empty |
| Customer since | `hs_lifecyclestage_customer_date` | standard "Became a Customer Date" |
| Renewal date | `cs_renewal_date` | date, custom; **companies without one are left out** |
| Onboarding milestones | `cs_onboarding_milestones_total` / `_done` / `_overdue` | numbers, custom; empty means no onboarding plan |
| Usage trend | `cs_usage_trend_30d` | % change in active users over 30 days |
| Days since last login | `cs_days_since_last_login` | number |
| NPS | `cs_nps_latest` | 0 to 10 |
| Champion left | `cs_champion_left` | true/false (or 1/0) |
| Seats in use | `cs_seat_utilisation` | % of purchased seats |
| Billing overdue | `cs_billing_overdue_days` | days |
| Severity-1 tickets | tickets, `hs_ticket_priority` in HIGH, URGENT, with no `closed_date` | counted per associated company |

Signal history comes from HubSpot's property history, so each change to one of these properties
shows on the account page's signal history, going back 90 days (`history_days`).

### Using your own property names
```yaml
# config/hubspot.yaml
arr: annual_recurring_revenue
renewal_date: contract_end_date
signals:
  usage_trend_30d: product_usage_change_30d
```

## What leaves your computer
- **Outbound:** only requests to `api.hubapi.com`, carrying your key.
- **Stored locally:**
  - the key, in your user profile
  - the fetched data, held in memory for 15 minutes; "Refresh data now" on the Connections page
    fetches it again
- Nothing is sent anywhere else.
