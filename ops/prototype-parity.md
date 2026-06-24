# Claude Prototype Parity Ledger

This is the implementation contract for the 46-page Claude design parity pass.

Scope rules:
- Preserve the existing Mise forest/gold palette.
- Preserve public and admin dark-mode toggles.
- Preserve both insignias: honey-badger in light admin, serpent/Slytherin crest in dark admin.
- Use the Claude files for composition, density, spacing, document hierarchy, and control treatment rather than copying slate/rose colors wholesale.
- Target count is 46 screens: every `*.dc.html` in `/tmp/mise-prototype-project` except `Home (original).dc.html`, which is treated as an archived reference variant.

| Claude screen | Live route / template | Current parity | Notes |
| --- | --- | --- | --- |
| About | `/about` · `templates/site/about.html` | Partial | Needs visual QA against hero/content cadence. |
| Book | `/book`, `/book/{slug}` · `templates/public/book_index.html`, `templates/public/book_event.html` | Partial | Public booking flow exists; needs page-level prototype polish review. |
| Contact | `/contact` · `templates/site/contact.html` | Partial | Existing workflow kept; compare contact composition and form surface. |
| Home | `/` · `templates/site/home.html` | Partial | Active home route; compare to final selected Claude home variant. |
| Home (terracotta) | `/` · `templates/site/home.html` | Partial | Reference for public candlelit editorial direction, with Mise palette preserved. |
| Portfolio | `/portfolio` · `templates/site/portfolio.html` | Partial | Gallery layout exists; needs page-by-page screenshot parity pass. |
| Reels | `/reels` · `templates/site/reels.html` | Partial | Motion page exists; needs prototype spacing/media treatment pass. |
| Services | `/services` · `templates/site/services.html` | Partial | Service cards/sections exist; needs exact section hierarchy review. |
| Work | `/work` · `templates/site/work_index.html` | Partial | Editorial case-study grid exists. |
| WorkDetail | `/work/{slug}` · `templates/site/work_detail.html` | Partial | Detail layout exists; needs media/credits cadence QA. |
| Admin Audit | `/admin/audit` · `templates/admin/audit.html` | Partial | Functional, needs prototype surface/density pass. |
| Admin Calendar | `/admin/calendar` · `templates/admin/calendar.html` | Close | Calendar grid already has strict prototype class system. |
| Admin Client | `/admin/studio/clients/{id}` · `templates/admin/client.html` | Partial | High-value next candidate after document slice. |
| Admin ClientPnL | `/admin/financials/clients` · `templates/admin/financials_clients.html` | Partial | Functional money view; needs prototype card/table pass. |
| Admin Content | `/admin/content` · `templates/admin/content.html` | Partial | Brand kit/caption surfaces exist; needs screenshot QA. |
| Admin Dashboard | `/admin/home` · `templates/admin/home.html` | Partial | Dashboard systems exist; needs final parity review. |
| Admin Deals | `/admin/studio` · `templates/admin/studio.html` | Partial | Maps to pipeline/deals board. |
| Admin Document | `/i/{slug}`, `/c/{slug}`, `/p/{slug}` · public document templates | Close | Invoice, proposal, and contract now share the Claude document-paper family with live pay/accept/sign behavior preserved. |
| Admin Expenses | `/admin/financials/expenses` · `templates/admin/financials_expenses.html` | Partial | Functional; design pass pending. |
| Admin Financials | `/admin/financials` · `templates/admin/financials.html` | Partial | Existing financial dashboard needs prototype parity QA. |
| Admin Forms | `/admin/forms`, `/admin/forms/{id}` · form templates | Partial | Functional builder exists; prototype polish pending. |
| Admin Galleries | `/admin/galleries` · `templates/admin/gallery.html`/galleries list | Partial | Existing management UI; parity QA pending. |
| Admin Gallery | `/admin/galleries/{id}` · `templates/admin/gallery.html` | Partial | Existing detail UI; parity QA pending. |
| Admin Inbox | `/admin/inbox` · `templates/admin/inbox.html` | Partial | Conversation UI exists; needs prototype screenshot comparison. |
| Admin Invoice | `/admin/studio/invoices/{id}` · `templates/admin/invoice.html` | In progress | Upgraded in this slice to document paper + action rail. |
| Admin Jobs | `/admin/jobs` · `templates/admin/jobs.html` | Partial | Functional worker monitor; prototype polish pending. |
| Admin Licensing | `/admin/licenses`, `/admin/licenses/{id}` · licensing templates | Partial | Functional; needs prototype pass. |
| Admin Login | `/admin/login` · `templates/admin/login.html` | Close | Already using Claude login composition with forest/gold. |
| Admin Mileage | `/admin/financials/mileage` · `templates/admin/financials_mileage.html` | Partial | Functional; needs prototype pass. |
| Admin Portal | `/admin/portals`, public portal routes · portal templates | Partial | Functional client hubs; visual parity pending. |
| Admin Press | `/admin/press` · `templates/admin/press.html` | Partial | Functional; design pass pending. |
| Admin Project | `/admin/studio/projects/{id}` · `templates/admin/project.html` | Partial | High-value next candidate. |
| Admin Receipts | `/admin/financials/receipts` · `templates/admin/financials_receipts.html` | Partial | Functional receipt cards; design pass pending. |
| Admin Recurring | `/admin/studio/recurring/{id}` · `templates/admin/recurring.html` | Partial | Functional recurring workflow; needs prototype pass. |
| Admin Reference | `/admin/reference` · `templates/admin/reference.html` | Partial | Functional; design pass pending. |
| Admin Reports | `/admin/reports` · `templates/admin/reports.html` | Partial | Functional; design pass pending. |
| Admin Scheduling | `/admin/scheduling` · scheduling templates | Partial | Strong existing surface; needs screenshot QA. |
| Admin Search | `/admin/search` · `templates/admin/search.html` | Partial | Functional; design pass pending. |
| Admin Sent | `/admin/sent` · `templates/admin/sent.html` | Partial | Functional; design pass pending. |
| Admin Settings | `/admin/settings` · `templates/admin/settings.html` | Partial | Functional; design pass pending. |
| Admin ShotList | Embedded in project routes · shotlist handlers | Missing standalone | Needs prototype treatment or explicit embedded mapping. |
| Admin Sidebar | Shared admin shell · `templates/admin/_nav.html` | Close | Preserve dark toggle and insignia swap. |
| Admin Studio | `/admin/studio` · `templates/admin/studio.html` | Close/Partial | Board is close; list/detail flows still need review. |
| Admin Tasks | `/admin/tasks` · `templates/admin/tasks.html` | Close | Three-column board already matches prototype direction. |
| Admin Templates | `/admin/templates`, `/admin/email-templates` · template galleries | Partial | Functional; design pass pending. |
| Admin Transfers | `/admin/transfers` · `templates/admin/transfers.html` | Partial | Functional upload/drop surface; design pass pending. |

Completed correction slices:
- `Admin Invoice` admin detail.
- `Admin Document` client document family for invoice, proposal, and contract.

Next recommended slice:
- Move to `Admin Project` + `Admin Client`, because those pages sit at the center of the 46-screen workflow.
