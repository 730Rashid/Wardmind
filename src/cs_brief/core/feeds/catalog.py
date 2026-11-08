"""Every provider the Add a connection catalog offers, in catalog order.

The wording here is what CSMs read on the cards and setup pages: plain, specific, sentence case.
Each provider module defines a class with `info` taken from INFOS below and implements fetch.

CONTRACT FILE: provider ids, fields and wording are fixed; implementers escalate before changing
them (they may fix a typo).
"""

from __future__ import annotations

from cs_brief.core.feeds.models import (
    Category,
    FeedRole,
    FieldKind,
    ProviderInfo,
    Schedule,
    SetupField,
)

_TIMED = [Schedule.HOURLY, Schedule.EVERY_15_MINUTES, Schedule.EVERY_6_HOURS, Schedule.DAILY]
_TIMED += [Schedule.MANUAL]
_FILE_SCHEDULES = [Schedule.ON_CHANGE, *_TIMED]

_AUTH_CHOICES = [
    ("none", "No sign-in"),
    ("bearer", "API key or token (sent as Bearer)"),
    ("header", "API key in a header I name"),
    ("basic", "Username and password"),
]
_AUTH_FIELDS = [
    SetupField(
        key="auth", label="Sign-in", kind=FieldKind.CHOICE, choices=_AUTH_CHOICES, default="none"
    ),
    SetupField(
        key="header_name",
        label="Header name",
        kind=FieldKind.TEXT,
        required=False,
        placeholder="X-API-Key",
        help='Only for "API key in a header I name".',
    ),
    SetupField(
        key="username",
        label="Username",
        kind=FieldKind.TEXT,
        required=False,
        help='Only for "Username and password".',
    ),
    SetupField(
        key="secret",
        label="Key, token or password",
        kind=FieldKind.SECRET,
        required=False,
        help="Stored in your user profile and only ever shown masked.",
    ),
]

