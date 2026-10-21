# Linnworks plugin for Claude (unofficial)

Connect Claude to your own [Linnworks](https://www.linnworks.com/) account and work with it in plain English: find orders, check stock, age dead inventory, raise and receive purchase orders, build pickwaves, and manage channel listings. The plugin runs a local [MCP](https://modelcontextprotocol.io/) server with 104 tools. It uses your own Linnworks API credentials, and nothing is hosted or proxied.

> **Unofficial.** This is an independent community project. It is not made, endorsed or supported by Linnworks. "Linnworks" is a trademark of its owner and is used here only to say what the plugin connects to.

> **It can change live data.** Many tools write to your Linnworks account, and through Linnworks to your live sales channels. Every write tool except one defaults to a dry run, and large batches need an explicit confirmation count. Read [What this server can read and change](#what-this-server-can-read-and-change) before you use it on a live account.

## Requirements

- [Claude Code](https://code.claude.com/), including the Code tab in the Claude desktop app
- [`uv`](https://docs.astral.sh/uv/getting-started/installation/) on your `PATH`. It installs the pinned Python dependencies from `uv.lock` on first start, and fetches Python 3.10+ if you don't have it.
- A Linnworks account and a Linnworks developer app (below)

## Install

```bash
claude plugin marketplace add josephgurney/linnworks-claude-plugin
```

```bash
claude plugin install linnworks@linnworks-plugins
```

Claude Code asks for your settings when the plugin is enabled. The three Linnworks credentials are stored in secure storage, not in `settings.json`.

| Setting | Required | What it is |
|---|---|---|
| Linnworks Application ID | Yes | From your app in the Linnworks developer portal |
| Linnworks Application Secret | Yes | From the same app |
| Linnworks installation token | Yes | The permanent token shown when you install the app (see below) |
| Default Shopify store | No | The Linnworks SubSource name of the Shopify store the listing tools use when you don't name one. Blank means Claude asks, and lists your stores. |
| Internal marker SKUs | No | Comma-separated SKUs you add to orders on purpose (gift wrap, a notes flag). `find_unlinked_order_lines` won't report them as orphaned. |
| Pick-ready order tag | No | An order tag that marks an order ready to pick. `generate_pick_waves` warns about any order without it. Blank means no check. |
| Shopify Admin access | No | Only for `repair_channel_listing_images`. JSON keyed by Linnworks SubSource: `{"My Store":{"shop_domain":"x.myshopify.com","access_token":"shpat_..."}}`. Scopes: `read_products`, `write_products`, `read_files`, `write_files`. |

### Getting your Linnworks credentials

1. Create an app in the [Linnworks developer portal](https://developer.linnworks.com/). It gives you an Application ID and an Application Secret.
2. While logged in to Linnworks, open `https://apps.linnworks.net/Authorization/Authorize/<your Application ID>`.
3. Install the app. The **permanent token** shown on the installation page is your installation token. If none is shown, set a Postback URL on the app and reinstall.

## Try it

- "What open orders are overdue for dispatch?"
- "How much stock do we have of SKU ABC-123, and where?"
- "Which items have been out of stock everywhere for more than 90 days?"
- "Show me the purchase orders still open with our main supplier."
- "Is SKU ABC-123 listed on Shopify, and on which store?"

Some reporting tools page through months of orders and make hundreds of API calls. Ask for one at a time.

<!-- DISCLOSURE:START -->
## What this server can read and change

This server can **change live data in your Linnworks account**, and through Linnworks it can change your live sales-channel listings. Some tools can also end live listings, cancel or refund orders, and delete items. Every tool declares MCP annotations (`readOnlyHint` / `destructiveHint`) so Claude and your MCP client can tell a read from a write before calling it.

| Kind | Count | What it means |
|---|---|---|
| Read-only | 47 | Reads data. Never changes anything. |
| Additive writes | 12 | Creates new records (orders, POs, notes, images, listings, pickwaves, categories, an export run). Never changes or removes existing ones. |
| Destructive writes | 45 | May overwrite, remove or end something that exists (stock levels, prices, item data, order state, refunds, cancellations, live channel listings, deletes). |

Every write tool except `run_readonly_query` defaults to `dry_run=True`, and large batches are staged behind a confirmation count (see below).

**Outside Linnworks.** One tool, `repair_channel_listing_images`, writes to the Shopify Admin API directly. It is optional and does nothing unless you configure Shopify credentials. No other tool calls any service except Linnworks.

<details><summary>Read-only tools (47)</summary>

`check_orders_pickable`, `find_composite_parents`, `find_dangling_glt_templates`, `find_inventory_item`, `find_open_orders_for_sku`, `find_orders_by_reference`, `find_unlinked_order_lines`, `get_categories`, `get_category_report`, `get_channel_listings`, `get_channel_listings_bulk`, `get_component_sales`, `get_export`, `get_export_list`, `get_extended_properties`, `get_import`, `get_import_list`, `get_inventory_item_descriptions`, `get_inventory_item_images`, `get_inventory_item_images_bulk`, `get_inventory_item_suppliers`, `get_inventory_item_titles`, `get_item_bins`, `get_item_relationships`, `get_locations`, `get_open_orders`, `get_order`, `get_order_notes`, `get_period_comparison`, `get_pick_wave_detail`, `get_pick_wave_users`, `get_pick_waves`, `get_processed_order_items`, `get_processed_orders`, `get_purchase_order`, `get_revenue_summary`, `get_rule`, `get_rules`, `get_sales_by_supplier`, `get_shipping_quote`, `get_stock_change_history`, `get_stock_level`, `get_suppliers`, `get_top_skus`, `list_inventory_items`, `search_inventory_items`, `search_purchase_orders`

</details>

<details><summary>Additive write tools (12)</summary>

`add_inventory_item_images`, `add_order_note`, `add_purchase_order_item`, `add_purchase_order_note`, `add_variation_group_items`, `create_category`, `create_order`, `create_purchase_order`, `create_variation_group`, `generate_pick_waves`, `list_to_shopify`, `run_export`

</details>

<details><summary>Destructive write tools (45)</summary>

`accept_shipping_quote`, `archive_inventory_items`, `cancel_order`, `create_or_update_inventory_item`, `delete_categories`, `delete_dangling_glt_template`, `delete_empty_categories`, `delete_extended_properties`, `delete_inventory_item`, `delete_inventory_item_images`, `delete_order_note`, `delete_order_notes_by_text`, `delete_purchase_order`, `delist_all_channel_listings`, `delist_all_shopify_listings`, `deliver_purchase_order`, `open_purchase_order`, `refresh_channel_listing`, `refund_order`, `refund_order_lines`, `relink_order_line`, `remove_order_item`, `remove_orders_from_pick_waves`, `remove_purchase_order_item`, `rename_category`, `rename_inventory_item_sku`, `repair_channel_listing_images`, `revise_ebay_listing_description`, `run_import`, `run_readonly_query`, `set_extended_properties`, `set_inventory_item_descriptions`, `set_inventory_item_image_order`, `set_inventory_item_prices`, `set_inventory_item_suppliers`, `set_inventory_item_titles`, `set_order_address`, `set_order_status`, `set_stock_levels`, `unarchive_inventory_items`, `unpublish_channel_listing`, `update_order_note`, `update_pick_wave`, `update_purchase_order_header`, `update_purchase_order_item`

</details>
<!-- DISCLOSURE:END -->

## Safety

- **Dry run by default.** Write tools describe what they would do and change nothing until you run them again with `dry_run=False`.
- **Staged batches.** Above a per-tool threshold (10 for deletes and listing take-downs, 25 to 100 elsewhere), a write returns a manifest and runs only when you echo back the exact item count as `confirmed_count`.
- **Read before write, read back after.** Write tools read the current state first and re-read it afterwards. Where a channel accepts a push but its effect cannot be seen through Linnworks (Amazon and eBay listing pushes, non-Shopify refunds), the result says so rather than reporting success.
- **Your credentials stay with you.** The server talks only to Linnworks, plus Shopify for the one optional tool above. Nothing is sent anywhere else.

## Development

```bash
uv run --frozen pytest
```

Bug reports and suggestions are welcome as issues on this repository.

## Licence

MIT. See [LICENSE](LICENSE).
