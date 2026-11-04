# Website observatory moved to OrgPortal

Open https://orgportal.cc/admin?tab=stats and sign in as a system administrator.
OrgPortal hosts the UI, report API and private retained history.
The independent probe is maintained in `../OrgPortal/tools/website-probe/`
and runs as the `orgportal-website-probe` user service. Its local history and sync
ledger live in `~/.local/share/orgportal/website-probe/`.
See `../OrgPortal/docs/WEBSITE_OBSERVATORY.md` for operations and data boundaries.
The LAN monitor no longer collects or serves website analytics.
