# Changelog

## 1.69.5 — 27 Oct 2026

- Shorter descriptions for the 20 longest tools, cutting the size of the tool definitions Claude loads by about a fifth. Every warning, parameter and response field is kept.
- Corrected descriptions that had fallen behind the code: `relink_order_line`, `unpublish_channel_listing`, `delist_all_channel_listings`, `refund_order_lines`, `cancel_order` and `create_or_update_inventory_item`.
- No change to what any tool does.

## 1.69.4 — 21 Oct 2026

First public release as a Claude Code plugin.

- 104 tools across orders, stock, inventory, purchase orders, pickwaves, reporting and channel listings, each annotated read-only, additive or destructive.
- Every write tool defaults to a dry run, except `run_readonly_query`, and large batches need an explicit confirmation count.
- Credentials are set through the plugin's settings and kept in secure storage.
- No store or SKU defaults ship: listing tools ask which Shopify store to use unless you set one.