INFOS: list[ProviderInfo] = [
    ProviderInfo(
        id="file",
        name="File on this computer",
        by="by cs-brief",
        description="Reads a CSV, TSV or Excel file, such as a nightly export saved to a synced "
        "folder, and updates whenever the file changes.",
        categories=[Category.FILES, Category.ACCOUNTS, Category.USAGE],
        roles=[FeedRole.ACCOUNTS, FeedRole.SIGNALS, FeedRole.CLIENT_UPDATES],
        fields=[
            SetupField(
                key="path",
                label="File",
                kind=FieldKind.PATH,
                placeholder=r"C:\Users\you\OneDrive\Exports\accounts.csv",
                help="The full path to a .csv, .tsv, .txt or .xlsx file.",
            ),
            SetupField(
                key="sheet",
                label="Excel sheet",
                kind=FieldKind.TEXT,
                required=False,
                help="Leave blank for the first sheet with data.",
            ),
        ],
        schedules=_FILE_SCHEDULES,
        mark="Fi",
    ),
    ProviderInfo(
        id="link",
        name="Link to a file",
        by="by cs-brief",
        description="Downloads a CSV, TSV, Excel or JSON file from a web address, such as an "
        "export link from your product or data team.",
        categories=[Category.FILES, Category.ACCOUNTS, Category.USAGE],
        roles=[FeedRole.ACCOUNTS, FeedRole.SIGNALS, FeedRole.CLIENT_UPDATES],
        fields=[
            SetupField(
                key="url",
                label="Web address",
                kind=FieldKind.URL,
                placeholder="https://exports.example.com/accounts.csv",
            ),
            SetupField(
                key="format",
                label="Format",
                kind=FieldKind.CHOICE,
                default="auto",
                choices=[
                    ("auto", "Work it out"),
                    ("csv", "CSV"),
                    ("tsv", "TSV"),
                    ("xlsx", "Excel"),
                    ("json", "JSON"),
                ],
            ),
            SetupField(
                key="records_path",
                label="Where the rows are (JSON only)",
                kind=FieldKind.TEXT,
                required=False,
                placeholder="data.items",
                help="Leave blank when the file is a list of rows.",
            ),
            *_AUTH_FIELDS,
        ],
        schedules=_TIMED,
        mark="Li",
    ),
    ProviderInfo(
        id="google_sheets",
        name="Google Sheets",
        by="by Google",
        description='Reads a Google Sheet shared as "Anyone with the link can view", or '
        "published to the web. No sign-in needed.",
        categories=[Category.FILES, Category.ACCOUNTS],
        roles=[FeedRole.ACCOUNTS, FeedRole.SIGNALS, FeedRole.CLIENT_UPDATES],
        fields=[
            SetupField(
                key="url",
                label="Sheet link",
                kind=FieldKind.URL,
                placeholder="https://docs.google.com/spreadsheets/d/…/edit#gid=0",
                help="Copy it from the address bar with the right tab open.",
            ),
        ],
        schedules=_TIMED,
        mark="GS",
    ),
    ProviderInfo(
        id="json_api",
        name="Any JSON API",
        by="by cs-brief",
        description="Reads rows from your product's or data team's API, with a key, and "
        "follows pages when the API says there are more.",
        categories=[Category.ACCOUNTS, Category.USAGE, Category.TICKETS, Category.BILLING],
        roles=[FeedRole.SIGNALS, FeedRole.ACCOUNTS],
        fields=[
            SetupField(
                key="url",
                label="Web address",
                kind=FieldKind.URL,
                placeholder="https://api.example.com/v1/accounts",
            ),
            SetupField(
                key="records_path",
                label="Where the rows are",
                kind=FieldKind.TEXT,
                required=False,
                placeholder="data",
                help="A dotted path into the response. Blank when it's a list.",
            ),
            SetupField(
                key="next_path",
                label="Where the next page's address is",
                kind=FieldKind.TEXT,
                required=False,
                placeholder="links.next",
                help="Leave blank if everything comes in one response.",
            ),
            *_AUTH_FIELDS,
        ],
        schedules=_TIMED,
        mark="{}",
    ),
    ProviderInfo(
        id="sqlite",
        name="SQLite database",
        by="by cs-brief",
        description="Runs your read-only query on a SQLite database file on this computer, "
        "such as a local copy of product data.",
        categories=[Category.FILES, Category.USAGE, Category.ACCOUNTS],
        roles=[FeedRole.SIGNALS, FeedRole.ACCOUNTS],
        fields=[
            SetupField(
                key="path",
                label="Database file",
                kind=FieldKind.PATH,
                placeholder=r"C:\Data\product.db",
            ),
            SetupField(
                key="query",
                label="Query",
                kind=FieldKind.QUERY,
                placeholder="SELECT company_domain AS Website, active_users_trend AS "
                '"Usage trend" FROM accounts',
                help="One SELECT. Name the columns after cs-brief's fields to match them "
                "for you. The file is opened read-only.",
            ),
        ],
        schedules=_FILE_SCHEDULES,
        mark="SQ",
    ),
    ProviderInfo(
        id="stripe",
        name="Stripe",
        by="by Stripe",
        description="Finds each customer's oldest unpaid invoice, so overdue billing shows up "
        "as a signal on the right account.",
        categories=[Category.BILLING],
        roles=[FeedRole.SIGNALS],
        fields=[
            SetupField(
                key="secret",
                label="Restricted key",
                kind=FieldKind.SECRET,
                placeholder="rk_live_…",
                help="Read access to Customers and Invoices is all it needs.",
            ),
        ],
        schedules=_TIMED,
        mark="St",
        docs_hint="In Stripe: Developers > API keys > Create restricted key.",
    ),
    ProviderInfo(
        id="chargebee",
        name="Chargebee",
        by="by Chargebee",
        description="Finds each customer's oldest unpaid invoice, so overdue billing shows up "
        "as a signal on the right account.",
        categories=[Category.BILLING],
        roles=[FeedRole.SIGNALS],
        fields=[
            SetupField(
                key="site",
                label="Site",
                kind=FieldKind.TEXT,
                placeholder="acme",
                help="The part before .chargebee.com.",
            ),
            SetupField(
                key="secret",
                label="API key",
                kind=FieldKind.SECRET,
                help="A read-only key is enough.",
            ),
        ],
        schedules=_TIMED,
        mark="Cb",
        docs_hint="In Chargebee: Settings > Configure Chargebee > API keys.",
    ),
    ProviderInfo(
        id="zendesk",
        name="Zendesk",
        by="by Zendesk",
        description="Counts each organisation's open urgent tickets and adds them as the "
        "severity-1 signal.",
        categories=[Category.TICKETS],
        roles=[FeedRole.SIGNALS],
        fields=[
            SetupField(
                key="subdomain",
                label="Subdomain",
                kind=FieldKind.TEXT,
                placeholder="acme",
                help="The part before .zendesk.com.",
            ),
            SetupField(key="email", label="Your Zendesk email", kind=FieldKind.TEXT),
            SetupField(key="secret", label="API token", kind=FieldKind.SECRET),
        ],
        schedules=_TIMED,
        mark="Ze",
        docs_hint="In Zendesk: Admin Center > Apps and integrations > Zendesk API.",
    ),
    ProviderInfo(
        id="freshdesk",
        name="Freshdesk",
        by="by Freshworks",
        description="Counts each company's open urgent tickets and adds them as the "
        "severity-1 signal.",
        categories=[Category.TICKETS],
        roles=[FeedRole.SIGNALS],
        fields=[
            SetupField(
                key="domain",
                label="Helpdesk name",
                kind=FieldKind.TEXT,
                placeholder="acme",
                help="The part before .freshdesk.com.",
            ),
            SetupField(key="secret", label="API key", kind=FieldKind.SECRET),
        ],
        schedules=_TIMED,
        mark="Fd",
        docs_hint="In Freshdesk: your profile picture > Profile settings > API key.",
    ),
    ProviderInfo(
        id="posthog",
        name="PostHog",
        by="by PostHog",
        description="Works out each company's active-user trend and days since anyone logged "
        "in, from your product analytics.",
        categories=[Category.USAGE],
        roles=[FeedRole.SIGNALS],
        fields=[
            SetupField(
                key="host",
                label="PostHog address",
                kind=FieldKind.URL,
                default="https://us.posthog.com",
                help="https://eu.posthog.com for EU cloud, or your own address.",
            ),
            SetupField(
                key="project_id",
                label="Project ID",
                kind=FieldKind.TEXT,
                help="The number in your project's address.",
            ),
            SetupField(
                key="group_key",
                label="Company property",
                kind=FieldKind.TEXT,
                default="$group_0",
                help="The event property that holds the company's ID, domain or name.",
            ),
            SetupField(
                key="query",
                label="Query",
                kind=FieldKind.QUERY,
                required=False,
                help="Leave blank for the standard usage query, or write your own "
                "HogQL with columns named after cs-brief's fields.",
            ),
            SetupField(
                key="secret",
                label="Personal API key",
                kind=FieldKind.SECRET,
                help="Needs the query:read scope.",
            ),
        ],
        schedules=_TIMED,
        mark="PH",
        docs_hint="In PostHog: Settings > Personal API keys.",
    ),
    ProviderInfo(
        id="airtable",
        name="Airtable",
        by="by Airtable",
        description="Reads a table or view, for teams that keep their customer list or health "
        "notes in Airtable.",
        categories=[Category.ACCOUNTS, Category.USAGE, Category.FILES],
        roles=[FeedRole.ACCOUNTS, FeedRole.SIGNALS, FeedRole.CLIENT_UPDATES],
        fields=[
            SetupField(
                key="base_id",
                label="Base ID",
                kind=FieldKind.TEXT,
                placeholder="appXXXXXXXXXXXXXX",
                help="It starts with app.",
            ),
            SetupField(key="table", label="Table name or ID", kind=FieldKind.TEXT),
            SetupField(key="view", label="View", kind=FieldKind.TEXT, required=False),
            SetupField(
                key="secret",
                label="Personal access token",
                kind=FieldKind.SECRET,
                help="Scope data.records:read on this base is enough.",
            ),
        ],
        schedules=_TIMED,
        mark="At",
        docs_hint="In Airtable: Builder hub > Personal access tokens.",
    ),
]


def provider_info(provider_id: str) -> ProviderInfo | None:
    return next((info for info in INFOS if info.id == provider_id), None)
